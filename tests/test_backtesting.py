from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import nexus_lens.backtesting as backtesting
from nexus_lens.backtesting import (
    BacktestConfig,
    PredictionRecord,
    build_backtest_dataset,
    build_rolling_origin_splits,
    calculate_metrics,
    calibration_table,
    evidence_bucket,
    fit_reference_policies,
    predict_reference_policy,
    write_backtest_dataset,
)
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_aggregation import Stage33AInput
from nexus_lens.draft_observations import (
    DRAFT_OBSERVATION_SCHEMA_VERSION,
    MatchDraftContext,
    ParticipantDraftObservation,
)

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def test_rolling_origin_split_is_strictly_chronological_and_match_level(
    tmp_path: Path,
) -> None:
    source = _stage33a(tmp_path, patches=("26.13", "26.14", "26.15"))
    splits = build_rolling_origin_splits(source, ("26.14", "26.15"))

    assert splits[0].training_patches == ("26.13",)
    assert splits[1].training_patches == ("26.13", "26.14")
    assert not splits[1].training_match_ids & splits[1].evaluation_match_ids
    for row in source.participants:
        memberships = sum(
            row.match_id in match_ids
            for match_ids in (
                splits[1].training_match_ids,
                splits[1].evaluation_match_ids,
            )
        )
        assert memberships == 1


def test_prediction_does_not_read_evaluation_outcome(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    split = build_rolling_origin_splits(source, ("26.15",))[0]
    fitted = fit_reference_policies(source.participants, split)
    lookup = {(row.match_id, row.participant_id): row for row in source.participants}
    teams = backtesting._team_members(source.participants)
    row = next(row for row in source.participants if row.public_patch == "26.15")
    changed = row.model_copy(update={"win": not row.win})

    first = predict_reference_policy(
        policy="shrunk_directional_matchup",
        fitted=fitted,
        row=row,
        participant_lookup=lookup,
        team_members=teams,
        prior_equivalent_games=10,
    )
    second = predict_reference_policy(
        policy="shrunk_directional_matchup",
        fitted=fitted,
        row=changed,
        participant_lookup=lookup,
        team_members=teams,
        prior_equivalent_games=10,
    )
    assert first == second


def test_fitting_leaves_evaluation_matches_out(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    split = build_rolling_origin_splits(source, ("26.15",))[0]
    fitted = fit_reference_policies(source.participants, split)

    assert fitted.training_match_ids == split.training_match_ids
    assert fitted.global_games == len(split.training_match_ids) * 10
    assert not fitted.training_match_ids & split.evaluation_match_ids


def test_directional_matchup_keys_do_not_collapse(tmp_path: Path) -> None:
    source = _stage33a(tmp_path, patches=("26.14", "26.15"))
    split = build_rolling_origin_splits(source, ("26.15",))[0]
    fitted = fit_reference_policies(source.participants, split)

    assert (1, "TOP", 11, "TOP") in fitted.matchup_stats
    assert (11, "TOP", 1, "TOP") in fitted.matchup_stats
    assert (
        fitted.matchup_stats[(1, "TOP", 11, "TOP")][0]
        != fitted.matchup_stats[(11, "TOP", 1, "TOP")][0]
    )


def test_cross_platform_input_is_rejected(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    source.matches[0] = source.matches[0].model_copy(update={"platform": "euw1"})
    with pytest.raises(Stage3ValidationError) as caught:
        _build(tmp_path, source)
    assert caught.value.category == "cross_platform_input"


def test_duplicate_match_is_rejected(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    source.matches.append(source.matches[0])
    with pytest.raises(Stage3ValidationError) as caught:
        _build(tmp_path, source)
    assert caught.value.category == "duplicate_match"


def test_unseen_champion_role_abstains_explicitly(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    eval_index = next(
        index
        for index, row in enumerate(source.participants)
        if row.public_patch == "26.15" and row.participant_id == 1
    )
    source.participants[eval_index] = source.participants[eval_index].model_copy(
        update={"champion_id": 999_999}
    )
    dataset = _build(
        tmp_path,
        source,
        policies=("champion_role_baseline",),
    )
    overall = dataset.metrics["folds"][0]["policy_results"][0]["overall"]
    assert overall["abstained_rows"] == 1
    assert overall["missing_reasons"] == {"unseen_champion_role": 1}


def test_metric_formulas_and_explicit_clipping_boundary() -> None:
    rows = [
        _prediction("a", 1, 0.8),
        _prediction("b", 0, 0.4),
    ]
    metrics = calculate_metrics(rows, calibration_bins=2)

    assert metrics["brier_score"] == pytest.approx((0.2**2 + 0.4**2) / 2)
    assert metrics["accuracy_at_0_5"] == 1
    assert metrics["coverage"] == 1
    assert metrics["log_loss"] == pytest.approx(
        (-__import__("math").log(0.8) - __import__("math").log(0.6)) / 2
    )


def test_unclipped_impossible_probability_is_null_not_infinite() -> None:
    metrics = calculate_metrics([_prediction("a", 1, 0.0)], calibration_bins=2)
    assert metrics["log_loss"] is None
    assert metrics["log_loss_undefined_reason"] == (
        "undefined_without_explicit_clipping"
    )


def test_calibration_bin_edges_include_zero_and_one() -> None:
    rows = [
        _prediction("a", 0, 0.0),
        _prediction("b", 1, 0.5),
        _prediction("c", 1, 1.0),
    ]
    bins = calibration_table(rows, 2)
    assert [item["count"] for item in bins] == [1, 2]
    assert bins[-1]["upper_inclusive"] == 1
    assert bins[-1]["upper_exclusive"] is None


@pytest.mark.parametrize(
    ("sample_size", "bucket"),
    [(0, "0_unseen"), (1, "1"), (2, "2_4"), (5, "5_9"), (10, "10_19"), (20, "20_plus")],
)
def test_evidence_buckets(sample_size: int, bucket: str) -> None:
    assert evidence_bucket(sample_size) == bucket


def test_deterministic_metrics_and_match_cluster_bootstrap() -> None:
    rows = [
        _prediction(str(index // 2), index % 2, 0.25 + index * 0.1)
        for index in range(6)
    ]
    first = calculate_metrics(
        rows, calibration_bins=3, bootstrap_replicates=20, bootstrap_seed=91
    )
    second = calculate_metrics(
        rows, calibration_bins=3, bootstrap_replicates=20, bootstrap_seed=91
    )
    assert first == second
    assert first["confidence_intervals"]["brier_score"]["replicates"] == 20


def test_validate_only_build_writes_nothing(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    assert not dataset.output_directory.exists()


def test_failed_validation_writes_nothing(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    config = _config(clip_min=0.1, clip_max=None)
    with pytest.raises(Stage3ValidationError):
        build_backtest_dataset(
            stage33a=source, output_root=tmp_path / "out", config=config
        )
    assert not (tmp_path / "out").exists()


def test_publication_is_deterministic_and_immutable(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    published = write_backtest_dataset(dataset)
    first = {path.name: path.read_bytes() for path in published.iterdir()}
    write_backtest_dataset(dataset)
    second = {path.name: path.read_bytes() for path in published.iterdir()}
    assert first == second

    changed = replace(dataset, markdown_report=dataset.markdown_report + "changed\n")
    with pytest.raises(Stage3ValidationError) as caught:
        write_backtest_dataset(changed)
    assert caught.value.category == "immutable_output_conflict"
    assert first == {path.name: path.read_bytes() for path in published.iterdir()}


def test_existing_manifest_hash_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _stage33a(tmp_path)
    dataset = _build(tmp_path, source)
    write_backtest_dataset(dataset)
    metadata_path = dataset.output_directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source"]["stage3_3a_sha256"]["metadata.json"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        backtesting, "load_stage3_3a_input", lambda *args, **kwargs: source
    )

    with pytest.raises(Stage3ValidationError) as caught:
        backtesting.run_stage3_3c(
            input_directory=source.input_directory,
            output_root=tmp_path / "out",
            config=_config(),
            validate_only=True,
            expected_match_count=2,
        )
    assert caught.value.category == "immutable_output_lineage_conflict"


def test_outputs_are_aggregate_only_and_privacy_safe(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    serialized = json.dumps(
        {
            "metrics": dataset.metrics,
            "quality": dataset.quality_report,
            "metadata": dataset.metadata,
        }
    )
    for forbidden in ('"player_key":', "puuid", "summonerId", "riotId", "MATCH-"):
        assert forbidden not in serialized
    assert dataset.quality_report["privacy"]["aggregate_only"] is True


def test_real_manifest_fixture_is_minimized_and_identifier_free() -> None:
    path = Path("tests/fixtures/stage33c/pilot_manifest.json")
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_size < 5_000
    assert set(fixture) == {"fixture_kind", "schema_version", "platforms"}
    serialized = path.read_text(encoding="utf-8")
    for forbidden in ("puuid", "summoner", "riot_id", "player_key", "match_id"):
        assert forbidden not in serialized.lower()


def _build(
    tmp_path: Path,
    source: Stage33AInput,
    *,
    policies: tuple[str, ...] = (
        "naive_empirical",
        "champion_role_baseline",
        "shrunk_directional_matchup",
        "shrunk_directional_synergy",
        "matchup_synergy_combination",
    ),
):
    return build_backtest_dataset(
        stage33a=source,
        output_root=tmp_path / "out",
        config=_config(policies=policies),
    )


def _config(**updates) -> BacktestConfig:
    values = {
        "analysis_region": "EUNE",
        "evaluation_patches": ("26.15",),
        "prior_equivalent_games": 10.0,
        "calibration_bins": 4,
        "clip_min": 0.01,
        "clip_max": 0.99,
        "bootstrap_replicates": 5,
    }
    values.update(updates)
    return BacktestConfig(**values)


def _prediction(match_id: str, outcome: int, probability: float) -> PredictionRecord:
    return PredictionRecord(
        policy="synthetic",
        match_id=match_id,
        public_patch="26.15",
        platform="eun1",
        role="TOP",
        champion_id=1,
        outcome=outcome,
        probability=probability,
        training_evidence=1,
        champion_role_training_games=1,
        missing_reason=None,
    )


def _stage33a(
    tmp_path: Path,
    patches: tuple[str, ...] = ("26.14", "26.15"),
) -> Stage33AInput:
    participants = []
    matches = []
    for match_number, patch in enumerate(patches, start=1):
        match_id = f"SYNTHETIC-{match_number}"
        rows, context = _match(match_id, patch, winner=100 if match_number % 2 else 200)
        participants.extend(rows)
        matches.append(context)
    return Stage33AInput(
        run_id="synthetic-stage33a",
        input_directory=tmp_path / "stage33a",
        stage3_1_directory=tmp_path / "stage31",
        stage3_2_directory=tmp_path / "stage32",
        participants=participants,
        teams=[],
        matches=matches,
        lineage_hashes={"metadata.json": "a" * 64},
        stage3_1_hashes={"metadata.json": "b" * 64},
        stage3_2_hashes={"metadata.json": "c" * 64},
    )


def _match(
    match_id: str, patch: str, *, winner: int
) -> tuple[list[ParticipantDraftObservation], MatchDraftContext]:
    participants = []
    first = (1, 2, 3, 4, 5)
    second = (11, 12, 13, 14, 15)
    champions = (*first, *second)
    for index, champion_id in enumerate(champions):
        participant_id = index + 1
        team_id = 100 if index < 5 else 200
        position_index = index % 5
        opponent_id = participant_id + 5 if team_id == 100 else participant_id - 5
        opponent_champion = (
            second[position_index] if team_id == 100 else first[position_index]
        )
        allies = first if team_id == 100 else second
        enemies = second if team_id == 100 else first
        won = team_id == winner
        participants.append(
            ParticipantDraftObservation(
                match_id=match_id,
                participant_id=participant_id,
                player_key=f"synthetic-{participant_id}",
                champion_id=champion_id,
                champion_name=None,
                team_id=team_id,
                win=won,
                team_win=won,
                enemy_team_win=not won,
                public_patch=patch,
                queue_id=420,
                platform="eun1",
                region=None,
                region_lineage_status="unavailable_in_approved_inputs",
                rank_bracket=None,
                collection_stratum=None,
                rank_lineage_status="unavailable_in_approved_inputs",
                game_duration_seconds=1_800,
                short_game=False,
                team_position=POSITIONS[position_index],
                individual_position=POSITIONS[position_index],
                analysis_position=POSITIONS[position_index],
                analysis_position_source="team_position",
                position_disagreement=False,
                position_pairing_eligibility=True,
                allied_champion_ids=[item for item in allies if item != champion_id],
                enemy_champion_ids=list(enemies),
                lane_opponent_participant_id=opponent_id,
                lane_opponent_champion_id=opponent_champion,
                lane_opponent_champion_name=None,
                lane_opponent_resolution_status="resolved_unique",
                lane_opponent_resolution_reason="unique_reciprocal_same_position",
                general_analysis_eligibility=True,
                role_analysis_eligibility=True,
                matchup_eligibility=True,
                matchup_exclusion_reasons=[],
                synergy_eligibility=True,
                synergy_exclusion_reasons=[],
                team_structure_valid=True,
                opponent_team_structure_valid=True,
                source_run_id="synthetic-stage33a",
                source_stage3_1_schema_version="stage3.1-v1",
                source_stage3_2_schema_version="stage3.2-v1",
                processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
            )
        )
    context = MatchDraftContext(
        match_id=match_id,
        public_patch=patch,
        queue_id=420,
        platform="eun1",
        region=None,
        region_lineage_status="unavailable_in_approved_inputs",
        rank_bracket=None,
        collection_stratum=None,
        rank_lineage_status="unavailable_in_approved_inputs",
        game_duration_seconds=1_800,
        short_game=False,
        participant_count=10,
        team_count=2,
        participants_complete=True,
        teams_complete=True,
        positions_complete=True,
        position_disagreement_count=0,
        position_fallback_count=0,
        unresolved_position_count=0,
        team_compositions=[
            {"team_id": 100, "champion_ids": list(first), "win": winner == 100},
            {"team_id": 200, "champion_ids": list(second), "win": winner == 200},
        ],
        team_bans=[{"team_id": 100, "bans": []}, {"team_id": 200, "bans": []}],
        resolved_lane_opponent_pairs=5,
        all_five_lane_opponent_pairs_resolved=True,
        general_analysis_eligibility=True,
        general_exclusion_reasons=[],
        matchup_eligibility=True,
        matchup_exclusion_reasons=[],
        synergy_eligibility=True,
        synergy_exclusion_reasons=[],
        source_run_id="synthetic-stage33a",
        source_stage3_1_schema_version="stage3.1-v1",
        source_stage3_2_schema_version="stage3.2-v1",
        processing_schema_version=DRAFT_OBSERVATION_SCHEMA_VERSION,
    )
    return participants, context
