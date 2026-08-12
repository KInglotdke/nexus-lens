from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import nexus_lens.stage34b as stage34b
import nexus_lens.stage34b_operations as stage34b_operations
import scripts.run_stage34b as stage34b_cli
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    POSITIONS,
    ChampionRoleAssignment,
    FeatureVocabulary,
    MatchDraftObservation,
)
from nexus_lens.pooled_development import PooledInput
from nexus_lens.stage34b import (
    ALL_POLICIES,
    MODEL_VARIANTS_B,
    PROTOCOL_ID,
    RESULT_SCHEMA_VERSION,
    ChronologicalFold,
    SharedInteractionModel,
    SharedModelConfig,
    Stage34BEvaluation,
    TimedDraft,
    load_stage34b_protocol,
)
from nexus_lens.stage34b_operations import (
    OPERATIONAL_AMENDMENT_SHA256,
    OperationalSource,
    Stage34BOperationalInput,
    Stage34BPreflight,
    build_publication_payloads,
    build_stage34b_preflight,
    join_draft_timestamps,
    load_operational_amendment,
    load_stage34b_operational_input,
    reconstruct_bundle_hash,
    validate_public_payload,
    write_publication_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIRECTORY = ROOT / "config/evaluation/stage3.4b-1-patch26.15-protocol-v2"
AMENDMENT_PATH = PROTOCOL_DIRECTORY / "operational-amendment-v2.json"
AMENDMENT_SCHEMA_PATH = PROTOCOL_DIRECTORY / "operational-amendment-v2.schema.json"


def test_operational_amendment_is_exact_and_expands_only_paired_comparisons() -> None:
    amendment = load_operational_amendment(
        AMENDMENT_PATH, schema_path=AMENDMENT_SCHEMA_PATH
    )

    assert amendment["frozen_baseline"]["repository_commit"] == (
        "cc5dd3ab764cf69eff4488890bbcd361e220b8df"
    )
    assert amendment["execution"]["real_data_fit_authorized_by_this_amendment"] is False
    assert amendment["elapsed_training_span_amendment"] == {
        "adequacy_principle": (
            "training adequacy is established by observation counts, class support, "
            "platform representation and strict chronology, not an arbitrary elapsed "
            "wall-clock duration"
        ),
        "failed_invocation_model_fits": 0,
        "failed_invocation_optimizer_fits": 0,
        "failed_invocation_repository_commit": (
            "f97befc992eee8314bd5b7f7e2314bc79461e8a6"
        ),
        "fold_boundaries_changed": False,
        "methodological_basis_found_for_48_hours": False,
        "model_definitions_or_outcomes_changed_or_inspected": False,
        "replacement_threshold_hours": None,
        "superseded_guard_hours": 48,
    }
    assert amendment["bootstrap_expansion"]["replicates"] == 2000
    assert len(amendment["bootstrap_expansion"]["comparison_pairs"]) == 5
    assert stage34b._sha256_json(amendment) == OPERATIONAL_AMENDMENT_SHA256


def test_timestamp_join_is_complete_utc_and_deterministic() -> None:
    drafts = (_draft("two", "euw1", 0), _draft("one", "eun1", 1))
    timestamps = {
        ("eun1", "one"): datetime(2026, 8, 1, 1, tzinfo=UTC),
        ("euw1", "two"): datetime(2026, 8, 2, 1, tzinfo=UTC),
    }

    first = join_draft_timestamps(drafts, timestamps)
    second = join_draft_timestamps(tuple(reversed(drafts)), timestamps)

    assert first == second
    assert [row.draft.match_id for row in first] == ["one", "two"]
    with pytest.raises(Stage3ValidationError, match="incomplete or extraneous"):
        join_draft_timestamps(drafts, {("eun1", "one"): timestamps[("eun1", "one")]})
    invalid = dict(timestamps)
    invalid[("euw1", "two")] = datetime(2026, 8, 2, 1)
    with pytest.raises(Stage3ValidationError, match="not UTC-aware"):
        join_draft_timestamps(drafts, invalid)


def test_preflight_constructs_every_real_inner_selection_context(
    monkeypatch,
) -> None:
    protocol = load_stage34b_protocol(
        PROTOCOL_DIRECTORY / "protocol.json",
        schema_path=PROTOCOL_DIRECTORY / "protocol.schema.json",
    )
    amendment = load_operational_amendment(
        AMENDMENT_PATH, schema_path=AMENDMENT_SCHEMA_PATH
    )
    base = tuple(
        TimedDraft(
            _draft(f"base-{index}", platform, outcome),
            datetime(2026, 7, 29, 12 + index, tzinfo=UTC),
        )
        for index, (platform, outcome) in enumerate(
            (("eun1", 0), ("eun1", 1), ("euw1", 0), ("euw1", 1))
        )
    )

    def repeated(rows, count):
        return tuple(rows[index % len(rows)] for index in range(count))

    outer_counts = (1285, 1730, 1854, 1499)
    outer_folds = tuple(
        ChronologicalFold(
            fold_id=f"outer-{index}",
            cutoff=datetime(2026, 8, 3 + 2 * index, tzinfo=UTC),
            validation_end=datetime(
                2026, 8, 5 + 2 * index if index < 3 else 12, tzinfo=UTC
            ),
            training=repeated(base, 3046 + index),
            validation=repeated(
                tuple(
                    TimedDraft(
                        row.draft, datetime(2026, 8, 3 + 2 * index, 1, tzinfo=UTC)
                    )
                    for row in base
                ),
                outer_counts[index],
            ),
        )
        for index in range(4)
    )
    monkeypatch.setattr(
        stage34b_operations,
        "construct_outer_folds",
        lambda *args, **kwargs: outer_folds,
    )
    calls = []

    def inner_folds(rows, cutoff, **kwargs):
        calls.append(cutoff)
        return tuple(
            ChronologicalFold(
                fold_id=f"inner-{index}",
                cutoff=cutoff,
                validation_end=cutoff,
                training=base,
                validation=base,
            )
            for index in range(3)
        )

    monkeypatch.setattr(stage34b_operations, "construct_inner_folds", inner_folds)
    operational = Stage34BOperationalInput(
        rows=base,
        accepted_counts={"overall": 10_000},
        eligible_counts={"eune": 4700, "euw": 4714, "overall": 9414},
        source_audit=(),
        combined_input_sha256="a" * 64,
        timestamp_join_sha256="b" * 64,
        operational_input_sha256="c" * 64,
    )

    summary = build_stage34b_preflight(
        operational_input=operational,
        scientific_protocol=protocol,
        operational_amendment=amendment,
        executable_bundle_sha256="d" * 64,
    ).summary

    assert len(calls) == 10  # five contexts, each reconstructed twice
    assert summary["inner_fold_count"] == 15
    assert summary["fit_operation_budget_reconciled"] is True
    assert summary["model_fits_executed"] == 0


def test_source_adapter_uses_hash_verified_stage31_timestamp_join(
    tmp_path: Path, monkeypatch
) -> None:
    drafts = (_draft("one", "eun1", 1), _draft("two", "euw1", 0))
    source_rows = (
        ("eune", "external", "eun1", "one"),
        ("eune", "retained_private", "eun1", "unused-eune"),
        ("euw", "external", "euw1", "two"),
        ("euw", "retained_private", "euw1", "unused-euw"),
    )
    sources = []
    source_audit = []
    for region, kind, platform, match_id in source_rows:
        source = tmp_path / f"{region}-{kind}"
        stage31 = tmp_path / f"{region}-{kind}-stage31"
        source.mkdir()
        stage31.mkdir()
        matches = stage31 / "matches.jsonl"
        matches.write_text(
            json.dumps(
                {
                    "match_id": match_id,
                    "platform": platform,
                    "game_creation": "2026-08-01T00:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source.joinpath("metadata.json").write_text(
            json.dumps(
                {
                    "inputs": {
                        "stage3_1": {
                            "directory": str(stage31),
                            "sha256": {
                                "matches.jsonl": stage34b_operations.sha256_file(
                                    matches
                                )
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        sources.append(OperationalSource(region, kind, source))
        source_audit.append(
            {
                "analysis_region": region,
                "source_kind": kind,
                "platform": platform,
                "composition_eligible_matches": int(match_id in {"one", "two"}),
            }
        )
    pooled = PooledInput(
        observations=drafts,
        combined_input_sha256="a" * 64,
        source_audit=tuple(source_audit),
        accepted_counts={"eune": 1, "euw": 1, "overall": 2},
        eligible_counts={"eune": 1, "euw": 1, "overall": 2},
        exclusion_counts={"eune": 0, "euw": 0, "overall": 0},
    )
    monkeypatch.setattr(
        stage34b_operations, "load_pooled_input", lambda **kwargs: pooled
    )
    protocol = {
        "data_scope": {
            "source_bundle_sha256": "a" * 64,
            "eligible_drafts": 2,
            "platform_counts": {"eun1": 1, "euw1": 1},
        }
    }

    result = load_stage34b_operational_input(
        scientific_protocol=protocol,
        stage34a_protocol_path=(
            ROOT
            / "config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1"
            / "protocol.json"
        ),
        sources=tuple(sources),
    )

    assert [(row.draft.platform, row.draft.match_id) for row in result.rows] == [
        ("eun1", "one"),
        ("euw1", "two"),
    ]
    assert sum(row["eligible_timestamps_joined"] for row in result.source_audit) == 2
    assert "directory" not in json.dumps(result.source_audit)


def test_expanded_bootstrap_is_paired_deterministic_and_zero_fit() -> None:
    records = tuple(
        stage34b._PredictionRow(
            private_key=(platform, f"private-{index}"),
            platform=platform,
            outer_block=f"outer-{index % 2}",
            outcome=index % 2,
            probabilities={
                policy: 0.42 + 0.01 * ((policy_index + index) % 10)
                for policy_index, policy in enumerate(ALL_POLICIES)
            },
        )
        for platform in ("eun1", "euw1")
        for index in range(8)
    )
    comparisons = (
        ("shared_allied_synergy", "composition_with_side_intercept"),
        ("combined_shared_interactions", "shared_lane_counter"),
    )

    first = stage34b.paired_bootstrap_comparisons(
        records, comparisons=comparisons, replicates=20, seed=34201
    )
    second = stage34b.paired_bootstrap_comparisons(
        records, comparisons=comparisons, replicates=20, seed=34201
    )

    assert first == second
    assert len(first) == 2
    assert all(
        values["replicates"] == 20
        and values["seed"] == 34201
        and values["orientation"] == "candidate_minus_comparator"
        for comparison in first.values()
        for values in comparison["metrics"].values()
    )


def test_instrumentation_counts_work_without_payload_data() -> None:
    events = []
    fits = stage34b._FitCounter(events.append)
    operation = fits.start(
        phase="synthetic",
        model="composition_with_side_intercept",
        optimizer_fit=True,
        inner_fold="inner-0",
    )
    fits.complete(
        operation,
        optimizer_iterations=3,
        optimizer_status="converged",
        parameter_count=11,
    )
    calibration = stage34b._CalibrationCounter(events.append)
    calibration.evaluate([0.5, 0.5], [0, 1], policy="constant", scope="overall")
    calibration.evaluate([0.4, 0.6], [0, 1], policy="varying", scope="overall")

    assert fits.count == 1
    assert fits.optimizer_fits == 1
    assert calibration.evaluations == 2
    assert calibration.optimizer_fits == 1
    assert calibration.analytic_evaluations == 1
    rendered = json.dumps(events, allow_nan=False)
    assert "private-" not in rendered
    assert "prediction" not in rendered
    assert "outcome" not in rendered


def test_atomic_publication_and_manifest_reconstruction(tmp_path: Path) -> None:
    amendment = load_operational_amendment(
        AMENDMENT_PATH, schema_path=AMENDMENT_SCHEMA_PATH
    )
    evaluation = _evaluation()
    preflight = _preflight()
    events = _reconciled_events(duration_seconds=1.25)

    payloads, expected_hash = build_publication_payloads(
        evaluation=evaluation,
        preflight=preflight,
        scientific_protocol={"protocol_id": PROTOCOL_ID},
        operational_amendment=amendment,
        repository_commit="a" * 40,
        diagnostic_events=events,
    )
    output = write_publication_bundle(payloads, tmp_path / "development-v1")

    assert reconstruct_bundle_hash(output) == expected_hash
    assert write_publication_bundle(payloads, output) == output
    assert set(path.name for path in output.iterdir()) == set(payloads)
    rendered = b"".join(payloads.values()).decode("utf-8")
    assert "private-match" not in rendered
    assert "player_key" not in rendered

    extra = output / "unexpected.txt"
    extra.write_text("unexpected", encoding="utf-8")
    with pytest.raises(Stage3ValidationError, match="file set differs"):
        reconstruct_bundle_hash(output)
    extra.unlink()

    changed = dict(payloads)
    changed["quality_report.json"] += b" "
    with pytest.raises(Stage3ValidationError, match="existing result differs"):
        write_publication_bundle(changed, output)

    with pytest.raises(Stage3ValidationError, match="do not reconcile"):
        build_publication_payloads(
            evaluation=evaluation,
            preflight=preflight,
            scientific_protocol={"protocol_id": PROTOCOL_ID},
            operational_amendment=amendment,
            repository_commit="a" * 40,
            diagnostic_events=events[:-1],
        )


def test_observational_timing_does_not_change_scientific_bundle_hash() -> None:
    amendment = load_operational_amendment(
        AMENDMENT_PATH, schema_path=AMENDMENT_SCHEMA_PATH
    )
    common = {
        "evaluation": _evaluation(),
        "preflight": _preflight(),
        "scientific_protocol": {"protocol_id": PROTOCOL_ID},
        "operational_amendment": amendment,
        "repository_commit": "a" * 40,
    }

    first, first_hash = build_publication_payloads(
        **common, diagnostic_events=_reconciled_events(duration_seconds=1.0)
    )
    second, second_hash = build_publication_payloads(
        **common, diagnostic_events=_reconciled_events(duration_seconds=9.0)
    )

    assert first_hash == second_hash
    assert first["development_results.json"] == second["development_results.json"]
    assert first["timing_summary.json"] != second["timing_summary.json"]
    first_manifest = json.loads(first["bundle_manifest.json"])
    second_manifest = json.loads(second["bundle_manifest.json"])
    assert (
        first_manifest["scientific_deterministic_bundle_sha256"]
        == (second_manifest["scientific_deterministic_bundle_sha256"])
    )
    assert (
        first_manifest["observational_file_sha256"]
        != second_manifest["observational_file_sha256"]
    )


def test_publication_privacy_rejects_identifiers_paths_and_nonfinite() -> None:
    with pytest.raises(Stage3ValidationError, match="contains identifier"):
        validate_public_payload({"match_id": "private"})
    with pytest.raises(Stage3ValidationError, match="external path"):
        validate_public_payload({"location": "E:\\private\\data"})
    with pytest.raises(Stage3ValidationError, match="non-finite"):
        validate_public_payload({"metric": float("nan")})


def test_diagnostic_log_is_privacy_checked_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "diagnostic.jsonl"
    diagnostic = stage34b_cli._DiagnosticLog(path, maximum_events=1, maximum_bytes=1000)
    try:
        diagnostic({"event": "safe", "phase": "synthetic"})
        with pytest.raises(Stage3ValidationError, match="event limit"):
            diagnostic({"event": "too-many", "phase": "synthetic"})
    finally:
        diagnostic.close()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    private_path = tmp_path / "private-diagnostic.jsonl"
    diagnostic = stage34b_cli._DiagnosticLog(
        private_path, maximum_events=2, maximum_bytes=1000
    )
    try:
        with pytest.raises(Stage3ValidationError, match="identifier"):
            diagnostic({"event": "unsafe", "match_id": "private"})
    finally:
        diagnostic.close()
    assert private_path.read_bytes() == b""


def test_cli_preflight_executes_zero_fits_and_writes_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    preflight = _preflight()
    preflight.summary.update(
        {
            "initial_training_drafts": 3046,
            "outer_blocks": [{}, {}, {}, {}],
            "combined_input_sha256": "a" * 64,
            "timestamp_join_sha256": "b" * 64,
            "outer_fold_sha256": "c" * 64,
            "inner_fold_count": 15,
            "inner_fold_sha256": "e" * 64,
            "executable_bundle_sha256": "d" * 64,
        }
    )
    monkeypatch.setattr(stage34b_cli, "_verify_git_state", lambda value: None)
    monkeypatch.setattr(stage34b_cli, "_validate_locations", lambda **kwargs: None)
    monkeypatch.setattr(
        stage34b_cli, "executable_bundle_sha256", lambda paths: "d" * 64
    )
    monkeypatch.setattr(stage34b_cli, "load_stage34b_protocol", lambda *a, **k: {})
    monkeypatch.setattr(stage34b_cli, "load_operational_amendment", lambda *a, **k: {})
    monkeypatch.setattr(
        stage34b_cli,
        "load_stage34b_operational_input",
        lambda **kwargs: preflight.operational_input,
    )
    monkeypatch.setattr(
        stage34b_cli, "build_stage34b_preflight", lambda **kwargs: preflight
    )

    def forbidden_fit(*args, **kwargs):
        raise AssertionError("preflight attempted a model fit")

    monkeypatch.setattr(stage34b_cli, "evaluate_stage34b", forbidden_fit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_stage34b.py",
            "--scientific-protocol",
            "science.json",
            "--scientific-schema",
            "science.schema.json",
            "--operational-amendment",
            "operations.json",
            "--operational-schema",
            "operations.schema.json",
            "--stage34a-protocol",
            "stage34a.json",
            "--eune-external",
            "source-1",
            "--eune-retained",
            "source-2",
            "--euw-external",
            "source-3",
            "--euw-retained",
            "source-4",
            "--output-directory",
            str(tmp_path / "development-v1"),
            "--diagnostic-log",
            str(tmp_path / "diagnostic.jsonl"),
            "--repository-commit",
            "a" * 40,
            "--preflight",
        ],
    )

    assert stage34b_cli.main() == 0
    assert "zero model fits" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def _draft(match_id: str, platform: str, outcome: int) -> MatchDraftObservation:
    return MatchDraftObservation(
        match_id=match_id,
        public_patch="26.15",
        platform=platform,
        queue_id=420,
        allied_team_id=100,
        opposing_team_id=200,
        allied=tuple(
            ChampionRoleAssignment(role, index + 1)
            for index, role in enumerate(POSITIONS)
        ),
        opposing=tuple(
            ChampionRoleAssignment(role, index + 11)
            for index, role in enumerate(POSITIONS)
        ),
        outcome=outcome,
    )


def _evaluation() -> Stage34BEvaluation:
    summary = {
        "matches": 10,
        "log_loss": 0.69,
        "brier_score": 0.249,
        "calibration_intercept": 0.0,
        "calibration_slope": 1.0,
        "calibration_slope_undefined_reason": None,
        "expected_calibration_error": 0.01,
        "coverage": 1.0,
        "prediction_mean": 0.5,
        "prediction_population_standard_deviation": 0.01,
        "prediction_minimum": 0.48,
        "prediction_maximum": 0.52,
    }
    metrics = {
        policy: {scope: dict(summary) for scope in ("overall", "eun1", "euw1")}
        for policy in ALL_POLICIES
    }
    fit_accounting = {
        "observed_predictive_training_operations": 171,
        "observed_predictive_optimizer_fits": 163,
        "observed_analytic_baseline_training_operations": 8,
        "observed_calibration_evaluations": 63,
        "observed_calibration_optimizer_fits": 52,
        "observed_calibration_analytic_evaluations": 11,
        "observed_bootstrap_model_fits": 0,
        "observed_failed_fits": 0,
        "observed_retried_fits": 0,
    }
    artifact = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "fit_accounting": fit_accounting,
        "metrics": metrics,
        "material_usefulness_evaluation": {
            candidate: {
                "passes_all": False,
                "checks": {"synthetic": False},
                "criteria": {
                    "synthetic": {
                        "required": ">=1",
                        "observed": 0,
                        "passed": False,
                        "supporting_aggregate_artifact_field": "metrics",
                    }
                },
            }
            for candidate in MODEL_VARIANTS_B
        },
        "future_holdout_gate_passed": False,
        "privacy": {
            "aggregate_only": True,
            "oof_rows_published": False,
            "match_or_player_identifiers_published": False,
            "external_paths_published": False,
        },
    }
    vocabulary = FeatureVocabulary(keys=(), index={}, include_lane_matchups=False)
    models = {
        candidate: SharedInteractionModel(
            variant=candidate,
            config=SharedModelConfig("synthetic", 0.1, 0.0, 0, 0),
            vocabulary=vocabulary,
            embedding_feature_indexes=(),
            intercept=0.0,
            composition_coefficients=(),
            synergy_embeddings=(),
            counter_attack_embeddings=(),
            counter_defense_embeddings=(),
            optimizer_iterations=1,
            optimizer_status="converged",
        )
        for candidate in MODEL_VARIANTS_B
    }
    return Stage34BEvaluation(artifact=artifact, final_candidate_models=models)


def _preflight() -> Stage34BPreflight:
    operational_input = Stage34BOperationalInput(
        rows=(),
        accepted_counts={"overall": 10_000},
        eligible_counts={"eune": 4700, "euw": 4714, "overall": 9414},
        source_audit=(),
        combined_input_sha256="a" * 64,
        timestamp_join_sha256="b" * 64,
        operational_input_sha256="c" * 64,
    )
    summary = {
        "schema_version": "stage3.4b-1-preflight-v2",
        "eligible_counts": operational_input.eligible_counts,
        "outer_evaluation_drafts": 6368,
        "source_audit": (),
    }
    return Stage34BPreflight(summary=summary, operational_input=operational_input)


def _reconciled_events(*, duration_seconds: float) -> tuple[dict, ...]:
    events = []
    for index in range(171):
        optimizer_fit = index < 163
        events.append(
            {
                "event": "predictive_training_operation_completed",
                "phase": "synthetic",
                "model": "synthetic",
                "optimizer_fit": optimizer_fit,
                "optimizer_status": "converged" if optimizer_fit else "analytic",
                "duration_seconds": duration_seconds,
                "elapsed_wall_seconds": duration_seconds,
            }
        )
    for index in range(63):
        optimizer_fit = index < 52
        events.append(
            {
                "event": "calibration_evaluation_completed",
                "phase": "calibration",
                "optimizer_fit": optimizer_fit,
                "optimizer_status": (
                    "converged" if optimizer_fit else "analytic_constant_logit"
                ),
                "duration_seconds": duration_seconds,
                "elapsed_wall_seconds": duration_seconds,
            }
        )
    return tuple(events)
