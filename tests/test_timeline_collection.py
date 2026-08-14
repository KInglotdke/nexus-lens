"""Synthetic network-boundary tests for Stage 3.5A timeline collection."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from nexus_lens.timeline_collection import (
    HistoryPolicyConfig,
    Stage35Config,
    TimelineMatchRef,
    TimelinePayloadReader,
    TimelinePlatformConfig,
    collect_platform_timelines,
    load_stage35_config,
    load_timeline_payloads,
)


@pytest.mark.asyncio
@respx.mock
async def test_collection_is_checksummed_resumable_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_environment(monkeypatch)
    config, platform = _config(tmp_path)
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/TEST_1/timeline"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {"matchId": "TEST_1"},
                "info": {"frames": []},
            },
        )
    )
    matches = (TimelineMatchRef("eun1", "TEST_1"),)

    first = await collect_platform_timelines(
        config=config, platform_config=platform, matches=matches
    )
    second = await collect_platform_timelines(
        config=config, platform_config=platform, matches=matches
    )

    assert route.call_count == 1
    assert first.downloaded_this_run == 1
    assert first.cumulative_request_attempts == 1
    assert second.already_downloaded == 1
    assert second.downloaded_this_run == 0
    assert second.timeline_set_sha256 == first.timeline_set_sha256
    assert load_timeline_payloads(platform)["TEST_1"]["info"]["frames"] == []
    reader = TimelinePayloadReader(platform)
    assert len(reader) == 1
    assert reader.verify_all() == 1
    assert reader.load("MISSING") is None
    raw_files = list(platform.raw_timeline_directory.rglob("*.json"))
    assert len(raw_files) == 1
    assert "TEST_1" not in raw_files[0].name


@pytest.mark.asyncio
@respx.mock
async def test_not_found_timeline_is_terminal_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_environment(monkeypatch)
    config, platform = _config(tmp_path)
    route = respx.get(
        "https://europe.api.riotgames.com/lol/match/v5/matches/TEST_404/timeline"
    ).mock(return_value=httpx.Response(404))
    matches = (TimelineMatchRef("eun1", "TEST_404"),)

    first = await collect_platform_timelines(
        config=config, platform_config=platform, matches=matches
    )
    second = await collect_platform_timelines(
        config=config, platform_config=platform, matches=matches
    )

    assert route.call_count == 1
    assert first.unavailable_this_run == 1
    assert first.cumulative_unavailable == 1
    assert first.target_complete is True
    assert second.remaining == 0


def test_checked_config_schema_and_template_validate() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_stage35_config(
        root / "config/stage3.5a/stage35a.example.json", allow_template=True
    )
    schema = json.loads(
        (root / "config/stage3.5a/stage35a.schema.json").read_text(encoding="utf-8")
    )
    assert config.template_only is True
    assert config.queue_id == 420
    assert schema["properties"]["queue_id"]["const"] == 420
    with pytest.raises(ValueError, match="not executable"):
        load_stage35_config(root / "config/stage3.5a/stage35a.example.json")


def _config(tmp_path: Path) -> tuple[Stage35Config, TimelinePlatformConfig]:
    platform = TimelinePlatformConfig(
        platform="eun1",
        routing_region="europe",
        raw_timeline_directory=tmp_path / "raw",
        checkpoint_database=tmp_path / "state.sqlite3",
        maximum_requests=10,
    )
    config = Stage35Config.model_construct(
        schema_version="stage3.5a-config-v1",
        template_only=False,
        public_patch="26.15",
        queue_id=420,
        target_eligible_matches=1,
        primary_timestamps_minutes=(5, 10, 15),
        exploratory_timestamps_minutes=(20, 25),
        minimum_game_duration_seconds=300,
        maximum_frame_lateness_ms=60_000,
        concurrency=1,
        private_output_directory=tmp_path / "private",
        aggregate_output_directory=tmp_path / "aggregate",
        maximum_aggregate_publication_bytes=5_000_000,
        sources=(),
        platforms=(platform,),
        history_policy=HistoryPolicyConfig(),
    )
    return config, platform


def _settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_LENS_RIOT_API_KEY", "synthetic-key")
    monkeypatch.setenv("NEXUS_LENS_GAME_NAME", "Synthetic")
    monkeypatch.setenv("NEXUS_LENS_TAG_LINE", "TEST")
