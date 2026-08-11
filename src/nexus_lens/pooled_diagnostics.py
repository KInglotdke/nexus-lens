"""Zero-fit aggregate diagnostics for the frozen pooled Stage 3.4A result."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from scipy.stats import spearmanr

import nexus_lens.pooled_development as pooled_development
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    MODEL_VARIANTS,
    POSITIONS,
    FeatureVocabulary,
    FittedCompositionModel,
    MatchDraftObservation,
    build_feature_vocabulary,
    build_match_draft_corpus,
    predict_probability,
    swap_teams,
    vectorize_draft,
)
from nexus_lens.data_seal import inventory_tree, sha256_file
from nexus_lens.draft_aggregation import Stage33AInput
from nexus_lens.draft_observations import (
    DRAFT_OBSERVATION_SCHEMA_VERSION,
    DRAFT_POLICY_VERSION,
    DRAFT_QUALITY_SCHEMA_VERSION,
    MatchDraftContext,
    ParticipantDraftObservation,
    TeamDraftObservation,
)
from nexus_lens.pooled_development import (
    PooledInput,
    RuntimeSource,
    load_protocol,
)

DIAGNOSTIC_SCHEMA_VERSION = "stage3.4a-post-publication-diagnostic-v1"
DIAGNOSTIC_FILE = "aggregate_diagnostics.json"
TARGET_PATCH = "26.15"
SAFE_MINIMUM_FEATURE_SUPPORT = 100
MAX_DISCLOSED_FEATURES_PER_FAMILY = 10
EFFECTIVELY_ZERO_ABSOLUTE = 1e-6
SUPPORT_BINS = (
    ("1-4", 1, 4),
    ("5-9", 5, 9),
    ("10-24", 10, 24),
    ("25-49", 25, 49),
    ("50-99", 50, 99),
    ("100+", 100, None),
)
QUANTILES = (0.0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1.0)
PREDICTION_BANDS = ((0.49, 0.51), (0.45, 0.55), (0.40, 0.60))


@dataclass(frozen=True)
class PublishedArtifacts:
    metrics: dict[str, Any]
    final_models: dict[str, Any]
    quality: dict[str, Any]
    manifest: dict[str, Any]
    execution: dict[str, Any]


def load_published_artifacts(directory: Path) -> PublishedArtifacts:
    """Load and verify the six immutable Stage 3.4A publication files."""

    expected = {
        "development_metrics.json",
        "development_report.md",
        "execution.json",
        "experiment_manifest.json",
        "final_models.json",
        "quality_report.json",
    }
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected:
        raise Stage3ValidationError(
            "diagnostic_published_file_set_conflict",
            "published result file set differs from the frozen six-file bundle",
        )
    payloads = {
        name: (directory / name).read_bytes()
        for name in expected
        if name != "execution.json"
    }
    execution = _load_json(directory / "execution.json")
    if _bundle_sha256(payloads) != execution["deterministic_bundle_sha256"]:
        raise Stage3ValidationError(
            "diagnostic_published_bundle_conflict",
            "published deterministic bundle hash does not reconcile",
        )
    manifest = _load_json(directory / "experiment_manifest.json")
    for name, expected_hash in manifest["deterministic_output_sha256"].items():
        if _sha256_bytes(payloads[name]) != expected_hash:
            raise Stage3ValidationError(
                "diagnostic_published_artifact_hash_conflict",
                "published deterministic artifact hash does not reconcile",
            )
    return PublishedArtifacts(
        metrics=_load_json(directory / "development_metrics.json"),
        final_models=_load_json(directory / "final_models.json"),
        quality=_load_json(directory / "quality_report.json"),
        manifest=manifest,
        execution=execution,
    )


def build_post_publication_diagnostic(
    *,
    pooled: PooledInput,
    protocol: dict[str, Any],
    published: PublishedArtifacts,
    exclusion_reasons: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Build deterministic aggregate diagnostics without fitting any model."""

    _validate_frozen_inputs(pooled, protocol, published)
    models = {
        row["variant"]: _deserialize_model(row)
        for row in published.final_models["models"]
    }
    if set(models) != set(MODEL_VARIANTS):
        raise Stage3ValidationError(
            "diagnostic_model_set_conflict", "saved final model set is incomplete"
        )
    for model in models.values():
        expected = build_feature_vocabulary(
            pooled.observations,
            include_lane_matchups=(
                model.variant == "composition_plus_lane_matchups"
            ),
        )
        if model.vocabulary.keys != expected.keys:
            raise Stage3ValidationError(
                "diagnostic_vocabulary_conflict",
                "saved final vocabulary differs from deterministic reconstruction",
            )

    predictions = _in_sample_predictions(pooled.observations, models)
    parameters = {
        row["variant"]: list(row["parameters"])
        for row in published.final_models["models"]
    }
    target = _target_structure(pooled.observations, published.metrics)
    diagnostic = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "status": "post_publication_zero_fit_aggregate_diagnostic",
        "interpretation": (
            "patch_26_15_in_sample_structural_diagnostics_not_oof_and_not_"
            "development_performance"
        ),
        "frozen_lineage": {
            "protocol_id": protocol["protocol_id"],
            "published_bundle_sha256": published.execution[
                "deterministic_bundle_sha256"
            ],
            "combined_input_sha256": pooled.combined_input_sha256,
            "outer_fold_sha256": published.metrics[
                "outer_fold_fingerprint_sha256"
            ],
            "final_selection_fold_sha256": published.metrics[
                "final_selection_fold_fingerprint_sha256"
            ],
            "published_result_files_modified": False,
            "sealed_sources_modified": False,
        },
        "audit_conclusion": {
            "material_correctness_defect_found": False,
            "model_fit_count": 0,
            "nested_cross_validation_rerun": False,
            "bootstrap_rerun": False,
            "oof_predictions_regenerated": False,
            "patch_26_16_used": False,
            "riot_api_used": False,
            "dotenv_or_env_used": False,
        },
        "invariance_audit": _invariance_audit(
            pooled.observations, models, protocol, published
        ),
        "published_uncertainty": _published_uncertainty(published.metrics),
        "dataset_and_target": {
            "accepted_counts": pooled.accepted_counts,
            "eligible_counts": pooled.eligible_counts,
            "excluded_counts": pooled.exclusion_counts,
            "blue_red_win_balance": target,
            "role_complete_eligible_matches": pooled.eligible_counts,
            "removed_draft_reasons": exclusion_reasons,
        },
        "disclosure_policy": {
            "support_bins": [row[0] for row in SUPPORT_BINS],
            "safe_minimum_named_feature_support": SAFE_MINIMUM_FEATURE_SUPPORT,
            "maximum_named_features_per_family": (
                MAX_DISCLOSED_FEATURES_PER_FAMILY
            ),
            "effectively_zero_absolute_coefficient_threshold": (
                EFFECTIVELY_ZERO_ABSOLUTE
            ),
            "low_support_features_published_only_as_aggregates": True,
        },
        "composition_models": {
            variant: _composition_diagnostics(parameters[variant])
            for variant in MODEL_VARIANTS
        },
        "lane_matchup_model": _lane_matchup_diagnostics(
            pooled.observations,
            models["composition_plus_lane_matchups"],
            parameters["composition_plus_lane_matchups"],
            published.metrics,
        ),
        "prediction_dispersion": {
            "status": "in_sample_structural_diagnostic_not_performance",
            "models": {
                variant: _scoped_prediction_summaries(
                    predictions[variant], pooled.observations
                )
                for variant in MODEL_VARIANTS
            },
            "model_disagreement": _prediction_disagreement(
                predictions, pooled.observations
            ),
        },
        "limitations": {
            "paired_bootstrap_model_difference_intervals": (
                "unavailable; paired replicate values were not retained and the "
                "frozen bootstrap was not rerun"
            ),
            "fold_specific_oof_prediction_dispersion": (
                "unavailable; row-level OOF predictions were not retained"
            ),
            "fold_specific_coefficient_diagnostics": (
                "unavailable; outer-fold coefficient states were retained only as "
                "hashes and counts"
            ),
            "historical_matchup_statistic_transform": (
                "not_applicable; the frozen model uses direct signed pair indicators"
            ),
        },
        "privacy": {
            "aggregate_only": True,
            "match_identifiers_present": False,
            "player_identifiers_or_keys_present": False,
            "prediction_rows_present": False,
            "external_paths_present": False,
            "named_features_require_safe_support": True,
        },
    }
    _reject_nonfinite(diagnostic)
    return diagnostic


def run_post_publication_diagnostic(
    *,
    protocol_path: Path,
    results_directory: Path,
    runtime_sources: tuple[RuntimeSource, ...],
) -> dict[str, Any]:
    """Load sealed sources and frozen artifacts, then build the zero-fit audit."""

    protocol = load_protocol(protocol_path)
    published = load_published_artifacts(results_directory)
    pooled, reasons = _load_diagnostic_pooled_input(protocol, runtime_sources)
    return build_post_publication_diagnostic(
        pooled=pooled,
        protocol=protocol,
        published=published,
        exclusion_reasons=reasons,
    )


def write_post_publication_diagnostic(
    diagnostic: dict[str, Any], output_directory: Path
) -> Path:
    """Publish one aggregate JSON file atomically and immutably."""

    payload = _json_bytes(diagnostic)
    target = output_directory / DIAGNOSTIC_FILE
    if target.exists():
        if target.read_bytes() == payload:
            return target
        raise Stage3ValidationError(
            "diagnostic_immutable_output_conflict",
            "an unequal diagnostic artifact already exists",
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            dir=output_directory.parent, prefix=f".{output_directory.name}."
        )
    )
    try:
        (staging / DIAGNOSTIC_FILE).write_bytes(payload)
        os.replace(staging, output_directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def diagnostic_sha256(diagnostic: dict[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(diagnostic))


def _validate_frozen_inputs(
    pooled: PooledInput,
    protocol: dict[str, Any],
    published: PublishedArtifacts,
) -> None:
    if pooled.combined_input_sha256 != published.manifest["combined_input_sha256"]:
        raise Stage3ValidationError(
            "diagnostic_combined_input_conflict",
            "sealed pooled input differs from the published experiment",
        )
    if pooled.accepted_counts != published.metrics["accepted_counts"]:
        raise Stage3ValidationError(
            "diagnostic_accepted_count_conflict", "accepted counts differ"
        )
    if pooled.eligible_counts != published.metrics["eligible_counts"]:
        raise Stage3ValidationError(
            "diagnostic_eligible_count_conflict", "eligible counts differ"
        )
    if protocol["features"]["intercept"] is not False:
        raise Stage3ValidationError(
            "diagnostic_intercept_conflict", "frozen protocol unexpectedly changed"
        )
    if published.quality["invariant_failures"]:
        raise Stage3ValidationError(
            "diagnostic_published_invariant_failure",
            "published result contains invariant failures",
        )


def _deserialize_model(row: dict[str, Any]) -> FittedCompositionModel:
    keys = tuple(_feature_key(parameter["feature"]) for parameter in row["parameters"])
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise Stage3ValidationError(
            "diagnostic_model_vocabulary_order_conflict",
            "saved model vocabulary is unordered or duplicated",
        )
    coefficients = tuple(float(row_["coefficient"]) for row_ in row["parameters"])
    counts = tuple(int(row_["training_match_count"]) for row_ in row["parameters"])
    model = FittedCompositionModel(
        variant=str(row["variant"]),
        vocabulary=FeatureVocabulary(
            keys=keys,
            index={key: index for index, key in enumerate(keys)},
            include_lane_matchups=row["variant"]
            == "composition_plus_lane_matchups",
        ),
        coefficients=coefficients,
        feature_training_counts=counts,
        l2_strength=float(row["l2_strength"]),
        optimizer_iterations=int(row["optimizer_iterations"]),
        optimizer_status=str(row["optimizer_status"]),
        training_match_set_sha256=str(row["training_match_set_sha256"]),
    )
    if len(keys) != int(row["coefficient_count"]):
        raise Stage3ValidationError(
            "diagnostic_model_dimension_conflict", "saved model dimension differs"
        )
    if _sha256_json([list(key) for key in keys]) != row["vocabulary_sha256"]:
        raise Stage3ValidationError(
            "diagnostic_vocabulary_hash_conflict", "saved vocabulary hash differs"
        )
    if _sha256_json(list(coefficients)) != row["coefficient_sha256"]:
        raise Stage3ValidationError(
            "diagnostic_coefficient_hash_conflict", "saved coefficient hash differs"
        )
    return model


def _feature_key(feature: dict[str, Any]) -> tuple[str, str, int, int]:
    family = str(feature["family"])
    role = str(feature["role"])
    if family == "composition":
        return family, role, int(feature["champion_id"]), 0
    if family == "lane_matchup":
        low = int(feature["lower_champion_id"])
        high = int(feature["higher_champion_id"])
        if low >= high:
            raise Stage3ValidationError(
                "diagnostic_matchup_orientation_conflict",
                "saved matchup pair is not strictly ordered",
            )
        return family, role, low, high
    raise Stage3ValidationError(
        "diagnostic_unknown_feature_family", "saved feature family is unknown"
    )


def _invariance_audit(
    drafts: tuple[MatchDraftObservation, ...],
    models: dict[str, FittedCompositionModel],
    protocol: dict[str, Any],
    published: PublishedArtifacts,
) -> dict[str, Any]:
    team_ids = {draft.allied_team_id for draft in drafts} | {
        draft.opposing_team_id for draft in drafts
    }
    target_correct = team_ids == {100, 200} and all(
        draft.allied_team_id == 100 and draft.opposing_team_id == 200
        for draft in drafts
    )
    swaps_negate = True
    probabilities_complement = True
    for draft in drafts:
        swapped = swap_teams(draft)
        for model in models.values():
            original = vectorize_draft(draft, model.vocabulary)
            reverse = vectorize_draft(swapped, model.vocabulary)
            swaps_negate &= original == {
                index: -value for index, value in reverse.items()
            }
            probabilities_complement &= math.isclose(
                predict_probability(model, draft)
                + predict_probability(model, swapped),
                1.0,
                rel_tol=0,
                abs_tol=1e-12,
            )
    return {
        "target_is_blue_team_100_win": target_correct,
        "blue_composition_sign": 1,
        "red_composition_sign": -1,
        "team_swap_negates_all_features": swaps_negate,
        "team_swap_complements_probability": probabilities_complement,
        "intercept": 0.0,
        "intercept_or_side_bias_feature_present": False,
        "vocabulary_deterministic_sorted_complete": True,
        "lane_opponents_paired_by_same_analysis_role": True,
        "matchup_positive_sign_means_blue_has_lower_champion_id": True,
        "fold_local_vocabulary": published.quality["leakage_controls"][
            "fold_local_vocabulary"
        ],
        "held_out_outer_fold_excluded_from_fit": published.quality[
            "leakage_controls"
        ]["match_grouped_outer_inner_and_final_selection"],
        "unseen_feature_fallback": protocol["features"][
            "evaluation_unseen_feature_fallback"
        ],
        "l2_objective": "mean_log_loss_plus_0.5_times_l2_times_squared_l2_norm",
        "selected_l2": published.manifest["selected_l2"],
        "platform_predictive_feature": False,
        "post_draft_or_outcome_derived_feature": False,
        "feature_families": ["composition", "lane_matchup"],
        "stage3_3b_outcome_statistics_used": False,
    }


def _published_uncertainty(metrics: dict[str, Any]) -> dict[str, Any]:
    models = {}
    for variant, result in metrics["out_of_fold_metrics"].items():
        models[variant] = result["confidence_intervals"]
    primary = metrics["out_of_fold_metrics"]["composition_plus_lane_matchups"]
    reference = metrics["out_of_fold_metrics"]["composition_only"]
    return {
        "saved_95_percent_intervals": models,
        "paired_model_difference": {
            "orientation": "composition_plus_lane_matchups_minus_composition_only",
            "log_loss_point_difference": (
                primary["overall"]["log_loss"]
                - reference["overall"]["log_loss"]
            ),
            "brier_score_point_difference": (
                primary["overall"]["brier_score"]
                - reference["overall"]["brier_score"]
            ),
            "log_loss_95_percent_interval": None,
            "brier_score_95_percent_interval": None,
            "unavailable_reason": (
                "paired bootstrap replicate differences were not preserved"
            ),
        },
    }


def _target_structure(
    drafts: tuple[MatchDraftObservation, ...], metrics: dict[str, Any]
) -> dict[str, Any]:
    computed = {}
    scopes = {
        "overall": drafts,
        "eun1": tuple(row for row in drafts if row.platform == "eun1"),
        "euw1": tuple(row for row in drafts if row.platform == "euw1"),
    }
    for scope, rows in scopes.items():
        blue_wins = sum(row.outcome for row in rows)
        computed[scope] = {
            "eligible_matches": len(rows),
            "blue_team_100_wins": blue_wins,
            "red_team_200_wins": len(rows) - blue_wins,
            "blue_win_rate": blue_wins / len(rows),
        }
        saved = metrics["class_balance"][scope]
        if (
            saved["lower_team_wins"] != blue_wins
            or saved["lower_team_losses"] != len(rows) - blue_wins
        ):
            raise Stage3ValidationError(
                "diagnostic_target_reconciliation_failure",
                "saved target counts differ from reconstructed Blue/Red outcomes",
            )
    return computed


def _composition_diagnostics(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in parameters if row["feature"]["family"] == "composition"]
    return {
        "intercept": 0.0,
        "champion_role_vocabulary_size": len(rows),
        "vocabulary_size_by_role": _count_by_role(rows),
        "support_distribution": _support_distribution(rows),
        "support_categories": _support_categories(rows),
        "coefficient_distribution": _coefficient_distribution(rows),
        "support_vs_absolute_coefficient": _support_relationship(rows),
        "highest_magnitude_safe_support_features": _top_supported(rows),
        "causal_interpretation_authorized": False,
    }


def _lane_matchup_diagnostics(
    drafts: tuple[MatchDraftObservation, ...],
    model: FittedCompositionModel,
    parameters: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    composition = [
        row for row in parameters if row["feature"]["family"] == "composition"
    ]
    matchups = [
        row for row in parameters if row["feature"]["family"] == "lane_matchup"
    ]
    champions_by_role: dict[str, set[int]] = defaultdict(set)
    for row in composition:
        champions_by_role[row["feature"]["role"]].add(
            int(row["feature"]["champion_id"])
        )
    possible_by_role = {
        role: len(champions_by_role[role]) * (len(champions_by_role[role]) - 1) // 2
        for role in POSITIONS
    }
    observed_by_role = _count_by_role(matchups)
    active_values = []
    missing_slots = 0
    same_champion_slots = 0
    for draft in drafts:
        allied = {row.role: row.champion_id for row in draft.allied}
        opposing = {row.role: row.champion_id for row in draft.opposing}
        vector = vectorize_draft(draft, model.vocabulary)
        for role in POSITIONS:
            low, high = sorted((allied[role], opposing[role]))
            if low == high:
                same_champion_slots += 1
                continue
            index = model.vocabulary.index.get(("lane_matchup", role, low, high))
            if index is None:
                missing_slots += 1
            else:
                active_values.append(vector[index])
    total_possible = sum(possible_by_role.values())
    total_observed = len(matchups)
    oof = metrics["unseen_feature_coverage"][
        "composition_plus_lane_matchups"
    ]
    return {
        "representation": "direct_signed_unordered_same_role_pair_indicator",
        "historical_matchup_rate_or_outcome_statistic_used": False,
        "smoothing_or_shrinkage_transform_before_logistic_regression": None,
        "total_possible_pairs_from_final_champion_role_vocabulary": total_possible,
        "actually_observed_pair_features": total_observed,
        "coverage": {
            "overall": total_observed / total_possible,
            "by_role": {
                role: {
                    "possible": possible_by_role[role],
                    "observed": observed_by_role.get(role, 0),
                    "proportion": (
                        observed_by_role.get(role, 0) / possible_by_role[role]
                        if possible_by_role[role]
                        else None
                    ),
                }
                for role in POSITIONS
            },
        },
        "support_distribution": _support_distribution(matchups),
        "support_categories": _support_categories(matchups),
        "full_data_feature_fallback": {
            "observed_slots_missing_from_final_vocabulary": missing_slots,
            "same_champion_slots_with_no_pair_feature": same_champion_slots,
            "default_or_smoothed_feature_values": 0,
        },
        "saved_outer_fold_unseen_coverage": {
            key: oof.get(key)
            for key in (
                "lane_matchup_slots",
                "unseen_lane_matchup_slots",
                "unseen_lane_matchup_slot_rate",
                "matches_with_unseen_lane_matchup",
                "matches_with_unseen_lane_matchup_rate",
            )
        },
        "active_feature_value_distribution": _numeric_distribution(active_values),
        "coefficient_distribution": _coefficient_distribution(matchups),
        "support_vs_absolute_coefficient": _support_relationship(matchups),
        "highest_magnitude_safe_support_features": _top_supported(matchups),
        "interpretation": (
            "sparse direct indicators regularized inside logistic regression; no "
            "separate smoothing stage exists"
        ),
    }


def _in_sample_predictions(
    drafts: tuple[MatchDraftObservation, ...],
    models: dict[str, FittedCompositionModel],
) -> dict[str, list[float]]:
    return {
        variant: [predict_probability(model, draft) for draft in drafts]
        for variant, model in models.items()
    }


def _scoped_prediction_summaries(
    values: list[float], drafts: tuple[MatchDraftObservation, ...]
) -> dict[str, Any]:
    indexes = {
        "overall": range(len(drafts)),
        "eun1": [i for i, row in enumerate(drafts) if row.platform == "eun1"],
        "euw1": [i for i, row in enumerate(drafts) if row.platform == "euw1"],
    }
    return {
        scope: _prediction_summary([values[index] for index in selected])
        for scope, selected in indexes.items()
    }


def _prediction_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": mean(values),
        "population_standard_deviation": pstdev(values),
        "minimum": min(values),
        "maximum": max(values),
        "quantiles": _quantile_map(values),
        "proportion_in_bands": {
            f"{lower:.2f}-{upper:.2f}": sum(
                lower <= value <= upper for value in values
            )
            / len(values)
            for lower, upper in PREDICTION_BANDS
        },
        "mean_absolute_distance_from_0_5": mean(abs(value - 0.5) for value in values),
    }


def _prediction_disagreement(
    predictions: dict[str, list[float]],
    drafts: tuple[MatchDraftObservation, ...],
) -> dict[str, Any]:
    first = predictions["composition_only"]
    second = predictions["composition_plus_lane_matchups"]
    indexes = {
        "overall": range(len(drafts)),
        "eun1": [i for i, row in enumerate(drafts) if row.platform == "eun1"],
        "euw1": [i for i, row in enumerate(drafts) if row.platform == "euw1"],
    }
    output = {}
    for scope, selected in indexes.items():
        differences = [second[index] - first[index] for index in selected]
        absolute = [abs(value) for value in differences]
        output[scope] = {
            "orientation": "composition_plus_lane_matchups_minus_composition_only",
            "mean_signed_difference": mean(differences),
            "mean_absolute_difference": mean(absolute),
            "maximum_absolute_difference": max(absolute),
            "absolute_difference_quantiles": _quantile_map(absolute),
        }
    return output


def _coefficient_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    coefficients = [float(row["coefficient"]) for row in rows]
    absolute = [abs(value) for value in coefficients]
    return {
        "count": len(coefficients),
        "mean": mean(coefficients),
        "population_standard_deviation": pstdev(coefficients),
        "minimum": min(coefficients),
        "maximum": max(coefficients),
        "signed_quantiles": _quantile_map(coefficients),
        "absolute_magnitude_quantiles": _quantile_map(absolute),
        "effectively_zero_count": sum(
            value <= EFFECTIVELY_ZERO_ABSOLUTE for value in absolute
        ),
        "effectively_zero_proportion": sum(
            value <= EFFECTIVELY_ZERO_ABSOLUTE for value in absolute
        )
        / len(absolute),
        "threshold": EFFECTIVELY_ZERO_ABSOLUTE,
    }


def _support_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    supports = [int(row["training_match_count"]) for row in rows]
    return {
        "feature_count": len(supports),
        "bins": {
            label: sum(
                lower <= value and (upper is None or value <= upper)
                for value in supports
            )
            for label, lower, upper in SUPPORT_BINS
        },
        "quantiles": _quantile_map([float(value) for value in supports]),
    }


def _support_categories(rows: list[dict[str, Any]]) -> dict[str, Any]:
    supports = [int(row["training_match_count"]) for row in rows]
    return {
        "unseen_in_final_full_data_vocabulary": 0,
        "rare_under_10": sum(value < 10 for value in supports),
        "well_supported_at_least_100": sum(value >= 100 for value in supports),
    }


def _support_relationship(rows: list[dict[str, Any]]) -> dict[str, Any]:
    supports = [float(row["training_match_count"]) for row in rows]
    magnitudes = [abs(float(row["coefficient"])) for row in rows]
    if len(rows) < 2 or len(set(supports)) < 2 or len(set(magnitudes)) < 2:
        return {"pearson_log1p_support": None, "spearman_support": None}
    logged = [math.log1p(value) for value in supports]
    pearson = _pearson(logged, magnitudes)
    spearman = float(spearmanr(supports, magnitudes).statistic)
    return {
        "pearson_log1p_support": pearson,
        "spearman_support": spearman if math.isfinite(spearman) else None,
    }


def _top_supported(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if int(row["training_match_count"]) >= SAFE_MINIMUM_FEATURE_SUPPORT
    ]
    ordered = sorted(
        eligible,
        key=lambda row: (
            -abs(float(row["coefficient"])),
            json.dumps(row["feature"], sort_keys=True),
        ),
    )[:MAX_DISCLOSED_FEATURES_PER_FAMILY]
    return [
        {
            "feature": row["feature"],
            "training_match_count": row["training_match_count"],
            "coefficient": row["coefficient"],
            "interpretation": "associational_model_parameter_not_causal_strength",
        }
        for row in ordered
    ]


def _count_by_role(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row["feature"]["role"]) for row in rows)
    return {role: counts[role] for role in POSITIONS}


def _numeric_distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": mean(values),
        "population_standard_deviation": pstdev(values),
        "minimum": min(values),
        "maximum": max(values),
        "quantiles": _quantile_map(values),
        "effectively_zero_count": sum(
            abs(value) <= EFFECTIVELY_ZERO_ABSOLUTE for value in values
        ),
    }


def _quantile_map(values: list[float]) -> dict[str, float]:
    return {
        f"{probability:.2f}": _quantile(values, probability)
        for probability in QUANTILES
    }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _pearson(first: list[float], second: list[float]) -> float | None:
    first_mean = mean(first)
    second_mean = mean(second)
    numerator = sum(
        (left - first_mean) * (right - second_mean)
        for left, right in zip(first, second, strict=True)
    )
    first_norm = math.sqrt(sum((value - first_mean) ** 2 for value in first))
    second_norm = math.sqrt(sum((value - second_mean) ** 2 for value in second))
    denominator = first_norm * second_norm
    return numerator / denominator if denominator else None


def _load_diagnostic_pooled_input(
    protocol: dict[str, Any], runtime_sources: tuple[RuntimeSource, ...]
) -> tuple[PooledInput, dict[str, dict[str, int]]]:
    declared_sources = {
        (str(row["analysis_region"]), str(row["source_kind"])): row
        for row in protocol["sealed_sources"]
    }
    runtime_keys = {
        (source.analysis_region, source.source_kind) for source in runtime_sources
    }
    if runtime_keys != set(declared_sources) or len(runtime_sources) != 4:
        raise Stage3ValidationError(
            "diagnostic_source_set_conflict",
            "diagnostic source set differs from the frozen protocol",
        )
    selected: list[MatchDraftObservation] = []
    source_audit = []
    accepted_by_region: Counter[str] = Counter()
    eligible_by_region: Counter[str] = Counter()
    excluded_by_region: Counter[str] = Counter()
    accepted_keys: set[tuple[str, str]] = set()
    by_region: dict[str, Counter[str]] = defaultdict(Counter)
    for source in sorted(
        runtime_sources, key=lambda row: (row.analysis_region, row.source_kind)
    ):
        declared = declared_sources[(source.analysis_region, source.source_kind)]
        stage33a, physical_hashes = _load_sealed_stage33a(
            source.input_directory, declared
        )
        corpus = build_match_draft_corpus(stage33a)
        if corpus.platform != declared["platform"]:
            raise Stage3ValidationError(
                "diagnostic_source_platform_conflict",
                "diagnostic source platform differs from the frozen protocol",
            )
        accepted = {
            (platform, match_id)
            for match_id, (patch, platform) in corpus.match_scope.items()
            if patch == TARGET_PATCH
        }
        eligible = tuple(
            row for row in corpus.observations if row.public_patch == TARGET_PATCH
        )
        if accepted_keys & accepted:
            raise Stage3ValidationError(
                "diagnostic_duplicate_match", "a match occurs in multiple components"
            )
        accepted_keys.update(accepted)
        if (
            len(accepted) != int(declared["accepted_matches"])
            or len(eligible)
            != int(declared["expected_composition_eligible_matches"])
        ):
            raise Stage3ValidationError(
                "diagnostic_source_count_conflict",
                "diagnostic source counts differ from the frozen protocol",
            )
        selected.extend(eligible)
        accepted_by_region[source.analysis_region] += len(accepted)
        eligible_by_region[source.analysis_region] += len(eligible)
        excluded_by_region[source.analysis_region] += len(accepted) - len(eligible)
        for match_id, reason in corpus.exclusions.items():
            patch, _ = corpus.match_scope[match_id]
            if patch == TARGET_PATCH:
                by_region[source.analysis_region][reason] += 1
        source_audit.append(
            {
                "analysis_region": source.analysis_region,
                "source_kind": source.source_kind,
                "platform": corpus.platform,
                "input_match_count": len(corpus.match_scope),
                "accepted_patch_26_15_matches": len(accepted),
                "composition_eligible_matches": len(eligible),
                "excluded_matches": len(accepted) - len(eligible),
                "lineage_sha256": dict(sorted(physical_hashes.items())),
                "declared_tree_sha256": declared.get("tree_sha256"),
            }
        )
    observations = tuple(sorted(selected, key=lambda row: (row.platform, row.match_id)))
    expected = protocol["combined_input"]["expected_counts"]
    if (
        len(accepted_keys) != int(expected["accepted_matches"])
        or len(observations) != int(expected["composition_eligible_matches"])
    ):
        raise Stage3ValidationError(
            "diagnostic_combined_count_conflict",
            "diagnostic combined counts differ from the frozen protocol",
        )
    pooled = PooledInput(
        observations=observations,
        combined_input_sha256=pooled_development._combined_input_sha256(observations),
        source_audit=tuple(source_audit),
        accepted_counts={
            **dict(sorted(accepted_by_region.items())),
            "overall": len(accepted_keys),
        },
        eligible_counts={
            **dict(sorted(eligible_by_region.items())),
            "overall": len(observations),
        },
        exclusion_counts={
            **dict(sorted(excluded_by_region.items())),
            "overall": len(accepted_keys) - len(observations),
        },
    )
    overall = sum(by_region.values(), Counter())
    reasons = {
        "eune": dict(sorted(by_region["eune"].items())),
        "euw": dict(sorted(by_region["euw"].items())),
        "overall": dict(sorted(overall.items())),
    }
    return pooled, reasons


def _load_sealed_stage33a(
    directory: Path, declared: dict[str, Any]
) -> tuple[Stage33AInput, dict[str, str]]:
    names = (
        "draft_observation_quality_report.json",
        "match_draft_context.jsonl",
        "metadata.json",
        "participant_draft_observations.jsonl",
        "team_draft_observations.jsonl",
    )
    if any(not (directory / name).is_file() for name in names):
        raise Stage3ValidationError(
            "diagnostic_stage3_3a_input_missing", "sealed Stage 3.3A file is missing"
        )
    physical_hashes = {
        name: sha256_file(directory / name) for name in names
    }
    if "tree_sha256" in declared:
        if inventory_tree(directory).sha256 != declared["tree_sha256"]:
            raise Stage3ValidationError(
                "diagnostic_source_tree_hash_conflict",
                "sealed external Stage 3.3A tree hash differs",
            )
    elif physical_hashes != declared["declared_file_sha256"]:
        raise Stage3ValidationError(
            "diagnostic_source_file_hash_conflict",
            "sealed retained Stage 3.3A file hashes differ",
        )
    metadata = _load_json(directory / "metadata.json")
    quality = _load_json(directory / "draft_observation_quality_report.json")
    if (
        metadata.get("processing_schema_version")
        != DRAFT_OBSERVATION_SCHEMA_VERSION
        or metadata.get("draft_policy_version") != DRAFT_POLICY_VERSION
        or quality.get("quality_report_schema_version")
        != DRAFT_QUALITY_SCHEMA_VERSION
        or quality.get("ready_for_matchup_synergy_aggregation") is not True
        or quality.get("invariant_failures")
        or quality.get("reconciliation_failures")
    ):
        raise Stage3ValidationError(
            "diagnostic_stage3_3a_quality_conflict",
            "sealed Stage 3.3A schema or quality gate differs",
        )
    recorded = metadata.get("output", {}).get("sha256", {})
    if any(physical_hashes.get(name) != value for name, value in recorded.items()):
        raise Stage3ValidationError(
            "diagnostic_stage3_3a_self_hash_conflict",
            "sealed Stage 3.3A self-recorded hashes differ",
        )
    participants = _load_jsonl(
        directory / "participant_draft_observations.jsonl",
        ParticipantDraftObservation,
    )
    teams = _load_jsonl(
        directory / "team_draft_observations.jsonl", TeamDraftObservation
    )
    matches = _load_jsonl(directory / "match_draft_context.jsonl", MatchDraftContext)
    counts = metadata["row_counts"]
    if (
        len(participants) != int(counts["participant_draft_observations"])
        or len(teams) != int(counts["team_draft_observations"])
        or len(matches) != int(counts["match_draft_context"])
    ):
        raise Stage3ValidationError(
            "diagnostic_stage3_3a_row_count_conflict",
            "sealed Stage 3.3A row counts differ",
        )
    stage33a = Stage33AInput(
        run_id=str(metadata["run_id"]),
        input_directory=directory,
        stage3_1_directory=Path("not_loaded_in_post_publication_diagnostic"),
        stage3_2_directory=Path("not_loaded_in_post_publication_diagnostic"),
        participants=participants,
        teams=teams,
        matches=matches,
        lineage_hashes=physical_hashes,
        stage3_1_hashes={},
        stage3_2_hashes={},
    )
    return stage33a, physical_hashes


def _load_jsonl(path: Path, model: Any) -> list[Any]:
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(model.model_validate_json(line))
    except (OSError, UnicodeError, ValueError) as error:
        raise Stage3ValidationError(
            "diagnostic_jsonl_invalid", "sealed diagnostic input JSONL is invalid"
        ) from error
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Stage3ValidationError(
            "diagnostic_json_invalid", "diagnostic input JSON is invalid"
        ) from error
    if not isinstance(value, dict):
        raise Stage3ValidationError(
            "diagnostic_json_shape_invalid", "diagnostic input JSON must be an object"
        )
    return value


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise Stage3ValidationError(
            "diagnostic_nonfinite_output", "diagnostic output contains non-finite data"
        )
    if isinstance(value, dict):
        for nested in value.values():
            _reject_nonfinite(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_nonfinite(nested)


def _bundle_sha256(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, payload in sorted(payloads.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_bytes(payload).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
