"""Prospectively frozen pooled patch-26.15 development evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform as runtime_platform
import random
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import scipy

from nexus_lens.backtesting import PredictionRecord, calculate_metrics
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    MODEL_VARIANTS,
    POSITIONS,
    FittedCompositionModel,
    MatchDraftObservation,
    build_match_draft_corpus,
    fit_composition_model,
    predict_probability,
)
from nexus_lens.data_seal import inventory_tree
from nexus_lens.draft_aggregation import load_stage3_3a_input

PROTOCOL_SCHEMA_VERSION = "stage3.4a-pooled-development-protocol-v1"
RESULT_SCHEMA_VERSION = "stage3.4a-pooled-development-results-v1"
MODEL_SCHEMA_VERSION = "stage3.4a-pooled-development-model-v1"
QUALITY_SCHEMA_VERSION = "stage3.4a-pooled-development-quality-v1"
EXPERIMENT_SCHEMA_VERSION = "stage3.4a-pooled-development-experiment-v1"
CODE_VERSION = "stage3.4a-pooled-development-engine-v1"
EXPECTED_PROTOCOL_TAG_COMMIT = "23ef5b99f6a644daac44b2c849717bcb6c952860"
EXPECTED_PROTOCOL_TAG_OBJECT = "e627a5ebd3529b07866ce49944f4260f55f25798"
TARGET_PATCH = "26.15"
QUEUE_ID = 420
PLATFORMS = ("eun1", "euw1")
L2_CANDIDATES = (0.1, 1.0)
TIE_TOLERANCE = 1e-12
DETERMINISTIC_FILES = (
    "development_metrics.json",
    "final_models.json",
    "quality_report.json",
    "experiment_manifest.json",
    "development_report.md",
)


@dataclass(frozen=True)
class RuntimeSource:
    analysis_region: str
    source_kind: str
    input_directory: Path


@dataclass(frozen=True)
class PooledDevelopmentConfig:
    repository_commit: str
    protocol_tag_object: str
    protocol_tag_commit: str
    outer_folds: int = 5
    inner_folds: int = 4
    final_selection_folds: int = 5
    seed: int = 34_001
    bootstrap_seed: int = 34_101
    bootstrap_replicates: int = 200
    calibration_bins: int = 10
    max_iterations: int = 500
    optimizer_tolerance: float = 1e-9
    max_publication_bytes: int = 10_000_000


@dataclass(frozen=True)
class PooledInput:
    observations: tuple[MatchDraftObservation, ...]
    combined_input_sha256: str
    source_audit: tuple[dict[str, Any], ...]
    accepted_counts: dict[str, int]
    eligible_counts: dict[str, int]
    exclusion_counts: dict[str, int]


@dataclass(frozen=True)
class FoldPlan:
    assignments: dict[tuple[str, str], int]
    fingerprint_sha256: str
    fold_counts: dict[str, int]
    stratum_fold_counts: dict[str, dict[str, int]]
    scope_label: str


@dataclass
class PooledDevelopmentResult:
    output_directory: Path
    metrics: dict[str, Any]
    final_models: dict[str, Any]
    quality_report: dict[str, Any]
    experiment_manifest: dict[str, Any]
    markdown_report: str
    execution_record: dict[str, Any]
    deterministic_bundle_sha256: str


def load_pooled_input(
    *,
    protocol: dict[str, Any],
    runtime_sources: tuple[RuntimeSource, ...],
) -> PooledInput:
    """Load four verified sources and construct one private in-memory union."""

    declared_sources = {
        (item["analysis_region"], item["source_kind"]): item
        for item in protocol["sealed_sources"]
    }
    runtime_keys = {
        (item.analysis_region, item.source_kind) for item in runtime_sources
    }
    if runtime_keys != set(declared_sources) or len(runtime_sources) != 4:
        raise Stage3ValidationError(
            "pooled_source_set_conflict", "runtime source set differs from protocol"
        )

    selected_observations: list[MatchDraftObservation] = []
    source_audit = []
    accepted_by_region: Counter[str] = Counter()
    eligible_by_region: Counter[str] = Counter()
    excluded_by_region: Counter[str] = Counter()
    accepted_keys_by_component: dict[tuple[str, str], set[tuple[str, str]]] = {}
    all_keys: set[tuple[str, str]] = set()

    for source in sorted(
        runtime_sources, key=lambda item: (item.analysis_region, item.source_kind)
    ):
        declared = declared_sources[(source.analysis_region, source.source_kind)]
        row_counts = _stage33a_row_counts(source.input_directory)
        stage33a = load_stage3_3a_input(
            source.input_directory,
            expected_participant_count=row_counts["participants"],
            expected_team_count=row_counts["teams"],
            expected_match_count=row_counts["matches"],
        )
        corpus = build_match_draft_corpus(stage33a)
        expected_platform = str(declared["platform"])
        if corpus.platform != expected_platform:
            raise Stage3ValidationError(
                "pooled_source_platform_conflict",
                "source platform differs from prospective protocol",
            )
        _verify_source_hashes(source.input_directory, stage33a.lineage_hashes, declared)

        accepted = {
            (platform, match_id)
            for match_id, (patch, platform) in corpus.match_scope.items()
            if patch == TARGET_PATCH
        }
        eligible = tuple(
            row for row in corpus.observations if row.public_patch == TARGET_PATCH
        )
        if any(
            row.queue_id != QUEUE_ID or row.platform != expected_platform
            for row in eligible
        ):
            raise Stage3ValidationError(
                "pooled_source_scope_conflict",
                "eligible source row has wrong queue or platform",
            )
        expected_accepted = int(declared["accepted_matches"])
        expected_eligible = int(declared["expected_composition_eligible_matches"])
        if len(accepted) != expected_accepted or len(eligible) != expected_eligible:
            raise Stage3ValidationError(
                "pooled_source_count_conflict",
                "source accepted or eligible count differs from protocol",
            )
        if all_keys & accepted:
            raise Stage3ValidationError(
                "pooled_duplicate_match", "a match occurs in multiple source components"
            )
        all_keys.update(accepted)
        accepted_keys_by_component[
            (source.analysis_region, source.source_kind)
        ] = accepted
        selected_observations.extend(eligible)
        accepted_by_region[source.analysis_region] += len(accepted)
        eligible_by_region[source.analysis_region] += len(eligible)
        excluded_by_region[source.analysis_region] += len(accepted) - len(eligible)
        source_audit.append(
            {
                "analysis_region": source.analysis_region,
                "source_kind": source.source_kind,
                "platform": expected_platform,
                "input_match_count": row_counts["matches"],
                "accepted_patch_26_15_matches": len(accepted),
                "composition_eligible_matches": len(eligible),
                "excluded_matches": len(accepted) - len(eligible),
                "lineage_sha256": dict(sorted(stage33a.lineage_hashes.items())),
                "declared_tree_sha256": declared.get("tree_sha256"),
            }
        )

    for region in ("eune", "euw"):
        external = accepted_keys_by_component[(region, "external")]
        retained = accepted_keys_by_component[(region, "retained_private")]
        if external & retained:
            raise Stage3ValidationError(
                "pooled_external_retained_overlap",
                "external and retained match sets overlap",
            )

    observations = tuple(
        sorted(selected_observations, key=lambda row: (row.platform, row.match_id))
    )
    expected = protocol["combined_input"]["expected_counts"]
    if len(all_keys) != int(expected["accepted_matches"]) or len(observations) != int(
        expected["composition_eligible_matches"]
    ):
        raise Stage3ValidationError(
            "pooled_combined_count_conflict", "combined counts differ from protocol"
        )
    if set(accepted_by_region) != {"eune", "euw"}:
        raise Stage3ValidationError(
            "pooled_region_conflict", "combined input does not contain both regions"
        )
    return PooledInput(
        observations=observations,
        combined_input_sha256=_combined_input_sha256(observations),
        source_audit=tuple(source_audit),
        accepted_counts={
            **dict(sorted(accepted_by_region.items())),
            "overall": len(all_keys),
        },
        eligible_counts={
            **dict(sorted(eligible_by_region.items())),
            "overall": len(observations),
        },
        exclusion_counts={
            **dict(sorted(excluded_by_region.items())),
            "overall": len(all_keys) - len(observations),
        },
    )


def construct_fold_plan(
    drafts: tuple[MatchDraftObservation, ...],
    *,
    fold_count: int,
    seed: int,
    scope_label: str,
) -> FoldPlan:
    """Assign platform/outcome strata to deterministic match-grouped folds."""

    if fold_count < 2 or len(drafts) < fold_count:
        raise ValueError("fold construction requires at least two nonempty folds")
    strata: dict[tuple[str, int], list[MatchDraftObservation]] = defaultdict(list)
    keys = set()
    for draft in drafts:
        key = _group_key(draft)
        if key in keys:
            raise Stage3ValidationError(
                "pooled_duplicate_observation",
                "one match creates multiple observations",
            )
        keys.add(key)
        if draft.platform not in PLATFORMS or draft.outcome not in {0, 1}:
            raise Stage3ValidationError(
                "pooled_stratum_conflict", "fold stratum value is invalid"
            )
        strata[(draft.platform, draft.outcome)].append(draft)

    assignments: dict[tuple[str, str], int] = {}
    stratum_fold_counts: dict[str, dict[str, int]] = {}
    for (platform, outcome), rows in sorted(strata.items()):
        ordered = sorted(
            rows,
            key=lambda row: (
                hashlib.sha256(
                    f"{scope_label}|{seed}|{_private_group_identity(row)}".encode()
                ).digest(),
                row.match_id,
            ),
        )
        label = f"{platform}|target={outcome}"
        counts: Counter[int] = Counter()
        for index, row in enumerate(ordered):
            fold = index % fold_count
            assignments[_group_key(row)] = fold
            counts[fold] += 1
        stratum_fold_counts[label] = {
            str(index): counts[index] for index in range(fold_count)
        }
    fold_counts = Counter(assignments.values())
    if any(fold_counts[index] == 0 for index in range(fold_count)):
        raise Stage3ValidationError(
            "pooled_empty_fold", "deterministic fold assignment produced an empty fold"
        )
    return FoldPlan(
        assignments=assignments,
        fingerprint_sha256=_fold_fingerprint(assignments),
        fold_counts={str(index): fold_counts[index] for index in range(fold_count)},
        stratum_fold_counts=stratum_fold_counts,
        scope_label=scope_label,
    )


def build_pooled_development_result(
    *,
    pooled: PooledInput,
    protocol: dict[str, Any],
    protocol_path: Path,
    output_directory: Path,
    config: PooledDevelopmentConfig,
) -> PooledDevelopmentResult:
    """Run deterministic nested development evaluation and final all-data fitting."""

    _validate_protocol(protocol, config)
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    try:
        outer_plan = construct_fold_plan(
            pooled.observations,
            fold_count=config.outer_folds,
            seed=config.seed,
            scope_label="outer",
        )
        oof_records: dict[str, list[PredictionRecord]] = {
            variant: [] for variant in MODEL_VARIANTS
        }
        outer_results = []
        unseen = {variant: _empty_unseen_counter() for variant in MODEL_VARIANTS}
        for outer_index in range(config.outer_folds):
            training = _partition(pooled.observations, outer_plan, outer_index, False)
            validation = _partition(pooled.observations, outer_plan, outer_index, True)
            inner_plan = construct_fold_plan(
                training,
                fold_count=config.inner_folds,
                seed=config.seed,
                scope_label=f"inner-outer-{outer_index}",
            )
            model_rows = []
            for variant in MODEL_VARIANTS:
                selected, candidates = select_l2_on_plan(
                    training,
                    plan=inner_plan,
                    variant=variant,
                    l2_candidates=L2_CANDIDATES,
                    max_iterations=config.max_iterations,
                    tolerance=config.optimizer_tolerance,
                )
                model = fit_composition_model(
                    training,
                    variant=variant,
                    l2_strength=selected,
                    max_iterations=config.max_iterations,
                    tolerance=config.optimizer_tolerance,
                )
                records = _prediction_records(variant, model, validation)
                oof_records[variant].extend(records)
                _accumulate_unseen(unseen[variant], model, validation)
                model_rows.append(
                    {
                        "variant": variant,
                        "selected_l2": selected,
                        "inner_candidates": candidates,
                        "outer_model_vocabulary_sha256": _vocabulary_sha256(model),
                        "outer_model_coefficient_sha256": _coefficient_sha256(model),
                        "outer_model_coefficient_count": len(model.coefficients),
                        "outer_model_optimizer_iterations": model.optimizer_iterations,
                        "outer_model_optimizer_status": model.optimizer_status,
                    }
                )
            outer_results.append(
                {
                    "outer_fold": outer_index,
                    "training_matches": len(training),
                    "validation_matches": len(validation),
                    "validation_class_balance": _class_balance(validation),
                    "inner_fold_fingerprint_sha256": inner_plan.fingerprint_sha256,
                    "inner_fold_counts": inner_plan.fold_counts,
                    "models": model_rows,
                }
            )

        ordered_records = {
            variant: sorted(rows, key=lambda row: (row.platform, row.match_id))
            for variant, rows in oof_records.items()
        }
        if any(
            len(rows) != len(pooled.observations)
            for rows in ordered_records.values()
        ):
            raise Stage3ValidationError(
                "pooled_oof_coverage_conflict",
                "OOF predictions do not cover every draft",
            )
        development_metrics = _development_metrics(
            ordered_records,
            calibration_bins=config.calibration_bins,
            bootstrap_replicates=config.bootstrap_replicates,
            bootstrap_seed=config.bootstrap_seed,
        )
        final_plan = construct_fold_plan(
            pooled.observations,
            fold_count=config.final_selection_folds,
            seed=config.seed,
            scope_label="final-selection",
        )
        final_model_rows = []
        final_selection = []
        for variant in MODEL_VARIANTS:
            selected, candidates = select_l2_on_plan(
                pooled.observations,
                plan=final_plan,
                variant=variant,
                l2_candidates=L2_CANDIDATES,
                max_iterations=config.max_iterations,
                tolerance=config.optimizer_tolerance,
            )
            model = fit_composition_model(
                pooled.observations,
                variant=variant,
                l2_strength=selected,
                max_iterations=config.max_iterations,
                tolerance=config.optimizer_tolerance,
            )
            final_selection.append(
                {
                    "variant": variant,
                    "selected_l2": selected,
                    "candidates": candidates,
                }
            )
            final_model_rows.append(_serialize_model(model))

        metrics = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "status": "patch_26_15_nested_cv_development_estimate_not_final_test",
            "queue_id": QUEUE_ID,
            "public_patch": TARGET_PATCH,
            "accepted_counts": pooled.accepted_counts,
            "eligible_counts": pooled.eligible_counts,
            "excluded_counts": pooled.exclusion_counts,
            "class_balance": _class_balance(pooled.observations),
            "outer_fold_fingerprint_sha256": outer_plan.fingerprint_sha256,
            "outer_fold_counts": outer_plan.fold_counts,
            "outer_stratum_fold_counts": outer_plan.stratum_fold_counts,
            "outer_folds": outer_results,
            "out_of_fold_metrics": development_metrics,
            "unseen_feature_coverage": {
                variant: _finalize_unseen_counter(values)
                for variant, values in unseen.items()
            },
            "final_selection_fold_fingerprint_sha256": final_plan.fingerprint_sha256,
            "final_selection_fold_counts": final_plan.fold_counts,
            "final_l2_selection": final_selection,
        }
        final_models = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "status": "locked_patch_26_15_development_models_for_future_26_16",
            "training_patch": TARGET_PATCH,
            "future_evaluation_patch": "26.16",
            "training_match_count": len(pooled.observations),
            "combined_input_sha256": pooled.combined_input_sha256,
            "platform_predictive_feature": False,
            "target": "lower_numeric_team_wins",
            "models": final_model_rows,
        }
        quality = _quality_report(pooled, metrics, final_models)
        report = _markdown_report(metrics, quality)
        deterministic_payloads = {
            "development_metrics.json": _json_bytes(metrics),
            "final_models.json": _json_bytes(final_models),
            "quality_report.json": _json_bytes(quality),
            "development_report.md": report.encode("utf-8"),
        }
        experiment_manifest = _experiment_manifest(
            pooled=pooled,
            protocol=protocol,
            protocol_path=protocol_path,
            config=config,
            output_hashes={
                name: _sha256_bytes(payload)
                for name, payload in sorted(deterministic_payloads.items())
            },
            metrics=metrics,
            final_models=final_models,
        )
        deterministic_payloads["experiment_manifest.json"] = _json_bytes(
            experiment_manifest
        )
        deterministic_bundle = _bundle_sha256(deterministic_payloads)
        execution = {
            "schema_version": "stage3.4a-pooled-development-execution-v1",
            "repository_commit": config.repository_commit,
            "deterministic_bundle_sha256": deterministic_bundle,
            "wall_seconds": time.perf_counter() - started_wall,
            "process_cpu_seconds": time.process_time() - started_cpu,
            "memory_measurement": (
                "not_collected; whole-run Python allocation tracing is excluded "
                "because it can dominate sparse optimizer runtime"
            ),
            "current_tracemalloc_bytes": None,
            "peak_tracemalloc_bytes": None,
        }
    finally:
        pass
    _reject_nonfinite(
        {
            "metrics": metrics,
            "final_models": final_models,
            "quality": quality,
            "manifest": experiment_manifest,
            "execution": execution,
        }
    )
    return PooledDevelopmentResult(
        output_directory=output_directory,
        metrics=metrics,
        final_models=final_models,
        quality_report=quality,
        experiment_manifest=experiment_manifest,
        markdown_report=report,
        execution_record=execution,
        deterministic_bundle_sha256=deterministic_bundle,
    )


def select_l2_on_plan(
    drafts: tuple[MatchDraftObservation, ...],
    *,
    plan: FoldPlan,
    variant: str,
    l2_candidates: tuple[float, ...],
    max_iterations: int,
    tolerance: float,
) -> tuple[float, list[dict[str, Any]]]:
    """Select L2 using only the supplied training partition and fold plan."""

    fold_indexes = sorted(set(plan.assignments.values()))
    results = []
    for strength in sorted(l2_candidates):
        losses: list[float] = []
        fold_rows = []
        for fold_index in fold_indexes:
            training = _partition(drafts, plan, fold_index, False)
            validation = _partition(drafts, plan, fold_index, True)
            if not training or not validation:
                raise Stage3ValidationError(
                    "pooled_empty_selection_partition",
                    "L2 selection partition is empty",
                )
            model = fit_composition_model(
                training,
                variant=variant,
                l2_strength=strength,
                max_iterations=max_iterations,
                tolerance=tolerance,
            )
            fold_losses = [
                _binary_log_loss(predict_probability(model, row), row.outcome)
                for row in validation
            ]
            losses.extend(fold_losses)
            fold_rows.append(
                {
                    "fold": fold_index,
                    "training_matches": len(training),
                    "validation_matches": len(validation),
                    "mean_validation_log_loss": mean(fold_losses),
                    "vocabulary_sha256": _vocabulary_sha256(model),
                    "coefficient_count": len(model.coefficients),
                    "optimizer_iterations": model.optimizer_iterations,
                    "optimizer_status": model.optimizer_status,
                }
            )
        results.append(
            {
                "l2_strength": strength,
                "mean_validation_log_loss": mean(losses),
                "validation_matches": len(losses),
                "folds": fold_rows,
            }
        )
    best = min(row["mean_validation_log_loss"] for row in results)
    tied = [
        row
        for row in results
        if math.isclose(
            row["mean_validation_log_loss"], best, rel_tol=0, abs_tol=TIE_TOLERANCE
        )
    ]
    return max(float(row["l2_strength"]) for row in tied), results


def write_pooled_development_result(result: PooledDevelopmentResult) -> Path:
    """Publish deterministic artifacts atomically; never replace unequal output."""

    payloads = _result_payloads(result)
    total_bytes = sum(len(value) for value in payloads.values())
    if total_bytes > 10_000_000:
        raise Stage3ValidationError(
            "pooled_publication_too_large", "safe JSON publication exceeds 10 MB"
        )
    target = result.output_directory
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload)
        if target.exists():
            for name in DETERMINISTIC_FILES:
                if (
                    not (target / name).is_file()
                    or (target / name).read_bytes() != payloads[name]
                ):
                    raise Stage3ValidationError(
                        "immutable_pooled_output_conflict",
                        "existing deterministic pooled result differs",
                    )
            return target
        os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_protocol(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Stage3ValidationError(
            "pooled_protocol_unreadable", "pooled protocol cannot be loaded"
        ) from error
    if not isinstance(payload, dict):
        raise Stage3ValidationError(
            "pooled_protocol_malformed", "pooled protocol must be an object"
        )
    return payload


def _validate_protocol(
    protocol: dict[str, Any], config: PooledDevelopmentConfig
) -> None:
    if protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "pooled_protocol_schema_conflict", "unexpected pooled protocol schema"
        )
    if protocol.get("status") != "prospectively_frozen_before_pooled_model_fitting":
        raise Stage3ValidationError(
            "pooled_protocol_status_conflict", "pooled protocol is not prospective"
        )
    cv = protocol["cross_validation"]
    expected = (
        cv["outer"]["folds"],
        cv["inner"]["folds"],
        cv["final_development_selection"]["folds"],
        cv["seed"],
        tuple(cv["inner"]["l2_candidates"]),
    )
    actual = (
        config.outer_folds,
        config.inner_folds,
        config.final_selection_folds,
        config.seed,
        L2_CANDIDATES,
    )
    if expected != actual:
        raise Stage3ValidationError(
            "pooled_protocol_parameter_conflict",
            "runtime split or selection parameters differ from protocol",
        )
    if not _sha40(config.protocol_tag_commit) or not _sha40(config.repository_commit):
        raise Stage3ValidationError(
            "pooled_commit_invalid", "repository or protocol commit is invalid"
        )
    if not _sha40(config.protocol_tag_object):
        raise Stage3ValidationError(
            "pooled_tag_object_invalid", "protocol tag object is invalid"
        )
    if (
        config.protocol_tag_commit != EXPECTED_PROTOCOL_TAG_COMMIT
        or config.protocol_tag_object != EXPECTED_PROTOCOL_TAG_OBJECT
    ):
        raise Stage3ValidationError(
            "pooled_protocol_tag_conflict",
            "protocol tag object or peeled commit differs",
        )
    if (
        config.bootstrap_replicates != protocol["randomness"]["bootstrap_replicates"]
        or config.bootstrap_seed != protocol["randomness"]["bootstrap_seed"]
        or config.calibration_bins != protocol["metrics"]["calibration_bins"]
        or config.max_iterations != protocol["optimizer"]["maximum_iterations"]
        or config.optimizer_tolerance != protocol["optimizer"]["tolerance"]
    ):
        raise Stage3ValidationError(
            "pooled_protocol_metric_optimizer_conflict",
            "runtime metric or optimizer settings differ from protocol",
        )


def _stage33a_row_counts(path: Path) -> dict[str, int]:
    try:
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        counts = metadata["row_counts"]
        output = {
            "matches": int(counts["match_draft_context"]),
            "participants": int(counts["participant_draft_observations"]),
            "teams": int(counts["team_draft_observations"]),
        }
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise Stage3ValidationError(
            "pooled_source_metadata_invalid", "source row counts are unavailable"
        ) from error
    if (
        output["participants"] != output["matches"] * 10
        or output["teams"] != output["matches"] * 2
    ):
        raise Stage3ValidationError(
            "pooled_source_shape_conflict", "source Stage 3.3A shape differs"
        )
    return output


def _verify_source_hashes(
    path: Path, actual_hashes: dict[str, str], declared: dict[str, Any]
) -> None:
    if "tree_sha256" in declared:
        inventory = inventory_tree(path)
        if inventory.sha256 != declared["tree_sha256"]:
            raise Stage3ValidationError(
                "pooled_source_tree_hash_conflict", "sealed source tree differs"
            )
    else:
        expected = declared.get("declared_file_sha256")
        if not isinstance(expected, dict) or dict(
            sorted(actual_hashes.items())
        ) != dict(sorted(expected.items())):
            raise Stage3ValidationError(
                "pooled_source_file_hash_conflict", "retained source hashes differ"
            )


def _partition(
    drafts: tuple[MatchDraftObservation, ...],
    plan: FoldPlan,
    held_out_fold: int,
    select_held_out: bool,
) -> tuple[MatchDraftObservation, ...]:
    return tuple(
        row
        for row in drafts
        if (plan.assignments[_group_key(row)] == held_out_fold) == select_held_out
    )


def _prediction_records(
    variant: str,
    model: FittedCompositionModel,
    validation: tuple[MatchDraftObservation, ...],
) -> list[PredictionRecord]:
    records = []
    for row in validation:
        probability = predict_probability(model, row)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise Stage3ValidationError(
                "pooled_invalid_prediction", "model emitted a non-finite probability"
            )
        records.append(
            PredictionRecord(
                policy=variant,
                match_id=row.match_id,
                public_patch=row.public_patch,
                platform=row.platform,
                role="MATCH",
                champion_id=0,
                outcome=row.outcome,
                probability=probability,
                training_evidence=0,
                champion_role_training_games=0,
                missing_reason=None,
            )
        )
    return records


def _development_metrics(
    records_by_variant: dict[str, list[PredictionRecord]],
    *,
    calibration_bins: int,
    bootstrap_replicates: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    output = {}
    for variant, records in records_by_variant.items():
        by_platform = {
            platform: [row for row in records if row.platform == platform]
            for platform in PLATFORMS
        }
        overall = calculate_metrics(records, calibration_bins=calibration_bins)
        platforms = {
            platform: calculate_metrics(rows, calibration_bins=calibration_bins)
            for platform, rows in by_platform.items()
        }
        output[variant] = {
            "overall": overall,
            "by_platform": platforms,
            "eune_minus_euw": _metric_difference(
                platforms["eun1"], platforms["euw1"]
            ),
        }
    intervals = _stratified_bootstrap(
        records_by_variant,
        calibration_bins=calibration_bins,
        replicates=bootstrap_replicates,
        seed=bootstrap_seed,
    )
    for variant in output:
        output[variant]["confidence_intervals"] = intervals[variant]
    return output


def _stratified_bootstrap(
    records_by_variant: dict[str, list[PredictionRecord]],
    *,
    calibration_bins: int,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    lookups = {
        variant: {(row.platform, row.match_id): row for row in records}
        for variant, records in records_by_variant.items()
    }
    reference = next(iter(lookups.values()))
    identities = {
        platform: sorted(key for key in reference if key[0] == platform)
        for platform in PLATFORMS
    }
    if any(set(lookup) != set(reference) for lookup in lookups.values()):
        raise Stage3ValidationError(
            "pooled_model_oof_set_conflict", "models evaluated different match sets"
        )
    rng = random.Random(seed)
    samples: dict[str, dict[str, dict[str, list[float]]]] = {
        variant: {
            "overall": defaultdict(list),
            "eun1": defaultdict(list),
            "euw1": defaultdict(list),
            "eune_minus_euw": defaultdict(list),
        }
        for variant in records_by_variant
    }
    for _ in range(replicates):
        sampled = {
            platform: [rng.choice(keys) for _ in keys]
            for platform, keys in identities.items()
        }
        for variant, lookup in lookups.items():
            rows_by_platform = {
                platform: [lookup[key] for key in sampled[platform]]
                for platform in PLATFORMS
            }
            metrics_by_platform = {
                platform: calculate_metrics(rows, calibration_bins=calibration_bins)
                for platform, rows in rows_by_platform.items()
            }
            overall = calculate_metrics(
                [row for platform in PLATFORMS for row in rows_by_platform[platform]],
                calibration_bins=calibration_bins,
            )
            difference = _metric_difference(
                metrics_by_platform["eun1"], metrics_by_platform["euw1"]
            )
            for scope, metrics in (
                ("overall", overall),
                ("eun1", metrics_by_platform["eun1"]),
                ("euw1", metrics_by_platform["euw1"]),
                ("eune_minus_euw", difference),
            ):
                for metric in _reported_scalar_metrics():
                    value = metrics[metric]
                    if value is not None:
                        samples[variant][scope][metric].append(float(value))
    return {
        variant: {
            scope: {
                metric: {
                    "method": "deterministic_platform_stratified_match_percentile",
                    "replicates": replicates,
                    "seed": seed,
                    "lower_0_025": _quantile(values, 0.025),
                    "upper_0_975": _quantile(values, 0.975),
                }
                for metric, values in sorted(metric_samples.items())
            }
            for scope, metric_samples in scopes.items()
        }
        for variant, scopes in samples.items()
    }


def _metric_difference(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    return {
        metric: (
            None
            if first[metric] is None or second[metric] is None
            else first[metric] - second[metric]
        )
        for metric in _reported_scalar_metrics()
    }


def _reported_scalar_metrics() -> tuple[str, ...]:
    return (
        "log_loss",
        "brier_score",
        "accuracy_at_0_5",
        "expected_calibration_error",
    )


def _class_balance(drafts: tuple[MatchDraftObservation, ...]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    scoped = [("overall", drafts)]
    scoped.extend(
        (platform, tuple(row for row in drafts if row.platform == platform))
        for platform in PLATFORMS
    )
    for label, rows in scoped:
        wins = sum(row.outcome for row in rows)
        output[label] = {
            "matches": len(rows),
            "lower_team_wins": wins,
            "lower_team_losses": len(rows) - wins,
            "lower_team_win_rate": wins / len(rows) if rows else None,
        }
    return output


def _empty_unseen_counter() -> Counter[str]:
    return Counter()


def _accumulate_unseen(
    counter: Counter[str],
    model: FittedCompositionModel,
    validation: tuple[MatchDraftObservation, ...],
) -> None:
    for draft in validation:
        champion_unseen = 0
        for assignment in (*draft.allied, *draft.opposing):
            counter["champion_role_slots"] += 1
            if (
                "composition",
                assignment.role,
                assignment.champion_id,
                0,
            ) not in model.vocabulary.index:
                champion_unseen += 1
        counter["unseen_champion_role_slots"] += champion_unseen
        counter["matches_with_unseen_champion_role"] += int(champion_unseen > 0)
        counter["matches"] += 1
        if model.variant == "composition_plus_lane_matchups":
            allied = {item.role: item.champion_id for item in draft.allied}
            opposing = {item.role: item.champion_id for item in draft.opposing}
            matchup_unseen = 0
            for role in POSITIONS:
                low, high = sorted((allied[role], opposing[role]))
                counter["lane_matchup_slots"] += 1
                if ("lane_matchup", role, low, high) not in model.vocabulary.index:
                    matchup_unseen += 1
            counter["unseen_lane_matchup_slots"] += matchup_unseen
            counter["matches_with_unseen_lane_matchup"] += int(matchup_unseen > 0)


def _finalize_unseen_counter(counter: Counter[str]) -> dict[str, Any]:
    champion_slots = counter["champion_role_slots"]
    matchup_slots = counter["lane_matchup_slots"]
    matches = counter["matches"]
    return {
        **dict(sorted(counter.items())),
        "unseen_champion_role_slot_rate": (
            counter["unseen_champion_role_slots"] / champion_slots
            if champion_slots
            else None
        ),
        "matches_with_unseen_champion_role_rate": (
            counter["matches_with_unseen_champion_role"] / matches if matches else None
        ),
        "unseen_lane_matchup_slot_rate": (
            counter["unseen_lane_matchup_slots"] / matchup_slots
            if matchup_slots
            else None
        ),
        "matches_with_unseen_lane_matchup_rate": (
            counter["matches_with_unseen_lane_matchup"] / matches
            if matchup_slots and matches
            else None
        ),
    }


def _serialize_model(model: FittedCompositionModel) -> dict[str, Any]:
    parameters = [
        {
            "feature": _feature_payload(key),
            "coefficient": coefficient,
            "training_match_count": model.feature_training_counts[index],
        }
        for index, (key, coefficient) in enumerate(
            zip(model.vocabulary.keys, model.coefficients, strict=True)
        )
    ]
    payload = {
        "variant": model.variant,
        "l2_strength": model.l2_strength,
        "optimizer": "scipy.optimize.minimize_L-BFGS-B",
        "optimizer_iterations": model.optimizer_iterations,
        "optimizer_status": model.optimizer_status,
        "training_match_set_sha256": model.training_match_set_sha256,
        "vocabulary_sha256": _vocabulary_sha256(model),
        "coefficient_sha256": _coefficient_sha256(model),
        "coefficient_count": len(model.coefficients),
        "parameters": parameters,
    }
    payload["model_sha256"] = _sha256_json(payload)
    return payload


def _feature_payload(key: tuple[str, str, int, int]) -> dict[str, Any]:
    family, role, first, second = key
    if family == "composition":
        return {"family": family, "role": role, "champion_id": first}
    return {
        "family": family,
        "role": role,
        "lower_champion_id": first,
        "higher_champion_id": second,
    }


def _quality_report(
    pooled: PooledInput, metrics: dict[str, Any], models: dict[str, Any]
) -> dict[str, Any]:
    invalid_predictions = 0
    for result in metrics["out_of_fold_metrics"].values():
        for scope in (result["overall"], *result["by_platform"].values()):
            invalid_predictions += int(
                scope["evaluated_rows"] != scope["candidate_rows"]
            )
            invalid_predictions += int(scope["log_loss"] is None)
    return {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "status": "development_only_not_final_test",
        "ready_for_future_locked_patch_26_16_evaluation": invalid_predictions == 0,
        "ready_for_recommendation_policy": False,
        "source_reconciliation": list(pooled.source_audit),
        "leakage_controls": {
            "logical_union_only": True,
            "match_grouped_outer_inner_and_final_selection": True,
            "platform_and_target_stratified": True,
            "one_oriented_observation_per_match": True,
            "mirrored_rows_present": False,
            "fold_local_vocabulary": True,
            "stage3_3b_aggregates_used": False,
            "validation_outcomes_used_in_features": False,
            "platform_predictive_feature": False,
            "team_side_predictive_feature": False,
        },
        "prediction_quality": {
            "invalid_or_nonfinite_predictions": invalid_predictions,
            "optimizer_warnings": [],
            "all_final_models_converged": all(
                row["optimizer_status"] == "converged" for row in models["models"]
            ),
        },
        "privacy": {
            "aggregate_metrics_only": True,
            "prediction_level_artifact": False,
            "match_identifiers_present": False,
            "player_identifiers_or_keys_present": False,
            "platform_is_metadata_not_feature": True,
        },
        "invariant_failures": [],
    }


def _experiment_manifest(
    *,
    pooled: PooledInput,
    protocol: dict[str, Any],
    protocol_path: Path,
    config: PooledDevelopmentConfig,
    output_hashes: dict[str, str],
    metrics: dict[str, Any],
    final_models: dict[str, Any],
) -> dict[str, Any]:
    config_payload = {
        "outer_folds": config.outer_folds,
        "inner_folds": config.inner_folds,
        "final_selection_folds": config.final_selection_folds,
        "l2_candidates": list(L2_CANDIDATES),
        "seed": config.seed,
        "bootstrap_seed": config.bootstrap_seed,
        "bootstrap_replicates": config.bootstrap_replicates,
        "calibration_bins": config.calibration_bins,
        "max_iterations": config.max_iterations,
        "optimizer_tolerance": config.optimizer_tolerance,
    }
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "status": "patch_26_15_development_not_final_test",
        "repository_commit_used_for_execution": config.repository_commit,
        "protocol": {
            "protocol_id": protocol["protocol_id"],
            "logical_path": (
                f"config/evaluation/{protocol['protocol_id']}/protocol.json"
            ),
            "sha256": _sha256_bytes(protocol_path.read_bytes()),
            "tag": protocol["prospective_tag"],
            "tag_object": config.protocol_tag_object,
            "peeled_commit": config.protocol_tag_commit,
        },
        "source_tags": protocol["source_tags"],
        "sealed_sources": list(pooled.source_audit),
        "combined_input_sha256": pooled.combined_input_sha256,
        "accepted_counts": pooled.accepted_counts,
        "eligible_counts": pooled.eligible_counts,
        "configuration": config_payload,
        "configuration_sha256": _sha256_json(config_payload),
        "fold_assignment": {
            "outer_sha256": metrics["outer_fold_fingerprint_sha256"],
            "final_selection_sha256": metrics[
                "final_selection_fold_fingerprint_sha256"
            ],
            "group": "match",
            "strata": ["platform", "binary_target"],
        },
        "target": protocol["target"],
        "features": protocol["features"],
        "models": protocol["models"],
        "selected_l2": {
            row["variant"]: row["selected_l2"]
            for row in metrics["final_l2_selection"]
        },
        "final_model_sha256": {
            row["variant"]: row["model_sha256"] for row in final_models["models"]
        },
        "software": {
            "python": runtime_platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "deterministic_output_sha256": output_hashes,
        "privacy": protocol["privacy"],
        "future_final_evaluation": protocol["future_final_evaluation"],
    }


def _markdown_report(metrics: dict[str, Any], quality: dict[str, Any]) -> str:
    lines = [
        "# Nexus Lens pooled patch-26.15 development report",
        "",
        "Status: **nested-CV development estimate; not an untouched final test**.",
        "",
        "EUNE and EUW were pooled for fitting while platform remained subgroup and "
        "stratification metadata only. Patch 26.16 was not used.",
        "",
        f"- Accepted matches: `{metrics['accepted_counts']['overall']}`",
        f"- Eligible drafts: `{metrics['eligible_counts']['overall']}`",
        f"- Excluded drafts: `{metrics['excluded_counts']['overall']}`",
        f"- Outer fold fingerprint: `{metrics['outer_fold_fingerprint_sha256']}`",
        "",
        "| Model | Scope | Log loss | Brier | Accuracy | ECE |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for variant, result in metrics["out_of_fold_metrics"].items():
        for scope, values in (
            ("overall", result["overall"]),
            ("EUNE", result["by_platform"]["eun1"]),
            ("EUW", result["by_platform"]["euw1"]),
        ):
            lines.append(
                f"| `{variant}` | {scope} | {_fmt(values['log_loss'])} | "
                f"{_fmt(values['brier_score'])} | "
                f"{_fmt(values['accuracy_at_0_5'])} | "
                f"{_fmt(values['expected_calibration_error'])} |"
            )
    lines.extend(
        [
            "",
            "Final all-development L2 selections:",
            "",
            *[
                f"- `{row['variant']}`: `{row['selected_l2']}`"
                for row in metrics["final_l2_selection"]
            ],
            "",
            "No recommendation policy was selected or implemented. Training metrics "
            "from the all-data fit are not reported as generalization evidence.",
            "",
            f"Ready for recommendation policy: "
            f"`{quality['ready_for_recommendation_policy']}`.",
            "",
        ]
    )
    return "\n".join(lines)


def _result_payloads(result: PooledDevelopmentResult) -> dict[str, bytes]:
    return {
        "development_metrics.json": _json_bytes(result.metrics),
        "final_models.json": _json_bytes(result.final_models),
        "quality_report.json": _json_bytes(result.quality_report),
        "experiment_manifest.json": _json_bytes(result.experiment_manifest),
        "development_report.md": result.markdown_report.encode("utf-8"),
        "execution.json": _json_bytes(result.execution_record),
    }


def _combined_input_sha256(drafts: tuple[MatchDraftObservation, ...]) -> str:
    digest = hashlib.sha256()
    for row in drafts:
        payload = {
            "private_group_sha256": _private_group_sha256(row),
            "platform": row.platform,
            "public_patch": row.public_patch,
            "queue_id": row.queue_id,
            "allied": [(item.role, item.champion_id) for item in row.allied],
            "opposing": [(item.role, item.champion_id) for item in row.opposing],
            "outcome": row.outcome,
        }
        digest.update(_json_bytes(payload))
    return digest.hexdigest()


def _fold_fingerprint(assignments: dict[tuple[str, str], int]) -> str:
    digest = hashlib.sha256()
    for (platform, match_id), fold in sorted(assignments.items()):
        private_hash = hashlib.sha256(
            f"{platform}\0{match_id}".encode()
        ).hexdigest()
        digest.update(f"{private_hash}\0{fold}\n".encode("ascii"))
    return digest.hexdigest()


def _group_key(draft: MatchDraftObservation) -> tuple[str, str]:
    return draft.platform, draft.match_id


def _private_group_identity(draft: MatchDraftObservation) -> str:
    return f"{draft.platform}\0{draft.match_id}"


def _private_group_sha256(draft: MatchDraftObservation) -> str:
    return hashlib.sha256(_private_group_identity(draft).encode("utf-8")).hexdigest()


def _vocabulary_sha256(model: FittedCompositionModel) -> str:
    return _sha256_json([list(key) for key in model.vocabulary.keys])


def _coefficient_sha256(model: FittedCompositionModel) -> str:
    return _sha256_json(list(model.coefficients))


def _binary_log_loss(probability: float, outcome: int) -> float:
    selected = probability if outcome else 1 - probability
    if not math.isfinite(selected) or selected <= 0:
        raise Stage3ValidationError(
            "pooled_undefined_log_loss", "prediction makes log loss undefined"
        )
    return -math.log(selected)


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bundle_sha256(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(payloads.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha40(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )


def _fmt(value: float | None) -> str:
    return "null" if value is None else f"{value:.8f}"


def _reject_nonfinite(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise Stage3ValidationError(
                "pooled_nonfinite_output", "publication contains NaN or infinity"
            )


def current_process_identity() -> dict[str, Any]:
    """Return non-sensitive execution environment facts."""

    return {
        "process_id": os.getpid(),
        "python_executable_name": Path(sys.executable).name,
    }
