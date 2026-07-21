"""Minimal asynchronous Riot API client for the Stage 0 experiment."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from nexus_lens.config import Settings
from nexus_lens.schemas import RiotAccount, RiotMatch


class RiotApiError(RuntimeError):
    """Raised when Riot returns a non-success response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Riot API returned HTTP {status_code}: {detail}")


class RiotClient:
    """Client for only the Account-V1 and Match-V5 calls needed in Stage 0."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._routing_region = settings.routing_region
        self._client = http_client or httpx.AsyncClient(
            headers={"X-Riot-Token": settings.riot_api_key},
            timeout=httpx.Timeout(20.0),
        )
        self._owns_client = http_client is None

    @property
    def base_url(self) -> str:
        return f"https://{self._routing_region}.api.riotgames.com"

    async def __aenter__(self) -> "RiotClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_account_by_riot_id(
        self, game_name: str, tag_line: str
    ) -> RiotAccount:
        encoded_name = quote(game_name, safe="")
        encoded_tag = quote(tag_line, safe="")
        payload = await self._get(
            f"/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}"
        )
        return RiotAccount.model_validate(payload)

    async def get_match_ids_by_puuid(
        self, puuid: str, *, start: int = 0, count: int = 5
    ) -> list[str]:
        payload = await self._get(
            f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids",
            params={"start": start, "count": count},
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, str) for item in payload
        ):
            raise TypeError("Riot match ID response was not a list of strings")
        return payload

    async def get_match(self, match_id: str) -> RiotMatch:
        payload = await self._get(
            f"/lol/match/v5/matches/{quote(match_id, safe='')}"
        )
        return RiotMatch.model_validate(payload)

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> object:
        response = await self._client.get(
            f"{self.base_url}{path}",
            params=params,
        )
        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RiotApiError(response.status_code, str(detail))
        return response.json()
