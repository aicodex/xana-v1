"""评测套件(docs/design/iteration-agent-design.md §8):核心只定格式与执行,业务内容由项目提供。

<项目>/.xana/evals/<名>/
  suite.yaml     agent(被测 agent)、scorer(打分命令,参数数组,在套件目录下运行)、concurrency、
                 env(按工具名的策略,同 ToolEnvConfig.strategies)、on_miss
  METRICS.md     评测说明(量什么、怎么算;对迭代者可见,打分代码与真值不可见)
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
from collections.abc import Awaitable, Callable
from typing import Any

import yaml

from xana.extension_api.capabilities import AgentRunner
from xana.state.budget import day_usage
from xana.state.interfaces import RunStore
from xana.tools.contracts import ToolEnvConfig

SPLITS = ("train", "heldout", "live")
SCORE_CACHE_S = 6 * 3600.0     # 打分结果的复用期限(无 pending 样本时)
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
    daily_tokens: int | None = None     # 当日 token 上限(按全部 run 的持久化用量计,iteration-agent-design §12)

    @property
    def metrics_doc(self) -> str:
        path = self.dir / "METRICS.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def fingerprint(self) -> str:
        """样本、打分命令、真值相关文件的内容哈希:批次运行后被改动则拒绝打分(防迭代者改题刷分,§9.3)。"""
        h = hashlib.sha256()
        # suite.yaml 只计打分命令与受保护文件清单:改并发、预算等运行配置不影响已有批次的打分
        h.update(json.dumps([list(self.scorer), list(self.protected)]).encode())
        files = sorted(p for p in self.dir.rglob("*") if p.is_file() and p.name != "suite.yaml"
                       and "batches" not in p.relative_to(self.dir).parts)
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
                     protected=tuple(str(x) for x in cfg.get("protected") or ()),
                     daily_tokens=int(cfg["daily_tokens"]) if cfg.get("daily_tokens") else None)


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

    def __init__(self, *, runner: AgentRunner, run_store: RunStore | None, evals_root: Path,
                 quota_wait: Callable[[], Awaitable[tuple[str, float] | None]] | None = None) -> None:
        """quota_wait:模型服务额度检查(如 Kimi 订阅额度),用到阈值时返回 (原因, 距重置秒数);每个样本开跑前调用。
        额度 / 当日预算用完时暂停开新样本,等重置后接着跑——不跳过样本(评测结果必须完整)。"""
        self._runner = runner
        self._runs = run_store
        self.root = Path(evals_root)
        self._quota_wait = quota_wait

    async def _blocked(self, suite: EvalSuite) -> tuple[str, float] | None:
        """(原因, 要等多少秒) 或 None(可以开跑)。"""
        if suite.daily_tokens and self._runs is not None:
            used = await day_usage(self._runs)
            if used >= suite.daily_tokens:
                tomorrow = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) + 86400 + 60
                return (f"当日预算已用完(已用 {used} / 上限 {suite.daily_tokens} token)", tomorrow - time.time())
        if self._quota_wait is not None:
            try:
                return await self._quota_wait()
            except Exception:   # 额度接口不可用不阻断评测
                return None
        return None

    async def _gate(self, suite: EvalSuite, progress: Callable[[str], None] | None) -> None:
        """额度 / 预算用完 → 原地等到重置再放行(每 10 分钟复查一次,提前恢复就提前放行)。"""
        while (blocked := await self._blocked(suite)) is not None:
            reason, wait_s = blocked
            if progress is not None:
                progress(f"{reason},暂停到 {time.strftime('%m-%d %H:%M', time.localtime(time.time() + wait_s))} 后继续")
            await asyncio.sleep(min(max(wait_s, 1.0), 600.0))

    def suite(self, name: str) -> EvalSuite:
        return load_suite(self.root, name)

    async def run(self, suite: EvalSuite, cases: tuple[EvalCase, ...], *, repeat: int = 1,
                  agent: str | None = None, label: str = "", tape: tuple[str, ...] = (),
                  on_case: Callable[[str], None] | None = None) -> str:
        """on_case:每个样本跑完回调一句进度(后台任务显示用)。"""
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
        done, total = [0], len(cases) * repeat

        async def one(case: EvalCase, rep: int) -> None:
            async with slots:
                await self._gate(suite, on_case)
                row = await self._run_case(suite, case, rep, agent or suite.agent, batch, tape)
            async with lock:
                with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
                done[0] += 1
                if on_case is not None:
                    on_case(f"{done[0]}/{total} · {case.id}#{rep} {row.status}")

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

    def heldout_run_ids(self) -> frozenset[str]:
        """所有套件、所有批次里留出集样本的 run_id(迭代团队不能查看这些运行的过程)。"""
        ids: set[str] = set()
        for runs in self.root.glob("*/batches/*/runs.jsonl"):
            for line in runs.read_text(encoding="utf-8").splitlines():
                if '"heldout"' in line:
                    row = json.loads(line)
                    if row.get("split") == "heldout" and row.get("run_id"):
                        ids.add(row["run_id"])
        return frozenset(ids)

    def resolve_batch(self, suite: EvalSuite, batch: str) -> str:
        """batch="latest" → 最近一个批次;否则接受完整批次号或它的唯一前缀(不带标签的写法)。"""
        done = sorted(p.name for p in (suite.dir / "batches").glob("*") if (p / "runs.jsonl").exists())
        if batch == "latest":
            if not done:
                raise SuiteError(f"套件 {suite.name} 还没有批次")
            return done[-1]
        if batch in done:
            return batch
        hits = [b for b in done if b.startswith(batch)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise SuiteError(f"批次号 {batch} 对应多个批次:{', '.join(hits)}")
        raise SuiteError(f"套件 {suite.name} 没有批次 {batch}(最近的批次:{', '.join(done[-5:]) or '无'})")

    def batch_runs(self, suite: EvalSuite, batch: str) -> list[dict]:
        batch = self.resolve_batch(suite, batch)
        path = suite.dir / "batches" / batch / "runs.jsonl"
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    async def score(self, suite: EvalSuite, batch: str, *, max_age_s: float = SCORE_CACHE_S) -> dict:
        """调套件的打分命令(确定性脚本),结果写 scores.json 并返回。
        复用已有结果:同一指纹、批次之后没有新运行、没有 pending 样本(真值可能已揭晓)、且不超过 max_age_s。"""
        batch = self.resolve_batch(suite, batch)
        runs = self.batch_runs(suite, batch)
        bdir = suite.dir / "batches" / batch
        meta = json.loads((bdir / "batch.json").read_text(encoding="utf-8"))
        fingerprint = suite.fingerprint()
        if meta.get("fingerprint") != fingerprint:
            raise SuiteError("评测套件(样本 / 打分命令 / 真值相关文件)在这个批次运行后被改动过,拒绝打分;"
                             "需要改套件请让用户来改,然后重新跑批次")
        cached = bdir / "scores.json"
        if cached.exists():
            prev = json.loads(cached.read_text(encoding="utf-8"))
            fresh = (prev.get("fingerprint") == fingerprint
                     and prev.get("scored_at", 0) >= (bdir / "runs.jsonl").stat().st_mtime
                     and time.time() - prev.get("scored_at", 0) < max_age_s
                     and not any(r.get("status") == "pending" for r in prev.get("per_case") or []))
            if fresh:
                return prev
        payload = {"suite": suite.name, "cases": [{"id": c.id, "split": c.split, "as_of": c.as_of, **c.meta}
                                                   for c in suite.cases],
                   "runs": runs}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(2):          # 打分进程偶发异常退出(如 -6):重试一次
            proc = await asyncio.create_subprocess_exec(
                *suite.scorer, cwd=str(suite.dir), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate(data)
            if proc.returncode == 0 or (proc.returncode or 0) > 0:
                break
        if proc.returncode != 0:
            raise SuiteError(f"打分命令失败(退出码 {proc.returncode}):{err.decode('utf-8', 'replace')[-2000:]}")
        result = json.loads(out.decode("utf-8"))
        result.update(batch=batch, fingerprint=fingerprint, scored_at=time.time())
        cached.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result


def compare_batches(base: dict, cand: dict, *, default_noise: float = 0.05) -> dict:
    """两批打分结果的配对对比(只用评测约定的 per_case.score,越高越好;§9.1 I4)。
    逐样本:两边均值 ± 标准差、差值;差值超过 2 倍噪声(两边标准差取大,没有重复时用 default_noise)
    才算改善 / 变坏。可信度:两边已打分次数不一致 → 列出(样本数失衡时对比不可信)。"""
    import statistics

    def by_case(result: dict) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for r in result.get("per_case") or []:
            if r.get("status") == "scored" and isinstance(r.get("score"), (int, float)):
                out.setdefault(r["case_id"], []).append(float(r["score"]))
        return out

    def stats(xs: list[float]) -> tuple[float, float]:
        return statistics.fmean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)

    b, c = by_case(base), by_case(cand)
    rows, improved, worse, same = [], [], [], []
    for case in sorted(set(b) & set(c)):
        (bm, bs), (cm, cs) = stats(b[case]), stats(c[case])
        noise = max(bs, cs) if (len(b[case]) > 1 or len(c[case]) > 1) else default_noise
        diff = cm - bm
        verdict = "improved" if diff > 2 * noise else "worse" if diff < -2 * noise else "same"
        (improved if verdict == "improved" else worse if verdict == "worse" else same).append(case)
        rows.append({"case_id": case, "base": [round(bm, 3), round(bs, 3), len(b[case])],
                     "cand": [round(cm, 3), round(cs, 3), len(c[case])], "diff": round(diff, 3), "verdict": verdict})
    imbalance = sorted(k for k in set(b) | set(c) if len(b.get(k, [])) != len(c.get(k, [])))
    diffs = [r["diff"] for r in rows]
    return {
        "paired_cases": len(rows),
        "mean_diff": round(statistics.fmean(diffs), 4) if diffs else None,
        "median_diff": round(statistics.median(diffs), 4) if diffs else None,
        "improved": improved, "worse": worse, "same": len(same),
        "imbalanced_cases": imbalance,
        "trustworthy": not imbalance,
        "per_case": rows,
    }
