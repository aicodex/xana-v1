"""项目记忆:项目根目录下的 memory/,一条记忆一个文件(docs/design/memory-recall-design.md §3.1)。

记忆文件是唯一权威;索引、置顶内容都由文件的 frontmatter 实时生成(可重建的视图,不落盘)。
frontmatter:name / description / type(user | feedback | project | reference)/ pinned / updated。
手工编辑的文件加载时容错:缺 description 取正文第一句,缺 type 记为 project。
"""
from __future__ import annotations

import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from xana.memory.contracts import MEMORY_TYPES, MemoryRecord
from xana.memory.markdown import _split_frontmatter, search_terms

MEMORY_DIR = "memory"
INDEX_MAX_LINES = 200
INDEX_MAX_CHARS = 8000
PINNED_MAX_CHARS = 2000
DESC_MAX_CHARS = 150
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_TYPE_TITLES = {"user": "用户", "feedback": "做法反馈", "project": "项目", "reference": "参考资源"}


class ProjectMemoryStore:
    def __init__(self, root: Path) -> None:
        """root = 项目根目录;记忆放在 root/memory/。"""
        self._root = Path(root)
        self.dir = self._root / MEMORY_DIR

    # ---------- 读 ----------

    def list(self) -> tuple[MemoryRecord, ...]:
        if not self.dir.is_dir():
            return ()
        records = []
        for path in sorted(self.dir.glob("*.md")):
            try:
                records.append(_parse(path, self._root))
            except (OSError, UnicodeDecodeError):
                continue
        return tuple(records)

    def get(self, slug: str) -> MemoryRecord | None:
        path = self.dir / f"{_slug(slug)}.md"
        return _parse(path, self._root) if path.exists() else None

    def index_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str:
        """索引:按类型分组,每条一行;超出行数 / 字数截断并提示。没有记忆返回空串。"""
        records = self.list() if records is None else records
        if not records:
            return ""
        lines: list[str] = []
        for mtype in MEMORY_TYPES:
            group = [r for r in records if r.type == mtype]
            if not group:
                continue
            lines.append(f"## {_TYPE_TITLES[mtype]}({mtype})")
            lines.extend(f"- {r.name}({r.path})— {r.description}{'  [置顶]' if r.pinned else ''}" for r in group)
        text, total = [], 0
        for i, line in enumerate(lines):
            if i >= INDEX_MAX_LINES or total + len(line) > INDEX_MAX_CHARS:
                text.append(f"…(索引过长已截断,共 {len(records)} 条记忆;其余用 memory_search 查找)")
                break
            text.append(line)
            total += len(line) + 1
        return "\n".join(text)

    def pinned_text(self, records: tuple[MemoryRecord, ...] | None = None) -> str:
        records = self.list() if records is None else records
        parts = [f"### {r.name}({r.path})\n{r.body.strip()}" for r in records if r.pinned]
        text = "\n\n".join(parts)
        if len(text) > PINNED_MAX_CHARS:
            text = text[:PINNED_MAX_CHARS] + "\n…(置顶记忆过长已截断,需要时用 Read 查看全文)"
        return text

    def lexical_rank(self, query: str, records: tuple[MemoryRecord, ...], limit: int) -> tuple[MemoryRecord, ...]:
        """字面检索兜底:查询词与 名称 / 描述 / 正文 的两字片段重叠率,名称与描述加权。"""
        terms = set(search_terms(query))
        if not terms:
            return ()
        scored = []
        for r in records:
            head = set(search_terms(f"{r.name} {r.description}"))
            body = set(search_terms(r.body))
            score = (2 * len(terms & head) + len(terms & body)) / len(terms)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return tuple(r for _, r in scored[:limit])

    def similar(self, name: str, description: str, *, exclude: str = "") -> tuple[MemoryRecord, float] | None:
        """与已有记忆的 名称 + 描述 最相近的一条及相似度(查重用)。"""
        terms = set(search_terms(f"{name} {description}"))
        best: tuple[MemoryRecord, float] | None = None
        for r in self.list():
            if r.slug == exclude or not terms:
                continue
            other = set(search_terms(f"{r.name} {r.description}"))
            score = len(terms & other) / len(terms | other) if other else 0.0
            if best is None or score > best[1]:
                best = (r, score)
        return best

    # ---------- 写 ----------

    def write(self, slug: str, *, name: str, description: str, mtype: str, body: str,
              pinned: bool = False) -> MemoryRecord:
        slug = _slug(slug)
        if mtype not in MEMORY_TYPES:
            raise ValueError(f"type 只能是 {list(MEMORY_TYPES)}")
        if not name.strip() or not description.strip():
            raise ValueError("name 与 description 不能为空")
        front: dict[str, Any] = {"name": name.strip(), "description": " ".join(description.split()),
                                 "type": mtype, "updated": date.today().isoformat()}
        if pinned:
            front["pinned"] = True
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{slug}.md"
        path.write_text("---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                        + "---\n\n" + body.strip() + "\n", encoding="utf-8")
        return _parse(path, self._root)

    def delete(self, slug: str) -> bool:
        path = self.dir / f"{_slug(slug)}.md"
        if not path.exists():
            return False
        path.unlink()
        return True


def _slug(slug: str) -> str:
    slug = slug.strip().removesuffix(".md").removeprefix(f"{MEMORY_DIR}/")
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"记忆文件名 {slug!r} 不合法:小写字母 / 数字开头,可含 - _,最长 64")
    return slug


def _parse(path: Path, root: Path) -> MemoryRecord:
    front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(front.get("name") or front.get("title") or path.stem).strip()
    desc = " ".join(str(front.get("description") or "").split()) or _first_sentence(body)
    if len(desc) > DESC_MAX_CHARS:
        desc = desc[: DESC_MAX_CHARS - 1] + "…"
    mtype = str(front.get("type") or "project")
    return MemoryRecord(
        slug=path.stem, path=str(path.relative_to(root)), name=name, description=desc,
        type=mtype if mtype in MEMORY_TYPES else "project", pinned=bool(front.get("pinned")),
        body=body.strip(), mtime=path.stat().st_mtime)


def _first_sentence(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("-*> ").strip()
        if line and not line.startswith("#") and line != "---":
            return re.split(r"(?<=[。!?.!?])", line, maxsplit=1)[0].strip()
    return ""


# ---------- 一次性迁移(§4):旧的 memory/*.md(title / status)与 L2/rules/*.md → 新格式 ----------

def migrate_legacy(root: Path, *, backup_root: Path) -> list[str]:
    """把旧记忆迁到 root/memory/ 新格式;返回迁移的文件(相对 root)。迁移前把涉及的文件复制到 backup_root。
    幂等:已是新格式(有 type)的跳过;迁移完的空 L2/ 目录删除。"""
    root = Path(root)
    store = ProjectMemoryStore(root)
    sources: list[tuple[Path, str]] = []
    for path in sorted((root / MEMORY_DIR).glob("*.md")):
        front, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
        if "type" not in front:
            sources.append((path, "user" if "preference" in path.stem else "project"))
    sources += [(p, "project") for p in sorted((root / "L2" / "rules").glob("*.md"))]
    if not sources:
        return []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path, _ in sources:
        dst = Path(backup_root) / stamp / path.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
    moved = []
    for path, mtype in sources:
        front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        if str(front.get("status")) == "deprecated":
            path.unlink()                      # 已被替代的旧规则不再迁移(备份里还有)
            continue
        name = str(front.get("name") or front.get("title") or path.stem)
        slug = path.stem.lower().replace(".", "-")
        store.write(slug, name=name, description=str(front.get("description") or _first_sentence(body) or name),
                    mtype=mtype, body=body, pinned=bool(front.get("pinned")))
        if path.parent != store.dir:
            path.unlink()
        moved.append(str(path.relative_to(root)))
    l2 = root / "L2"
    if l2.is_dir() and not any(p.is_file() for p in l2.rglob("*")):
        shutil.rmtree(l2)
    return moved
