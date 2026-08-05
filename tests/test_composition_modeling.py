from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

import nexus_lens.composition_modeling as modeling
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    CompositionConfig,
    build_composition_dataset,
    build_feature_vocabulary,
    build_match_draft_corpus,
    evaluate_counterfactual,
    fit_composition_model,
    predict_champion_role_baseline,
    predict_probability,
    run_stage3_4a,
    swap_teams,
    vectorize_draft,
    write_composition_dataset,
)
from nexus_lens.draft_aggregation import Stage33AInput
from nexus_lens.draft_observations import (
    DRAFT_OBSERVATION_SCHEMA_VERSION,
    MatchDraftContext,
    ParticipantDraftObservation,
)

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def test_match_level_feature_construction_and_role_ordering(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    corpus = build_match_draft_corpus(source)
    draft = corpus.observations[0]
    vocabulary = build_feature_vocabulary(
        corpus.observations, include_lane_matchups=True
    )
    vector = vectorize_draft(draft, vocabulary)

    assert len(corpus.observations) == len(source.matches)
    assert tuple(item.role for item in draft.allied) == POSITIONS
    assert tuple(item.role for item in draft.opposing) == POSITIONS
    assert len(vector) == 15
    assert draft.outcome in {0, 1}


def test_input_participant_order_does_not_change_features(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    first = build_match_draft_corpus(source)
    reordered = replace(source, participants=list(reversed(source.participants)))
    second = build_match_draft_corpus(reordered)
    vocabulary = build_feature_vocabulary(
        first.observations, include_lane_matchups=True
    )

    assert first.observations == second.observations
    assert vectorize_draft(first.observations[0], vocabulary) == vectorize_draft(
        second.observations[0], vocabulary
    )


def test_team_swap_complements_features_and_probability(tmp_path: Path) -> None:
    corpus = build_match_draft_corpus(_stage33a(tmp_path))
    training = tuple(row for row in corpus.observations if row.public_patch == "26.14")
    model = fit_composition_model(
        training,
        variant="composition_plus_lane_matchups",
        l2_strength=0.1,
        max_iterations=200,
        tolerance=1e-9,
    )
    draft = training[0]
    original = vectorize_draft(draft, model.vocabulary)
    swapped = vectorize_draft(swap_teams(draft), model.vocabulary)

    assert original == {index: -value for index, value in swapped.items()}
    assert predict_probability(model, draft) + predict_probability(
        model, swap_teams(draft)
    ) == pytest.approx(1)


def test_champion_role_baseline_also_complements_team_swap(tmp_path: Path) -> None:
    corpus = build_match_draft_corpus(_stage33a(tmp_path))
    training = tuple(row for row in corpus.observations if row.public_patch == "26.14")
    statistics = modeling.fit_champion_role_baseline(training)
    draft = training[0]
    assert predict_champion_role_baseline(
        draft, statistics
    ) + predict_champion_role_baseline(swap_teams(draft), statistics) == pytest.approx(
        1
    )


def test_evaluation_vocabulary_and_outcome_are_isolated(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    evaluation_index = next(
        index
        for index, row in enumerate(source.participants)
        if row.public_patch == "26.15" and row.participant_id == 1
    )
    source.participants[evaluation_index] = source.participants[
        evaluation_index
    ].model_copy(update={"champion_id": 999_999})
    first = _build(tmp_path, source)
    flipped = replace(
        source,
        participants=[
            row.model_copy(update={"win": not row.win})
            if row.public_patch == "26.15"
            else row
            for row in source.participants
        ],
    )
    second = _build(tmp_path / "second", flipped)

    first_models = first.model_artifacts["models"]
    second_models = second.model_artifacts["models"]
    assert first_models == second_models
    assert all(
        item["feature"].get("champion_id") != 999_999
        and item["feature"].get("lower_champion_id") != 999_999
        and item["feature"].get("higher_champion_id") != 999_999
        for model in first_models
        for item in model["coefficients"]
    )


def test_hyperparameter_selection_uses_training_patch_only(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    first = _build(tmp_path, source)
    for row in source.participants:
        if row.public_patch == "26.15":
            row.win = not row.win
    second = _build(tmp_path / "other", source)

    assert [
        model["selected_l2_strength"] for model in first.model_artifacts["models"]
    ] == [model["selected_l2_strength"] for model in second.model_artifacts["models"]]
    assert all(
        result["fit_scope"] == "training_patch_match_grouped_only"
        for model in first.model_artifacts["models"]
        for result in model["cv_results"]
    )


def test_frozen_hyperparameters_skip_evaluation_tuning(tmp_path: Path) -> None:
    dataset = build_composition_dataset(
        stage33a=_stage33a(tmp_path),
        output_root=tmp_path / "out",
        config=_config(
            composition_only_l2=0.1,
            composition_plus_lane_matchups_l2=1.0,
        ),
    )
    models = dataset.model_artifacts["models"]
    assert [model["selected_l2_strength"] for model in models] == [0.1, 1.0]
    assert all(
        model["cv_results"][0]["selection_status"] == "frozen_before_evaluation"
        for model in models
    )


def test_chronological_and_platform_isolation(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    dataset = _build(tmp_path, source)
    assert dataset.metrics["training_patch"] == "26.14"
    assert dataset.metrics["evaluation_patch"] == "26.15"
    assert dataset.quality_report["leakage_and_invariance"][
        "training_evaluation_match_sets_disjoint"
    ]

    source.matches[0] = source.matches[0].model_copy(update={"platform": "euw1"})
    with pytest.raises(Stage3ValidationError) as caught:
        _build(tmp_path / "cross", source)
    assert caught.value.category == "cross_platform_input"


def test_duplicate_match_and_participant_are_rejected(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    source.matches.append(source.matches[0])
    with pytest.raises(Stage3ValidationError) as caught:
        build_match_draft_corpus(source)
    assert caught.value.category == "duplicate_match"

    source = _stage33a(tmp_path)
    source.participants.append(source.participants[0])
    with pytest.raises(Stage3ValidationError) as caught:
        build_match_draft_corpus(source)
    assert caught.value.category == "duplicate_participant"


def test_unseen_champion_has_defined_zero_contribution(tmp_path: Path) -> None:
    corpus = build_match_draft_corpus(_stage33a(tmp_path))
    training = tuple(row for row in corpus.observations if row.public_patch == "26.14")
    model = fit_composition_model(
        training,
        variant="composition_only",
        l2_strength=1,
        max_iterations=200,
        tolerance=1e-9,
    )
    evaluation = next(row for row in corpus.observations if row.public_patch == "26.15")
    changed = replace(
        evaluation,
        allied=tuple(
            replace(item, champion_id=999_999) if item.role == "TOP" else item
            for item in evaluation.allied
        ),
    )

    assert all(key[2] != 999_999 and key[3] != 999_999 for key in model.vocabulary.keys)
    assert math.isfinite(predict_probability(model, changed))


def test_counterfactual_changes_one_slot_and_never_ranks(tmp_path: Path) -> None:
    corpus = build_match_draft_corpus(_stage33a(tmp_path))
    training = tuple(row for row in corpus.observations if row.public_patch == "26.14")
    model = fit_composition_model(
        training,
        variant="composition_plus_lane_matchups",
        l2_strength=0.1,
        max_iterations=200,
        tolerance=1e-9,
    )
    draft = training[0]
    first = evaluate_counterfactual(
        model=model,
        draft=draft,
        side="allied",
        role="TOP",
        candidate_champion_id=999,
    )
    second = evaluate_counterfactual(
        model=model,
        draft=draft,
        side="allied",
        role="TOP",
        candidate_champion_id=998,
    )

    assert first.unchanged_nine_slots_sha256 == second.unchanged_nine_slots_sha256
    assert first.interpretation == "mechanical_non_causal_not_a_recommendation"
    assert not hasattr(first, "rank")


def test_deterministic_fitting_and_evaluation(tmp_path: Path) -> None:
    source = _stage33a(tmp_path)
    first = _build(tmp_path, source)
    second = _build(tmp_path, source)
    assert first.metrics == second.metrics
    assert first.model_artifacts == second.model_artifacts
    assert first.metadata == second.metadata


def test_validate_only_build_writes_nothing(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    assert not dataset.output_directory.exists()


def test_failed_storage_preflight_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(Stage3ValidationError) as caught:
        build_composition_dataset(
            stage33a=_stage33a(tmp_path),
            output_root=tmp_path / "out",
            config=_config(minimum_free_space_reserve_bytes=10**18),
        )
    assert caught.value.category == "storage_reserve_failure"
    assert not (tmp_path / "out").exists()


def test_publication_is_deterministic_and_immutable(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    published = write_composition_dataset(dataset)
    before = {path.name: path.read_bytes() for path in published.iterdir()}
    write_composition_dataset(dataset)
    assert before == {path.name: path.read_bytes() for path in published.iterdir()}

    changed = replace(dataset, markdown_report=dataset.markdown_report + "changed\n")
    with pytest.raises(Stage3ValidationError) as caught:
        write_composition_dataset(changed)
    assert caught.value.category == "immutable_output_conflict"
    assert before == {path.name: path.read_bytes() for path in published.iterdir()}


def test_existing_manifest_hash_conflict_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _stage33a(tmp_path)
    dataset = _build(tmp_path, source)
    write_composition_dataset(dataset)
    metadata_path = dataset.output_directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source"]["stage3_3a_sha256"]["metadata.json"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        modeling, "load_stage3_3a_input", lambda *args, **kwargs: source
    )

    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_4a(
            input_directory=source.input_directory,
            output_root=tmp_path / "out",
            config=_config(),
            validate_only=True,
            expected_match_count=len(source.matches),
        )
    assert caught.value.category == "immutable_output_lineage_conflict"


def test_input_hash_validation_failure_propagates_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise Stage3ValidationError("stage3_3a_hash_conflict", "synthetic")

    monkeypatch.setattr(modeling, "load_stage3_3a_input", fail)
    with pytest.raises(Stage3ValidationError) as caught:
        run_stage3_4a(
            input_directory=tmp_path / "input",
            output_root=tmp_path / "out",
            config=_config(),
            validate_only=False,
            expected_match_count=10,
        )
    assert caught.value.category == "stage3_3a_hash_conflict"
    assert not (tmp_path / "out").exists()


def test_public_artifacts_are_aggregate_and_privacy_safe(tmp_path: Path) -> None:
    dataset = _build(tmp_path, _stage33a(tmp_path))
    payloads = [dataset.metrics, dataset.model_artifacts, dataset.quality_report]
    forbidden_keys = {
        "match_id",
        "player_key",
        "puuid",
        "summonerId",
        "accountId",
        "riotIdGameName",
        "riotIdTagline",
        "summonerName",
    }

    for payload in payloads:
        keys = _all_keys(payload)
        assert not keys & forbidden_keys
    serialized = json.dumps(payloads)
    assert "synthetic-player" not in serialized
    assert dataset.quality_report["privacy"]["prediction_level_artifact"] is False


def _all_keys(value) -> set[str]:
    keys = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            keys.update(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return keys


def _build(tmp_path: Path, source: Stage33AInput):
    return build_composition_dataset(
        stage33a=source,
        output_root=tmp_path / "out",
        config=_config(),
    )


def _config(**updates) -> CompositionConfig:
    values = {
        "analysis_region": "EUNE",
        "l2_grid": (0.1, 1.0),
        "cv_folds": 2,
        "seed": 34_001,
        "calibration_bins": 4,
        "bootstrap_replicates": 5,
        "bootstrap_seed": 34_101,
        "max_iterations": 200,
        "optimizer_tolerance": 1e-9,
        "max_publication_bytes": 5_000_000,
        "minimum_free_space_reserve_bytes": 0,
    }
    values.update(updates)
    return CompositionConfig(**values)


def _stage33a(tmp_path: Path) -> Stage33AInput:
    participants = []
    matches = []
    patches = ("26.14",) * 8 + ("26.15",) * 3
    for index, patch in enumerate(patches, start=1):
        rows, context = _match(
            f"SYNTHETIC-{index}",
            patch,
            index=index,
            winner=100 if index % 2 else 200,
        )
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
    match_id: str, patch: str, *, index: int, winner: int
) -> tuple[list[ParticipantDraftObservation], MatchDraftContext]:
    first = tuple(index * 100 + value for value in (1, 2, 3, 4, 5))
    second = tuple(index * 100 + value for value in (11, 12, 13, 14, 15))
    champions = (*first, *second)
    participants = []
    for offset, champion_id in enumerate(champions):
        participant_id = offset + 1
        team_id = 100 if offset < 5 else 200
        position_index = offset % 5
        opponent_id = participant_id + 5 if team_id == 100 else participant_id - 5
        opponent = second[position_index] if team_id == 100 else first[position_index]
        allies = first if team_id == 100 else second
        enemies = second if team_id == 100 else first
        won = team_id == winner
        participants.append(
            ParticipantDraftObservation(
                match_id=match_id,
                participant_id=participant_id,
                player_key=f"synthetic-player-{index}-{participant_id}",
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
                allied_champion_ids=[value for value in allies if value != champion_id],
                enemy_champion_ids=list(enemies),
                lane_opponent_participant_id=opponent_id,
                lane_opponent_champion_id=opponent,
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
