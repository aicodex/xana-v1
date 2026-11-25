"""Default ToolDisclosurePolicy: deferred tool catalog, tool_search, and native capability yielding."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xana.protocol.primitives import ComponentVersion
from xana.tools.contracts import (
    NOTIFY_USER_TOOL,
    TOOL_SEARCH_TOOL_NAME,
    DisclosureContext,
    DisclosurePlan,
    ToolSpec,
)
from xana.tools.interfaces import ToolDisclosurePolicy


def to_anthropic_tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """Pack a ToolSpec into `{name, description, input_schema}`."""
    return {
        "name": spec.component.name,
        "description": spec.component.description or "",
        "input_schema": dict(spec.input_schema),
    }


def split_tool_schemas(
    specs: Sequence[ToolSpec],
    revealed: set[str] | frozenset[str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Deferred & unrevealed tools -> `(name, description)`; all others -> full schema."""
    schemas: list[dict[str, Any]] = []
    deferred: list[tuple[str, str]] = []
    for s in specs:
        if s.defer and s.component.name not in revealed:
            deferred.append((s.component.name, s.component.description or ""))
        else:
            schemas.append(to_anthropic_tool_schema(s))
    return schemas, deferred


def _can_use(available: frozenset[str] | tuple[str, ...] | None, name: str) -> bool:
    return available is None or name in available


class DefaultToolDisclosurePolicy(ToolDisclosurePolicy):
    """Standard disclosure policy matching Claude Code / Xana deferred-tool semantics."""

    POLICY_VERSION = "1.0.0"

    def component_version(self) -> ComponentVersion:
        return ComponentVersion(
            name="default_tool_disclosure_policy",
            kind="tools_policy",
            version=self.POLICY_VERSION,
            policy_version=self.POLICY_VERSION,
        )

    def plan(
        self,
        specs: Sequence[ToolSpec],
        ctx: DisclosureContext,
        *,
        ext_specs: Sequence[ToolSpec] = (),
    ) -> DisclosurePlan:
        revealed = set(ctx.revealed_tools)
        if not _can_use(ctx.available_tools, TOOL_SEARCH_TOOL_NAME):
            revealed = {sp.component.name for sp in specs}

        schemas, deferred = split_tool_schemas(specs, revealed)

        if not ctx.unattended:
            schemas = [sc for sc in schemas if sc["name"] != NOTIFY_USER_TOOL]
            deferred = [d for d in deferred if d[0] != NOTIFY_USER_TOOL]

        if ext_specs:
            ext_names = {sp.component.name for sp in ext_specs}
            schemas = [s for s in schemas if s["name"] not in ext_names] + [
                to_anthropic_tool_schema(sp) for sp in ext_specs
            ]
            deferred = [d for d in deferred if d[0] not in ext_names]

        schemas = [sc for sc in schemas if _can_use(ctx.available_tools, sc["name"])]
        deferred = [d for d in deferred if _can_use(ctx.available_tools, d[0])]

        web_search_native = False
        yielding = {sp.component.name for sp in specs if sp.yields_to_native}
        if (
            yielding
            and ctx.search_provider in (None, "", "native")
            and ctx.provider.startswith("deepseek")
            and (any(sc["name"] in yielding for sc in schemas) or any(d[0] in yielding for d in deferred))
        ):
            schemas = [sc for sc in schemas if sc["name"] not in yielding]
            deferred = [d for d in deferred if d[0] not in yielding]
            web_search_native = True

        return DisclosurePlan(
            schemas=tuple(schemas),
            deferred=tuple(deferred),
            web_search_native=web_search_native,
        )
