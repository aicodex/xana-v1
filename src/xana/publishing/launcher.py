"""Run an installed capability through the existing setup, model clients and serve loop."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import sys

from xana.publishing.install import load_package


def initialize(package: Path) -> Path:
    manifest=load_package(package)
    state=package.parent/'state'
    os.environ['XANA_HOME']=str(state)
    os.environ['XANA_PROJECT']='default'
    home=state/'projects/default'
    workspace=home/'workspace'
    if not (home/'project.json').exists():
        home.mkdir(parents=True,exist_ok=True)
        shutil.copytree(package/'workspace',workspace,dirs_exist_ok=True)
        (home/'project.json').write_text(json.dumps({'root':str(workspace)}))
        (home/'config.json').write_text(json.dumps({'agent_defaults':manifest.agent_defaults}))
        # Rewrite registered paths for the recipient; source-machine absolute paths never persist.
        from xana.skills.markdown_loader import FileSkillLoader
        import asyncio
        async def bindings():
            data={}
            for agent,paths in manifest.skills.items():
                data[agent]={}
                for rel in paths:
                    skill=await FileSkillLoader().load(str(workspace/rel))
                    data[agent][skill.spec().component.name]=str(workspace/rel)
            return data
        (home/'skill_bindings.json').write_text(json.dumps(asyncio.run(bindings()),ensure_ascii=False))
    return state


def configure_agent_models(workspace: Path, *, ask=input) -> None:
    """Map author model aliases in the recipient copy, preserving workflow bodies."""
    from xana.config.project_config import load_system_config
    from xana.skills.markdown_loader import split_frontmatter
    import yaml
    cfg=load_system_config()
    default=cfg.default_model.chat
    provider=cfg.model(default).type
    allowed={m.name for m in cfg.models if m.type==provider}
    for path in sorted((workspace/'.xana/agents').glob('*/AGENT.md')):
        front,body=split_frontmatter(path.read_text(),path=path)
        old=front.get('model')
        if old and old not in allowed:
            chosen=ask(f"{front.get('name',path.parent.name)} 原模型 {old}，接收方模型 [默认 {default}]: ").strip() or default
            if chosen not in allowed:
                raise ValueError('所选模型不存在或不属于当前 provider')
            front['model']=chosen
            path.write_text('---\n'+yaml.safe_dump(front,allow_unicode=True,sort_keys=False)+'---\n'+body+'\n')


def main(package: Path, argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description='Independent xana agent application')
    parser.add_argument('action',choices=['setup','run','login','pair'],nargs='?',default='run')
    args,extra=parser.parse_known_args(argv)
    manifest=load_package(package)
    initialize(package)
    if args.action=='setup':
        from xana.cli.commands.setup_wizard import run_wizard
        answer=input('模型配置：1) Kimi OAuth  2) API Key [1/2]: ').strip()
        if answer=='1':
            from xana.cli.commands.login import run
            if run(argparse.Namespace(region=None),out=sys.stdout):
                return 1
        result=run_wizard(ask=input,out=sys.stdout)
        if result:
            return result
        configure_agent_models(package.parent/"state/projects/default/workspace")
        values={}
        for name,description in manifest.settings.items():
            ask=getpass.getpass if any(part in name for part in ('PASSWORD','SECRET','TOKEN','API_KEY')) else input
            value=ask(f'{name} — {description}: ').strip()
            if not value:
                print(f'缺少业务配置 {name}',file=sys.stderr)
                return 1
            values[name]=value
        settings=package.parent/'state/settings.json'
        settings.write_text(json.dumps(values,ensure_ascii=False))
        settings.chmod(0o600)
        return 0
    if args.action in ('login','pair'):
        from xana.cli.xana import main as cli
        return cli([args.action,*extra])
    from xana.config.project_config import load_system_config
    cfg=load_system_config()
    if not cfg.default_model.chat:
        print('尚未配置模型；请先运行此应用的 setup。',file=sys.stderr)
        return 1
    settings_file=package.parent/'state/settings.json'
    values=json.loads(settings_file.read_text()) if settings_file.exists() else {}
    missing=set(manifest.settings)-values.keys()
    if missing:
        print('缺少业务配置，请运行 setup: '+', '.join(sorted(missing)),file=sys.stderr)
        return 1
    os.environ.update({name:values[name] for name in manifest.settings})
    missing_commands=[name for name in manifest.executables if shutil.which(name) is None]
    if missing_commands:
        print('缺少外部程序: '+', '.join(missing_commands),file=sys.stderr)
        return 1
    from xana.cli.serve import main as serve
    return serve(['--project','default','--agent',manifest.entry_agent])
