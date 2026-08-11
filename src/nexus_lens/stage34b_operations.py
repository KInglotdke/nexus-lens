"""Operational source, preflight, and publication path for Stage 3.4B-1."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform as runtime_platform
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import scipy

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import MatchDraftObservation
from nexus_lens.data_seal import sha256_file
from nexus_lens.pooled_development import (
    RuntimeSource,
    load_pooled_input,
    load_protocol,
)
from nexus_lens.stage34b import (
    MODEL_VARIANTS_B,
    PROTOCOL_ID,
    PROTOCOL_SHA256,
    Stage34BEvaluation,
    TimedDraft,
    construct_outer_folds,
    validate_stage34b_artifact,
)

OPERATIONAL_SCHEMA_VERSION = "stage3.4b-1-operational-amendment-v1"
OPERATIONAL_PROTOCOL_ID = "stage3.4b-1-patch26.15-operational-amendment-v1"
OPERATIONAL_AMENDMENT_SHA256 = (
    "88586a85aa81dfe292d8a8b8bb5476599752069bc7de20c1aaf956cc4b95e69d"
)
OPERATIONAL_SCHEMA_SHA256 = (
    "76b0a8063b5fae1a9c2c04c45f4f3d0e12ba3a3648efc1049404c9e4b439ba69"
)
STAGE34A_PROTOCOL_SHA256 = (
    "ac7b9f221b98a6e9b0c021d071642bb6ecc5706112d46d1432216e7ae0fe8a00"
)
FORBIDDEN_PUBLIC_KEYS = {
    "accountId",
    "match_id",
    "player_key",
    "puuid",
    "raw_path",
    "riotIdGameName",
    "riotIdTagline",
    "source_payload_reference",
    "summonerId",
    "summonerName",
}


@dataclass(frozen=True)
class OperationalSource:
    analysis_region: str
    source_kind: str
    input_directory: Path


@dataclass(frozen=True)
class Stage34BOperationalInput:
    rows: tuple[TimedDraft, ...]
    accepted_counts: dict[str, int]
    eligible_counts: dict[str, int]
    source_audit: tuple[dict[str, Any], ...]
    combined_input_sha256: str
    timestamp_join_sha256: str
    operational_input_sha256: str


@dataclass(frozen=True)
class Stage34BPreflight:
    summary: dict[str, Any]
    operational_input: Stage34BOperationalInput


def load_operational_amendment(
    path: Path, *, schema_path: Path
) -> dict[str, Any]:
    amendment = _load_json(path, "stage34b_operational_amendment")
    schema = _load_json(schema_path, "stage34b_operational_schema")
    if _sha256_json(amendment) != OPERATIONAL_AMENDMENT_SHA256:
        raise Stage3ValidationError(
            "stage34b_operational_hash", "operational amendment hash differs"
        )
    if _sha256_json(schema) != OPERATIONAL_SCHEMA_SHA256:
        raise Stage3ValidationError(
            "stage34b_operational_schema_hash", "operational schema hash differs"
        )
    if (
        amendment.get("schema_version") != OPERATIONAL_SCHEMA_VERSION
        or amendment.get("operational_protocol_id") != OPERATIONAL_PROTOCOL_ID
        or amendment.get("status")
        != "prospectively_frozen_before_real_stage3_4b_1_execution"
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or set(schema.get("required", [])) != set(amendment)
    ):
        raise Stage3ValidationError(
            "stage34b_operational_contract", "operational amendment differs"
        )
    baseline = amendment["frozen_baseline"]
    if (
        baseline["repository_commit"]
        != "cc5dd3ab764cf69eff4488890bbcd361e220b8df"
        or baseline["scientific_protocol_canonical_sha256"] != PROTOCOL_SHA256
        or amendment["source_adapter"]["stage3_4a_protocol_file_sha256"]
        != STAGE34A_PROTOCOL_SHA256
        or amendment["execution"]["real_data_fit_authorized_by_this_amendment"]
    ):
        raise Stage3ValidationError(
            "stage34b_operational_baseline", "operational baseline differs"
        )
    _reject_nonfinite(amendment)
    return amendment


def load_stage34b_operational_input(
    *,
    scientific_protocol: dict[str, Any],
    stage34a_protocol_path: Path,
    sources: tuple[OperationalSource, ...],
) -> Stage34BOperationalInput:
    if sha256_file(stage34a_protocol_path) != STAGE34A_PROTOCOL_SHA256:
        raise Stage3ValidationError(
            "stage34b_stage34a_protocol_hash", "Stage 3.4A protocol hash differs"
        )
    stage34a_protocol = load_protocol(stage34a_protocol_path)
    runtime_sources = tuple(
        RuntimeSource(
            source.analysis_region, source.source_kind, source.input_directory
        )
        for source in sources
    )
    pooled = load_pooled_input(
        protocol=stage34a_protocol, runtime_sources=runtime_sources
    )
    expected = scientific_protocol["data_scope"]
    if (
        pooled.combined_input_sha256 != expected["source_bundle_sha256"]
        or pooled.eligible_counts["overall"] != expected["eligible_drafts"]
        or pooled.eligible_counts["eune"] != expected["platform_counts"]["eun1"]
        or pooled.eligible_counts["euw"] != expected["platform_counts"]["euw1"]
    ):
        raise Stage3ValidationError(
            "stage34b_operational_input_conflict",
            "Stage 3.4B operational input differs from the scientific protocol",
        )

    eligible_keys = {
        (row.platform, row.match_id) for row in pooled.observations
    }
    timestamps: dict[tuple[str, str], datetime] = {}
    stage31_audit: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sorted(
        sources, key=lambda item: (item.analysis_region, item.source_kind)
    ):
        metadata = _load_json(
            source.input_directory / "metadata.json", "stage34b_stage33a_metadata"
        )
        stage31 = metadata.get("inputs", {}).get("stage3_1")
        if not isinstance(stage31, dict) or not isinstance(
            stage31.get("sha256"), dict
        ):
            raise Stage3ValidationError(
                "stage34b_timestamp_lineage", "Stage 3.1 timestamp lineage is missing"
            )
        stage31_directory = Path(str(stage31.get("directory", "")))
        matches_path = stage31_directory / "matches.jsonl"
        declared_hash = stage31["sha256"].get("matches.jsonl")
        if (
            not isinstance(declared_hash, str)
            or sha256_file(matches_path) != declared_hash
        ):
            raise Stage3ValidationError(
                "stage34b_timestamp_hash", "Stage 3.1 match timestamp hash differs"
            )
        expected_platform = "eun1" if source.analysis_region == "eune" else "euw1"
        joined = 0
        for key, timestamp in _load_timestamp_rows(matches_path):
            if key[0] != expected_platform:
                raise Stage3ValidationError(
                    "stage34b_timestamp_platform", "timestamp platform differs"
                )
            if key not in eligible_keys:
                continue
            if key in timestamps:
                raise Stage3ValidationError(
                    "stage34b_timestamp_duplicate", "timestamp join key is duplicated"
                )
            timestamps[key] = timestamp
            joined += 1
        stage31_audit[(source.analysis_region, source.source_kind)] = {
            "analysis_region": source.analysis_region,
            "source_kind": source.source_kind,
            "platform": expected_platform,
            "stage3_1_matches_sha256": declared_hash,
            "eligible_timestamps_joined": joined,
        }
    rows = join_draft_timestamps(pooled.observations, timestamps)
    timestamp_hash = _timestamp_join_sha256(rows)
    operational_hash = _sha256_json(
        {
            "combined_input_sha256": pooled.combined_input_sha256,
            "timestamp_join_sha256": timestamp_hash,
        }
    )
    pooled_audit = {
        (row["analysis_region"], row["source_kind"]): row
        for row in pooled.source_audit
    }
    if set(pooled_audit) != set(stage31_audit):
        raise Stage3ValidationError(
            "stage34b_source_audit",
            "Stage 3.4A and timestamp source component sets differ",
        )
    source_audit = tuple(
        {
            **pooled_audit[key],
            **stage31_audit[key],
        }
        for key in sorted(pooled_audit)
    )
    return Stage34BOperationalInput(
        rows=rows,
        accepted_counts=pooled.accepted_counts,
        eligible_counts=pooled.eligible_counts,
        source_audit=source_audit,
        combined_input_sha256=pooled.combined_input_sha256,
        timestamp_join_sha256=timestamp_hash,
        operational_input_sha256=operational_hash,
    )


def join_draft_timestamps(
    observations: tuple[MatchDraftObservation, ...],
    timestamps: dict[tuple[str, str], datetime],
) -> tuple[TimedDraft, ...]:
    expected = {(row.platform, row.match_id) for row in observations}
    if set(timestamps) != expected:
        raise Stage3ValidationError(
            "stage34b_timestamp_join", "timestamp join is incomplete or extraneous"
        )
    rows = tuple(
        TimedDraft(row, timestamps[(row.platform, row.match_id)])
        for row in observations
    )
    if any(
        row.game_creation.tzinfo is None
        or row.game_creation.utcoffset() != timedelta(0)
        for row in rows
    ):
        raise Stage3ValidationError(
            "stage34b_timestamp_timezone", "timestamp join is not UTC-aware"
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.game_creation,
                row.draft.platform,
                row.draft.match_id,
            ),
        )
    )


def build_stage34b_preflight(
    *,
    operational_input: Stage34BOperationalInput,
    scientific_protocol: dict[str, Any],
    operational_amendment: dict[str, Any],
) -> Stage34BPreflight:
    folds = construct_outer_folds(
        operational_input.rows,
        scientific_protocol,
        enforce_frozen_counts=True,
    )
    fold_rows = []
    for fold in folds:
        train_max = max(row.game_creation for row in fold.training)
        test_min = min(row.game_creation for row in fold.validation)
        if train_max >= test_min:
            raise Stage3ValidationError(
                "stage34b_preflight_chronology", "preflight chronology differs"
            )
        counts = Counter(row.draft.platform for row in fold.validation)
        fold_rows.append(
            {
                "outer_block": fold.fold_id,
                "training_drafts": len(fold.training),
                "evaluation_drafts": len(fold.validation),
                "evaluation_by_platform": dict(sorted(counts.items())),
                "training_maximum_utc": _utc_text(train_max),
                "evaluation_minimum_utc": _utc_text(test_min),
                "evaluation_end_exclusive_utc": _utc_text(fold.validation_end),
            }
        )
    scored = sum(row["evaluation_drafts"] for row in fold_rows)
    expected_scored = scientific_protocol["validation"]["scored_outer_test_drafts"]
    expected_initial = scientific_protocol["validation"][
        "unscored_initial_training_drafts"
    ]
    if scored != expected_scored or fold_rows[0]["training_drafts"] != expected_initial:
        raise Stage3ValidationError(
            "stage34b_preflight_count", "preflight fold counts differ"
        )
    summary = {
        "schema_version": "stage3.4b-1-preflight-v1",
        "status": "zero_fit_preflight_passed",
        "scientific_protocol_id": scientific_protocol["protocol_id"],
        "scientific_protocol_sha256": PROTOCOL_SHA256,
        "operational_protocol_id": operational_amendment["operational_protocol_id"],
        "operational_amendment_sha256": OPERATIONAL_AMENDMENT_SHA256,
        "combined_input_sha256": operational_input.combined_input_sha256,
        "timestamp_join_sha256": operational_input.timestamp_join_sha256,
        "operational_input_sha256": operational_input.operational_input_sha256,
        "accepted_counts": operational_input.accepted_counts,
        "eligible_counts": operational_input.eligible_counts,
        "initial_training_drafts": expected_initial,
        "outer_evaluation_drafts": expected_scored,
        "outer_blocks": fold_rows,
        "outer_fold_sha256": _fold_sha256(folds),
        "paired_policy_count": 9,
        "paired_evaluation_rows_identical": True,
        "predictive_training_operations_executed": 0,
        "model_fits_executed": 0,
        "scientific_files_written": 0,
        "source_audit": operational_input.source_audit,
    }
    validate_public_payload(summary)
    return Stage34BPreflight(summary=summary, operational_input=operational_input)


def build_publication_payloads(
    *,
    evaluation: Stage34BEvaluation,
    preflight: Stage34BPreflight,
    scientific_protocol: dict[str, Any],
    operational_amendment: dict[str, Any],
    repository_commit: str,
    diagnostic_events: tuple[dict[str, Any], ...],
) -> tuple[dict[str, bytes], str]:
    validate_stage34b_artifact(evaluation.artifact)
    timing = _timing_summary(diagnostic_events)
    fit_reconciliation = _fit_reconciliation(
        evaluation.artifact, diagnostic_events
    )
    if not fit_reconciliation["all_frozen_fit_counts_reconciled"]:
        raise Stage3ValidationError(
            "stage34b_fit_reconciliation",
            "instrumented fit events do not reconcile with the frozen budget",
        )
    final_summaries = _final_model_summaries(evaluation)
    quality = _quality_report(evaluation.artifact, fit_reconciliation)
    input_manifest = {
        **preflight.summary,
        "status": "input_lineage_verified_for_completed_execution",
        "repository_commit": repository_commit,
    }
    environment = {
        "schema_version": "stage3.4b-1-execution-environment-v1",
        "python": runtime_platform.python_version(),
        "python_implementation": runtime_platform.python_implementation(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "platform_feature_used": False,
        "riot_api_used": False,
        "environment_file_loaded": False,
    }
    report = _development_report(
        evaluation.artifact, fit_reconciliation, preflight.summary
    )
    objects: dict[str, Any] = {
        "development_results.json": evaluation.artifact,
        "execution_environment.json": environment,
        "final_model_summaries.json": final_summaries,
        "fit_reconciliation.json": fit_reconciliation,
        "input_manifest.json": input_manifest,
        "quality_report.json": quality,
        "timing_summary.json": timing,
    }
    for value in objects.values():
        validate_public_payload(value)
    payloads = {name: _json_bytes(value) for name, value in objects.items()}
    validate_public_payload(report)
    payloads["development_report.md"] = report.encode("utf-8")
    published_file_hashes = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(payloads.items())
    }
    scientific_names = set(
        operational_amendment["publication"]["scientific_deterministic_files"]
    )
    observational_names = set(
        operational_amendment["publication"]["observational_files"]
    )
    if (
        scientific_names & observational_names
        or scientific_names | observational_names != set(payloads)
    ):
        raise Stage3ValidationError(
            "stage34b_publication_classification",
            "published artifacts are not partitioned into scientific and "
            "observational files",
        )
    scientific_hashes = {
        name: published_file_hashes[name] for name in sorted(scientific_names)
    }
    observational_hashes = {
        name: published_file_hashes[name] for name in sorted(observational_names)
    }
    bundle_hash = _sha256_json(scientific_hashes)
    manifest = {
        "schema_version": "stage3.4b-1-bundle-manifest-v1",
        "scientific_protocol_id": PROTOCOL_ID,
        "scientific_protocol_sha256": PROTOCOL_SHA256,
        "operational_protocol_id": OPERATIONAL_PROTOCOL_ID,
        "operational_amendment_sha256": OPERATIONAL_AMENDMENT_SHA256,
        "repository_commit": repository_commit,
        "published_file_sha256": published_file_hashes,
        "scientific_deterministic_file_sha256": scientific_hashes,
        "observational_file_sha256": observational_hashes,
        "scientific_deterministic_bundle_sha256": bundle_hash,
        "observational_values_are_excluded_from_scientific_fingerprint": True,
    }
    validate_public_payload(manifest)
    payloads["bundle_manifest.json"] = _json_bytes(manifest)
    expected_files = set(operational_amendment["publication"]["published_files"])
    if set(payloads) != expected_files:
        raise Stage3ValidationError(
            "stage34b_publication_file_set", "publication file set differs"
        )
    if sum(map(len, payloads.values())) > operational_amendment["publication"][
        "maximum_bundle_bytes"
    ]:
        raise Stage3ValidationError(
            "stage34b_publication_size", "publication bundle is too large"
        )
    return payloads, bundle_hash


def write_publication_bundle(
    payloads: dict[str, bytes], output_directory: Path
) -> Path:
    target = output_directory.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for name, payload in sorted(payloads.items()):
            (staging / name).write_bytes(payload)
        if target.exists():
            if not target.is_dir() or {
                path.name for path in target.iterdir()
            } != set(payloads):
                raise Stage3ValidationError(
                    "stage34b_publication_conflict", "existing result differs"
                )
            for name, payload in payloads.items():
                if (target / name).read_bytes() != payload:
                    raise Stage3ValidationError(
                        "stage34b_publication_conflict", "existing result differs"
                    )
            return target
        os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def reconstruct_bundle_hash(directory: Path) -> str:
    manifest = _load_json(
        directory / "bundle_manifest.json", "stage34b_bundle_manifest"
    )
    expected = manifest.get("published_file_sha256")
    if not isinstance(expected, dict):
        raise Stage3ValidationError(
            "stage34b_manifest_shape", "bundle manifest differs"
        )
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != set(expected) | {"bundle_manifest.json"}:
        raise Stage3ValidationError(
            "stage34b_manifest_file_set", "published bundle file set differs"
        )
    observed = {
        name: sha256_file(directory / name) for name in sorted(expected)
    }
    if observed != expected:
        raise Stage3ValidationError(
            "stage34b_manifest_hash", "bundle artifact hash differs"
        )
    scientific = manifest.get("scientific_deterministic_file_sha256")
    observational = manifest.get("observational_file_sha256")
    if (
        not isinstance(scientific, dict)
        or not isinstance(observational, dict)
        or set(scientific) & set(observational)
        or set(scientific) | set(observational) != set(observed)
        or any(observed.get(name) != digest for name, digest in scientific.items())
        or any(observed.get(name) != digest for name, digest in observational.items())
    ):
        raise Stage3ValidationError(
            "stage34b_manifest_classification", "bundle manifest classification differs"
        )
    reconstructed = _sha256_json(scientific)
    if reconstructed != manifest.get("scientific_deterministic_bundle_sha256"):
        raise Stage3ValidationError(
            "stage34b_bundle_hash", "bundle hash differs"
        )
    return reconstructed


def validate_public_payload(payload: Any) -> None:
    _reject_nonfinite(payload)
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if set(value) & FORBIDDEN_PUBLIC_KEYS:
                raise Stage3ValidationError(
                    "stage34b_public_identifier", "public payload contains identifier"
                )
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        elif isinstance(value, str) and (
            ":\\" in value or value.startswith(("/home/", "/Users/"))
        ):
            raise Stage3ValidationError(
                "stage34b_public_path", "public payload contains an external path"
            )


def _load_timestamp_rows(
    path: Path,
) -> tuple[tuple[tuple[str, str], datetime], ...]:
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = json.loads(line)
                match_id = value["match_id"]
                platform = value["platform"]
                timestamp = datetime.fromisoformat(
                    value["game_creation"].replace("Z", "+00:00")
                ).astimezone(UTC)
                if (
                    not isinstance(match_id, str)
                    or not isinstance(platform, str)
                    or timestamp.utcoffset() != timedelta(0)
                ):
                    raise ValueError("invalid timestamp row")
                rows.append(((platform, match_id), timestamp))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
        raise Stage3ValidationError(
            "stage34b_timestamp_rows", "Stage 3.1 timestamp rows are invalid"
        ) from error
    keys = [key for key, _ in rows]
    if len(keys) != len(set(keys)):
        raise Stage3ValidationError(
            "stage34b_timestamp_duplicate", "Stage 3.1 timestamps are duplicated"
        )
    return tuple(rows)


def _timestamp_join_sha256(rows: tuple[TimedDraft, ...]) -> str:
    digest = hashlib.sha256()
    for row in sorted(
        rows,
        key=lambda item: (
            item.game_creation,
            item.draft.platform,
            item.draft.match_id,
        ),
    ):
        private = hashlib.sha256(
            f"{row.draft.platform}\0{row.draft.match_id}".encode()
        ).hexdigest()
        digest.update(f"{private}\0{_utc_text(row.game_creation)}\n".encode("ascii"))
    return digest.hexdigest()


def _fold_sha256(folds: tuple[Any, ...]) -> str:
    digest = hashlib.sha256()
    for fold in folds:
        for row in fold.validation:
            private = hashlib.sha256(
                f"{row.draft.platform}\0{row.draft.match_id}".encode()
            ).hexdigest()
            digest.update(f"{private}\0{fold.fold_id}\n".encode("ascii"))
    return digest.hexdigest()


def _fit_reconciliation(
    artifact: dict[str, Any], events: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    accounting = artifact["fit_accounting"]
    predictive_completed = [
        row
        for row in events
        if row.get("event") == "predictive_training_operation_completed"
    ]
    predictive_failed = [
        row
        for row in events
        if row.get("event") == "predictive_training_operation_failed"
    ]
    calibration_completed = [
        row
        for row in events
        if row.get("event") == "calibration_evaluation_completed"
    ]
    calibration_failed = [
        row
        for row in events
        if row.get("event") == "calibration_evaluation_failed"
    ]
    predictive_optimizer = [
        row for row in predictive_completed if row.get("optimizer_fit") is True
    ]
    predictive_analytic = [
        row for row in predictive_completed if row.get("optimizer_fit") is False
    ]
    calibration_optimizer = [
        row for row in calibration_completed if row.get("optimizer_fit") is True
    ]
    calibration_analytic = [
        row for row in calibration_completed if row.get("optimizer_fit") is False
    ]
    predictive_converged = sum(
        row.get("optimizer_status") == "converged" for row in predictive_optimizer
    )
    calibration_converged = sum(
        row.get("optimizer_status") == "converged" for row in calibration_optimizer
    )
    statuses_match = (
        predictive_converged == len(predictive_optimizer)
        and calibration_converged == len(calibration_optimizer)
        and all(
            row.get("optimizer_status") == "analytic"
            for row in predictive_analytic
        )
        and all(
            row.get("optimizer_status") == "analytic_constant_logit"
            for row in calibration_analytic
        )
    )
    event_counts_match = (
        len(predictive_completed)
        == accounting["observed_predictive_training_operations"]
        and len(predictive_optimizer)
        == accounting["observed_predictive_optimizer_fits"]
        and len(predictive_analytic)
        == accounting["observed_analytic_baseline_training_operations"]
        and len(calibration_completed)
        == accounting["observed_calibration_evaluations"]
        and len(calibration_optimizer)
        == accounting["observed_calibration_optimizer_fits"]
        and len(calibration_analytic)
        == accounting["observed_calibration_analytic_evaluations"]
        and not predictive_failed
        and not calibration_failed
    )
    frozen_counts_match = (
        accounting["observed_predictive_training_operations"] == 171
        and accounting["observed_predictive_optimizer_fits"] == 163
        and accounting["observed_analytic_baseline_training_operations"] == 8
        and accounting["observed_calibration_evaluations"] == 63
        and accounting["observed_calibration_optimizer_fits"] <= 63
        and accounting["observed_bootstrap_model_fits"] == 0
        and accounting["observed_failed_fits"] == 0
        and accounting["observed_retried_fits"] == 0
    )
    return {
        "schema_version": "stage3.4b-1-fit-reconciliation-v1",
        **accounting,
        "predictive_optimizer_converged": predictive_converged,
        "predictive_optimizer_nonconverged": (
            len(predictive_optimizer) - predictive_converged
        ),
        "calibration_optimizer_converged": calibration_converged,
        "calibration_optimizer_nonconverged": (
            len(calibration_optimizer) - calibration_converged
        ),
        "total_optimizer_invocations": (
            len(predictive_optimizer) + len(calibration_optimizer)
        ),
        "instrumented_predictive_operations": len(predictive_completed),
        "instrumented_calibration_evaluations": len(calibration_completed),
        "instrumented_failed_operations": (
            len(predictive_failed) + len(calibration_failed)
        ),
        "instrumented_retries": 0,
        "instrumented_event_counts_match_artifact": event_counts_match,
        "all_optimizer_and_analytic_statuses_successful": statuses_match,
        "all_frozen_fit_counts_reconciled": (
            frozen_counts_match and event_counts_match and statuses_match
        ),
    }


def _final_model_summaries(evaluation: Stage34BEvaluation) -> dict[str, Any]:
    rows = []
    for variant in MODEL_VARIANTS_B:
        model = evaluation.final_candidate_models[variant]
        parameter_count = 1 + len(model.composition_coefficients) + sum(
            len(row)
            for matrix in (
                model.synergy_embeddings,
                model.counter_attack_embeddings,
                model.counter_defense_embeddings,
            )
            for row in matrix
        )
        rows.append(
            {
                "model": variant,
                "selected_config_id": model.config.config_id,
                "vocabulary_size": len(model.vocabulary.keys),
                "embedding_feature_count": len(model.embedding_feature_indexes),
                "parameter_count": parameter_count,
                "optimizer_iterations": model.optimizer_iterations,
                "optimizer_status": model.optimizer_status,
                "coefficients_published": False,
            }
        )
    return {
        "schema_version": "stage3.4b-1-final-model-summaries-v1",
        "models": rows,
        "interpretation": "aggregate_configuration_summary_not_deployable_model",
    }


def _quality_report(
    artifact: dict[str, Any], fit_reconciliation: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "stage3.4b-1-quality-v1",
        "finite_values": True,
        "paired_rows_reconciled": True,
        "fit_counts_reconciled": fit_reconciliation[
            "all_frozen_fit_counts_reconciled"
        ],
        "privacy": artifact["privacy"],
        "platform_used_as_predictive_feature": False,
        "row_level_predictions_published": False,
        "ready_for_recommendations": False,
        "future_holdout_gate_passed": artifact["future_holdout_gate_passed"],
    }


def _timing_summary(events: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    completed = [
        row
        for row in events
        if row.get("event")
        in {
            "predictive_training_operation_completed",
            "calibration_evaluation_completed",
            "phase_completed",
        }
    ]
    phase_seconds: dict[str, float] = defaultdict(float)
    model_seconds: dict[str, float] = defaultdict(float)
    for row in completed:
        duration = float(row.get("duration_seconds", 0.0))
        phase_seconds[str(row.get("phase", "metric"))] += duration
        if row.get("model"):
            model_seconds[str(row["model"])] += duration
    return {
        "schema_version": "stage3.4b-1-timing-v1",
        "event_count": len(events),
        "wall_seconds": max(
            (float(row.get("elapsed_wall_seconds", 0.0)) for row in events),
            default=0.0,
        ),
        "phase_seconds": dict(sorted(phase_seconds.items())),
        "model_seconds": dict(sorted(model_seconds.items())),
        "timing_is_operational_not_a_scientific_input": True,
    }


def _development_report(
    artifact: dict[str, Any],
    reconciliation: dict[str, Any],
    preflight: dict[str, Any],
) -> str:
    lines = [
        "# Nexus Lens Stage 3.4B-1 patch-26.15 development report",
        "",
        "Status: **rolling-origin development estimate, not final test performance**.",
        "",
        f"- Eligible drafts: `{preflight['eligible_counts']['overall']}`",
        f"- Outer evaluation drafts: `{preflight['outer_evaluation_drafts']}`",
        f"- Predictive optimizer fits: "
        f"`{reconciliation['observed_predictive_optimizer_fits']}`",
        "- Candidate contrasts: mechanical and non-causal.",
        "- Recommendation reliability authorized: `False`.",
        "",
        "## Overall development metrics",
        "",
        "| Policy | Log loss | Brier | Calibration intercept | "
        "Calibration slope | ECE | Dispersion | Coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for policy, scopes in artifact["metrics"].items():
        row = scopes["overall"]
        lines.append(
            f"| `{policy}` | {_fmt(row['log_loss'])} | "
            f"{_fmt(row['brier_score'])} | "
            f"{_fmt(row['calibration_intercept'])} | "
            f"{_fmt(row['calibration_slope'])} | "
            f"{_fmt(row['expected_calibration_error'])} | "
            f"{_fmt(row['prediction_population_standard_deviation'])} | "
            f"{_fmt(row['coverage'])} |"
        )
    lines.extend(
        [
            "",
            "## Fit reconciliation",
            "",
            f"- Predictive training operations: "
            f"`{reconciliation['observed_predictive_training_operations']}`",
            f"- Predictive optimizer fits: "
            f"`{reconciliation['observed_predictive_optimizer_fits']}`",
            f"- Analytic baseline estimates: "
            f"`{reconciliation['observed_analytic_baseline_training_operations']}`",
            f"- Calibration evaluations: "
            f"`{reconciliation['observed_calibration_evaluations']}`",
            f"- Calibration optimizer fits: "
            f"`{reconciliation['observed_calibration_optimizer_fits']}`",
            f"- Bootstrap model fits: "
            f"`{reconciliation['observed_bootstrap_model_fits']}`",
            f"- All frozen counts and statuses reconciled: "
            f"`{reconciliation['all_frozen_fit_counts_reconciled']}`",
            "",
            "## Paired candidate comparisons",
            "",
            "Differences are candidate minus comparator; negative loss differences "
            "indicate improvement.",
            "",
            "| Candidate | Comparator | Metric | Point | 2.5% | 97.5% | "
            "Replicates | Seed |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for candidate, comparators in artifact.get(
        "paired_bootstrap_intervals", {}
    ).items():
        for comparator, metrics in comparators.items():
            for metric, values in metrics.items():
                lines.append(
                    _comparison_report_row(candidate, comparator, metric, values)
                )
    for comparison in artifact.get("paired_ablation_intervals", {}).values():
        for metric, values in comparison["metrics"].items():
            lines.append(
                _comparison_report_row(
                    comparison["candidate"],
                    comparison["comparator"],
                    metric,
                    values,
                )
            )
    lines.extend(
        [
            "",
            "## Selected configurations",
            "",
            "| Model | Scope | Selected configuration |",
            "| --- | --- | --- |",
        ]
    )
    for model, selections in artifact.get("outer_selections", {}).items():
        for selection in selections:
            lines.append(
                f"| `{model}` | `{selection['outer_block']}` | "
                f"`{selection['selected_config_id']}` |"
            )
    for model, selection in artifact.get("final_selection", {}).items():
        lines.append(
            f"| `{model}` | `final all-development` | "
            f"`{selection['selected_config_id']}` |"
        )
    lines.extend(
        [
            "",
            "## Material-usefulness gate",
            "",
        ]
    )
    for candidate, result in artifact["material_usefulness_evaluation"].items():
        lines.append(f"### `{candidate}` — passes all: `{result['passes_all']}`")
        lines.append("")
        lines.append("| Criterion | Required | Observed | Pass | Field |")
        lines.append("| --- | --- | --- | ---: | --- |")
        for criterion, values in result["criteria"].items():
            observed = json.dumps(
                values["observed"], sort_keys=True, allow_nan=False
            )
            lines.append(
                f"| `{criterion}` | `{values['required']}` | `{observed}` | "
                f"`{values['passed']}` | "
                f"`{values['supporting_aggregate_artifact_field']}` |"
            )
        lines.append("")
    passed = [
        name
        for name, result in artifact["material_usefulness_evaluation"].items()
        if result["passes_all"]
    ]
    lines.extend(
        [
            "## Interpretation",
            "",
            _scientific_interpretation(artifact),
            "",
            (
                "At least one candidate passed the prospectively frozen development "
                "gate; this does not authorize holdout access or recommendations."
                if passed
                else "No candidate passed the complete prospectively frozen gate; "
                "Stage 3.4B-1 produced no model suitable for advancement."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _comparison_report_row(
    candidate: str, comparator: str, metric: str, values: dict[str, Any]
) -> str:
    return (
        f"| `{candidate}` | `{comparator}` | `{metric}` | "
        f"{_fmt(values['point_difference'])} | "
        f"{_fmt(values['lower_0_025'])} | "
        f"{_fmt(values['upper_0_975'])} | "
        f"{values['replicates']} | {values['seed']} |"
    )


def _scientific_interpretation(artifact: dict[str, Any]) -> str:
    metrics = artifact["metrics"]
    blue = "training_fold_blue_win_rate_intercept"
    composition = "stage3_4a_composition_only_l2_0_1_no_intercept"
    best = min(
        MODEL_VARIANTS_B, key=lambda model: metrics[model]["overall"]["log_loss"]
    )
    best_loss = metrics[best]["overall"]["log_loss"]
    blue_gain = metrics[blue]["overall"]["log_loss"] - best_loss
    composition_gain = metrics[composition]["overall"]["log_loss"] - best_loss
    gate = artifact["material_usefulness_evaluation"]
    passed = [model for model, values in gate.items() if values["passes_all"]]
    ablations = artifact.get("paired_ablation_intervals", {})
    synergy_key = (
        "shared_allied_synergy_vs_composition_with_side_intercept"
    )
    counter_key = "shared_lane_counter_vs_composition_with_side_intercept"
    synergy_difference = _ablation_point(ablations, synergy_key)
    counter_difference = _ablation_point(ablations, counter_key)
    only_one_family_improves = (
        synergy_difference is not None
        and counter_difference is not None
        and (synergy_difference < 0) != (counter_difference < 0)
    )
    unstable = any(
        not values["checks"].get("each_platform_improves_vs_composition", False)
        or not values["checks"].get("chronological_direction_repeats", False)
        for values in gate.values()
    )
    if passed:
        conclusion = "shared interactions passed the frozen material-usefulness gate"
    elif only_one_family_improves:
        conclusion = "only one interaction family showed an observed incremental gain"
    elif blue_gain > 0 and composition_gain > 0 and unstable:
        conclusion = "observed gains were unstable across platform or time"
    elif blue_gain > 0 and composition_gain > 0:
        conclusion = "shared interactions showed observed but insufficient signal"
    else:
        conclusion = "the available draft-only data were insufficient for advancement"
    failed_best = [
        name for name, value in gate[best]["checks"].items() if not value
    ]
    failed_text = ", ".join(failed_best) if failed_best else "none"
    return (
        f"The best observed development candidate by overall log loss was `{best}`; "
        "this is not a model-selection decision. Its observed log-loss differences "
        f"were {_fmt(-blue_gain)} versus the Blue-rate baseline and "
        f"{_fmt(-composition_gain)} versus the composition baseline. Mechanically, "
        f"{conclusion}. Failed frozen criteria for that candidate: `{failed_text}`. "
        "These draft contrasts are non-causal and do not authorize recommendations."
    )


def _ablation_point(ablations: dict[str, Any], key: str) -> float | None:
    row = ablations.get(key)
    if not isinstance(row, dict):
        return None
    metrics = row.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("log_loss"), dict):
        return None
    value = metrics["log_loss"].get("point_difference")
    return float(value) if isinstance(value, (int, float)) else None


def _load_json(path: Path, category: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Stage3ValidationError(category, "JSON artifact is invalid") from error
    if not isinstance(payload, dict):
        raise Stage3ValidationError(category, "JSON artifact must be an object")
    return payload


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise Stage3ValidationError(
            "stage34b_nonfinite_publication", "publication value is non-finite"
        )
    if isinstance(value, dict):
        for nested in value.values():
            _reject_nonfinite(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_nonfinite(nested)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fmt(value: float | None) -> str:
    return "null" if value is None else f"{value:.8f}"
