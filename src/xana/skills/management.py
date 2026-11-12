"""Local package inventory and explicit per-agent activation; installation is not activation."""
from __future__ import annotations

import asyncio
import json
import os
import logging
from pathlib import Path
import re
import shutil
import tempfile

from xana.skills.interfaces import Skill
from xana.skills.markdown_loader import FileSkillLoader, SkillManifestError, scan_skill_docs


class SkillManagement:
    def __init__(self, *, roots: tuple[Path, ...], install_root: Path, bindings_path: Path) -> None:
        self.roots = roots
        self.install_root = install_root
        self.bindings_path = bindings_path
        self._lock = asyncio.Lock()

    async def inventory(self) -> dict[str, Skill]:
        found = {}
        for root in self.roots:
            skills, errors = await scan_skill_docs(root)
            if errors:
                for error in errors:
                    logging.getLogger(__name__).warning('SKILL.md 加载失败: %s', error)
            for skill in skills:
                found[skill.spec().component.name] = skill
        return found

    def bindings(self) -> dict[str, dict[str, str]]:
        if not self.bindings_path.exists():
            return {}
        return json.loads(self.bindings_path.read_text())

    async def enabled(self, agent: str) -> tuple[Skill, ...]:
        out = []
        for name, raw_path in self.bindings().get(agent, {}).items():
            path = Path(raw_path)
            if not any(path.resolve().is_relative_to(root.resolve()) for root in self.roots):
                raise ValueError(f'技能 {name} 的注册路径不在技能目录内')
            if not path.exists():
                continue
            try:
                skill = await FileSkillLoader().load(str(path))
            except (OSError, SkillManifestError) as exc:
                logging.getLogger(__name__).warning("已启用的技能 %s 加载失败: %s", name, exc)
                continue
            if skill.spec().component.name != name:
                raise ValueError(f'技能 {name} 的名称已变更，请重新启用')
            out.append(skill)
        return tuple(out)

    async def set_enabled(self, names: tuple[str, ...], agents: tuple[str, ...], *, enabled: bool) -> None:
        if not names or not agents:
            raise ValueError('必须明确指定技能和目标 agents')
        async with self._lock:
            inventory = await self.inventory()
            if enabled and set(names) - inventory.keys():
                raise ValueError('技能尚未安装: ' + ', '.join(sorted(set(names) - inventory.keys())))
            data = self.bindings()
            for agent in agents:
                entries = data.setdefault(agent, {})
                for name in names:
                    if enabled:
                        entries[name] = str(inventory[name].spec().package.instructions_path.resolve())
                    else:
                        entries.pop(name, None)
                if not entries:
                    data.pop(agent, None)
            self.bindings_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w',dir=self.bindings_path.parent,delete=False) as file:
                tmp = Path(file.name)
                try:
                    json.dump(data,file,ensure_ascii=False,indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                    os.replace(tmp,self.bindings_path)
                finally:
                    tmp.unlink(missing_ok=True)

    async def adopt(self, root: Path) -> tuple[str, ...]:
        """Adopt packages staged by official npx skills. Existing local packages win."""
        skills, errors = await scan_skill_docs(root)
        if errors:
            raise ValueError(str(errors[0]))
        if not skills:
            raise ValueError('安装器没有生成可用的技能包')
        async with self._lock:
            existing = await self.inventory()
            selected = []
            for skill in skills:
                name = skill.spec().component.name
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',name):
                    raise ValueError(f'无效的技能名: {name!r}')
                if name not in existing:
                    selected.append(skill)
            await asyncio.to_thread(self._copy,selected)
            return tuple(s.spec().component.name for s in skills)

    def _copy(self, skills: list[Skill]) -> None:
        self.install_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.install-',dir=self.install_root) as tmp:
            staged = Path(tmp)
            size,count = 0,0
            for skill in skills:
                package = skill.spec().package.root
                name = skill.spec().component.name
                if package.is_symlink():
                    raise ValueError('技能包不能是符号链接')
                for path in package.rglob('*'):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise ValueError('技能包包含符号链接或特殊文件')
                    if path.is_file():
                        count += 1
                        size += path.stat().st_size
                    if count > 2000 or size > 100*1024*1024:
                        raise ValueError('技能包超过 2000 文件或 100 MiB')
                if (self.install_root/name).exists():
                    raise ValueError(f'目标技能目录已存在: {name}')
                shutil.copytree(package,staged/name)
            installed = []
            try:
                for skill in skills:
                    target = self.install_root/skill.spec().component.name
                    (staged/target.name).rename(target)
                    installed.append(target)
            except BaseException:
                for path in installed:
                    shutil.rmtree(path)
                raise
