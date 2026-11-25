"""Credential resolvers decoupling auth (API keys, OAuth rotation) from wire transports."""
from __future__ import annotations

from xana.llm_driver.auth.kimi_oauth import KimiTokenStore
from xana.llm_driver.interfaces import CredentialResolver


class StaticApiKeyResolver(CredentialResolver):
    """Static Bearer or custom-header API key resolver (non-refreshable)."""

    def __init__(
        self,
        api_key: str,
        *,
        header_name: str = "Authorization",
        prefix: str = "Bearer ",
    ) -> None:
        self._api_key = api_key
        self._header_name = header_name
        self._prefix = prefix

    @property
    def can_refresh(self) -> bool:
        return False

    async def auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {self._header_name: f"{self._prefix}{self._api_key}"}


class KimiOAuthResolver(CredentialResolver):
    """Kimi OAuth token resolver backed by KimiTokenStore with cross-process lock."""

    def __init__(self, store: KimiTokenStore) -> None:
        self._store = store

    @property
    def store(self) -> KimiTokenStore:
        return self._store

    @property
    def can_refresh(self) -> bool:
        return True

    async def auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = await self._store.access_token(force_refresh=force_refresh)
        return {"Authorization": f"Bearer {token}"}
