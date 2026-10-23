"""评测套件(docs/design/iteration-agent-design.md §8):核心只定格式与执行,业务内容由项目提供。

<项目>/.xana/evals/<名>/
  suite.yaml     agent(被测 agent)、scorer(打分命令,参数数组,在套件目录下运行)、concurrency、
                 env(按工具名的策略,同 ToolEnvConfig.strategies)、on_miss
  cases.jsonl    一行一个样本:{id, split(train / heldout / live), input(原样交给被测 agent), as_of?, ...}
  batches/<批次>/runs.jsonl    每次运行一行(核心写入):case_id、rep、run_id、status、text、用量
  batches/<批次>/scores.json   打分命令的输出

打分命令:标准输入 {"suite", "cases": [...], "runs": [...]},标准输出
{"per_case": [{case_id, rep, status(scored / pending / invalid), score?, big_miss?, miss_type?, detail?}],
 "summary": {...}};确定性,不用模型打分。真值由打分命令自己读,不放进 cases.jsonl。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from xana.extension_api.capabilities import AgentRunner
from xana.state.interfaces import RunStore
from xana.tools.contracts import ToolEnvConfig

SPLITS = ("train", "heldout", "live")
EVAL_PREAMBLE = "这是一次评测运行:独立完成下面的任务,不要提问;最终回答按任务要求的格式给出,会被自动打分。"


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class EvalCase:
    id: str
    split: str
    input: str
    as_of: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalSuite:
    name: str
    dir: Path
    agent: str
    scorer: tuple[str, ...]
    cases: tuple[EvalCase, ...]
    concurrency: int = 3
    strategies: dict[str, str] = field(default_factory=dict)
    on_miss: str = "live"
    protected: tuple[str, ...] = ()     # 套件目录外、影响打分的文件(如计算真值的代码),一并做完整性校验

    def fingerprint(self) -> str:
        """样本、打分命令、真值相关文件的内容哈希:批次运行后被改动则拒绝打分(防迭代者改题刷分,§9.3)。"""
        h = hashlib.sha256()
        files = sorted(p for p in self.dir.rglob("*") if p.is_file() and "batches" not in p.relative_to(self.dir).parts)
        files += [(self.dir / extra).resolve() for extra in self.protected]
        for p in files:
            h.update(str(p.relative_to(self.dir) if p.is_relative_to(self.dir) else p).encode())
            h.update(p.read_bytes() if p.exists() else b"<missing>")
        return h.hexdigest()[:16]

    def select(self, *, split: str | None = None, case_ids: tuple[str, ...] = ()) -> tuple[EvalCase, ...]:
        if case_ids:
            known = {c.id: c for c in self.cases}
            missing = [i for i in case_ids if i not in known]
            if missing:
                raise SuiteError(f"套件 {self.name} 没有样本 {', '.join(missing)}")
            return tuple(known[i] for i in case_ids)
        return tuple(c for c in self.cases if split is None or c.split == split)


def load_suite(root: Path, name: str) -> EvalSuite:
    d = Path(root) / name
    try:
        cfg = yaml.safe_load((d / "suite.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise SuiteError(f"没有评测套件 {name}({d / 'suite.yaml'} 不存在)") from None
    if not isinstance(cfg.get("agent"), str) or not cfg["agent"]:
        raise SuiteError("suite.yaml 需要 agent")
    scorer = cfg.get("scorer")
    if not isinstance(scorer, list) or not scorer or not all(isinstance(x, str) for x in scorer):
        raise SuiteError("suite.yaml 的 scorer 必须是命令参数数组")
    cases: list[EvalCase] = []
    for n, line in enumerate((d / "cases.jsonl").read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("split") not in SPLITS or not row.get("id") or not row.get("input"):
            raise SuiteError(f"cases.jsonl 第 {n} 行需要 id、split({'/'.join(SPLITS)})、input")
        cases.append(EvalCase(id=str(row["id"]), split=row["split"], input=str(row["input"]),
                              as_of=row.get("as_of"),
                              meta={k: v for k, v in row.items() if k not in ("id", "split", "input", "as_of")}))
    env = cfg.get("env") or {}
    return EvalSuite(name=name, dir=d, agent=cfg["agent"], scorer=tuple(scorer), cases=tuple(cases),
                     concurrency=int(cfg.get("concurrency", 3)), strategies=dict(env.get("strategies") or {}),
                     on_miss=str(env.get("on_miss", "live")),
                     protected=tuple(str(x) for x in cfg.get("protected") or ()))


@dataclass
class RunRow:
    case_id: str
    rep: int
    split: str
    run_id: str
    status: str
    text: str
    error: str = ""
    tool_counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model_calls: int = 0


class EvalRunner:
    """按套件跑样本:每个样本一个独立会话,工具环境 = 样本的 as_of + 套件策略;结果写进批次目录。"""

    def __init__(self, *, runner: AgentRunner, run_store: RunStore | None, evals_root: Path) -> None:
        self._runner = runner
        self._runs = run_store
        self.root = Path(evals_root)

    def suite(self, name: str) -> EvalSuite:
        return load_suite(self.root, name)

    async def run(self, suite: EvalSuite, cases: tuple[EvalCase, ...], *, repeat: int = 1,
                  agent: str | None = None, label: str = "", tape: tuple[str, ...] = ()) -> str:
        batch = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:4]}" + (f"-{label}" if label else "")
        out_dir = suite.dir / "batches" / batch
        out_dir.mkdir(parents=True)
        (out_dir / "batch.json").write_text(json.dumps({
            "suite": suite.name, "agent": agent or suite.agent, "label": label, "repeat": repeat,
            "cases": [c.id for c in cases], "started": time.time(), "tape": list(tape),
            "fingerprint": suite.fingerprint()}, ensure_ascii=False),
            encoding="utf-8")
        slots = asyncio.Semaphore(max(1, suite.concurrency))
        lock = asyncio.Lock()

        async def one(case: EvalCase, rep: int) -> None:
            async with slots:
                row = await self._run_case(suite, case, rep, agent or suite.agent, batch, tape)
            async with lock:
                with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

        await asyncio.gather(*(one(c, r) for c in cases for r in range(repeat)))
        return batch

    async def _run_case(self, suite: EvalSuite, case: EvalCase, rep: int, agent: str, batch: str,
                        tape: tuple[str, ...]) -> RunRow:
        env = ToolEnvConfig(as_of=case.as_of, tape=tape, strategies=dict(suite.strategies),  # type: ignore[arg-type]
                            on_miss=suite.on_miss)  # type: ignore[arg-type]
        t0 = time.monotonic()
        try:
            out = await self._runner.run(agent_name=agent, prompt=case.input,
                                         session_id=f"eval-{suite.name}-{batch}-{case.id}-{rep}",
                                         parent_run_id=f"eval:{suite.name}:{batch}", tool_env=env,
                                         preamble=EVAL_PREAMBLE)
            row = RunRow(case_id=case.id, rep=rep, split=case.split, run_id=out.run_id, status=out.status,
                         text=out.text, error=out.error, tool_counts=dict(out.tool_counts))
        except Exception as exc:   # 单个样本失败不影响整批
            row = RunRow(case_id=case.id, rep=rep, split=case.split, run_id="", status="error", text="",
                         error=f"{type(exc).__name__}: {exc}")
        row.elapsed_s = round(time.monotonic() - t0, 1)
        if self._runs is not None and row.run_id:
            rec = await self._runs.get(row.run_id)
            if rec is not None:
                row.tokens_in, row.tokens_out = rec.usage.total_input_tokens, rec.usage.total_output_tokens
                row.model_calls = rec.usage.model_calls
        return row

    def resolve_batch(self, suite: EvalSuite, batch: str) -> str:
        """batch="latest" → 最近一个批次。"""
        if batch != "latest":
            return batch
        done = sorted(p.name for p in (suite.dir / "batches").glob("*") if (p / "runs.jsonl").exists())
        if not done:
            raise SuiteError(f"套件 {suite.name} 还没有批次")
        return done[-1]

    def batch_runs(self, suite: EvalSuite, batch: str) -> list[dict]:
        batch = self.resolve_batch(suite, batch)
        path = suite.dir / "batches" / batch / "runs.jsonl"
        if not path.exists():
            raise SuiteError(f"套件 {suite.name} 没有批次 {batch}")
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    async def score(self, suite: EvalSuite, batch: str) -> dict:
        """调套件的打分命令(确定性脚本),结果写 scores.json 并返回。"""
        batch = self.resolve_batch(suite, batch)
        runs = self.batch_runs(suite, batch)
        meta = json.loads((suite.dir / "batches" / batch / "batch.json").read_text(encoding="utf-8"))
        if meta.get("fingerprint") != suite.fingerprint():
            raise SuiteError("评测套件(样本 / 打分命令 / 真值相关文件)在这个批次运行后被改动过,拒绝打分;"
                             "需要改套件请让用户来改,然后重新跑批次")
        payload = {"suite": suite.name, "cases": [{"id": c.id, "split": c.split, "as_of": c.as_of, **c.meta}
                                                   for c in suite.cases],
                   "runs": runs}
        proc = await asyncio.create_subprocess_exec(
            *suite.scorer, cwd=str(suite.dir), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if proc.returncode != 0:
            raise SuiteError(f"打分命令失败(退出码 {proc.returncode}):{err.decode('utf-8', 'replace')[-2000:]}")
        result = json.loads(out.decode("utf-8"))
        result["batch"] = batch
        (suite.dir / "batches" / batch / "scores.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result
