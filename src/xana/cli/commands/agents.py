"""npx-skills-style capability installation; execution is the separate shared runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile
import httpx
from typing import TextIO

from xana.config.paths import xana_home
from xana.publishing.contracts import MANIFEST
from xana.publishing.install import install_bundle, install_package, load_package


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub=parser.add_subparsers(dest='operation',required=True)
    add=sub.add_parser('add')
    add.add_argument('source')
    add.add_argument('--list',action='store_true')
    add.add_argument('--agent',nargs='+')
    sub.add_parser('list')
    for action in ['setup','run']:
        cmd=sub.add_parser(action)
        cmd.add_argument('name')


def _add_local(root: Path,args,out: TextIO) -> None:
    paths=[root] if (root/MANIFEST).is_file() else sorted(p.parent for p in root.glob('*/'+MANIFEST))
    manifests=[(path,load_package(path)) for path in paths]
    if not manifests:
        raise ValueError('来源中没有 xana 能力包')
    if args.list:
        for _path,m in manifests:
            print(f'{m.name}: {m.description} ({m.version})',file=out)
        return
    if not args.agent:
        raise ValueError('请用 --agent 指定要安装的能力包，或 --agent "*"')
    requested=set(args.agent)
    available={m.name for _,m in manifests}
    if requested!={'*'} and requested-available:
        raise ValueError('来源中没有指定的能力包')
    for path,m in manifests:
        if '*' in requested or m.name in requested:
            command=install_package(path)
            print(f'已安装 {m.name}。配置: {command} setup；启动: {command} run',file=out)


def run(args: argparse.Namespace, *, out: TextIO) -> int:
    try:
        if args.operation=='list':
            for manifest in sorted((xana_home()/'apps').glob('*/package/'+MANIFEST)):
                m=load_package(manifest.parent)
                print(f'{m.name} {m.version}',file=out)
        elif args.operation in ('setup','run'):
            if Path(args.name).name!=args.name:
                raise ValueError('无效应用名')
            command=xana_home()/'bin'/args.name
            return subprocess.run([str(command),args.operation],check=False).returncode
        else:
            source=Path(args.source).expanduser()
            if source.is_file() and source.suffix=='.zip':
                if args.list:
                    with zipfile.ZipFile(source) as archive:
                        for name in archive.namelist():
                            if name.endswith('/'+MANIFEST):
                                print(json.loads(archive.read(name))['name'],file=out)
                else:
                    for command in install_bundle(source,tuple(args.agent) if args.agent else None):
                        print(f'已安装；请配置: {command} setup',file=out)
            elif source.is_dir():
                _add_local(source,args,out)
            else:
                if args.source.startswith('-'):
                    raise ValueError('来源不能是命令选项')
                with tempfile.TemporaryDirectory(prefix='xana-agents-') as tmp:
                    command=['npx','--yes','skills','add',args.source]
                    if args.list:
                        command+=['--list']
                    else:
                        if not args.agent:
                            raise ValueError('必须用 --agent 明确选择能力包')
                        command+=['--skill',*args.agent,'--agent','universal','--yes','--copy']
                    result=subprocess.run(command,cwd=tmp,capture_output=True,text=True,timeout=300)
                    if result.returncode:
                        raise ValueError(result.stderr[-2000:] or result.stdout[-2000:])
                    if args.list:
                        print(result.stdout,file=out)
                    else:
                        _add_local(Path(tmp)/'.agents/skills',args,out)
        return 0
    except (OSError,ValueError,subprocess.SubprocessError,httpx.HTTPError,zipfile.BadZipFile) as exc:
        print(f'能力包操作失败: {exc}',file=out)
        return 1
