"""Install capability packages and prepare their pinned kernel without changing the host xana."""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile

import xana
from xana.config.paths import xana_home
from xana.publishing.contracts import AgentPackage, MANIFEST
from xana.publishing.artifacts import file_sha256
from xana.skills.markdown_loader import MarkdownDocParser


def load_package(root: Path) -> AgentPackage:
    manifest=AgentPackage.model_validate_json((root/MANIFEST).read_text())
    for name,digest in manifest.files.items():
        rel=PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or '\\' in name:
            raise ValueError('能力包路径越界')
        path=root/'workspace'/name
        if path.is_symlink() or not path.resolve().is_relative_to((root/'workspace').resolve()):
            raise ValueError('能力包包含外部链接')
        if not path.is_file() or file_sha256(path)!=digest:
            raise ValueError(f'能力包文件缺失或校验失败: {name}')
    for paths in manifest.skills.values():
        if any(path not in manifest.files or not path.endswith('/SKILL.md') for path in paths):
            raise ValueError('技能引用不在能力包内')
    for name in manifest.files:
        if name.startswith('.xana/agents/') and name.endswith('/AGENT.md'):
            path=root/'workspace'/name
            definition=MarkdownDocParser().parse_sync(path)
            if definition.metadata.get('kind')!='agent' or definition.name!=path.parent.name:
                raise ValueError('agent 定义与发布目录不匹配')
    if manifest.entry_agent!='chat' and f'.xana/agents/{manifest.entry_agent}/AGENT.md' not in manifest.files:
        raise ValueError('入口 agent 缺失')
    return manifest


def _runtime_python(app: Path, version: str, wheel: Path | None, dependencies: list[str]) -> tuple[Path, Path | None]:
    if metadata.version('xana')==version:
        if not dependencies:
            return Path(sys.executable),Path(xana.__file__).parent.parent
        if Path(sys.prefix)==app/'venv':
            subprocess.run([sys.executable,'-m','pip','install',*dependencies],check=True)
            return Path(sys.executable),Path(xana.__file__).parent.parent

    env=app/'venv'
    if env.exists():
        raise FileExistsError('应用运行环境已存在，未覆盖')
    venv.EnvBuilder(with_pip=True).create(env)
    python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    requirement=str(wheel) if wheel else 'xana=='+version
    subprocess.run([str(python),'-m','pip','install',requirement,*dependencies],check=True)
    return python,None


def _validate_runtime(wheel: Path, manifest: AgentPackage) -> None:
    if manifest.runtime_sha256 and file_sha256(wheel)!=manifest.runtime_sha256:
        raise ValueError('运行核 SHA-256 与能力包不匹配')
    from email.parser import Parser
    with zipfile.ZipFile(wheel) as archive:
        names=[n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(names)!=1:
            raise ValueError('无效的运行核 wheel')
        headers=Parser().parsestr(archive.read(names[0]).decode())
        if headers['Name']!='xana' or headers['Version']!=manifest.runtime_version:
            raise ValueError('运行核版本与能力包不匹配')


def _prepare_runtime(app: Path, manifest: AgentPackage, wheel: Path | None):
    if wheel is not None:
        _validate_runtime(wheel,manifest)
    if wheel is None and metadata.version('xana')!=manifest.runtime_version and manifest.runtime_url:
        import httpx
        from urllib.parse import urlsplit
        name=Path(urlsplit(manifest.runtime_url).path).name
        if not name.startswith('xana-') or not name.endswith('.whl'):
            raise ValueError('运行核 URL 必须指向 xana wheel 文件')
        with tempfile.TemporaryDirectory(prefix='xana-kernel-') as tmp:
            downloaded=Path(tmp)/name
            total=0
            with httpx.stream('GET',manifest.runtime_url,follow_redirects=True,timeout=120) as response:
                response.raise_for_status()
                with downloaded.open('wb') as file:
                    for data in response.iter_bytes(65536):
                        total+=len(data)
                        if total>64*1024*1024:
                            raise ValueError('运行核 wheel 超过 64 MiB')
                        file.write(data)
            _validate_runtime(downloaded,manifest)
            return _runtime_python(app,manifest.runtime_version,downloaded,manifest.python_dependencies)
    return _runtime_python(app,manifest.runtime_version,wheel,manifest.python_dependencies)


def install_package(root: Path, *, runtime_wheel: Path | None = None) -> Path:
    manifest=load_package(root)
    app=xana_home()/'apps'/manifest.name
    command=xana_home()/'bin'/manifest.name
    if command.exists():
        raise FileExistsError(f'命令已存在: {command}')
    target=app/'package'
    if target.exists():
        raise FileExistsError(f'{manifest.name} 已安装；未覆盖已有应用和数据')
    app.mkdir(parents=True,exist_ok=True)
    python,code=_prepare_runtime(app,manifest,runtime_wheel)
    with tempfile.TemporaryDirectory(prefix='.package-',dir=app) as tmp:
        staged=Path(tmp)/'package'
        staged.mkdir()
        shutil.copy2(root/MANIFEST,staged/MANIFEST)
        workspace=staged/'workspace'
        workspace.mkdir()
        # Only declared, hashed files enter the installed application.
        for name in manifest.files:
            dest=workspace/name
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(root/'workspace'/name,dest)
        staged.rename(target)
    command.parent.mkdir(parents=True,exist_ok=True)
    lines=[f'#!{python}', 'import sys', 'from pathlib import Path']
    if code is not None:
        lines.append(f'sys.path.insert(0, {str(code)!r})')
    lines.extend(['from xana.publishing.launcher import main',f'raise SystemExit(main(Path({str(target)!r})))'])
    command.write_text('\n'.join(lines)+'\n')
    command.chmod(0o755)
    return command


def install_bundle(path: Path, names: tuple[str, ...] | None = None) -> list[Path]:
    with tempfile.TemporaryDirectory(prefix='xana-agent-bundle-') as tmp:
        root=Path(tmp)
        with zipfile.ZipFile(path) as archive:
            total=0
            for info in archive.infolist():
                rel=PurePosixPath(info.filename)
                total+=info.file_size
                if rel.is_absolute() or '..' in rel.parts or '\\' in info.filename or total>2*1024**3:
                    raise ValueError('发布包路径不合法或超过 2 GiB')
                if (info.external_attr>>16)&0o170000==0o120000:
                    raise ValueError('发布包不能包含符号链接')
            archive.extractall(root)
            for info in archive.infolist():
                target=root/info.filename
                if target.is_file():
                    target.chmod((info.external_attr>>16)&0o777 or 0o644)
        checks=json.loads((root/'checksums.json').read_text())
        wheels=[]
        for name,digest in checks.items():
            if Path(name).name!=name or not name.endswith('.whl'):
                raise ValueError('无效的运行核清单')
            wheel=root/name
            if file_sha256(wheel)!=digest:
                raise ValueError('运行核校验失败')
            wheels.append(wheel)
        if len(wheels)!=1:
            raise ValueError('发布包需要一个运行核')
        packages=[(p.parent,load_package(p.parent)) for p in root.glob('*/'+MANIFEST)]
        if not packages:
            raise ValueError('发布包中没有能力包')
        available={manifest.name for _,manifest in packages}
        if names and set(names)!={'*'} and set(names)-available:
            raise ValueError('发布包中没有指定的能力包')
        if not names and len(packages)>1:
            raise ValueError('请明确选择要安装的能力包')
        return [install_package(root,runtime_wheel=wheels[0]) for root,manifest in packages
                if not names or '*' in names or manifest.name in names]
