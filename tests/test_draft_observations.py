"""Synthetic tests for Stage 3.3A factual draft observations."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import nexus_lens.draft_observations as draft
from nexus_lens.analytics import Stage31Input, build_analytical_dataset, run_stage3_2
from nexus_lens.canonical import (
    ApprovedPayload,
    Stage3ValidationError,
    build_canonical_dataset,
    write_canonical_dataset,
)
from nexus_lens.draft_observations import (
    Stage32Input,
    build_draft_observation_dataset,
    run_stage3_3a,
    write_draft_observation_dataset,
)
from tests.factories import make_match_payload


def test_normal_five_vs_five_observations_and_compositions(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    participant = dataset.participant_observations[0]
    team = dataset.team_observations[0]
    context = dataset.match_contexts[0]

    assert len(dataset.participant_observations) == 10
    assert len(dataset.team_observations) == 2
    assert len(dataset.match_contexts) == 1
    assert participant.allied_champion_ids == [1001, 1002, 1003, 1004]
    assert participant.enemy_champion_ids == [1005, 1006, 1007, 1008, 1009]
    assert participant.champion_id not in participant.allied_champion_ids
    assert participant.champion_id not in participant.enemy_champion_ids
    assert team.champion_ids == [1000, 1001, 1002, 1003, 1004]
    assert team.opponent_champion_ids == [1005, 1006, 1007, 1008, 1009]
    assert context.all_five_lane_opponent_pairs_resolved is True
    assert context.resolved_lane_opponent_pairs == 5


def test_unique_lane_opponents_are_reciprocal_and_match_position(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path)
    rows = {row.participant_id: row for row in dataset.participant_observations}

    for row in rows.values():
        opponent = rows[row.lane_opponent_participant_id]
        assert row.lane_opponent_resolution_status == "resolved_unique"
        assert opponent.lane_opponent_participant_id == row.participant_id
        assert opponent.analysis_position == row.analysis_position


def test_rows_and_composition_lists_are_deterministically_ordered(
    tmp_path: Path,
) -> None:
    stage31, stage32 = _prior_inputs(tmp_path)
    stage31 = replace(stage31, participants=list(reversed(stage31.participants)))
    stage32 = replace(
        stage32,
        participant_features=list(reversed(stage32.participant_features)),
        team_features=list(reversed(stage32.team_features)),
    )
    dataset = _build(tmp_path, stage31, stage32)

    keys = [
        (row.match_id, row.participant_id) for row in dataset.participant_observations
    ]
    assert keys == sorted(keys)
    assert dataset.team_observations[0].champion_ids == [1000, 1001, 1002, 1003, 1004]


def test_missing_position_keeps_row_and_nulls_lane_opponent(tmp_path: Path) -> None:
    stage31, _ = _prior_inputs(tmp_path)
    changed = stage31.participants[0].model_copy(
        update={"team_position": None, "individual_position": None}
    )
    participants = [changed, *stage31.participants[1:]]
    stage31, stage32 = _rebuild_analytics(tmp_path, stage31, participants)
    dataset = _build(tmp_path, stage31, stage32)
    row = dataset.participant_observations[0]

    assert row.analysis_position is None
    assert row.lane_opponent_champion_id is None
    assert row.lane_opponent_resolution_status == "participant_position_ineligible"
    assert row.matchup_eligibility is False


def test_position_disagreement_keeps_team_position_but_disables_matchup(
    tmp_path: Path,
) -> None:
    stage31, _ = _prior_inputs(tmp_path)
    changed = stage31.participants[0].model_copy(
        update={"team_position": "TOP", "individual_position": "JUNGLE"}
    )
    stage31, stage32 = _rebuild_analytics(
        tmp_path, stage31, [changed, *stage31.participants[1:]]
    )
    dataset = _build(tmp_path, stage31, stage32)
    row = dataset.participant_observations[0]

    assert row.analysis_position == "TOP"
    assert row.position_disagreement is True
    assert row.lane_opponent_champion_id is None
    assert row.matchup_eligibility is False
    assert row.synergy_eligibility is True


def test_duplicated_opponent_position_is_ambiguous_without_guessing(
    tmp_path: Path,
) -> None:
    stage31, _ = _prior_inputs(tmp_path)
    participants = list(stage31.participants)
    participants[6] = participants[6].model_copy(
        update={"team_position": "TOP", "individual_position": "TOP"}
    )
    stage31, stage32 = _rebuild_analytics(tmp_path, stage31, participants)
    dataset = _build(tmp_path, stage31, stage32)
    statuses = {
        row.lane_opponent_resolution_status
        for row in dataset.participant_observations
        if row.analysis_position == "TOP"
    }

    assert "ambiguous_opponent_position" in statuses
    assert "nonreciprocal_position_group" in statuses
    assert dataset.quality_report["reconciliation_failures"] == {}


def test_short_game_rows_remain_factual_but_aggregation_ineligible(
    tmp_path: Path,
) -> None:
    stage31, _ = _prior_inputs(tmp_path, duration=299)
    stage31, stage32 = _rebuild_analytics(tmp_path, stage31, stage31.participants)
    dataset = _build(tmp_path, stage31, stage32)

    assert len(dataset.participant_observations) == 10
    assert all(row.short_game for row in dataset.participant_observations)
    assert all(not row.matchup_eligibility for row in dataset.participant_observations)
    assert all(not row.synergy_eligibility for row in dataset.participant_observations)
    assert all(
        len(row.allied_champion_ids) == 4 for row in dataset.participant_observations
    )


def test_invalid_team_size_preserves_rows_without_ally_relationships(
    tmp_path: Path,
) -> None:
    stage31, _ = _prior_inputs(tmp_path)
    participants = stage31.participants[:-1]
    stage31, stage32 = _rebuild_analytics(tmp_path, stage31, participants)
    dataset = _build(
        tmp_path,
        stage31,
        stage32,
        expected_participants=9,
    )

    assert len(dataset.participant_observations) == 9
    assert all(not row.synergy_eligibility for row in dataset.participant_observations)
    assert all(
        len(row.allied_champion_ids) == (4 if row.team_id == 100 else 0)
        for row in dataset.participant_observations
    )
    assert all(
        row.lane_opponent_resolution_status == "invalid_team_structure"
        for row in dataset.participant_observations
    )


def test_missing_champion_name_preserves_champion_id_lineage(tmp_path: Path) -> None:
    stage31, stage32 = _prior_inputs(tmp_path)
    participant = stage31.participants[0].model_copy(update={"champion_name": None})
    feature = stage32.participant_features[0].model_copy(update={"champion_name": None})
    stage31 = replace(stage31, participants=[participant, *stage31.participants[1:]])
    stage32 = replace(
        stage32,
        participant_features=[feature, *stage32.participant_features[1:]],
    )
    dataset = _build(tmp_path, stage31, stage32)

    assert dataset.participant_observations[0].champion_id == 1000
    assert dataset.participant_observations[0].champion_name is None
    assert dataset.quality_report["missing_champion_names"] == 1


def test_bans_reconcile_and_explicit_no_ban_is_preserved(tmp_path: Path) -> None:
    stage31, stage32 = _prior_inputs(tmp_path)
    first = stage31.bans[0].model_copy(update={"champion_id": -1})
    stage31 = replace(stage31, bans=[first, *stage31.bans[1:]])
    dataset = _build(tmp_path, stage31, stage32)

    assert dataset.team_observations[0].bans[0].champion_id == -1
    assert dataset.match_contexts[0].team_bans[0].bans[0].champion_id == -1
    assert dataset.quality_report["bans"] == {
        "rows": 10,
        "explicit_no_ban_rows": 1,
    }


def test_deterministic_rerun_and_failure_safe_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset(tmp_path)
    output = write_draft_observation_dataset(dataset)
    first = _file_hashes(output)
    write_draft_observation_dataset(dataset)
    assert _file_hashes(output) == first

    def fail_write(*_: object, **__: object) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(draft, "_write_staged_dataset", fail_write)
    with pytest.raises(OSError, match="synthetic publication failure"):
        write_draft_observation_dataset(dataset)
    assert _file_hashes(output) == first


def test_validation_only_and_publication_preserve_prior_stage_hashes(
    tmp_path: Path,
) -> None:
    stage31_dir, stage32_dir = _write_prior_runs(tmp_path)
    before31 = _file_hashes(stage31_dir)
    before32 = _file_hashes(stage32_dir)
    output_root = tmp_path / "draft-output"

    run_stage3_3a(
        stage3_1_directory=stage31_dir,
        stage3_2_directory=stage32_dir,
        output_root=output_root,
        validate_only=True,
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_ban_count=10,
        expected_patch_counts={"26.14": 1},
    )
    assert not output_root.exists()
    run_stage3_3a(
        stage3_1_directory=stage31_dir,
        stage3_2_directory=stage32_dir,
        output_root=output_root,
        validate_only=False,
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_ban_count=10,
        expected_patch_counts={"26.14": 1},
    )
    assert _file_hashes(stage31_dir) == before31
    assert _file_hashes(stage32_dir) == before32


def test_incompatible_stage32_schema_fails_closed(tmp_path: Path) -> None:
    stage31_dir, stage32_dir = _write_prior_runs(tmp_path)
    metadata_path = stage32_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["processing_schema_version"] = "incompatible"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_3a(
            stage3_1_directory=stage31_dir,
            stage3_2_directory=stage32_dir,
            output_root=tmp_path / "output",
            validate_only=True,
            expected_match_count=1,
            expected_participant_count=10,
            expected_team_count=2,
            expected_ban_count=10,
            expected_patch_counts={"26.14": 1},
        )
    assert caught.value.category == "incompatible_stage3_2_schema"


def test_privacy_and_metadata_contract(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    columns = type(dataset.participant_observations[0]).model_fields
    report = json.dumps(dataset.quality_report)

    assert "player_key" in columns
    assert (
        not {
            "puuid",
            "summoner_id",
            "summoner_name",
            "riot_id",
            "riot_id_game_name",
            "riot_id_tagline",
        }
        & columns.keys()
    )
    assert "synthetic-player" not in report
    assert (
        dataset.metadata["generation_configuration"]["pick_order_reconstruction"]
        is False
    )
    assert "metadata.json" not in dataset.metadata["output"]["sha256"]


def _dataset(tmp_path: Path):
    stage31, stage32 = _prior_inputs(tmp_path)
    return _build(tmp_path, stage31, stage32)


def _build(
    tmp_path: Path,
    stage31: Stage31Input,
    stage32: Stage32Input,
    *,
    expected_participants: int = 10,
):
    return build_draft_observation_dataset(
        stage31=stage31,
        stage32=stage32,
        output_directory=tmp_path / "draft-output-direct",
        expected_match_count=1,
        expected_participant_count=expected_participants,
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )


def _prior_inputs(
    tmp_path: Path, *, duration: int = 1_800
) -> tuple[Stage31Input, Stage32Input]:
    payload = make_match_payload(game_version="16.14.1.1")
    payload["info"]["gameDuration"] = duration
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    approved = ApprovedPayload(
        match_id="TEST_1",
        public_patch="26.14",
        path=path,
        source_reference="synthetic/payload.json",
        catalog={
            "status": "processed",
            "api_game_version": "16.14.1.1",
            "patch_resolution_status": "resolved",
            "public_patch": "26.14",
            "queue_id": 420,
        },
        partition_record={
            "match_id": "TEST_1",
            "api_game_version": "16.14.1.1",
            "public_patch": "26.14",
            "queue_id": 420,
        },
    )
    canonical = build_canonical_dataset(
        run_id="20260722T125547567196Z-population",
        input_manifest="synthetic/manifest.json",
        platform="test1",
        approved_payloads=[approved],
        output_directory=tmp_path / "canonical",
        expected_match_count=1,
        expected_patch_counts={"26.14": 1},
    )
    stage31 = Stage31Input(
        run_id=canonical.run_id,
        input_directory=tmp_path / "canonical",
        matches=canonical.matches,
        participants=canonical.participants,
        teams=canonical.teams,
        bans=canonical.bans,
        lineage_hashes={"matches.jsonl": "stage31-synthetic"},
    )
    return _rebuild_analytics(tmp_path, stage31, stage31.participants)


def _rebuild_analytics(
    tmp_path: Path,
    stage31: Stage31Input,
    participants: list[object],
) -> tuple[Stage31Input, Stage32Input]:
    stage31 = replace(stage31, participants=participants)
    analytical = build_analytical_dataset(
        run_id=stage31.run_id,
        input_directory=stage31.input_directory,
        output_directory=tmp_path / "analytical",
        matches=stage31.matches,
        participants=stage31.participants,
        teams=stage31.teams,
        lineage_hashes=stage31.lineage_hashes,
        expected_match_count=1,
        expected_participant_count=len(stage31.participants),
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )
    stage32 = Stage32Input(
        run_id=stage31.run_id,
        input_directory=tmp_path / "analytical",
        participant_features=analytical.participant_features,
        team_features=analytical.team_features,
        match_contexts=analytical.match_contexts,
        lineage_hashes={"participant_match_features.jsonl": "stage32-synthetic"},
    )
    return stage31, stage32


def _write_prior_runs(tmp_path: Path) -> tuple[Path, Path]:
    stage31, _ = _prior_inputs(tmp_path)
    stage3_root = tmp_path / "stage3"
    canonical = build_canonical_dataset(
        run_id=stage31.run_id,
        input_manifest="synthetic/manifest.json",
        platform="test1",
        approved_payloads=[_approved_from_stage31(tmp_path)],
        output_directory=(stage3_root / "schema=stage3.1-v1" / f"run={stage31.run_id}"),
        expected_match_count=1,
        expected_patch_counts={"26.14": 1},
    )
    stage31_dir = write_canonical_dataset(canonical)
    analytical = run_stage3_2(
        input_directory=stage31_dir,
        output_root=stage3_root,
        validate_only=False,
        expected_match_count=1,
        expected_participant_count=10,
        expected_team_count=2,
        expected_patch_counts={"26.14": 1},
    )
    return stage31_dir, analytical.output_directory


def _approved_from_stage31(tmp_path: Path) -> ApprovedPayload:
    payload_path = tmp_path / "payload.json"
    return ApprovedPayload(
        match_id="TEST_1",
        public_patch="26.14",
        path=payload_path,
        source_reference="synthetic/payload.json",
        catalog={
            "status": "processed",
            "api_game_version": "16.14.1.1",
            "patch_resolution_status": "resolved",
            "public_patch": "26.14",
            "queue_id": 420,
        },
        partition_record={
            "match_id": "TEST_1",
            "api_game_version": "16.14.1.1",
            "public_patch": "26.14",
            "queue_id": 420,
        },
    )


def _file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
