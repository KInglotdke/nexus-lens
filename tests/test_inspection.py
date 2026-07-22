import json

import pytest

from nexus_lens.inspection import inspect_recent_ranked_matches
from nexus_lens.schemas import RiotAccount, RiotMatch
from tests.factories import make_match_payload


class StubInspectionClient:
    async def get_account_by_riot_id(
        self, game_name: str, tag_line: str
    ) -> RiotAccount:
        return RiotAccount(
            puuid="private-test-player",
            gameName=game_name,
            tagLine=tag_line,
        )

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        *,
        count: int,
    ) -> list[str]:
        assert count <= 5
        return ["PRIVATE_MATCH_ID"]

    async def get_match(self, match_id: str) -> RiotMatch:
        payload = make_match_payload(
            match_id=match_id,
            game_version="16.14.792.1234",
        )
        payload["info"]["participants"][0]["puuid"] = "private-test-player"
        payload["info"]["participants"][0]["championName"] = "SafeChampion"
        return RiotMatch.model_validate(payload)


@pytest.mark.asyncio
async def test_recent_inspection_returns_only_approved_fields() -> None:
    rows = await inspect_recent_ranked_matches(
        StubInspectionClient(),  # type: ignore[arg-type]
        game_name="private-name",
        tag_line="private-tag",
        count=5,
    )

    assert rows == [
        {
            "sequence": 1,
            "game_date_utc": "2026-06-09",
            "public_patch": "26.14",
            "api_patch": "16.14",
            "api_game_version": "16.14.792.1234",
            "patch_resolution_status": "resolved",
            "queue_id": 420,
            "champion": "SafeChampion",
            "is_public_26_13_or_26_14": True,
        }
    ]
    rendered = json.dumps(rows)
    assert "private-test-player" not in rendered
    assert "private-name" not in rendered
    assert "private-tag" not in rendered
    assert "PRIVATE_MATCH_ID" not in rendered
