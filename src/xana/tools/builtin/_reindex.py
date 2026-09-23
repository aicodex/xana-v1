"""Shared reindex hook for file-writing builtin tools (Write/Edit).

After a successful write, markdown files under the memory tree root are
incrementally re-indexed so memory_search sees them without a restart.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xana.memory.markdown import MarkdownMemoryTree


async def maybe_reindex_markdown(tree: "MarkdownMemoryTree", file_path: str, cwd: str) -> None:
    """Reindex ``file_path`` when it is a markdown file under the tree root."""
    path = Path(file_path)
    if not path.is_absolute():
        path = (Path(cwd) / path).resolve()
    if path.suffix != ".md":
        return
    try:
        path.relative_to(tree.root)
    except ValueError:
        return
    await tree.reindex_paths((path,))
