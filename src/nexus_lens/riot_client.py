"""Resilient, metrics-aware asynchronous client for required Riot APIs."""

import asyncio
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from nexus_lens.config import Settings
from nexus_lens.schemas import (
    RANKED_SOLO_QUEUE_ID,
    LeagueEntry,
    RiotAccount,
    RiotMatch,
    SummonerRecord,
)


@dataclass
class RequestMetrics:
    attempted_requests: int = 0
    successful_requests: int = 0
    retries: int = 0
    responses_429: int = 0
    responses_5xx: int = 0
    authentication_failures: int = 0
    total_backoff_seconds: float = 0.0
    proactive_backoff_seconds: float = 0.0
    reactive_retry_after_seconds: float = 0.0
    transient_backoff_seconds: float = 0.0
    requests_by_endpoint: Counter[str] = field(default_factory=Counter)
    proactive_backoff_by_endpoint: Counter[str] = field(default_factory=Counter)
    reactive_retry_after_by_endpoint: Counter[str] = field(default_factory=Counter)
    observed_rate_limit_headers: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted_requests": self.attempted_requests,
            "successful_requests": self.successful_requests,
            "retries": self.retries,
            "responses_429": self.responses_429,
            "responses_5xx": self.responses_5xx,
            "authentication_failures": self.authentication_failures,
            "total_backoff_seconds": round(self.total_backoff_seconds, 6),
            "proactive_backoff_seconds": round(
                self.proactive_backoff_seconds, 6
            ),
            "reactive_retry_after_seconds": round(
                self.reactive_retry_after_seconds, 6
            ),
            "transient_backoff_seconds": round(
                self.transient_backoff_seconds, 6
            ),
            "requests_by_endpoint": dict(sorted(self.requests_by_endpoint.items())),
            "proactive_backoff_by_endpoint": dict(
                sorted(self.proactive_backoff_by_endpoint.items())
            ),
            "reactive_retry_after_by_endpoint": dict(
                sorted(self.reactive_retry_after_by_endpoint.items())
            ),
            "observed_rate_limit_headers": sorted(
                self.observed_rate_limit_headers
            ),
        }


class RiotApiError(RuntimeError):
    """A non-sensitive terminal Riot request failure."""

    def __init__(self, status_code: int, category: str) -> None:
        self.status_code = status_code
        self.category = category
        super().__init__(f"Riot API returned HTTP {status_code} for {category}")


class RiotRetryExhausted(RuntimeError):
    """Raised after bounded retries for a transient request failure."""


class RiotRequestBudgetExceeded(RuntimeError):
    """Raised before a request would exceed an explicit safety budget."""


class RiotClient:
    """Client for Account-V1, League-V4, Summoner-V4, and Match-V5."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        max_backoff: float = 10.0,
        jitter: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_source: random.Random | None = None,
        max_requests: int | None = None,
    ) -> None:
        self._routing_region = settings.routing_region
        self._platform_region = settings.platform_region
        self._client = http_client or httpx.AsyncClient(
            headers={"X-Riot-Token": settings.riot_api_key},
            timeout=httpx.Timeout(20.0),
        )
        if http_client is not None and "X-Riot-Token" not in self._client.headers:
            self._client.headers["X-Riot-Token"] = settings.riot_api_key
        self._owns_client = http_client is None
        self._max_retries = max(0, max_retries)
        self._base_backoff = max(0.0, base_backoff)
        self._max_backoff = max(0.0, max_backoff)
        self._jitter = max(0.0, jitter)
        self._sleep = sleep
        self._random = random_source or random.Random()
        self._max_requests = max_requests
        self.metrics = RequestMetrics()

    @property
    def base_url(self) -> str:
        return self._regional_url

    @property
    def _regional_url(self) -> str:
        return f"https://{self._routing_region}.api.riotgames.com"

    @property
    def _platform_url(self) -> str:
        return f"https://{self._platform_region}.api.riotgames.com"

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
            self._regional_url,
            f"/riot/account/v1/accounts/by-riot-id/{encoded_name}/{encoded_tag}",
            category="account",
        )
        return RiotAccount.model_validate(payload)

    async def get_league_entries(
        self,
        tier: str,
        division: str,
        *,
        page: int = 1,
    ) -> list[LeagueEntry]:
        payload = await self._get(
            self._platform_url,
            "/lol/league/v4/entries/"
            f"RANKED_SOLO_5x5/{quote(tier, safe='')}/{quote(division, safe='')}",
            category="league_entries",
            params={"page": page},
        )
        if not isinstance(payload, list):
            raise TypeError("League-V4 response was not a list")
        return [LeagueEntry.model_validate(entry) for entry in payload]

    async def get_summoner_by_id(self, encrypted_summoner_id: str) -> SummonerRecord:
        payload = await self._get(
            self._platform_url,
            f"/lol/summoner/v4/summoners/{quote(encrypted_summoner_id, safe='')}",
            category="summoner",
        )
        return SummonerRecord.model_validate(payload)

    async def get_match_ids_by_puuid(
        self, puuid: str, *, start: int = 0, count: int = 5
    ) -> list[str]:
        payload = await self._get(
            self._regional_url,
            f"/lol/match/v5/matches/by-puuid/{quote(puuid, safe='')}/ids",
            category="match_history",
            params={
                "start": start,
                "count": count,
                "queue": RANKED_SOLO_QUEUE_ID,
            },
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, str) for item in payload
        ):
            raise TypeError("Riot match ID response was not a list of strings")
        return payload

    async def get_match(self, match_id: str) -> RiotMatch:
        payload = await self._get(
            self._regional_url,
            f"/lol/match/v5/matches/{quote(match_id, safe='')}",
            category="match_payload",
        )
        return RiotMatch.model_validate(payload)

    async def _get(
        self,
        base_url: str,
        path: str,
        *,
        category: str,
        params: Mapping[str, Any] | None = None,
    ) -> object:
        last_status = 0
        for attempt in range(self._max_retries + 1):
            if (
                self._max_requests is not None
                and self.metrics.attempted_requests >= self._max_requests
            ):
                raise RiotRequestBudgetExceeded(
                    "Riot request safety budget exhausted"
                )
            self.metrics.attempted_requests += 1
            self.metrics.requests_by_endpoint[category] += 1
            try:
                response = await self._client.get(
                    f"{base_url}{path}",
                    params=params,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise RiotRetryExhausted(
                        f"Riot request retries exhausted for {category}"
                    ) from error
                await self._backoff(attempt, None, category)
                continue

            self._observe_rate_headers(response)
            last_status = response.status_code
            if response.status_code < 400:
                self.metrics.successful_requests += 1
                await self._pause_for_full_rate_bucket(response, category)
                return response.json()
            if response.status_code in (401, 403):
                self.metrics.authentication_failures += 1
                raise RiotApiError(response.status_code, category)
            if response.status_code == 429:
                self.metrics.responses_429 += 1
                if attempt >= self._max_retries:
                    break
                await self._backoff(
                    attempt, response.headers.get("Retry-After"), category
                )
                continue
            if 500 <= response.status_code <= 599:
                self.metrics.responses_5xx += 1
                if attempt >= self._max_retries:
                    break
                await self._backoff(attempt, None, category)
                continue
            raise RiotApiError(response.status_code, category)
        raise RiotRetryExhausted(
            f"Riot request retries exhausted for {category} (HTTP {last_status})"
        )

    async def _backoff(
        self, attempt: int, retry_after: str | None, category: str
    ) -> None:
        try:
            header_delay = float(retry_after) if retry_after is not None else None
        except ValueError:
            header_delay = None
        if header_delay is not None:
            delay = max(0.0, header_delay)
        else:
            delay = min(self._max_backoff, self._base_backoff * (2**attempt))
        if self._jitter and header_delay is None:
            delay = min(
                self._max_backoff,
                delay + self._random.uniform(0.0, self._jitter),
            )
        self.metrics.retries += 1
        self.metrics.total_backoff_seconds += delay
        if header_delay is not None:
            self.metrics.reactive_retry_after_seconds += delay
            self.metrics.reactive_retry_after_by_endpoint[category] += delay
        else:
            self.metrics.transient_backoff_seconds += delay
        await self._sleep(delay)

    async def _pause_for_full_rate_bucket(
        self, response: httpx.Response, category: str
    ) -> None:
        delays: list[float] = []
        for prefix in ("X-App", "X-Method"):
            limits = _parse_rate_buckets(response.headers.get(f"{prefix}-Rate-Limit"))
            counts = _parse_rate_buckets(
                response.headers.get(f"{prefix}-Rate-Limit-Count")
            )
            for window, limit in limits.items():
                if counts.get(window, 0) >= limit:
                    delays.append(float(window))
        if delays:
            delay = max(delays)
            self.metrics.total_backoff_seconds += delay
            self.metrics.proactive_backoff_seconds += delay
            self.metrics.proactive_backoff_by_endpoint[category] += delay
            await self._sleep(delay)

    def _observe_rate_headers(self, response: httpx.Response) -> None:
        for header in (
            "X-App-Rate-Limit",
            "X-App-Rate-Limit-Count",
            "X-Method-Rate-Limit",
            "X-Method-Rate-Limit-Count",
            "X-Rate-Limit-Type",
        ):
            if header in response.headers:
                self.metrics.observed_rate_limit_headers.add(header)


def _parse_rate_buckets(value: str | None) -> dict[int, int]:
    buckets: dict[int, int] = {}
    if not value:
        return buckets
    for component in value.split(","):
        try:
            count, window = component.strip().split(":", maxsplit=1)
            buckets[int(window)] = int(count)
        except (TypeError, ValueError):
            continue
    return buckets
