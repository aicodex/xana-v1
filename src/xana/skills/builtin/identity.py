"""identity: CC-style always-on agent-loop prompt.

Emits a stack of prompt blocks the harness concatenates into the model's
system message. Structure mirrors how Claude Code organizes its prompt:

  1. Identity  — who the agent is (configurable via constructor)
  2. Tone & style — concise, file:line refs, no emojis, no trailing summaries
  3. Tool use  — prefer dedicated tools, call independent tools in parallel
  4. Environment — cwd, platform, date, git repo flag (dynamic per activation)
  5. Output format — end-of-turn summary length, no narration

The environment block reads ``activation.cwd`` (already on the existing
SkillActivation contract); when None it falls back to "(unknown)". The
git probe is a cheap ``.git`` existence check — no subprocess.
"""
from __future__ import annotations

import datetime
import platform
from pathlib import Path

from xana.component.contracts import ComponentSource, ComponentSpec
from xana.skills.contracts import (
    SkillActivation,
    SkillContribution,
    SkillManifest,
    SkillPackage,
    SkillSpec,
)
from xana.skills.interfaces import Skill


_DEFAULT_IDENTITY = (
    "You are xana, a clean refactor of the yuxu agent runtime. "
    "You assist with software engineering tasks: read code, search, run shell "
    "commands, and edit files using the available tools. Match response length "
    "to the task — be concise for simple questions, thorough only when needed."
)

_TONE_AND_STYLE = (
    "# Tone and style\n"
    "- Concise, direct. No emojis unless the user asks.\n"
    "- When referencing code, use `file_path:line_number` so the user can navigate.\n"
    "- Don't summarize what you just did at the end of every response; the user can read the diff.\n"
    "- Don't write comments explaining WHAT code does — names already do that. Only add WHY comments when behavior would surprise a reader."
)

_TOOL_USE = (
    "# Using tools\n"
    "- Prefer dedicated tools over Bash when one fits (Read, Edit, Glob, Grep).\n"
    "- Call independent tools in the same response — make them run in parallel.\n"
    "- Read before Edit. Verify file contents before destructive operations.\n"
    "- Don't add error handling, fallbacks, or validation for scenarios that can't happen. Trust internal code and framework guarantees."
)

_OUTPUT_FORMAT = (
    "# Output format\n"
    "- End-of-turn summary: one or two sentences. What changed and what's next.\n"
    "- State results and decisions directly. Don't narrate internal deliberation.\n"
    "- For exploratory questions, respond in 2-3 sentences with a recommendation and the main tradeoff — present it as redirectable, not decided."
)


class IdentitySkill(Skill):
    def __init__(self, identity_text: str = _DEFAULT_IDENTITY) -> None:
        self.identity_text = identity_text
        self._spec = SkillSpec(
            component=ComponentSpec(
                kind="skill",
                name="identity",
                qualified_name="builtin.identity",
                description="Always-on CC-style agent-loop prompt (identity + tone + tool use + environment + output format).",
                source=ComponentSource(source_type="builtin", scope="system"),
            ),
            when_to_use="every turn",
            package=SkillPackage(
                root=None,
                manifest=SkillManifest(name="identity", description="agent loop prompt"),
                instructions_path=None,
            ),
        )

    def spec(self) -> SkillSpec:
        return self._spec

    async def activate(self, activation: SkillActivation) -> SkillContribution:
        return SkillContribution(
            prompt_blocks=(
                self.identity_text,
                _TONE_AND_STYLE,
                _TOOL_USE,
                _build_environment_block(activation.cwd),
                _OUTPUT_FORMAT,
            )
        )

    async def deactivate(self, session_id: str) -> None:
        return None


def _build_environment_block(cwd: Path | None) -> str:
    cwd_str = str(cwd) if cwd is not None else "(unknown)"
    is_git = cwd is not None and (cwd / ".git").exists()
    today = datetime.date.today().isoformat()
    return (
        "# Environment\n"
        f"- Working directory: {cwd_str}\n"
        f"- Platform: {platform.system().lower() or 'unknown'}\n"
        f"- Today's date: {today}\n"
        f"- Is a git repository: {'true' if is_git else 'false'}"
    )
