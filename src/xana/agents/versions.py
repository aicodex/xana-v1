"""文件 agent 的版本:候选 / 采纳 / 回滚(docs/design/iteration-agent-design.md §4、§9.2)。

只动工作流正文与同目录 playbook.md;frontmatter(目标契约、工具、评测套件)原样保留,不接受任何修改。
- 候选:<agents 目录>/<名>-c<N>/AGENT.md,frontmatter 复制自原 agent(只换 name),作为独立 agent 加载,
  供 eval_run(agent=候选名) 与原版本对比;candidate.json 记录来源与说明。
- 采纳:原 AGENT.md 与 playbook.md 先存档到 <名>/versions/v<K>.md、v<K>.playbook.md,再换成候选的正文与经验条目。
- 回滚:当前版本先存档,再恢复指定存档版本的正文与经验条目(frontmatter 仍用当前的)。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Mapping
from pathlib import Path

import yaml

from xana.agents.interfaces import Agent, AgentRegistry
from xana.agents.registry import PLAYBOOK_NAME
from xana.skills.markdown_loader import split_frontmatter

_CANDIDATE = re.compile(r"^(?P<base>[a-z][a-z0-9-]*?)-c(?P<n>\d+)$")


class VersionError(ValueError):
    pass


RESEARCH_DIR = "research"            # 研究知识库:任务模型、最佳实践、资料摘录、A3、决策记录(§9.6)
_MAX_NOTE_CHARS = 200_000


def _write_doc(path: Path, front: dict, body: str) -> None:
    path.write_text("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n"
                    + body.strip() + "\n", encoding="utf-8")


class AgentVersions:
    def __init__(self, *, registry: AgentRegistry, agents: Mapping[str, Agent]) -> None:
        self._registry = registry
        self._agents = agents

    async def _doc(self, name: str) -> Path:
        await self._registry.refresh(force=True)
        agent = self._agents.get(name)
        if agent is None:
            raise VersionError(f"没有 agent {name}")
        src = agent.spec().component.source
        if src.source_type == "builtin" or src.path is None:
            raise VersionError(f"{name} 是内置 agent,不能做版本管理")
        return Path(src.path)

    @staticmethod
    def _read(doc: Path) -> tuple[dict, str, str]:
        fm, body = split_frontmatter(doc.read_text(encoding="utf-8"), path=doc)
        pb = doc.parent / PLAYBOOK_NAME
        return fm, body, pb.read_text(encoding="utf-8") if pb.exists() else ""

    async def show(self, name: str) -> dict:
        doc = await self._doc(name)
        fm, body, playbook = self._read(doc)
        versions = sorted((p.stem for p in (doc.parent / "versions").glob("v*.md") if ".playbook" not in p.name),
                          key=lambda v: int(v[1:]))
        candidates = sorted(p.name for p in doc.parent.parent.glob(f"{name}-c*") if (p / "AGENT.md").exists())
        return {"name": name, "path": str(doc), "frontmatter": fm, "body": body, "playbook": playbook,
                "versions": versions, "candidates": candidates}

    async def candidate(self, name: str, *, body: str | None, playbook: str | None, note: str) -> str:
        if _CANDIDATE.match(name):
            raise VersionError("请基于正式 agent 创建候选,而不是候选的候选")
        if body is not None and body.lstrip().startswith("---"):
            raise VersionError("body 只能是工作流正文,不能带 frontmatter(目标契约只有人能改)")
        doc = await self._doc(name)
        fm, cur_body, cur_pb = self._read(doc)
        n = 1 + max((int(m["n"]) for p in doc.parent.parent.glob(f"{name}-c*") if (m := _CANDIDATE.match(p.name))),
                    default=0)
        cand = f"{name}-c{n}"
        d = doc.parent.parent / cand
        d.mkdir()
        _write_doc(d / "AGENT.md", {**fm, "name": cand}, cur_body if body is None else body)
        (d / PLAYBOOK_NAME).write_text(cur_pb if playbook is None else playbook, encoding="utf-8")
        (d / "candidate.json").write_text(json.dumps({"of": name, "note": note, "created": time.time()},
                                                     ensure_ascii=False), encoding="utf-8")
        errors = [e for e in await self._registry.refresh(force=True) if str(d) in str(e)]
        if errors or cand not in self._agents:
            shutil.rmtree(d)
            await self._registry.refresh(force=True)
            raise VersionError(f"候选加载失败:{errors[0] if errors else '未出现在 agent 表中'}")
        return cand

    def _archive(self, doc: Path) -> str:
        vdir = doc.parent / "versions"
        vdir.mkdir(exist_ok=True)
        k = 1 + max((int(p.stem[1:]) for p in vdir.glob("v*.md") if ".playbook" not in p.name), default=0)
        shutil.copy2(doc, vdir / f"v{k}.md")
        pb = doc.parent / PLAYBOOK_NAME
        (vdir / f"v{k}.playbook.md").write_text(pb.read_text(encoding="utf-8") if pb.exists() else "",
                                                encoding="utf-8")
        return f"v{k}"

    def _install(self, doc: Path, body: str, playbook: str) -> None:
        fm, _old, _pb = self._read(doc)
        _write_doc(doc, fm, body)                      # frontmatter 保持当前(人可能改过)
        (doc.parent / PLAYBOOK_NAME).write_text(playbook, encoding="utf-8")

    async def promote(self, cand: str) -> tuple[str, str]:
        m = _CANDIDATE.match(cand)
        if not m:
            raise VersionError(f"{cand} 不是候选(名字形如 <agent>-c<N>)")
        cdoc = await self._doc(cand)
        doc = await self._doc(m["base"])
        _fm, body, playbook = self._read(cdoc)
        archived = self._archive(doc)
        self._install(doc, body, playbook)
        shutil.rmtree(cdoc.parent)
        await self._registry.refresh(force=True)
        return m["base"], archived

    async def rollback(self, name: str, version: str) -> str:
        doc = await self._doc(name)
        src = doc.parent / "versions" / f"{version}.md"
        if not src.exists():
            raise VersionError(f"{name} 没有存档版本 {version}")
        _fm, body = split_frontmatter(src.read_text(encoding="utf-8"), path=src)
        pb = doc.parent / "versions" / f"{version}.playbook.md"
        archived = self._archive(doc)
        self._install(doc, body, pb.read_text(encoding="utf-8") if pb.exists() else "")
        await self._registry.refresh(force=True)
        return archived

    async def discard(self, cand: str) -> None:
        if not _CANDIDATE.match(cand):
            raise VersionError(f"{cand} 不是候选")
        cdoc = await self._doc(cand)
        shutil.rmtree(cdoc.parent)
        await self._registry.refresh(force=True)

    # ---- 研究知识库(.xana/agents/<名>/research/,只经这里读写) ----

    async def _research(self, name: str) -> Path:
        doc = await self._doc(name)
        if _CANDIDATE.match(name):
            raise VersionError("研究知识库属于正式 agent,不属于候选")
        return doc.parent / RESEARCH_DIR

    @staticmethod
    def _safe(root: Path, rel: str) -> Path:
        path = (root / rel).resolve()
        if root.resolve() not in path.parents or path.suffix != ".md":
            raise VersionError("路径必须是研究知识库内的 .md 文件,如 practices.md、sources/xxx.md")
        return path

    async def notes_list(self, name: str) -> list[str]:
        root = await self._research(name)
        return sorted(str(p.relative_to(root)) for p in root.rglob("*.md")) if root.exists() else []

    async def notes_read(self, name: str, rel: str) -> str:
        path = self._safe(await self._research(name), rel)
        if not path.exists():
            raise VersionError(f"研究知识库里没有 {rel}")
        return path.read_text(encoding="utf-8")

    async def notes_write(self, name: str, rel: str, content: str) -> Path:
        if len(content) > _MAX_NOTE_CHARS:
            raise VersionError(f"单个文件不超过 {_MAX_NOTE_CHARS} 字符,拆成多个文件")
        path = self._safe(await self._research(name), rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
