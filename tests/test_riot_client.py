import httpx
import pytest
import respx

from nexus_lens.config import Settings
from nexus_lens.riot_client import (
    RiotApiError,
    RiotClient,
    RiotRequestBudgetExceeded,
    RiotRetryExhausted,
)


def make_settings() -> Settings:
    return Settings(
        riot_api_key="test-api-key",
        routing_region="europe",
        game_name="Example Player",
        tag_line="EUW",
    )


@pytest.mark.asyncio
@respx.mock
async def test_resolves_account_and_sends_api_key() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/riot/account/v1/accounts/"
        "by-riot-id/Example%20Player/EUW"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "puuid": "example-puuid",
                "gameName": "Example Player",
                "tagLine": "EUW",
            },
        )
    )

    async with RiotClient(make_settings()) as client:
        account = await client.get_account_by_riot_id("Example Player", "EUW")

    assert account.puuid == "example-puuid"
    assert route.calls.last.request.headers["X-Riot-Token"] == "test-api-key"


@pytest.mark.asyncio
@respx.mock
async def test_requests_ranked_solo_match_ids_with_pagination() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(return_value=httpx.Response(200, json=["EUW1_1", "EUW1_2"]))

    async with RiotClient(make_settings()) as client:
        match_ids = await client.get_match_ids_by_puuid(
            "example-puuid", start=10, count=2
        )

    assert match_ids == ["EUW1_1", "EUW1_2"]
    assert dict(route.calls.last.request.url.params) == {
        "start": "10",
        "count": "2",
        "queue": "420",
    }


@pytest.mark.asyncio
@respx.mock
async def test_requests_match_timeline_from_regional_route() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/EUW1_1/timeline"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {"matchId": "EUW1_1"},
                "info": {"frames": []},
            },
        )
    )

    async with RiotClient(make_settings()) as client:
        timeline = await client.get_match_timeline("EUW1_1")

    assert timeline["metadata"]["matchId"] == "EUW1_1"
    assert route.call_count == 1
    assert client.metrics.requests_by_endpoint == {"match_timeline": 1}


@pytest.mark.asyncio
@respx.mock
async def test_raises_domain_error_for_riot_failure() -> None:
    respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/EUW1_missing"
    ).mock(
        return_value=httpx.Response(
            404,
            json={"status": {"message": "Data not found", "status_code": 404}},
        )
    )

    async with RiotClient(make_settings()) as client:
        with pytest.raises(RiotApiError, match="HTTP 404") as exc_info:
            await client.get_match("EUW1_missing")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_429_uses_retry_after_and_records_metrics() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(
                200,
                json=["TEST_1"],
                headers={"X-App-Rate-Limit": "20:1"},
            ),
        ]
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with RiotClient(
        make_settings(),
        max_retries=1,
        jitter=0,
        sleep=record_sleep,
    ) as client:
        assert await client.get_match_ids_by_puuid("example-puuid") == ["TEST_1"]
        metrics = client.metrics.as_dict()

    assert route.call_count == 2
    assert delays == [2.0]
    assert metrics["retries"] == 1
    assert metrics["responses_429"] == 1
    assert metrics["successful_requests"] == 1
    assert metrics["reactive_retry_after_seconds"] == 2.0
    assert metrics["proactive_backoff_seconds"] == 0.0
    assert metrics["total_backoff_seconds"] == 2.0
    assert "X-App-Rate-Limit" in metrics["observed_rate_limit_headers"]


@pytest.mark.asyncio
@respx.mock
async def test_transient_5xx_is_retried() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=[]),
        ]
    )

    async def no_sleep(delay: float) -> None:
        return None

    async with RiotClient(
        make_settings(),
        max_retries=1,
        jitter=0,
        sleep=no_sleep,
    ) as client:
        await client.get_match_ids_by_puuid("example-puuid")
        assert client.metrics.responses_5xx == 1
        assert client.metrics.retries == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status_code", [401, 403])
async def test_authentication_failure_is_not_retried(status_code: int) -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(return_value=httpx.Response(status_code))

    async with RiotClient(make_settings(), max_retries=3) as client:
        with pytest.raises(RiotApiError):
            await client.get_match_ids_by_puuid("example-puuid")
        assert client.metrics.authentication_failures == 1
        assert client.metrics.retries == 0
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_retry_limit_exhaustion_is_bounded() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(return_value=httpx.Response(500))

    async def no_sleep(delay: float) -> None:
        return None

    async with RiotClient(
        make_settings(),
        max_retries=2,
        jitter=0,
        sleep=no_sleep,
    ) as client:
        with pytest.raises(RiotRetryExhausted):
            await client.get_match_ids_by_puuid("example-puuid")
        assert client.metrics.retries == 2
        assert client.metrics.responses_5xx == 3
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_full_rate_limit_bucket_causes_conservative_pause() -> None:
    respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={
                "X-App-Rate-Limit": "20:1,100:120",
                "X-App-Rate-Limit-Count": "20:1,50:120",
            },
        )
    )
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with RiotClient(make_settings(), sleep=record_sleep) as client:
        await client.get_match_ids_by_puuid("example-puuid")

    assert delays == [1.0]
    assert client.metrics.total_backoff_seconds == 1.0
    assert client.metrics.proactive_backoff_seconds == 1.0
    assert client.metrics.reactive_retry_after_seconds == 0.0
    assert client.metrics.proactive_backoff_by_endpoint == {"match_history": 1.0}


@pytest.mark.asyncio
@respx.mock
async def test_request_budget_stops_before_extra_request() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(return_value=httpx.Response(200, json=[]))

    async with RiotClient(make_settings(), max_requests=1) as client:
        await client.get_match_ids_by_puuid("example-puuid")
        with pytest.raises(RiotRequestBudgetExceeded):
            await client.get_match_ids_by_puuid("example-puuid")

    assert route.call_count == 1
