"""Ledger —— ~/.xana/evolution/ledger.jsonl 的 append-only 账本(计划 §6.1)。

FileHistoryMirror 同款机制的第三个实例:只有 append/read/lint,
不提供任何 update/delete API(测试以 hasattr 断言锁死)。

- append:verdict 必填(None 拒收)——账本里不允许存在无 verdict 的条目
- lint:逐行校验,缺 verdict / 非法 verdict / JSON 损坏 → LintViolation
- recent(n):元 prompt 组装的近期 N 条消费方(§6.3)
"""
from __future__ import annotations

import json
from pathlib import Path

from xana.config.paths import project_home
from xana.evolution.contracts import LedgerEntry, LintViolation

_VERDICTS = ("kept", "rolled_back", "abandoned", "preregistered")   # preregistered:实验前的登记(§9.1 I1)


def default_evolution_home() -> Path:
    """当前项目的 projects/<id>/evolution(进化记录属于项目)。"""
    return project_home() / "evolution"


class Ledger:
    """append-only 进化账本。构造不给 path 时用 default_evolution_home()。"""

    def __init__(self, *, path: Path | None = None, home: Path | None = None) -> None:
        if path is None:
            root = Path(home) if home is not None else default_evolution_home()
            path = root / "ledger.jsonl"
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    async def append(self, entry: LedgerEntry) -> None:
        """追加一条闭合记录。verdict 缺失/非法 → ValueError,不落地。"""
        if entry.verdict is None:
            raise ValueError("账本条目必须带 verdict(kept/rolled_back/abandoned)")
        if entry.verdict not in _VERDICTS:
            raise ValueError(f"非法 verdict: {entry.verdict!r}")
        if not entry.episode_id:
            raise ValueError("账本条目必须带 episode_id")
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")

    async def read_all(self) -> tuple[LedgerEntry, ...]:
        out: list[LedgerEntry] = []
        if not self._path.exists():
            return ()
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(LedgerEntry.from_json(json.loads(line)))
        return tuple(out)

    async def recent(self, n: int) -> tuple[LedgerEntry, ...]:
        """近期 N 条(新在后),元 prompt 组装消费方。"""
        entries = await self.read_all()
        return entries[-n:] if n > 0 else ()

    def lint(self) -> list[LintViolation]:
        """逐行校验:JSON 损坏、缺 verdict、非法 verdict → 违例列表。"""
        violations: list[LintViolation] = []
        if not self._path.exists():
            return violations
        for idx, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                violations.append(LintViolation(line_no=idx, reason=f"行损坏: {exc}"))
                continue
            verdict = raw.get("verdict")
            if verdict is None:
                violations.append(LintViolation(line_no=idx, reason="条目缺 verdict"))
            elif verdict not in _VERDICTS:
                violations.append(LintViolation(line_no=idx, reason=f"非法 verdict: {verdict!r}"))
        return violations

    # 刻意不提供 update/delete/rewrite 等任何修改旧条目的 API(append-only)。
