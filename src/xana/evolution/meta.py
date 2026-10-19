"""MetaEvolutionService —— 元 agent 定时唤醒(计划 §6.3)。

- 专用 session:sid 前缀 "evolution";每次 fire 用全新窗口(新 sid,
  不继承任何 worker 上下文)。
- cron:recurring,install_cron 幂等(已存在同 marker 任务 → 不重复建);
  首个 episode(fire/declare)时自动确保已安装("boot 或首个 episode 时创建")。
- 元 prompt 组装(harness 侧固定结构,不是自由发挥):
  L3 当前计划(TodoStore) + 账本近期 N 条 + 当前 Agent 文档 +
  上轮评估结果(visible 集;held-out 分数不回流,§6.2 两账纪律)。
- cron 到点 → SchedulerRunner dispatch → serve 的 dispatch 回调识别
  marker 后调 fire() → declare 新 episode(§6.2 状态机)+ 注入元 prompt。

concrete helper,不抽 ABC。
"""
from __future__ import annotations

import json
import time as _time
import uuid as _uuid
from pathlib import Path
from typing import Any, Callable

from xana.evolution.episode import (
    EPISODE_SESSION_ID,
    EpisodeRunner,
    load_eval_set,
    render_eval_summary,
)
from xana.evolution.ledger import Ledger, default_evolution_home

META_CRON_MARKER = "[xana-evolution-meta]"
META_SESSION_ID = "evolution-meta"
DEFAULT_META_BUDGET = {"max_tokens": 100_000}
DEFAULT_META_CRON = "0 */6 * * *"  # 每 6 小时唤醒一次


class MetaEvolutionService:
    def __init__(
        self,
        *,
        scheduler: Any = None,
        gateway: Any = None,
        episode_runner: EpisodeRunner,
        ledger: Ledger,
        todo_store: Any = None,
        agents_root: Path | None = None,
        agent_name: str = "chat",
        home: Path | None = None,
        recent_n: int = 10,
        clock: Callable[[], float] = _time.time,
    ) -> None:
        self._scheduler = scheduler
        self._gateway = gateway
        self._runner = episode_runner
        self._ledger = ledger
        self._todo_store = todo_store
        self._agents_root = Path(agents_root) if agents_root is not None else None
        self._agent_name = agent_name
        self._home = Path(home) if home is not None else default_evolution_home()
        self._recent_n = recent_n
        self._clock = clock

    # ------------------------------------------------------------------
    # cron 安装(幂等)
    # ------------------------------------------------------------------

    def owns(self, text: str) -> bool:
        """dispatch 回调识别:这条 scheduler 任务是否归本服务消费。"""
        return str(text or "").strip().startswith(META_CRON_MARKER)

    async def install_cron(self, *, cron: str = DEFAULT_META_CRON) -> str | None:
        """注册 recurring 元 agent cron;已安装(marker 任务存在)→ 返回 None。
        FileSchedulerService.session_id 是公有可变属性(cron 工具同款覆写手法)。"""
        if self._scheduler is None:
            return None
        for job in await self._scheduler.list_all():
            if self.owns(str(job.get("prompt") or "")):
                return None  # 幂等:不重复创建
        old_sid = self._scheduler.session_id
        self._scheduler.session_id = META_SESSION_ID
        try:
            return await self._scheduler.add(
                cron=cron, prompt=META_CRON_MARKER, recurring=True,
            )
        finally:
            self._scheduler.session_id = old_sid

    # ------------------------------------------------------------------
    # 元 prompt 组装
    # ------------------------------------------------------------------

    async def assemble_prompt(self) -> str:
        """L3 计划 + 账本近期 N 条 + 当前 Agent 文档 + 上轮评估结果(visible)。"""
        sections: list[str] = [META_CRON_MARKER, "", "## 当前计划(L3)"]
        sections.append(self._render_todos())
        sections.append("\n## 账本近期记录")
        entries = await self._ledger.recent(self._recent_n)
        if entries:
            for e in entries:
                sections.append(
                    f"- {e.episode_id}: verdict={e.verdict} "
                    f"delta={e.delta_vs_base} closure_tokens={e.closure_tokens_used}"
                )
        else:
            sections.append("(账本为空)")
        sections.append("\n## 当前 Agent 文档")
        sections.append(self._render_agent_doc())
        sections.append("\n## 上轮评估结果(可见集)")
        sections.append(self._render_last_eval())
        sections.append(
            "\n按 episode 闭环:提出假设 → 冻结 → 评估 → 判决;"
            "不要直接改 Agent 文档(被 policy 拦截)。"
        )
        return "\n".join(sections)

    def _render_todos(self) -> str:
        if self._todo_store is None:
            return "(无 TodoStore)"
        try:
            todos = self._todo_store.load(META_SESSION_ID)
        except Exception:
            return "(计划读取失败)"
        if not todos:
            return "(无进行中的计划)"
        lines = []
        for idx, item in enumerate(todos, start=1):
            mark = {"in_progress": "⏳", "completed": "✅"}.get(
                getattr(item, "status", ""), "☐"
            )
            content = getattr(item, "content", "") or ""
            active = getattr(item, "active_form", "") or ""
            label = f"{active} ({content})" if active and active != content else content
            lines.append(f"{idx}. {mark} {label}")
        return "\n".join(lines)

    def _render_agent_doc(self) -> str:
        if self._agents_root is not None:
            path = self._agents_root / self._agent_name / "AGENT.md"
            if path.exists():
                return f"({self._agent_name})```markdown\n" + path.read_text(
                    encoding="utf-8") + "\n```"
        gateway = self._gateway
        if gateway is not None:
            agent = getattr(gateway, "agents", {}).get(self._agent_name)
            if agent is not None:
                prompt = agent.spec().system_prompt or "(xana 默认系统提示词)"
                return f"({self._agent_name})\n{prompt}"
        return f"({self._agent_name},无文档)"

    def _render_last_eval(self) -> str:
        """visible 集分数;held-out 分数不写入元 agent 可见的注入内容(§6.2)。"""
        path = self._home / "last_eval.json"
        if not path.exists():
            return "(无)"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return "(无)"
        scores = {str(k): float(v) for k, v in (raw.get("scores") or {}).items()}
        eval_set = load_eval_set(self._home)
        return render_eval_summary(eval_set, scores, include_held_out=False)

    # ------------------------------------------------------------------
    # fire:cron 到点 → 产出 episode + 全新窗口注入
    # ------------------------------------------------------------------

    async def fire(self) -> tuple[str, str]:
        """到点唤醒:装 cron(幂等,首个 episode 时创建)→ declare 新 episode
        → 元 prompt 注入全新 session(不继承 worker 上下文)。
        返回 (episode_id, session_id)。"""
        await self.install_cron()

        episode_id = f"ep-{_uuid.uuid4().hex[:8]}"
        session_id = f"evolution-{_uuid.uuid4().hex[:8]}"  # 全新窗口
        doc_path = None
        if self._agents_root is not None:
            candidate = self._agents_root / self._agent_name / "AGENT.md"
            if candidate.exists():
                doc_path = candidate
        await self._runner.declare(
            episode_id=episode_id,
            goal=f"迭代 agent {self._agent_name}",
            budget=dict(DEFAULT_META_BUDGET),
            doc_path=doc_path,
            session_id=EPISODE_SESSION_ID,
        )
        await self._runner.start()

        gateway = self._gateway
        if gateway is not None and getattr(gateway, "adapters", None):
            from xana.agent_adapter.contracts import InboundMessage

            prompt = await self.assemble_prompt()
            adapter_id = next(iter(gateway.adapters))
            inbound = InboundMessage(
                adapter_id=adapter_id,
                session_id=session_id,
                text=prompt,
                metadata={
                    "target_agent": self._agent_name,
                    "source": "evolution_meta",
                    "episode_id": episode_id,
                },
            )
            import asyncio

            async def _drain():
                try:
                    async for _ in gateway.submit(inbound):
                        pass
                except Exception:
                    pass

            asyncio.create_task(_drain())
        return episode_id, session_id
