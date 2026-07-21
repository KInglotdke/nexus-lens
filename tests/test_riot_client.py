import httpx
import pytest
import respx

from nexus_lens.config import Settings
from nexus_lens.riot_client import RiotApiError, RiotClient


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
async def test_requests_match_ids_with_pagination() -> None:
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/"
        "by-puuid/example-puuid/ids"
    ).mock(return_value=httpx.Response(200, json=["EUW1_1", "EUW1_2"]))

    async with RiotClient(make_settings()) as client:
        match_ids = await client.get_match_ids_by_puuid(
            "example-puuid", start=10, count=2
        )

    assert match_ids == ["EUW1_1", "EUW1_2"]
    assert dict(route.calls.last.request.url.params) == {"start": "10", "count": "2"}


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
