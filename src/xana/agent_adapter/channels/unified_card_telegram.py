"""Telegram renderer: UnifiedCardState -> (markdown_v2_text, inline_keyboard).

输出 **MarkdownV2** (TG parse_mode="MarkdownV2"),用 telegramify-markdown 处理
模型输出的 markdown(headers/lists/code/bold/italic 等正确转 V2 + escape)。
容器标记(fold/title/footer)自己手写,见下。

Two rendering modes (state.expand_mode):
  - "default": thinking + tools(visible) + answer + footer, 每段按预算截断;
    截过的段出"展开 X"按钮切到 expand_mode=X。
  - "thinking" / "tools" / "answer": 只渲染该段(分页) + "上一页 / N/M / 下一页 / 返回"。

Budget allocation (TG 4096 cap):
  - default total ~3600: thinking 500, tools (3 × 200), answer 2400
  - expand page budget 3500
"""
from __future__ import annotations

import re

import telegramify_markdown as _tg_md

from xana.agent_adapter.channels.process_view import build_view, policy_for
from xana.agent_adapter.channels.unified_card import (
    progress_line,
    AnswerSection,
    ButtonRow,
    CompactSection,
    UnifiedCardState,
)


# Budgets — TG hard cap is 4096 chars.
_BUDGET_DEFAULT_THINKING = 500
_BUDGET_DEFAULT_TOOL_BODY = 200
_BUDGET_DEFAULT_VISIBLE_TOOLS = 3
_BUDGET_DEFAULT_ANSWER = 2400
_BUDGET_EXPAND_PAGE = 3500
_BUDGET_EXPAND_TOOL_BODY = 150  # 10 tools/页 × 200(V2 esc 后) ≈ 2000,安全
_TG_CALLBACK_DATA_LIMIT = 64


# MarkdownV2 reserved chars(非 syntax 位置需 `\` escape)。
_MDV2_RESERVED = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(s: str) -> str:
    """MarkdownV2 escape:静态文本(标题、标签、footer 等)用,不是 markdown 字段。"""
    return _MDV2_RESERVED.sub(r"\\\1", s or "")


def _md(text: str) -> str:
    """模型输出的 markdown(thinking/tool body/answer 等)→ MarkdownV2。
    telegramify-markdown 处理 escape + V2 语法转换。"""
    if not text:
        return ""
    try:
        return _tg_md.markdownify(text)
    except Exception:
        # 兜底:退回到纯 escape
        return _esc(text)


def _progress_bar(cells: int) -> str:
    cells = max(0, min(10, cells))
    return "▰" * cells + "▱" * (10 - cells)


def _fold(title: str, body: str, *, md: bool = True) -> str:
    """MarkdownV2 expandable blockquote。
    md=True:body 走 markdown 解析(thinking/answer);
    md=False:body 只 escape,不解析(tool body — 文件内容里 `*`/`>`/`#` 不该被
    当 markdown)。
    """
    body_text = _md(body or " ") if md else _esc(body or " ")
    title_md = _esc(title)
    lines = body_text.split("\n")
    # 标题不再 bold:移动端 bold 占视觉重量但宽度差不多,
    # 视觉上不 bold 显得更"小",减少标题"看上去 2 行"的压迫感。
    if not lines:
        return f"**>{title_md}||"
    out = [f"**>{title_md}"]
    for ln in lines[:-1]:
        out.append(f">{ln}")
    out.append(f">{lines[-1]}||")
    return "\n".join(out)


def _trunc(s: str, cap: int, suffix: str = "\n…(已折叠)") -> tuple[str, bool]:
    if len(s) <= cap:
        return s, False
    return s[:cap].rstrip() + suffix, True


def _trunc_rendered(text: str, cap: int, *, md: bool = True,
                    suffix: str = "\n…(已折叠)") -> tuple[str, bool]:
    """按 markdown 渲染后长度算预算 — V2 escape 会膨胀 1.2-1.5x,纯 raw cap
    会让展开按钮在实际超长时不出。md=False 走纯 escape。"""
    rendered = _md(text) if md else _esc(text)
    if len(rendered) <= cap:
        return text, False
    ratio = cap / max(1, len(rendered))
    raw_cap = max(50, int(len(text) * ratio) - 30)
    truncated = text[:raw_cap].rstrip()
    # 截断点在 ``` 内 → 补一个闭合,避免 V2 parser "Can't find end of Pre".
    if truncated.count("```") % 2 == 1:
        truncated += "\n```"
    # 未闭合的单反引号(排除 fenced 影响)
    if truncated.replace("```", "").count("`") % 2 == 1:
        truncated += "`"
    return truncated + suffix, True


def _render_compact(sec: CompactSection) -> str:
    """一行进度条+状态;下方折叠"已压缩 N 条消息"。"""
    if sec.state == "completed":
        status = "完成压缩"
    elif sec.state == "cancelled":
        status = "压缩已取消"
    else:
        status = "压缩中"
    parts: list[str] = []
    if sec.state == "cancelled":
        parts.append(f"*{_esc(status)}*")
    else:
        # 进度条 + 状态同一行(MDV2: bar 已是块字符不需 escape;status 加粗)
        parts.append(f"{_progress_bar(sec.progress_cells)} *{_esc(status)}*")
    if sec.state == "completed":
        panel_title = f"已压缩 {sec.compacted_n} 条消息"
    elif sec.state == "cancelled":
        panel_title = "压缩已取消"
    else:
        panel_title = "压缩中"
    parts.append(_fold(panel_title, sec.summary_text or " ", md=False))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Default mode
# ---------------------------------------------------------------------------


def _expand_btn(label: str, card_id: str, section: str) -> dict:
    return {
        "text": label,
        "callback_data": f"__expand__:{card_id}:{section}"[
            :_TG_CALLBACK_DATA_LIMIT],
    }


_BUDGET_GROUP_BODY = 300


def _render_process(sec: AnswerSection, mode: str) -> list[str]:
    """grouped / summary:过程组折叠块;失败的调用单独一行(✗ 前缀已在行内),不折叠。"""
    alive = sec.state in ("streaming", "detached")
    view = build_view(sec, mode, alive=alive)
    parts: list[str] = []
    for b in view.blocks:
        body, _ = _trunc_rendered(b.body or " ", _BUDGET_GROUP_BODY, md=False)
        parts.append(_fold(b.title, body, md=False))
    if view.line and alive:
        parts.append(f"_{_esc('› ' + view.line)}_")
    for line in view.failed:
        parts.append(_esc(line))
    return parts


def _render_default_answer(
    sec: AnswerSection, *, extra_kb: list[list[dict]], card_id: str, view_mode: str = "detailed",
) -> str:
    parts: list[str] = []

    think_title = "❋ 思考过程" if sec.thinking_text else "❋ 思考中"
    think_body, think_truncated = _trunc_rendered(
        sec.thinking_text or " ", _BUDGET_DEFAULT_THINKING, md=False)
    parts.append(_fold(think_title, think_body, md=False))

    policy = policy_for(view_mode)
    if policy.unit != "call":
        parts.extend(_render_process(sec, view_mode))
    visible_n = min(_BUDGET_DEFAULT_VISIBLE_TOOLS, len(sec.tools)) if policy.unit == "call" else 0
    tools_truncated = False
    for t in sec.tools[:visible_n]:
        body, truncated = _trunc_rendered(
            t.body_md or " ", _BUDGET_DEFAULT_TOOL_BODY, md=False)
        if truncated:
            tools_truncated = True
        # 工具 body 不走 markdown(文件内容含 `*`/`>`/`#` 会被误解析)
        parts.append(_fold(t.header, body, md=False))
    hidden_tools = ((len(sec.tools) - visible_n) + sec.tools_pending) if policy.unit == "call" else 0
    if hidden_tools > 0:
        tools_truncated = True
        # inline 文字计数(不是按钮,点不动);用户走底部"展开工具"
        # expand_mode 按钮查看。
        parts.append(_esc(f"其他 ✎×{hidden_tools},点下方展开工具查看"))

    # 进度行(§5.5a):进行中显示最近一条进度说明,斜体一行
    line = progress_line(sec) if policy.progress_line else ""
    if line:
        parts.append(f"_{_esc(line)}_")

    answer_body, answer_truncated = _trunc_rendered(
        sec.answer_text or " ", _BUDGET_DEFAULT_ANSWER, md=True)
    # 回答不再 fold,也不再带"回答"标题(避免冗余)。
    # 用一行横线作为视觉分割:把 thinking/tools 区隔开。
    # cancelled 时单独标记一行,提示用户。
    if sec.state == "cancelled":
        parts.append(f"\n━━━━━━━ ✦ ━━━━━━━\n\n_{_esc('回答已取消')}_")
    else:
        parts.append(f"\n━━━━━━━ ✦ ━━━━━━━\n\n{_md(answer_body)}")

    row: list[dict] = []
    if think_truncated:
        row.append(_expand_btn("展开思考", card_id, "thinking"))
    if tools_truncated:
        row.append(_expand_btn("展开工具", card_id, "tools"))
    if answer_truncated:
        row.append(_expand_btn("展开回答", card_id, "answer"))
    if row:
        extra_kb.append(row)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Expand mode
# ---------------------------------------------------------------------------


def _pagination_kb(card_id: str, page: int, total: int) -> list[dict]:
    row: list[dict] = []
    if page > 0:
        row.append({
            "text": "上一页",
            "callback_data": f"__expand_page__:{card_id}:{page - 1}"[
                :_TG_CALLBACK_DATA_LIMIT],
        })
    row.append({
        "text": f"{page + 1}/{total}",
        "callback_data": "__noop__",
    })
    if page < total - 1:
        row.append({
            "text": "下一页",
            "callback_data": f"__expand_page__:{card_id}:{page + 1}"[
                :_TG_CALLBACK_DATA_LIMIT],
        })
    row.append({
        "text": "返回",
        "callback_data": f"__expand_back__:{card_id}"[
            :_TG_CALLBACK_DATA_LIMIT],
    })
    return row


def _paginate_text(text: str, page: int, page_size: int) -> tuple[str, int, int]:
    if not text:
        text = " "
    total = max(1, (len(text) + page_size - 1) // page_size)
    page = max(0, min(page, total - 1))
    start = page * page_size
    chunk = text[start:start + page_size]
    # 补全未闭合的 ``` 和 `,避免 V2 parser 找不到 Pre/Code 结尾。
    fenced = chunk.count("```")
    if fenced % 2 == 1:
        chunk += "\n```"
        # 重算单反引号(去掉 fenced 影响)
    remaining = chunk.replace("```", "")
    if remaining.count("`") % 2 == 1:
        chunk += "`"
    return chunk, page, total


def _render_expand_thinking(
    sec: AnswerSection, *, extra_kb: list[list[dict]], card_id: str, page: int,
) -> str:
    chunk, page, total = _paginate_text(
        sec.thinking_text or "(无思考内容)", page, _BUDGET_EXPAND_PAGE)
    extra_kb.append(_pagination_kb(card_id, page, total))
    title = f"❋ 思考过程 ({page + 1}/{total})" if total > 1 else "❋ 思考过程"
    return _fold(title, chunk, md=False)


def _render_expand_answer(
    sec: AnswerSection, *, extra_kb: list[list[dict]], card_id: str, page: int,
) -> str:
    chunk, page, total = _paginate_text(
        sec.answer_text or " ", page, _BUDGET_EXPAND_PAGE)
    extra_kb.append(_pagination_kb(card_id, page, total))
    # 同 default 一样不折叠,不带"回答"标题,用横线分割。
    # expand 全屏页:翻页号放在按钮上,不在 body 里。
    return f"\n━━━━━━━ ✦ ━━━━━━━\n\n{_md(chunk)}"


_TOOLS_PER_PAGE = 10


def _render_expand_tools(
    sec: AnswerSection, *, extra_kb: list[list[dict]], card_id: str, page: int,
) -> str:
    """固定每页 _TOOLS_PER_PAGE 个,避免按 size 切出现"第一页 10 个、第二页 5 个"
    的不均匀现象。每个 tool body 截到 _BUDGET_EXPAND_TOOL_BODY 上限保证总长 OK。
    """
    all_tools = list(sec.tools) + list(sec._pending_tools_buf)
    total = max(1, (len(all_tools) + _TOOLS_PER_PAGE - 1) // _TOOLS_PER_PAGE)
    page = max(0, min(page, total - 1))
    extra_kb.append(_pagination_kb(card_id, page, total))
    title = f"*工具调用 \\({page + 1}/{total}\\)*" if total > 1 else "*工具调用*"
    chunk = all_tools[page * _TOOLS_PER_PAGE:(page + 1) * _TOOLS_PER_PAGE]
    if not chunk:
        return title + "\n" + _esc("(无工具调用)")
    panels = []
    for t in chunk:
        body, _ = _trunc(t.body_md or " ", _BUDGET_EXPAND_TOOL_BODY)
        panels.append(_fold(t.header, body, md=False))
    return title + "\n" + "\n".join(panels)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


def _kb_row(row: ButtonRow, *, ph_run_id: str | None) -> list[dict]:
    out: list[dict] = []
    for b in row.buttons:
        item: dict = {"text": b.text}
        if b.send:
            item["callback_data"] = b.send[:_TG_CALLBACK_DATA_LIMIT]
        elif b.action == "load_tools":
            item["callback_data"] = f"__load_tools__:{b.action_arg or ''}"[
                :_TG_CALLBACK_DATA_LIMIT]
        elif b.action == "detach":
            arg = b.action_arg or ph_run_id or ""
            item["callback_data"] = f"__detach__:{arg}"[
                :_TG_CALLBACK_DATA_LIMIT]
        else:
            item["callback_data"] = f"__detach__:{ph_run_id or ''}"[
                :_TG_CALLBACK_DATA_LIMIT]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------


def render(state: UnifiedCardState) -> tuple[str, list[list[dict]]]:
    """Render UnifiedCardState → (markdown_v2_text, inline_keyboard rows)."""
    parts: list[str] = []
    extra_kb: list[list[dict]] = []

    if state.expand_mode != "default" and state.answer is not None:
        if state.expand_mode == "thinking":
            parts.append(_render_expand_thinking(
                state.answer, extra_kb=extra_kb,
                card_id=state.card_id, page=state.expand_page))
        elif state.expand_mode == "answer":
            parts.append(_render_expand_answer(
                state.answer, extra_kb=extra_kb,
                card_id=state.card_id, page=state.expand_page))
        elif state.expand_mode == "tools":
            parts.append(_render_expand_tools(
                state.answer, extra_kb=extra_kb,
                card_id=state.card_id, page=state.expand_page))
    else:
        if state.compact is not None:
            parts.append(_render_compact(state.compact))
        if state.answer is not None:
            parts.append(_render_default_answer(
                state.answer, extra_kb=extra_kb, card_id=state.card_id, view_mode=state.view_mode))
        if state.menu is not None:
            parts.append(_md(state.menu.body_md))

    footer_text = state.footer.render()
    if footer_text:
        parts.append(f"_{_esc(footer_text)}_")
    text = "\n\n".join(parts)

    # 兜底硬截(理论上 budget 之后不应触发)
    if len(text) > 4050:
        text = text[:4000] + "\n…(已截断)"

    kb: list[list[dict]] = []
    for row in state.top_buttons:
        kb.append(_kb_row(row, ph_run_id=state.ph_run_id))
    kb.extend(extra_kb)
    if not state.linkify_commands:
        for row in state.bottom_buttons:
            kb.append(_kb_row(row, ph_run_id=state.ph_run_id))
    return text, kb
