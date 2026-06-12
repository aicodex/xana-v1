"""src/xana/extension_api/registry.py

ExtensionRegistryBuilder collects extensions; build() seals them into a frozen
ExtensionRegistry that the harness loop reads only. add() fans an object out
into every Contributor kind it implements (one class may implement several).
"""
from __future__ import annotations

from dataclasses import dataclass

from xana.extension_api.interfaces import (
    ApprovalReviewContributor,
    ConfigContributor,
    ContextContributor,
    ThreadLifecycleContributor,
    ToolContributor,
    ToolLifecycleContributor,
    TurnInputContributor,
    TurnLifecycleContributor,
)


@dataclass(frozen=True)
class ExtensionRegistry:
    context: tuple[ContextContributor, ...] = ()
    turn_input: tuple[TurnInputContributor, ...] = ()
    tools: tuple[ToolContributor, ...] = ()
    tool_lifecycle: tuple[ToolLifecycleContributor, ...] = ()
    turn_lifecycle: tuple[TurnLifecycleContributor, ...] = ()
    thread_lifecycle: tuple[ThreadLifecycleContributor, ...] = ()
    config: tuple[ConfigContributor, ...] = ()
    approval_review: tuple[ApprovalReviewContributor, ...] = ()


_KIND_FIELDS: tuple[tuple[type, str], ...] = (
    (ContextContributor, "context"),
    (TurnInputContributor, "turn_input"),
    (ToolContributor, "tools"),
    (ToolLifecycleContributor, "tool_lifecycle"),
    (TurnLifecycleContributor, "turn_lifecycle"),
    (ThreadLifecycleContributor, "thread_lifecycle"),
    (ConfigContributor, "config"),
    (ApprovalReviewContributor, "approval_review"),
)


class ExtensionRegistryBuilder:
    def __init__(self) -> None:
        self._buckets: dict[str, list] = {name: [] for _, name in _KIND_FIELDS}

    def add(self, extension: object) -> "ExtensionRegistryBuilder":
        matched = False
        for abc_type, name in _KIND_FIELDS:
            if isinstance(extension, abc_type):
                self._buckets[name].append(extension)
                matched = True
        if not matched:
            raise TypeError(
                f"{type(extension).__name__} implements no Contributor ABC"
            )
        return self

    def build(self) -> ExtensionRegistry:
        return ExtensionRegistry(
            **{name: tuple(items) for name, items in self._buckets.items()}
        )
