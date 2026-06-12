"""src/xana/agent_adapter/channels/_draft.py

每轮(run)一份 TurnDraft:平台无关的渲染状态。adapter 在事件流上累积它,
平台渲染器(feishu_card / telegram_html)把它画成原生形态。
移植自 yuxu_backup services/gateway/draft.py 的 DraftMessage,工具标题
改用 xanac 风格(_tool_titles.tool_header)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from xana.agent_adapter._tool_titles import tool_header

# TODO(compact): Phase 2 接 ModelConfig.context_window 后由配置驱动
CONTEXT_BUDGET_TOKENS = 967_000

_BODY_LIMIT = 3000  # 折叠块内 args/result 各自的截断上限


@dataclass
class ToolBlock:
    call_id: str
    name: str
    args: dict
    running: bool = True
    ok: bool = True
    result: str = ""
    elapsed_ms: int | None = None

    def header(self) -> str:
        return tool_header(
            self.name, self.args, ok=self.ok,
            running=self.running, elapsed_ms=self.elapsed_ms,
        )


@dataclass
class TurnDraft:
    run_id: str
    content: str = ""
    thinking: str = ""
    footer_meta: list[tuple[str, str]] = field(default_factory=list)
    buttons: list[list[dict]] = field(default_factory=list)
    tool_blocks: list[ToolBlock] = field(default_factory=list)

    def append_content(self, chunk: str) -> None:
        self.content += chunk

    def append_thinking(self, chunk: str) -> None:
        self.thinking += chunk

    def start_tool(self, *, call_id: str, name: str, args: dict) -> None:
        self.tool_blocks.append(ToolBlock(call_id=call_id, name=name, args=dict(args or {})))

    def finish_tool(self, *, call_id: str, ok: bool, result: str, elapsed_ms: int | None) -> None:
        for blk in self.tool_blocks:
            if blk.call_id == call_id and blk.running:
                blk.running = False
                blk.ok = ok
                blk.result = result
                blk.elapsed_ms = elapsed_ms
                return
        # 没有匹配的 started(容错): 落成独立完成块,事件不丢
        self.tool_blocks.append(ToolBlock(
            call_id=call_id, name="?", args={}, running=False,
            ok=ok, result=result, elapsed_ms=elapsed_ms,
        ))


def clip_body(s: str, limit: int = _BODY_LIMIT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…(truncated)"


# 移植 yuxu_backup agents/chat_agent/handler.py:322-326 的 _BUTTONS_RE
# (容忍缺右括号/全角冒号/全角竖线;锚定行尾)
_BUTTONS_RE = re.compile(
    r"\[(?:选项|options|buttons|按钮)\s*[:：]?\s*([^\]\n]+?)\]?\s*$",
    re.MULTILINE,
)


def extract_buttons(content: str) -> tuple[str, list[list[dict]]]:
    """从 LLM 回复末尾抽 `[选项: a | b | c]` 行 → (清理后正文, 按钮行)。
    一行一个按钮,最多 6 个(yuxu handler.py:328-350 语义)。"""
    if not content:
        return content, []
    m = _BUTTONS_RE.search(content)
    if not m:
        return content, []
    options = [o.strip() for o in re.split(r"[|｜]", m.group(1)) if o.strip()][:6]
    if not options:
        return content, []
    cleaned = (content[: m.start()].rstrip()
               + ("\n" + content[m.end():].lstrip() if content[m.end():].strip() else ""))
    return cleaned.strip(), [[{"text": o, "send": o}] for o in options]


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k")


# 流式期间的停止按钮:点击复用现有按钮回流(send_text → gateway 命令路由 → /cancel)
STOP_BUTTON_ROWS = [[{"text": "⏹ 停止", "send": "/cancel"}]]


def _context_pair(tin: int) -> tuple[str, str]:
    pct = round(tin * 100 / CONTEXT_BUDGET_TOKENS)
    return ("context", f"{_fmt_k(tin)}/{CONTEXT_BUDGET_TOKENS // 1000}k ({pct}%)")


def build_footer_meta(usage) -> list[tuple[str, str]]:
    """脚标: model | context used/budget (pct%)。tokens in/out 不展示(不重要)。
    usage 为 UsageSnapshot 或 None(鸭子类型,避免依赖 state contracts)。"""
    if usage is None:
        return []
    pairs: list[tuple[str, str]] = []
    if getattr(usage, "model", None):
        pairs.append(("model", str(usage.model)))
    tin = getattr(usage, "exact_input_tokens", None)
    if tin:
        pairs.append(_context_pair(tin))
    return pairs


def footer_from_progress(payload: dict, *, model: str | None = None) -> list[tuple[str, str]]:
    """usage_progress payload → 脚标对(与 build_footer_meta 同公式/同格式)。
    payload 形如 {"kind":"usage_progress","tokens_in":...,"model":...}。"""
    pairs: list[tuple[str, str]] = []
    m = model or payload.get("model")
    if m:
        pairs.append(("model", str(m)))
    tin = int(payload.get("tokens_in") or 0)
    if tin:
        pairs.append(_context_pair(tin))
    return pairs
