"""Declarative ProviderProfiles for built-in LLM endpoints.

Adding a new OpenAI-compatible or Anthropic-compatible provider requires only
declaring a ProviderProfile + CredentialResolver — zero new transport classes or
`if provider == ...` branches in the driver or harness.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from xana.llm_driver.contracts import ProviderProfile

KIMI_SUPPORTED_EFFORTS: tuple[str, ...] = ("low", "high", "max")
DEEPSEEK_SUPPORTED_EFFORTS: tuple[str, ...] = ("high", "max")

KIMI_PROFILE = ProviderProfile(
    name="kimi",
    dialect="openai_chat",
    base_url="https://api.kimi.com/coding/v1",
    max_tokens_param="max_completion_tokens",
    default_max_tokens=32768,
    supports_prompt_cache_key=True,
    supports_cache_control=False,
    supports_parallel_tools=True,
    supports_thinking=True,
    preserve_thinking_history=True,
    thinking_param_style="kimi",
    supported_efforts=KIMI_SUPPORTED_EFFORTS,
    omit_temperature=True,
    default_headers={"User-Agent": "KimiCLI/1.0"},
    timeout_s=120.0,
)

OPENAI_COMPATIBLE_PROFILE = ProviderProfile(
    name="openai_compatible",
    dialect="openai_chat",
    base_url="",
    max_tokens_param="max_tokens",
    default_max_tokens=32768,
    supports_prompt_cache_key=False,
    supports_cache_control=False,
    supports_parallel_tools=True,
    supports_thinking=False,
    preserve_thinking_history=False,
    thinking_param_style="none",
    supported_efforts=(),
    omit_temperature=False,
    timeout_s=120.0,
)

ANTHROPIC_DEEPSEEK_PROFILE = ProviderProfile(
    name="deepseek",
    dialect="anthropic_messages",
    base_url="https://api.deepseek.com/anthropic",
    max_tokens_param="max_tokens",
    default_max_tokens=16384,
    supports_prompt_cache_key=False,
    supports_cache_control=False,
    supports_parallel_tools=True,
    supports_thinking=True,
    preserve_thinking_history=True,
    thinking_param_style="anthropic",
    supported_efforts=DEEPSEEK_SUPPORTED_EFFORTS,
    enable_dsml_recovery=True,
    supports_native_web_search=True,
    anthropic_version="2023-06-01",
    timeout_s=120.0,
)

BUILTIN_PROFILES: dict[str, ProviderProfile] = {
    "kimi": KIMI_PROFILE,
    "openai_compatible": OPENAI_COMPATIBLE_PROFILE,
    "deepseek": ANTHROPIC_DEEPSEEK_PROFILE,
}


def make_profile(base: ProviderProfile, **overrides: Any) -> ProviderProfile:
    """Create a customized ProviderProfile from a built-in template."""
    return replace(base, **overrides)
