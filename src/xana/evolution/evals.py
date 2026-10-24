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
                 quota_wait: Callable[[], Awaitable[tuple[str, float] | None]] | None = None,
                 agent_fingerprint: Callable[[str], str | None] | None = None) -> None:
        """quota_wait:模型服务额度检查(如 Kimi 订阅额度),用到阈值时返回 (原因, 距重置秒数);每个样本开跑前调用。
        额度 / 当日预算用完时暂停开新样本,等重置后接着跑——不跳过样本(评测结果必须完整)。"""
        self._runner = runner
        self._runs = run_store
        self.root = Path(evals_root)
        self._quota_wait = quota_wait
        # 被测 agent 的版本指纹(提示词 + 经验条目 + 工具 + 模型):同一版本已跑过的样本可复用,不重跑
        self._agent_fp = agent_fingerprint or (lambda _name: None)

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

    def _run_key(self, suite: EvalSuite, agent: str, tape: tuple[str, ...]) -> dict | None:
        """可复用的条件:同一 agent 版本、同一套件指纹、同一环境策略、没有用回放。"""
        fp = self._agent_fp(agent)
        if fp is None or tape:
            return None
        return {"agent_fp": fp, "fingerprint": suite.fingerprint(),
                "env": {"strategies": dict(suite.strategies), "on_miss": suite.on_miss}}

    def reusable_rows(self, suite: EvalSuite, agent: str) -> dict[str, list[dict]]:
        """当前版本已完成的运行,按样本归组(跨批次;复用来的行不重复计)。"""
        key = self._run_key(suite, agent, ())
        out: dict[str, list[dict]] = {}
        if key is None:
            return out
        for bdir in sorted((suite.dir / "batches").glob("*")):
            meta_path, runs_path = bdir / "batch.json", bdir / "runs.jsonl"
            if not meta_path.exists() or not runs_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if any(meta.get(k) != v for k, v in key.items()):
                continue
            for line in runs_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("status") == "completed" and not row.get("reused_from"):
                    out.setdefault(row["case_id"], []).append({**row, "_batch": bdir.name})
        return out

    async def run(self, suite: EvalSuite, cases: tuple[EvalCase, ...], *, repeat: int = 1,
                  agent: str | None = None, label: str = "", tape: tuple[str, ...] = (),
                  on_case: Callable[[str], None] | None = None, reuse: bool = True) -> str:
        """on_case:每个样本跑完回调一句进度(后台任务显示用)。
        reuse:同一 agent 版本在同一套件 / 环境下已完成的运行直接复用(记 reused_from),只补跑不够的次数。"""
        agent = agent or suite.agent
        batch = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:4]}" + (f"-{label}" if label else "")
        out_dir = suite.dir / "batches" / batch
        out_dir.mkdir(parents=True)
        key = self._run_key(suite, agent, tape) or {}
        prior = self.reusable_rows(suite, agent) if (reuse and key) else {}
        (out_dir / "batch.json").write_text(json.dumps({
            "suite": suite.name, "agent": agent, "label": label, "repeat": repeat,
            "cases": [c.id for c in cases], "started": time.time(), "tape": list(tape),
            "fingerprint": suite.fingerprint(), **key}, ensure_ascii=False),
            encoding="utf-8")
        reused_rows = []
        todo: list[tuple[EvalCase, int]] = []
        for c in cases:
            have = prior.get(c.id, [])[:repeat]
            for rep, row in enumerate(have):
                reused_rows.append({**{k: v for k, v in row.items() if k != "_batch"}, "rep": rep,
                                    "reused_from": row["_batch"]})
            todo += [(c, rep) for rep in range(len(have), repeat)]
        with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
            for row in reused_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if reused_rows and on_case is not None:
            on_case(f"复用同一版本已有的 {len(reused_rows)} 次运行,补跑 {len(todo)} 次")
        slots = asyncio.Semaphore(max(1, suite.concurrency))
        lock = asyncio.Lock()
        done, total = [0], len(todo)

        async def one(case: EvalCase, rep: int) -> None:
            async with slots:
                await self._gate(suite, on_case)
                row = await self._run_case(suite, case, rep, agent, batch, tape)
            async with lock:
                with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
                done[0] += 1
                if on_case is not None:
                    on_case(f"{done[0]}/{total} · {case.id}#{rep} {row.status}")

        await asyncio.gather(*(one(c, r) for c, r in todo))
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
        run_ids = {(r["case_id"], r["rep"]): r.get("run_id") for r in runs}
        for row in result.get("per_case") or []:          # 打分命令不必回传 run_id:按 (样本, 第几次) 补上
            row.setdefault("run_id", run_ids.get((row.get("case_id"), row.get("rep"))))
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


def _scored(result: dict) -> list[dict]:
    return [r for r in result.get("per_case") or [] if r.get("status") == "scored"
            and isinstance(r.get("score"), (int, float))]


async def plan_for_version(evals: "EvalRunner", suite: EvalSuite, agent: str) -> dict:
    """当前版本(按指纹)跨批次的逐样本表现与噪声,建议小规模实验的样本(代码算,不让模型算)。
    noise = 有 ≥2 次结果的样本的标准差中位数;gate = max(2·noise, 0.05) 为差异门槛。
    目标候选:训练集平均分 < 0.7 或出现过大偏差,或波动明显高于噪声;保持候选:其余训练样本中平均分 ≥ 0.8
    且波动不超过门槛的。真值未揭晓的样本单列(pending_truth),不算缺重复次数。"""
    import statistics

    rows = evals.reusable_rows(suite, agent)
    by_run: dict[str, dict] = {}
    for batch in sorted({r["_batch"] for rs in rows.values() for r in rs}):
        for r in _scored(await evals.score(suite, batch)):
            by_run[r.get("run_id") or ""] = r
    split = {c.id: c.split for c in suite.cases}
    stats: dict[str, dict] = {}
    for case, rs in rows.items():
        scored = [by_run[r["run_id"]] for r in rs if r.get("run_id") in by_run]
        scores = [r["score"] for r in scored]
        if not scores:
            continue
        stats[case] = {"split": split.get(case, "?"), "n": len(scores), "mean": round(statistics.fmean(scores), 3),
                       "sd": round(statistics.stdev(scores), 3) if len(scores) > 1 else None,
                       "big_miss": any(r.get("big_miss") for r in scored)}
    sds = [v["sd"] for v in stats.values() if v["sd"] is not None]
    noise = round(statistics.median(sds), 3) if sds else None
    gate = round(max(2 * (noise or 0.025), 0.05), 3)
    train = {k: v for k, v in stats.items() if v["split"] == "train"}
    targets = sorted(k for k, v in train.items()
                     if v["mean"] < 0.7 or v["big_miss"] or (v["sd"] is not None and noise and v["sd"] > 2 * noise))
    keep = sorted((k for k, v in train.items() if k not in targets and v["mean"] >= 0.8
                   and (v["sd"] is None or v["sd"] <= gate)), key=lambda k: -train[k]["mean"])[:4]
    pending = sorted(k for k in rows if k not in stats)          # 有完成的运行但还打不了分(真值未揭晓)
    return {
        "agent": agent, "agent_fp": evals._agent_fp(agent), "noise": noise, "gate": gate,
        "suggested_target_cases": targets, "suggested_keep_cases": keep,
        "needs_more_reps": sorted(c.id for c in suite.cases if c.split == "train" and c.id not in pending
                                  and stats.get(c.id, {}).get("n", 0) < 2),
        "pending_truth": pending,
        "per_case": stats,
    }


def verdict(base: list[dict], cand: list[dict], *, target: list[str], keep: list[str], min_gain: float,
            heldout: set[str], gate: float) -> dict:
    """按预注册判据判定(代码判,不让模型判):
    - 作废:目标 / 保持 / 留出集样本两边打分次数不一致;
    - 不通过:保持样本有超出噪声的变坏;目标样本平均改善 < max(min_gain, gate) 或有目标样本变坏;
      留出集平均变化 < −gate;
    - 提醒:留出集平均改善 > 3·gate(好得反常,先查泄漏 / 评测漏洞)。"""
    comp = compare_batches({"per_case": base}, {"per_case": cand}, default_noise=gate / 2)
    rows = {r["case_id"]: r for r in comp["per_case"]}
    relevant = set(target) | set(keep) | heldout
    reasons, warnings = [], []
    imbalanced = sorted(set(comp["imbalanced_cases"]) & relevant)
    missing = sorted(c for c in set(target) | set(keep) if c not in rows)
    if imbalanced or missing:
        return {"verdict": "invalid", "reasons": [f"两边打分次数不一致:{imbalanced}" if imbalanced else "",
                                                  f"缺少结果:{missing}" if missing else ""], "warnings": []}
    t_diffs = [rows[c]["diff"] for c in target]
    t_worse = [c for c in target if rows[c]["verdict"] == "worse"]
    t_mean = sum(t_diffs) / len(t_diffs) if t_diffs else 0.0
    if t_mean < max(min_gain, gate):
        reasons.append(f"目标样本平均改善 {t_mean:+.3f},未达 {max(min_gain, gate):.3f}")
    if t_worse:
        reasons.append(f"目标样本变坏:{t_worse}")
    k_worse = [c for c in keep if rows[c]["verdict"] == "worse"]
    if k_worse:
        reasons.append(f"必须保持的样本变坏(一票否决):{k_worse}")
    h_rows = [rows[c] for c in heldout if c in rows]
    h_mean = sum(r["diff"] for r in h_rows) / len(h_rows) if h_rows else None
    if h_mean is None:
        reasons.append("没有留出集结果:采纳需要留出集")
    elif h_mean < -gate:
        reasons.append(f"留出集平均变化 {h_mean:+.3f},差于 −{gate:.3f}")
    elif h_mean > 3 * gate:
        warnings.append(f"留出集平均改善 {h_mean:+.3f} 超过 3 倍门槛:好得反常,先查泄漏与评测漏洞")
    return {"verdict": "fail" if reasons else "pass", "reasons": reasons, "warnings": warnings,
            "target": {"mean_diff": round(t_mean, 4), "worse": t_worse,
                       "per_case": {c: rows[c]["diff"] for c in target}},
            "keep": {"worse": k_worse},
            "heldout": {"cases": len(h_rows), "mean_diff": round(h_mean, 4) if h_mean is not None else None,
                        "improved": sum(r["verdict"] == "improved" for r in h_rows),
                        "worse": sum(r["verdict"] == "worse" for r in h_rows)}}
