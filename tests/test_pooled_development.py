from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import nexus_lens.pooled_development as pooled_modeling
from nexus_lens.composition_modeling import (
    build_match_draft_corpus,
    fit_composition_model,
)
from nexus_lens.pooled_development import (
    EXPECTED_PROTOCOL_TAG_COMMIT,
    EXPECTED_PROTOCOL_TAG_OBJECT,
    PooledDevelopmentConfig,
    PooledInput,
    build_pooled_development_result,
    construct_fold_plan,
    select_l2_on_plan,
    write_pooled_development_result,
)
from tests.test_composition_modeling import _stage33a

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = (
    ROOT
    / "config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1/protocol.json"
)


def test_stratified_match_folds_are_balanced_and_deterministic(tmp_path: Path) -> None:
    drafts = _pooled_drafts(tmp_path)

    first = construct_fold_plan(drafts, fold_count=5, seed=34_001, scope_label="outer")
    second = construct_fold_plan(drafts, fold_count=5, seed=34_001, scope_label="outer")

    assert first == second
    assert first.fold_counts == {str(index): 8 for index in range(5)}
    assert all(
        values == {str(index): 2 for index in range(5)}
        for values in first.stratum_fold_counts.values()
    )
    assert len(first.assignments) == len(drafts)


def test_selection_is_fold_local_and_platform_is_not_a_feature(tmp_path: Path) -> None:
    drafts = _pooled_drafts(tmp_path)
    plan = construct_fold_plan(drafts, fold_count=4, seed=34_001, scope_label="inner")

    selected, results = select_l2_on_plan(
        drafts,
        plan=plan,
        variant="composition_plus_lane_matchups",
        l2_candidates=(0.1, 1.0),
        max_iterations=200,
        tolerance=1e-9,
    )

    assert selected == 1.0
    assert [row["l2_strength"] for row in results] == [0.1, 1.0]
    training = pooled_modeling._partition(drafts, plan, 0, False)
    validation = pooled_modeling._partition(drafts, plan, 0, True)
    model = fit_composition_model(
        training,
        variant="composition_plus_lane_matchups",
        l2_strength=selected,
        max_iterations=200,
        tolerance=1e-9,
    )
    assert all(
        key[0] in {"composition", "lane_matchup"}
        for key in model.vocabulary.keys
    )
    assert all(
        "eun" not in str(key) and "euw" not in str(key)
        for key in model.vocabulary.keys
    )

    changed = replace(
        validation[0],
        allied=(
            replace(validation[0].allied[0], champion_id=999_999),
            *validation[0].allied[1:],
        ),
        outcome=1 - validation[0].outcome,
    )
    assert (
        "composition",
        changed.allied[0].role,
        999_999,
        0,
    ) not in model.vocabulary.index


def test_pooled_development_is_deterministic_private_and_immutable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    drafts = _pooled_drafts(tmp_path)
    pooled = PooledInput(
        observations=drafts,
        combined_input_sha256=pooled_modeling._combined_input_sha256(drafts),
        source_audit=(),
        accepted_counts={"eune": 20, "euw": 20, "overall": 40},
        eligible_counts={"eune": 20, "euw": 20, "overall": 40},
        exclusion_counts={"eune": 0, "euw": 0, "overall": 0},
    )
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    config = PooledDevelopmentConfig(
        repository_commit="a" * 40,
        protocol_tag_object=EXPECTED_PROTOCOL_TAG_OBJECT,
        protocol_tag_commit=EXPECTED_PROTOCOL_TAG_COMMIT,
        max_iterations=200,
    )
    protocol["optimizer"]["maximum_iterations"] = 200
    real_fit = pooled_modeling.fit_composition_model
    real_prediction_records = pooled_modeling._prediction_records
    fit_calls = 0
    fit_schedule = []
    prediction_batches = []

    def counted_fit(*args, **kwargs):
        nonlocal fit_calls
        fit_calls += 1
        fit_schedule.append(
            (
                kwargs["variant"],
                kwargs["l2_strength"],
                kwargs["max_iterations"],
                kwargs["tolerance"],
                len(args[0]),
            )
        )
        return real_fit(*args, **kwargs)

    def recorded_predictions(*args, **kwargs):
        rows = real_prediction_records(*args, **kwargs)
        prediction_batches.append(
            tuple(
                (row.platform, row.match_id, row.outcome, row.probability)
                for row in rows
            )
        )
        return rows

    monkeypatch.setattr(pooled_modeling, "fit_composition_model", counted_fit)
    monkeypatch.setattr(pooled_modeling, "_prediction_records", recorded_predictions)
    progress_events: list[dict[str, object]] = []

    first = build_pooled_development_result(
        pooled=pooled,
        protocol=protocol,
        protocol_path=PROTOCOL_PATH,
        output_directory=tmp_path / "results",
        config=config,
        progress_callback=progress_events.append,
    )
    assert fit_calls == 112
    first_fit_schedule = tuple(fit_schedule)
    first_prediction_batches = tuple(prediction_batches)
    assert len(
        [row for row in progress_events if row["event"] == "model_fit_completed"]
    ) == 112
    bootstrap_start = next(
        index
        for index, row in enumerate(progress_events)
        if row["event"] == "bootstrap_started"
    )
    bootstrap_end = next(
        index
        for index, row in enumerate(progress_events)
        if row["event"] == "bootstrap_completed"
    )
    assert not any(
        row["event"] == "model_fit_started"
        for row in progress_events[bootstrap_start : bootstrap_end + 1]
    )
    second = build_pooled_development_result(
        pooled=pooled,
        protocol=protocol,
        protocol_path=PROTOCOL_PATH,
        output_directory=tmp_path / "results",
        config=config,
    )
    assert fit_calls == 224
    assert tuple(fit_schedule[112:]) == first_fit_schedule
    assert tuple(prediction_batches[len(first_prediction_batches) :]) == (
        first_prediction_batches
    )

    assert first.deterministic_bundle_sha256 == second.deterministic_bundle_sha256
    assert first.execution_record["current_tracemalloc_bytes"] is None
    assert first.execution_record["peak_tracemalloc_bytes"] is None
    assert first.execution_record["memory_measurement"].startswith("not_collected")
    assert first.metrics == second.metrics
    assert first.final_models == second.final_models
    assert first.experiment_manifest == second.experiment_manifest
    assert all(
        row["selected_l2"] == 1.0 for row in first.metrics["final_l2_selection"]
    )
    rendered = json.dumps(
        [
            first.metrics,
            first.final_models,
            first.quality_report,
            first.experiment_manifest,
        ]
    )
    assert "SYNTHETIC" not in rendered
    assert "player_key" not in rendered
    assert (
        first.quality_report["leakage_controls"]["platform_predictive_feature"]
        is False
    )

    fits_before_publication = fit_calls
    publication_events: list[dict[str, object]] = []
    write_pooled_development_result(
        first, progress_callback=publication_events.append
    )
    assert fit_calls == fits_before_publication
    assert [row["event"] for row in publication_events] == [
        "publication_started",
        "publication_payloads_serialized",
        "publication_completed",
    ]
    before = {
        path.name: path.read_bytes()
        for path in first.output_directory.iterdir()
        if path.name != "execution.json"
    }
    write_pooled_development_result(second)
    assert before == {
        path.name: path.read_bytes()
        for path in first.output_directory.iterdir()
        if path.name != "execution.json"
    }


def _pooled_drafts(tmp_path: Path):
    base = build_match_draft_corpus(_stage33a(tmp_path)).observations[0]
    return tuple(
        replace(
            base,
            match_id=f"SYNTHETIC-{index:03d}",
            public_patch="26.15",
            platform="eun1" if index < 20 else "euw1",
            outcome=index % 2,
        )
        for index in range(40)
    )
