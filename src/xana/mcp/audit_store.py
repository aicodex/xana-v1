"""MCP 审核结果脱敏落盘:只追加 JSONL,不保存原始描述/schema(design §6)。

每行一条记录:时间戳、server、tool、定义指纹、generation、决定与 finding 的
rule/path/message。原始定义与可疑原文永不写入,文件仅为审计证据,运行状态权威
在 manager。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from xana.mcp.audit import AuditFinding


class AuditStore:
    """向 `mcp/audit.jsonl` 追加脱敏审核记录;构造时确保目录存在。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        server: str,
        tool: str,
        fingerprint: str,
        generation: int,
        findings: Iterable[AuditFinding],
    ) -> None:
        findings = tuple(findings)
        entry = {
            "ts": time.time(),
            "server": server,
            "tool": tool,
            "fingerprint": fingerprint,
            "generation": generation,
            "decision": "rejected" if findings else "accepted",
            "findings": [
                {"rule": f.rule, "path": f.path, "message": f.message} for f in findings
            ],
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
