"""Synthetic tests for Stage 3.5A trajectory and history feasibility."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexus_lens.canonical import CanonicalMatch, CanonicalParticipant
from nexus_lens.stage35a import (
    SourceTopMatch,
    _dataset_sha256,
    _is_finite,
    add_strict_history_features,
    build_stage35a_dataset,
    champion_select_feature_record,
    identify_top_participants,
    select_timeline_frame,
    transform_top_match,
    write_stage35a_dataset,
)
from nexus_lens.timeline_collection import (
    REQUIRED_STAGE31_FILES,
    HistoryPolicyConfig,
    Stage31SourceConfig,
    Stage35Config,
    TimelineCatalog,
    TimelinePlatformConfig,
    _timeline_policy_sha256,
)


def test_exact_endpoint_frame_selection_and_lateness() -> None:
    frames = [
        {"timestamp": 299_999},
        {"timestamp": 300_010},
        {"timestamp": 300_020},
    ]
    assert (
        select_timeline_frame(frames, 5, maximum_lateness_ms=60_000)["timestamp"]
        == 300_010
    )
    assert (
        select_timeline_frame([{"timestamp": 360_001}], 5, maximum_lateness_ms=60_000)
        is None
    )


def test_missing_or_early_ended_timeline_is_ineligible() -> None:
    timeline = _timeline(minutes=(0, 5, 10))
    with pytest.raises(ValueError, match="primary_frame_missing"):
        transform_top_match(_source(), timeline, _config())


def test_top_participant_identification_requires_one_per_team() -> None:
    participants = [_participant(1, 100), _participant(6, 200)]
    assert [row.participant_id for row in identify_top_participants(participants)] == [
        1,
        6,
    ]
    with pytest.raises(ValueError, match="top_participant_identification"):
        identify_top_participants(participants + [_participant(2, 100)])


def test_perspectives_reverse_targets_win_and_tower() -> None:
    first, second = transform_top_match(_source(), _timeline(), _config())
    assert first.match_group_id == second.match_group_id
    assert first.scientific_weight == second.scientific_weight == 0.5
    assert first.game_win is True
    assert second.game_win is False
    assert first.trajectory[10].gold_difference == 100  # type: ignore[union-attr]
    assert second.trajectory[10].gold_difference == -100  # type: ignore[union-attr]
    assert first.tower.primary_label_at_15 == "enemy_top_outer_first"
    assert second.tower.primary_label_at_15 == "allied_top_outer_first"


def test_history_is_strictly_earlier_and_future_rows_cannot_change_past() -> None:
    first_source = _source(match_id="TEST_1", day=1)
    second_source = _source(match_id="TEST_2", day=2)
    first = transform_top_match(first_source, _timeline(), _config())[0]
    second = transform_top_match(second_source, _timeline(gold_offset=50), _config())[0]
    policy = HistoryPolicyConfig()

    early_alone = add_strict_history_features((first,), policy)[0]
    with_future = add_strict_history_features((second, first), policy)
    early_with_future = next(
        row for row in with_future if row.match_group_id == first.match_group_id
    )
    later = next(
        row for row in with_future if row.match_group_id == second.match_group_id
    )

    assert early_alone.familiarity == early_with_future.familiarity
    assert early_with_future.familiarity.observed_prior_champion_games == 0  # type: ignore[union-attr]
    assert later.familiarity.observed_prior_champion_games == 1  # type: ignore[union-attr]


def test_low_history_win_rate_is_shrunk_and_uncertainty_declines() -> None:
    rows = []
    for day in range(1, 4):
        source = _source(match_id=f"TEST_{day}", day=day)
        rows.append(transform_top_match(source, _timeline(), _config())[0])
    enriched = add_strict_history_features(tuple(rows), HistoryPolicyConfig())

    assert enriched[0].familiarity.shrunk_historical_win_rate == 0.5  # type: ignore[union-attr]
    assert enriched[1].familiarity.shrunk_historical_win_rate == pytest.approx(6 / 11)  # type: ignore[union-attr]
    assert enriched[1].familiarity.shrunk_historical_win_rate < 1.0  # type: ignore[union-attr]
    assert (
        enriched[2].familiarity.shrunk_win_rate_standard_error  # type: ignore[union-attr]
        < enriched[0].familiarity.shrunk_win_rate_standard_error  # type: ignore[union-attr]
    )
    assert enriched[0].familiarity.limited_history_or_smurf_like is True  # type: ignore[union-attr]


def test_feature_contract_excludes_outcomes_and_opponent_accounts() -> None:
    row = transform_top_match(_source(), _timeline(), _config())[0]
    row = add_strict_history_features((row,), HistoryPolicyConfig())[0]
    features = champion_select_feature_record(row)
    rendered = str(features).lower()

    assert "enemy_top_champion_id" in features
    assert "game_win" not in features
    assert "trajectory" not in features
    assert "tower" not in features
    assert "intervention" not in features
    for forbidden in ("puuid", "player_key", "opponent_rank", "mastery"):
        assert forbidden not in rendered


def test_resumable_catalog_deduplicates_and_fingerprints(tmp_path) -> None:
    path = tmp_path / "checkpoint.sqlite3"
    with TimelineCatalog(path, config_sha256="a" * 64) as catalog:
        catalog.record_download("PRIVATE_MATCH", "b" * 64, "bb/payload.json")
        catalog.record_request_attempt("match_timeline")
        first = catalog.timeline_set_sha256()
        catalog.record_download("PRIVATE_MATCH", "b" * 64, "bb/payload.json")
        assert catalog.successful_ids() == {"PRIVATE_MATCH"}
        assert catalog.status_counts()["downloaded"] == 1
        assert catalog.timeline_set_sha256() == first
        assert catalog.request_attempts() == 1
    with TimelineCatalog(path, config_sha256="a" * 64) as resumed:
        assert resumed.successful_ids() == {"PRIVATE_MATCH"}
        assert resumed.request_attempts() == 1
    with pytest.raises(ValueError, match="configuration differs"):
        TimelineCatalog(path, config_sha256="c" * 64)


def test_private_dataset_fingerprint_is_deterministic() -> None:
    rows = transform_top_match(_source(), _timeline(), _config())
    enriched = add_strict_history_features(rows, HistoryPolicyConfig())
    assert _dataset_sha256(enriched) == _dataset_sha256(enriched)
    assert len(_dataset_sha256(enriched)) == 64


def test_nonfinite_values_fail_quality_primitive() -> None:
    row = transform_top_match(_source(), _timeline(), _config())[0]
    familiarity = add_strict_history_features((row,), HistoryPolicyConfig())[
        0
    ].familiarity
    broken = familiarity.model_copy(update={"shrunk_historical_win_rate": float("nan")})
    assert _is_finite(broken) is False


def test_end_to_end_publication_is_deterministic_and_aggregate_only(
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "stage31"
    source_directory.mkdir()
    match = _source().match
    participants = (_participant(1, 100), _participant(6, 200))
    payloads = {
        "bans.jsonl": "",
        "matches.jsonl": _line(match.model_dump(mode="json")),
        "metadata.json": json.dumps(
            {
                "processing_schema_version": "stage3.1-v1",
                "queue_id": 420,
                "match_counts_by_public_patch": {"26.15": 1},
                "row_counts": {"matches": 1, "participants": 2},
            }
        ),
        "participants.jsonl": "".join(
            _line(_complete_participant(row).model_dump(mode="json"))
            for row in participants
        ),
        "quality_report.json": "{}",
        "teams.jsonl": "",
    }
    for name, content in payloads.items():
        (source_directory / name).write_text(content, encoding="utf-8")
    hashes = {
        name: hashlib.sha256((source_directory / name).read_bytes()).hexdigest()
        for name in REQUIRED_STAGE31_FILES
    }
    platform = TimelinePlatformConfig(
        platform="eun1",
        routing_region="europe",
        raw_timeline_directory=tmp_path / "timelines",
        checkpoint_database=tmp_path / "checkpoint.sqlite3",
        maximum_requests=10,
    )
    timeline_payload = _timeline()
    encoded = (
        json.dumps(timeline_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    relative = Path(digest[:2]) / f"{digest}.json"
    (platform.raw_timeline_directory / relative.parent).mkdir(parents=True)
    (platform.raw_timeline_directory / relative).write_bytes(encoded)
    config = Stage35Config(
        schema_version="stage3.5a-config-v1",
        public_patch="26.15",
        queue_id=420,
        target_eligible_matches=1,
        primary_timestamps_minutes=(5, 10, 15),
        exploratory_timestamps_minutes=(20, 25),
        minimum_game_duration_seconds=300,
        maximum_frame_lateness_ms=60_000,
        concurrency=1,
        private_output_directory=tmp_path / "private-output",
        aggregate_output_directory=tmp_path / "aggregate-output",
        sources=(
            Stage31SourceConfig(
                label="synthetic",
                platform="eun1",
                source_kind="extension",
                directory=source_directory,
                file_sha256=hashes,
            ),
        ),
        platforms=(platform,),
    )
    with TimelineCatalog(
        platform.checkpoint_database,
        config_sha256=_timeline_policy_sha256(config, platform),
    ) as catalog:
        catalog.record_download("TEST_MATCH", digest, relative.as_posix())

    first = build_stage35a_dataset(config)
    second = build_stage35a_dataset(config)
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.selected_match_count == 1
    assert len(first.rows) == 2
    assert first.quality_report["predictive_models_fitted"] == 0
    write_stage35a_dataset(first, config)

    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.aggregate_output_directory.iterdir()
    ).lower()
    assert "test_match" not in public_text
    assert "same-focal-player" not in public_text
    assert "opponent-player" not in public_text
    assert '"player_key":' not in public_text
    assert {path.name for path in config.aggregate_output_directory.iterdir()} == {
        "audit.json",
        "manifest.json",
        "quality_report.json",
        "feasibility_report.md",
    }


def _config() -> Stage35Config:
    return Stage35Config.model_construct(
        schema_version="stage3.5a-config-v1",
        template_only=False,
        public_patch="26.15",
        queue_id=420,
        target_eligible_matches=10_000,
        primary_timestamps_minutes=(5, 10, 15),
        exploratory_timestamps_minutes=(20, 25),
        minimum_game_duration_seconds=300,
        maximum_frame_lateness_ms=60_000,
        concurrency=1,
        private_output_directory=None,
        aggregate_output_directory=None,
        maximum_aggregate_publication_bytes=5_000_000,
        sources=(),
        platforms=(),
        history_policy=HistoryPolicyConfig(),
    )


def _source(*, match_id: str = "TEST_MATCH", day: int = 1) -> SourceTopMatch:
    match = CanonicalMatch.model_construct(
        match_id=match_id,
        public_patch="26.15",
        game_version="16.15.1.1",
        platform="eun1",
        queue_id=420,
        game_creation=datetime(2026, 8, day, tzinfo=UTC),
        game_start_timestamp=None,
        game_end_timestamp=None,
        game_duration_seconds=1_800,
        winning_team_id=100,
        is_remake_or_short_game=False,
        source_payload_reference="private",
        processing_schema_version="stage3.1-v1",
    )
    first = _participant(1, 100)
    second = _participant(6, 200)
    positions = {identifier: "MIDDLE" for identifier in range(1, 11)}
    positions.update({1: "TOP", 2: "JUNGLE", 6: "TOP", 7: "JUNGLE"})
    return SourceTopMatch(match, (first, second), positions)


def _participant(identifier: int, team_id: int) -> CanonicalParticipant:
    return CanonicalParticipant.model_construct(
        match_id="TEST_MATCH",
        participant_id=identifier,
        team_id=team_id,
        player_key="same-focal-player" if team_id == 100 else "opponent-player",
        champion_id=10 if team_id == 100 else 20,
        champion_name="Alpha" if team_id == 100 else "Beta",
        team_position="TOP",
        individual_position="TOP",
        win=team_id == 100,
    )


def _complete_participant(
    partial: CanonicalParticipant,
) -> CanonicalParticipant:
    values = {name: None for name in CanonicalParticipant.model_fields}
    values.update(partial.model_dump())
    values.update(
        {
            "match_id": "TEST_MATCH",
            "participant_id": partial.participant_id,
            "team_id": partial.team_id,
            "player_key": partial.player_key,
            "champion_id": partial.champion_id,
            "champion_name": partial.champion_name,
            "team_position": "TOP",
            "individual_position": "TOP",
            "win": partial.win,
            "processing_schema_version": "stage3.1-v1",
        }
    )
    return CanonicalParticipant.model_validate(values)


def _line(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str) + "\n"


def _timeline(
    *,
    minutes: tuple[int, ...] = (0, 5, 10, 15, 20, 25),
    gold_offset: int = 0,
) -> dict:
    frames = []
    for minute in minutes:
        events = []
        if minute == 10:
            events = [
                {
                    "type": "CHAMPION_KILL",
                    "timestamp": 550_000,
                    "killerId": 1,
                    "victimId": 6,
                    "assistingParticipantIds": [2],
                    "position": {"x": 4_000, "y": 11_000},
                },
                {
                    "type": "BUILDING_KILL",
                    "timestamp": 580_000,
                    "buildingType": "TOWER_BUILDING",
                    "towerType": "OUTER_TURRET",
                    "laneType": "TOP_LANE",
                    "teamId": 200,
                },
            ]
        frames.append(
            {
                "timestamp": minute * 60_000,
                "participantFrames": {
                    "1": {
                        "totalGold": 500 + minute * 100 + gold_offset,
                        "xp": 100 + minute * 50,
                        "minionsKilled": minute * 6,
                        "jungleMinionsKilled": 0,
                        "level": min(18, 1 + minute // 2),
                    },
                    "6": {
                        "totalGold": 400 + minute * 100,
                        "xp": 90 + minute * 50,
                        "minionsKilled": minute * 6 - (1 if minute else 0),
                        "jungleMinionsKilled": 0,
                        "level": min(18, 1 + minute // 2),
                    },
                },
                "events": events,
            }
        )
    return {
        "metadata": {"matchId": "TEST_MATCH"},
        "info": {"frameInterval": 60_000, "frames": frames},
    }
