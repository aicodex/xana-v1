"""Isolated bounded Python regex search; invoked by GrepTool, never on the event loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_HITS = 250
MAX_OUTPUT_CHARS = 48000


def search(args: dict) -> dict:
    try:
        regex = re.compile(args["pattern"], re.IGNORECASE if args.get("case_insensitive") else 0)
    except re.error as exc:
        return {"ok": False, "content": None, "error": f"invalid regex: {exc}",
                "failure_kind": "validation", "rendered": f"[Grep failed: invalid regex: {exc}]"}
    root = Path(args["path"])
    if not root.exists():
        return {"ok": False, "content": None, "error": f"path not found: {root}",
                "failure_kind": "not_found", "rendered": f"[Grep failed: path not found: {root}]"}
    paths = (root,) if root.is_file() else root.glob(args.get("glob") or "**/*")
    hits: list[str] = []
    skipped = 0
    used = 0
    truncated = False
    for path in paths:
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    continue
                data = file.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or b"\0" in data:
                skipped += 1
                continue
            text = data.decode("utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        if args.get("output_mode", "files_with_matches") == "files_with_matches":
            matches = (str(path),) if regex.search(text) else ()
        else:
            matches = (f"{path}:" + (f"{i}:" if args.get("line_numbers", True) else "") + line
                       for i, line in enumerate(text.splitlines(), 1) if regex.search(line))
        for match in matches:
            remaining = MAX_OUTPUT_CHARS - used
            if len(match) > remaining:
                hits.append(match[:remaining] + "…[line truncated]")
                truncated = True
                break
            hits.append(match)
            used += len(match) + 1
            if len(hits) >= MAX_HITS or used >= MAX_OUTPUT_CHARS:
                truncated = True
                break
        if truncated:
            break
    rendered = "\n".join(hits) if hits else "(no matches)"
    if skipped:
        rendered += f"\n[Grep: 跳过 {skipped} 个二进制、超过 8 MiB 或无法读取的文件]"
    if truncated:
        rendered += "\n[Grep: 输出已截断，请缩小路径或 glob]"
    return {"ok": True, "content": hits, "rendered": rendered, "truncated": truncated}


if __name__ == "__main__":
    # A pathological regex is killed by the parent timeout; memory stays bounded too.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    print(json.dumps(search(json.loads(sys.argv[1])), ensure_ascii=False))
