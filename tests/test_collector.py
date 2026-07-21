import json
import logging
from pathlib import Path

import pytest

from nexus_lens.collector import FeasibilityCollector
from nexus_lens.config import Settings
from nexus_lens.schemas import RiotAccount, RiotMatch


class StubRiotClient:
    def __init__(self, match: RiotMatch) -> None:
        self._match = match

    async def get_account_by_riot_id(
        self, game_name: str, tag_line: str
    ) -> RiotAccount:
        return RiotAccount(
            puuid="example-puuid",
            gameName=game_name,
            tagLine=tag_line,
        )

    async def get_match_ids_by_puuid(
        self, puuid: str, *, count: int
    ) -> list[str]:
        return [self._match.metadata.matchId]

    async def get_match(self, match_id: str) -> RiotMatch:
        return self._match


def make_settings(raw_data_dir: Path) -> Settings:
    return Settings(
        riot_api_key="unused-test-key",
        routing_region="europe",
        game_name="Example Player",
        tag_line="EUNE",
        raw_data_dir=raw_data_dir,
    )


def make_match(queue_id: int) -> RiotMatch:
    return RiotMatch.model_validate(
        {
            "metadata": {
                "matchId": f"EUN1_queue_{queue_id}",
                "participants": ["example-puuid"],
            },
            "info": {
                "gameCreation": 1_700_000_000_000,
                "gameDuration": 1_800,
                "queueId": queue_id,
                "participants": [{"puuid": "example-puuid"}],
            },
        }
    )


@pytest.mark.asyncio
async def test_queue_420_match_is_accepted_and_reported(tmp_path: Path) -> None:
    match = make_match(420)
    collector = FeasibilityCollector(
        make_settings(tmp_path / "raw"),
        StubRiotClient(match),  # type: ignore[arg-type]
    )

    snapshot_dir = await collector.collect(count=1)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text("utf-8"))

    assert manifest["queue_id"] == 420
    assert manifest["accepted_match_ids"] == [match.metadata.matchId]
    assert manifest["skipped_matches"] == []
    assert (snapshot_dir / manifest["match_files"][0]).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_id", [0, 400, 430, 440, 450, 700, 1700])
async def test_every_non_420_queue_is_rejected_and_reported(
    queue_id: int,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match = make_match(queue_id)
    collector = FeasibilityCollector(
        make_settings(tmp_path / "raw"),
        StubRiotClient(match),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING):
        snapshot_dir = await collector.collect(count=1)
    manifest = json.loads((snapshot_dir / "manifest.json").read_text("utf-8"))

    assert manifest["queue_id"] == 420
    assert manifest["accepted_match_ids"] == []
    assert manifest["match_files"] == []
    assert manifest["skipped_matches"] == [
        {
            "match_id": match.metadata.matchId,
            "queue_id": queue_id,
            "reason": f"expected queueId 420, got {queue_id}",
        }
    ]
    assert f"Skipping match {match.metadata.matchId}" in caplog.text
