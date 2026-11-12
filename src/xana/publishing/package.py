"""Publish immutable capability packages; runtime artifact stays separate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from xana.publishing.contracts import AgentPackage, MANIFEST
from xana.skills.markdown_loader import MarkdownDocParser, SkillManifestError
from xana.publishing.artifacts import build_runtime, file_sha256

_SKIP = {'.git','__pycache__','.venv','node_modules'}
_TEXT = {'.py','.sh','.md','.txt','.yaml','.yml','.json','.toml'}
_SECRET = re.compile(r'''(?i)(?:password|api_key|access_token|app_secret|bot_token)\s*[=:]\s*["']([^"']{4,})["']''')


def collect(root: Path, includes: list[str]) -> tuple[dict[str, Path], list[dict]]:
    files, issues = {}, []
    for name in includes:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('include 必须是项目内相对路径')
        path = root/relative
        if not path.exists():
            issues.append({'path':name,'reason':'文件不存在'})
            continue
        paths = [path] if path.is_file() else sorted(path.rglob('*'))
        for item in paths:
            rel = item.relative_to(root)
            if any(p in _SKIP for p in rel.parts) or item.is_dir():
                continue
            if item.is_symlink() or not item.is_file() or not item.resolve().is_relative_to(root.resolve()):
                issues.append({'path':str(rel),'reason':'不支持链接、特殊文件或项目外文件'})
                continue
            if item.name.startswith('.env') or item.suffix in ('.pem','.key'):
                issues.append({'path':str(rel),'reason':'凭据文件不能发布'})
                continue
            files[rel.as_posix()] = item
            if item.suffix in _TEXT:
                if item.stat().st_size > 1024*1024:
                    issues.append({'path':str(rel),'reason':'文本超过 1 MiB，无法完成发布检查'})
                    continue
                text = item.read_text(errors='replace')
                for number,line in enumerate(text.splitlines(),1):
                    if _SECRET.search(line):
                        issues.append({'path':str(rel),'line':number,'reason':'包含硬编码凭据，请改为接收方配置'})
                    # This is deliberately a blocker/report, never an automatic semantic rewrite.
                    for absolute in re.findall(r'/(?:home|Users)/[^\s"\'`<>;)]+',line):
                        if not absolute.startswith(str(root)):
                            issues.append({'path':str(rel),'line':number,'reason':'引用了项目外绝对路径'})
                            break
    if sum(p.stat().st_size for p in files.values()) > 1024**3:
        issues.append({'path':'.','reason':'能力包超过 1 GiB'})
    return files, issues


def _payload(root: Path, includes: list[str], package_sources: dict[str, Path] | None = None):
    files, issues = collect(root, includes)
    for prefix, source in (package_sources or {}).items():
        if Path(prefix).is_absolute() or '..' in Path(prefix).parts:
            raise ValueError('无效技能目标路径')
        additions, errors = collect(source, ['.'])
        issues.extend({'path':prefix+'/'+e['path'], **{k:v for k,v in e.items() if k!='path'}} for e in errors)
        for relative,path in additions.items():
            name=prefix+'/'+relative
            if name in files and file_sha256(files[name])!=file_sha256(path):
                raise ValueError(f'技能资源同名冲突: {name}')
            files[name]=path
    return files,issues


def inspect_package(root: Path, includes: list[str], entry_agent: str, package_sources: dict[str,Path] | None = None) -> dict:
    files, issues = _payload(root,includes,package_sources)
    for name,path in files.items():
        if name.startswith('.xana/agents/') and name.endswith('/AGENT.md'):
            try:
                definition=MarkdownDocParser().parse_sync(path)
                if definition.metadata.get('kind')!='agent' or definition.name!=path.parent.name:
                    raise ValueError('agent 名称与发布目录不匹配')
            except (SkillManifestError,ValueError) as exc:
                issues.append({'path':name,'reason':str(exc)})
    if entry_agent!='chat' and f'.xana/agents/{entry_agent}/AGENT.md' not in files:
        issues.append({'path':entry_agent,'reason':'入口 agent 定义不在发布范围'})
    return {'files':sorted(files),'bytes':sum(p.stat().st_size for p in files.values()),'issues':issues,'ready':not issues}


def publish(root: Path, *, name: str, version: str, description: str, entry_agent: str,
            includes: list[str], agent_defaults: dict, skills: dict[str,list[str]], output: Path,
            package_sources: dict[str,Path] | None = None,
            python_dependencies: list[str] | None = None, executables: list[str] | None = None,
            settings: dict[str,str] | None = None, runtime_url: str | None = None) -> Path:
    # Validate metadata before writing/building anything.
    AgentPackage(name=name,version=version,description=description,entry_agent=entry_agent,runtime_version='pending', settings=settings or {})
    report = inspect_package(root,includes,entry_agent,package_sources)
    if not report['ready']:
        raise ValueError('发布检查未通过: '+json.dumps(report['issues'],ensure_ascii=False))
    if output.exists():
        raise FileExistsError(f'发布文件已存在: {output}')
    files,_ = _payload(root,includes,package_sources)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='xana-publish-') as temp:
        bundle = Path(temp)
        wheel,runtime_version = build_runtime(bundle)
        package = bundle/name
        workspace = package/'workspace'
        workspace.mkdir(parents=True)
        for relative,path in files.items():
            target=workspace/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target)
            if target.suffix in _TEXT:
                text=target.read_text()
                target.write_text(text.replace(str(root),'.'))
        agents=[Path(f).parts[2] for f in files if f.startswith('.xana/agents/') and f.endswith('/AGENT.md')]
        for agent,paths in skills.items():
            if agent not in ['chat',*agents]:
                raise ValueError(f'技能绑定引用了未发布的 agent: {agent}')
            if any(p not in files or not p.endswith('/SKILL.md') for p in paths):
                raise ValueError(f'{agent} 的技能文件未包含在发布范围')
        manifest=AgentPackage(name=name,version=version,description=description,entry_agent=entry_agent,
            agents=agents,skills=skills,agent_defaults=agent_defaults,runtime_version=runtime_version,
            runtime_url=runtime_url,runtime_sha256=file_sha256(wheel),
            python_dependencies=python_dependencies or [],executables=executables or [],settings=settings or {},
            files={p.relative_to(workspace).as_posix():file_sha256(p)
                   for p in sorted(workspace.rglob('*')) if p.is_file()})
        (package/MANIFEST).write_text(manifest.model_dump_json(indent=2))
        (package/'SKILL.md').write_text('---\nname: '+name+'\ndescription: '+json.dumps(description,ensure_ascii=False)+'\n---\nThis is a xana agent capability package. Install with `xana agents add`; runtime and configuration are required.\n')
        from xana.publishing import bootstrap
        shutil.copy2(Path(bootstrap.__file__),bundle/'install.py')
        (bundle/'checksums.json').write_text(json.dumps({wheel.name:file_sha256(wheel)}))
        with tempfile.NamedTemporaryFile(prefix='.publish-',dir=output.parent,delete=False) as file:
            temporary=Path(file.name)
        try:
            with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(bundle.rglob('*')):
                    if path.is_file():
                        archive.write(path,str(path.relative_to(bundle)))
            os.link(temporary,output)
        finally:
            temporary.unlink(missing_ok=True)
    return output
