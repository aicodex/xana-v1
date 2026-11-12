"""Project-local Markdown evidence retrieval using the SAG2 reference algorithm.

Source files remain authoritative. Every query validates a content fingerprint;
the SQLite corpus is rebuilt when its selected source snapshot changes. The
scoring backend is an explicit dependency, independent of chat models.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from collections.abc import Callable

from xana.memory.contracts import (
    EvidenceCandidate,
    EvidenceJudge,
    MemoryNode,
    SAG2Options,
    SearchHit,
    SearchQuery,
)
from xana.memory.interfaces import MemorySearchEngine
from xana.memory.markdown import _split_frontmatter
from xana.memory.sag2.index import CorpusIndex
from xana.memory.sag2.retrieval import search_sag2
from xana.memory.store import MEMORY_DIR


@dataclass(frozen=True)
class _Source:
    path: str
    sha256: str
    title: str
    text: str
    body: str
    frontmatter: dict
    mtime: float


class ProjectMemorySearch(MemorySearchEngine):
    def __init__(
        self,
        *,
        project_root: Path,
        db_path: Path,
        judge: EvidenceJudge,
        timeout_s: float,
        options: SAG2Options = SAG2Options(),
        on_query: Callable[[dict], None] | None = None,
    ) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("memory query timeout must be positive")
        if not callable(judge):
            raise ValueError("memory evidence judge is required")
        self.project_root = project_root.resolve()
        self.memory_dir = self.project_root / MEMORY_DIR
        self.db_path = db_path.resolve()
        self._judge, self._timeout, self._options = judge, timeout_s, options
        self._on_query = on_query
        self._lock = asyncio.Lock()

    def _sources(self, scopes: tuple[str, ...] = ()) -> tuple[_Source, ...]:
        # Resolve both the folder and every file before reading any source.
        if self.memory_dir.resolve() != self.memory_dir:
            raise ValueError("memory folder must belong to this project")
        result = []
        for path in sorted(self.memory_dir.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(self.memory_dir):
                raise ValueError("memory source must belong to this project")
            rel = path.relative_to(self.project_root).as_posix()
            if scopes and not any(
                rel == prefix.rstrip("/") or rel.startswith(prefix.rstrip("/") + "/")
                for prefix in scopes
            ):
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            front, body = _split_frontmatter(text)
            if front.get("status") == "deprecated" or not body.strip():
                continue
            title = str(front.get("name") or front.get("title") or path.stem)
            result.append(
                _Source(
                    rel,
                    hashlib.sha256(raw).hexdigest(),
                    title,
                    text,
                    body.strip(),
                    front,
                    path.stat().st_mtime,
                )
            )
        return tuple(result)

    def _open(self, sources: tuple[_Source, ...]) -> CorpusIndex:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS xana_memory_owner (project_root TEXT PRIMARY KEY)"
            )
            owner = connection.execute(
                "SELECT project_root FROM xana_memory_owner"
            ).fetchone()
            if owner is not None and owner[0] != str(self.project_root):
                raise ValueError("memory index belongs to another project")
            connection.execute(
                "INSERT OR IGNORE INTO xana_memory_owner VALUES (?)",
                (str(self.project_root),),
            )
        corpus = [
            {
                "title": (
                    source.title
                    + " "
                    + str(source.frontmatter.get("description") or "")
                ).strip(),
                "text": source.text,
                "path": source.path,
                "sha256": source.sha256,
                "idx": source.path,
            }
            for source in sources
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                [(source.path, source.sha256, source.title) for source in sources],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return CorpusIndex(
            self.db_path, corpus, fingerprint, source_dir=self.project_root
        )

    async def synchronize(self) -> None:
        """Index memory/ even when the project has no README or knowledge tree."""
        async with self._lock:
            index = self._open(self._sources())
            index.close()

    async def index(self, node: MemoryNode) -> None:
        if node.path.startswith(f"{MEMORY_DIR}/"):
            await self.synchronize()

    async def remove(self, node_path: str) -> None:
        if node_path.startswith(f"{MEMORY_DIR}/"):
            await self.synchronize()

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        if not query.text.strip():
            return ()
        if type(query.top_k) is not int or not 1 <= query.top_k <= 100:
            raise ValueError("memory top_k must be between 1 and 100")

        async def retrieve() -> tuple[SearchHit, ...]:
            async with self._lock:
                return await self._search(query)

        return await asyncio.wait_for(retrieve(), self._timeout)

    async def _search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        sources = self._sources(query.scope_paths)
        if not sources:
            return ()
        by_document = {index + 1: source for index, source in enumerate(sources)}
        models, usage = (
            set(),
            {"requests": 0, "judgments": 0, "input_tokens": 0, "output_tokens": 0},
        )

        def candidate(row: dict) -> EvidenceCandidate:
            source = by_document[row["document_id"]]
            return EvidenceCandidate(
                str(row["id"]),
                source.path,
                source.sha256,
                row["title"],
                row["content"],
                tuple(tuple(span) for span in json.loads(row["spans"])),
                row.get("previous_jev_score"),
            )

        async def score(
            text: str, rows: list[dict], evidence: list[dict]
        ) -> tuple[list[dict], dict]:
            result = await self._judge(
                text,
                tuple(candidate(row) for row in rows),
                tuple(candidate(row) for row in evidence),
            )
            scores = {item.candidate_id: item.score for item in result.scores}
            if len(scores) != len(result.scores) or set(scores) != {
                str(row["id"]) for row in rows
            }:
                raise ValueError(
                    "memory judge must return exactly one score for each candidate"
                )
            if any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
                for value in scores.values()
            ):
                raise ValueError(
                    "memory judge scores must be finite numbers between zero and one"
                )
            if any(
                type(value) is not int or value < 0
                for value in (
                    result.input_tokens,
                    result.output_tokens,
                    result.requests,
                )
            ):
                raise ValueError("memory judge usage must be non-negative integers")
            if not result.model.strip():
                raise ValueError("memory judge model identity is required")
            models.add(result.model)
            current = {
                "requests": result.requests,
                "judgments": len(rows),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "batches": [],
            }
            for key in usage:
                usage[key] += current[key]
            return [
                {**row, "jev_score": scores[str(row["id"])]} for row in rows
            ], current

        index = self._open(sources)
        try:
            result = await search_sag2(
                index, score, query.text, **asdict(self._options)
            )
        finally:
            index.close()
        if self._sources(query.scope_paths) != sources:
            raise ValueError(
                "memory sources changed during retrieval; repeat the query"
            )
        hits: dict[str, SearchHit] = {}
        for row in result["candidates"]:
            source = by_document[row["document_id"]]
            if row["jev_score"] < query.min_score:
                continue
            if source.path in hits:
                continue
            hits[source.path] = SearchHit(
                source.path,
                row["jev_score"],
                (row["content"],),
                MemoryNode(
                    source.path,
                    "doc",
                    source.title,
                    source.body,
                    frontmatter=source.frontmatter,
                    updated_ts=source.mtime,
                ),
                {
                    "source_hash": source.sha256,
                    "spans": json.loads(row["spans"]),
                    "hop": row["hop"],
                    "judge_models": sorted(models),
                },
            )
            if len(hits) >= query.top_k:
                break
        if self._on_query is not None:
            self._on_query(
                {
                    "query": query.text,
                    "scope_paths": query.scope_paths,
                    "algorithm": "SAG2",
                    "judge_models": sorted(models),
                    "usage": usage,
                    "trace": result["trace"],
                }
            )
        return tuple(hits.values())
