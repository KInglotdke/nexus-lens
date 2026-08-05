"""Prospective, development-only freeze analysis for Stage 3.4A.

This module never loads environment variables or contacts Riot.  It derives paired
loss distributions, power estimates, training-data diagnostics, and immutable
pre-evaluation manifests from the retained 26.14 -> 26.15 development fold.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import scipy
from scipy.stats import t as student_t

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import (
    MODEL_VARIANTS,
    POSITIONS,
    DraftCorpus,
    MatchDraftObservation,
    build_feature_vocabulary,
    build_match_draft_corpus,
    fit_composition_model,
    predict_probability,
    vectorize_draft,
)
from nexus_lens.draft_aggregation import Stage33AInput, load_stage3_3a_input

FREEZE_SCHEMA_VERSION = "stage3.4a-pre-26.16-freeze-v1"
ANALYSIS_SCHEMA_VERSION = "stage3.4a-prospective-analysis-v1"
FREEZE_ID = "stage3.4a-pre-26.16-v1"
DEVELOPMENT_TRAINING_PATCH = "26.14"
DEVELOPMENT_EVALUATION_PATCH = "26.15"
FROZEN_TRAINING_PATCH = "26.15"
FROZEN_EVALUATION_PATCH = "26.16"
QUEUE_ID = 420
ALPHA = 0.05
POWER_TARGETS = (0.8, 0.9)
EFFECT_SIZES = (0.0025, 0.005, 0.01)
CI_SAMPLE_SIZES = (1_000, 2_500, 5_000, 10_000)
LEARNING_FRACTIONS = (0.25, 0.5, 0.75, 1.0)
OPERATIONAL_RESERVE_FRACTION = 0.10
POWER_BOOTSTRAP_REPLICATES = 10_000
POWER_BOOTSTRAP_SEED = 34_201


@dataclass(frozen=True)
class PlatformFreezeSpec:
    analysis_region: str
    platform: str
    input_directory: Path
    development_output_directory: Path
    composition_only_l2: float
    composition_plus_matchups_l2: float


@dataclass(frozen=True)
class FreezeBundle:
    analysis: dict[str, Any]
    manifests: dict[str, dict[str, Any]]
    output_directory: Path


def analyze_platform(
    *,
    stage33a: Stage33AInput,
    spec: PlatformFreezeSpec,
    power_replicates: int = POWER_BOOTSTRAP_REPLICATES,
    power_seed: int = POWER_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Analyze one platform without pooling and without using future outcomes."""

    if power_replicates < 100:
        raise ValueError("power bootstrap requires at least 100 replicates")
    corpus = build_match_draft_corpus(stage33a)
    if corpus.platform != spec.platform:
        raise Stage3ValidationError(
            "freeze_platform_conflict", "freeze platform disagrees with input"
        )
    training = _patch_rows(corpus, DEVELOPMENT_TRAINING_PATCH)
    evaluation = _patch_rows(corpus, DEVELOPMENT_EVALUATION_PATCH)
    if not training or not evaluation:
        raise Stage3ValidationError(
            "freeze_development_fold_missing",
            "26.14 training and 26.15 evaluation rows are required",
        )
    selected = {
        "composition_only": spec.composition_only_l2,
        "composition_plus_lane_matchups": spec.composition_plus_matchups_l2,
    }
    models = {
        variant: fit_composition_model(
            training,
            variant=variant,
            l2_strength=selected[variant],
            max_iterations=500,
            tolerance=1e-9,
        )
        for variant in MODEL_VARIANTS
    }
    outcomes = np.asarray([row.outcome for row in evaluation], dtype=np.float64)
    probabilities = {
        variant: np.asarray(
            [predict_probability(models[variant], row) for row in evaluation],
            dtype=np.float64,
        )
        for variant in MODEL_VARIANTS
    }
    naive = np.full(len(evaluation), 0.5, dtype=np.float64)
    losses = {
        "fixed_0_5": _log_losses(naive, outcomes),
        **{
            variant: _log_losses(values, outcomes)
            for variant, values in probabilities.items()
        },
    }
    comparisons = (
        (
            "composition_only_minus_fixed_0_5",
            losses["composition_only"] - losses["fixed_0_5"],
        ),
        (
            "composition_plus_matchup_minus_composition_only",
            losses["composition_plus_lane_matchups"]
            - losses["composition_only"],
        ),
    )
    total_evaluation = sum(
        patch == DEVELOPMENT_EVALUATION_PATCH
        for patch, _platform in corpus.match_scope.values()
    )
    eligibility_rate = len(evaluation) / total_evaluation
    paired = {
        name: _paired_analysis(
            differences,
            eligibility_rate=eligibility_rate,
            replicates=power_replicates,
            seed=_derived_seed(power_seed, spec.platform, name),
        )
        for name, differences in comparisons
    }
    return {
        "platform": spec.platform,
        "analysis_region": spec.analysis_region,
        "queue_id": QUEUE_ID,
        "development_fold": {
            "training_patch": DEVELOPMENT_TRAINING_PATCH,
            "training_eligible_matches": len(training),
            "evaluation_patch": DEVELOPMENT_EVALUATION_PATCH,
            "evaluation_total_matches": total_evaluation,
            "evaluation_eligible_matches": len(evaluation),
            "eligibility_rate": eligibility_rate,
            "one_observation_per_eligible_match": True,
            "match_sets_disjoint": not bool(
                {row.match_id for row in training}
                & {row.match_id for row in evaluation}
            ),
        },
        "team_side": _team_side_audit(training, evaluation),
        "paired_log_loss": paired,
        "training_adequacy": _training_adequacy(
            training=training,
            evaluation=evaluation,
            selected_l2=selected,
            seed=34_001,
        ),
    }


def build_freeze_bundle(
    *,
    specs: tuple[PlatformFreezeSpec, ...],
    output_directory: Path,
    dependency_lock_path: Path,
    expected_match_count: int = 1_000,
    power_replicates: int = POWER_BOOTSTRAP_REPLICATES,
) -> FreezeBundle:
    if {spec.analysis_region for spec in specs} != {"EUNE", "EUW"}:
        raise ValueError("exactly separate EUNE and EUW freeze specs are required")
    dependency_hash = _sha256_file(dependency_lock_path)
    platform_results = []
    development_lineage = {}
    for spec in sorted(specs, key=lambda item: item.analysis_region):
        stage33a = load_stage3_3a_input(
            spec.input_directory,
            expected_participant_count=expected_match_count * 10,
            expected_team_count=expected_match_count * 2,
            expected_match_count=expected_match_count,
        )
        publication = _verify_development_publication(
            spec.development_output_directory,
            expected_l2=(
                spec.composition_only_l2,
                spec.composition_plus_matchups_l2,
            ),
        )
        result = analyze_platform(
            stage33a=stage33a,
            spec=spec,
            power_replicates=power_replicates,
        )
        platform_results.append(result)
        development_lineage[spec.analysis_region] = {
            "stage3_3a_input_directory": spec.input_directory.as_posix(),
            "stage3_3a_input_sha256": dict(sorted(stage33a.lineage_hashes.items())),
            "stage3_1_input_sha256": dict(sorted(stage33a.stage3_1_hashes.items())),
            "stage3_2_input_sha256": dict(sorted(stage33a.stage3_2_hashes.items())),
            "stage3_4a_development_output_directory": (
                spec.development_output_directory.as_posix()
            ),
            **publication,
        }
    analysis = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "freeze_id": FREEZE_ID,
        "status": "prospective_development_only",
        "future_evaluation_outcomes_observed": False,
        "development_data_only": True,
        "platforms_pooled": False,
        "alpha_two_sided": ALPHA,
        "effect_sizes_mean_log_loss_improvement": list(EFFECT_SIZES),
        "power_targets": list(POWER_TARGETS),
        "power_method": {
            "name": "deterministic_centered_empirical_residual_bootstrap",
            "replicates": power_replicates,
            "base_seed": POWER_BOOTSTRAP_SEED,
            "test": "two_sided_student_t_on_paired_match_level_mean",
        },
        "confidence_interval_width_method": (
            "two_sided_student_t_width_using_development_paired_sample_sd"
        ),
        "operational_reserve_fraction": OPERATIONAL_RESERVE_FRACTION,
        "outcome_dependent_early_stopping": False,
        "platform_results": platform_results,
    }
    analysis_hash = _sha256_json(analysis)
    manifests = {}
    for spec in sorted(specs, key=lambda item: item.analysis_region):
        platform_analysis = next(
            item
            for item in platform_results
            if item["analysis_region"] == spec.analysis_region
        )
        manifests[spec.analysis_region] = _freeze_manifest(
            spec=spec,
            analysis_hash=analysis_hash,
            dependency_lock_path=dependency_lock_path,
            dependency_hash=dependency_hash,
            development_lineage=development_lineage[spec.analysis_region],
            platform_analysis=platform_analysis,
        )
    _reject_nonfinite(analysis)
    _reject_nonfinite(manifests)
    return FreezeBundle(
        analysis=analysis,
        manifests=manifests,
        output_directory=output_directory,
    )


def write_freeze_bundle(bundle: FreezeBundle) -> Path:
    """Atomically publish a small immutable freeze bundle."""

    payloads = _bundle_payloads(bundle)
    target = bundle.output_directory
    if target.exists():
        existing = {
            path.name: path.read_bytes() for path in target.iterdir() if path.is_file()
        }
        if existing != payloads:
            raise Stage3ValidationError(
                "immutable_freeze_conflict", "existing freeze bundle differs"
            )
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload)
        os.replace(staging, target)
    except Exception:
        if staging.exists():
            for path in staging.iterdir():
                path.unlink()
            staging.rmdir()
        raise
    return target


def validate_freeze_bundle(bundle: FreezeBundle, dependency_lock_path: Path) -> None:
    expected_analysis_hash = _sha256_json(bundle.analysis)
    expected_dependency_hash = _sha256_file(dependency_lock_path)
    for region, manifest in bundle.manifests.items():
        if manifest.get("schema_version") != FREEZE_SCHEMA_VERSION:
            raise Stage3ValidationError("freeze_schema_conflict", region)
        if manifest.get("status") != "frozen_before_26.16_evaluation":
            raise Stage3ValidationError("freeze_status_conflict", region)
        if manifest.get("future_evaluation_outcomes_observed") is not False:
            raise Stage3ValidationError("future_outcome_claim_conflict", region)
        if manifest["sample_size_analysis"]["sha256"] != expected_analysis_hash:
            raise Stage3ValidationError("sample_size_hash_conflict", region)
        if manifest["software"]["dependency_lock_sha256"] != expected_dependency_hash:
            raise Stage3ValidationError("dependency_lock_hash_conflict", region)
        if manifest["queue_id"] != QUEUE_ID:
            raise Stage3ValidationError("freeze_queue_conflict", region)
    if bundle.output_directory.exists():
        expected = _bundle_payloads(bundle)
        actual = {
            path.name: path.read_bytes()
            for path in bundle.output_directory.iterdir()
            if path.is_file()
        }
        if actual != expected:
            raise Stage3ValidationError(
                "physical_freeze_hash_conflict",
                "physical freeze files disagree with calculated payloads",
            )


def _paired_analysis(
    differences: np.ndarray,
    *,
    eligibility_rate: float,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    count = len(differences)
    if count < 2 or not np.all(np.isfinite(differences)):
        raise ValueError("paired differences require at least two finite values")
    sample_variance = float(np.var(differences, ddof=1))
    sample_sd = math.sqrt(sample_variance)
    quantiles = {
        str(probability): float(np.quantile(differences, probability))
        for probability in (0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0)
    }
    requirements = []
    for effect in EFFECT_SIZES:
        for target_power in POWER_TARGETS:
            eligible = _required_sample_size(
                differences,
                true_improvement=effect,
                target_power=target_power,
                replicates=replicates,
                seed=_derived_seed(seed, str(effect), str(target_power)),
            )
            accepted = math.ceil(eligible / eligibility_rate)
            reserved = math.ceil(accepted * (1 + OPERATIONAL_RESERVE_FRACTION))
            requirements.append(
                {
                    "true_mean_log_loss_improvement": effect,
                    "power": target_power,
                    "eligible_evaluation_matches": eligible,
                    "eligibility_inflated_accepted_matches": accepted,
                    "accepted_matches_with_10_percent_operational_reserve": reserved,
                }
            )
    widths = []
    for sample_size in CI_SAMPLE_SIZES:
        critical = float(student_t.ppf(1 - ALPHA / 2, sample_size - 1))
        widths.append(
            {
                "eligible_evaluation_matches": sample_size,
                "expected_two_sided_95_percent_ci_full_width": (
                    2 * critical * sample_sd / math.sqrt(sample_size)
                ),
            }
        )
    return {
        "difference_definition": "model_log_loss_minus_comparator_log_loss",
        "negative_mean_favors_first_named_model": True,
        "paired_match_count": count,
        "empirical_mean": float(np.mean(differences)),
        "sample_variance": sample_variance,
        "sample_standard_deviation": sample_sd,
        "empirical_quantiles": quantiles,
        "power_requirements": requirements,
        "expected_confidence_interval_widths": widths,
    }


def _required_sample_size(
    differences: np.ndarray,
    *,
    true_improvement: float,
    target_power: float,
    replicates: int,
    seed: int,
) -> int:
    if true_improvement <= 0 or not 0 < target_power < 1:
        raise ValueError("effect and power target are invalid")
    residuals = differences - float(np.mean(differences))
    probabilities = np.full(len(residuals), 1 / len(residuals))

    def power(sample_size: int) -> float:
        rng = np.random.default_rng(_derived_seed(seed, str(sample_size)))
        rejected = 0
        completed = 0
        critical = float(student_t.ppf(1 - ALPHA / 2, sample_size - 1))
        residual_sq = residuals * residuals
        while completed < replicates:
            batch = min(500, replicates - completed)
            counts = rng.multinomial(sample_size, probabilities, size=batch)
            sums = counts @ residuals
            sum_squares = counts @ residual_sq
            means = sums / sample_size - true_improvement
            variances = np.maximum(
                (sum_squares - (sums * sums) / sample_size) / (sample_size - 1),
                0,
            )
            standard_errors = np.sqrt(variances / sample_size)
            statistics = np.divide(
                np.abs(means),
                standard_errors,
                out=np.full_like(means, np.inf),
                where=standard_errors > 0,
            )
            rejected += int(np.count_nonzero(statistics >= critical))
            completed += batch
        return rejected / replicates

    lower = 2
    upper = 32
    while power(upper) < target_power:
        lower = upper + 1
        upper *= 2
        if upper > 10_000_000:
            raise Stage3ValidationError(
                "power_requirement_unbounded",
                "required sample size exceeds the supported analysis bound",
            )
    while lower < upper:
        midpoint = (lower + upper) // 2
        if power(midpoint) >= target_power:
            upper = midpoint
        else:
            lower = midpoint + 1
    return lower


def _training_adequacy(
    *,
    training: tuple[MatchDraftObservation, ...],
    evaluation: tuple[MatchDraftObservation, ...],
    selected_l2: dict[str, float],
    seed: int,
) -> dict[str, Any]:
    future_vocabularies = {
        variant: build_feature_vocabulary(
            evaluation,
            include_lane_matchups=variant == "composition_plus_lane_matchups",
        )
        for variant in MODEL_VARIANTS
    }
    future_counts = {
        variant: _feature_counts(evaluation, vocabulary)
        for variant, vocabulary in future_vocabularies.items()
    }
    development_vocabularies = {
        variant: build_feature_vocabulary(
            training,
            include_lane_matchups=variant == "composition_plus_lane_matchups",
        )
        for variant in MODEL_VARIANTS
    }
    unseen = {
        variant: _unseen_feature_rates(
            evaluation, development_vocabularies[variant]
        )
        for variant in MODEL_VARIANTS
    }
    learning_curves = {
        variant: _learning_curve(
            training=training,
            evaluation=evaluation,
            variant=variant,
            l2_strength=selected_l2[variant],
            seed=seed,
        )
        for variant in MODEL_VARIANTS
    }
    champion_counts = Counter(
        (assignment.role, assignment.champion_id)
        for draft in evaluation
        for assignment in (*draft.allied, *draft.opposing)
    )
    return {
        "future_training_patch": FROZEN_TRAINING_PATCH,
        "currently_available_eligible_matches": len(evaluation),
        "champion_role_vocabulary_size": len(champion_counts),
        "champion_role_frequency_distribution": _frequency_distribution(
            champion_counts.values()
        ),
        "development_26_14_to_26_15_unseen_feature_rates": unseen,
        "future_26_15_coefficient_dimensions": {
            variant: {
                "coefficient_count": len(vocabulary.keys),
                "eligible_matches": len(evaluation),
                "coefficients_per_eligible_match": (
                    len(vocabulary.keys) / len(evaluation)
                ),
                "training_frequency_distribution": _frequency_distribution(
                    future_counts[variant]
                ),
            }
            for variant, vocabulary in future_vocabularies.items()
        },
        "match_grouped_learning_curves": learning_curves,
        "projection_scope": (
            "descriptive_only; larger training sets should reduce unseen and sparse "
            "features, but no future performance is inferred"
        ),
        "evaluation_size_cannot_repair_training": (
            "more 26.16 evaluation matches narrow evaluation uncertainty but do not "
            "change 26.15 vocabulary, coefficient estimates, or unseen-feature rates"
        ),
    }


def _learning_curve(
    *,
    training: tuple[MatchDraftObservation, ...],
    evaluation: tuple[MatchDraftObservation, ...],
    variant: str,
    l2_strength: float,
    seed: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        training,
        key=lambda row: (
            hashlib.sha256(f"{seed}:{row.match_id}".encode()).digest(),
            row.match_id,
        ),
    )
    full_model = fit_composition_model(
        tuple(sorted(training, key=lambda row: row.match_id)),
        variant=variant,
        l2_strength=l2_strength,
        max_iterations=500,
        tolerance=1e-9,
    )
    full_predictions = np.asarray(
        [predict_probability(full_model, row) for row in evaluation]
    )
    outcomes = np.asarray([row.outcome for row in evaluation])
    output = []
    for fraction in LEARNING_FRACTIONS:
        count = max(2, math.ceil(len(ordered) * fraction))
        subset = tuple(sorted(ordered[:count], key=lambda row: row.match_id))
        model = fit_composition_model(
            subset,
            variant=variant,
            l2_strength=l2_strength,
            max_iterations=500,
            tolerance=1e-9,
        )
        predictions = np.asarray(
            [predict_probability(model, row) for row in evaluation]
        )
        common_keys = sorted(
            set(model.vocabulary.keys) & set(full_model.vocabulary.keys)
        )
        small_coefficients = np.asarray(
            [model.coefficients[model.vocabulary.index[key]] for key in common_keys]
        )
        full_coefficients = np.asarray(
            [
                full_model.coefficients[full_model.vocabulary.index[key]]
                for key in common_keys
            ]
        )
        output.append(
            {
                "training_fraction": fraction,
                "training_matches": len(subset),
                "coefficient_count": len(model.coefficients),
                "common_coefficients_with_full_model": len(common_keys),
                "coefficient_cosine_similarity_to_full_model": _cosine_similarity(
                    small_coefficients, full_coefficients
                ),
                "prediction_mean_absolute_difference_from_full_model": float(
                    np.mean(np.abs(predictions - full_predictions))
                ),
                "prediction_correlation_with_full_model": _correlation(
                    predictions, full_predictions
                ),
                "evaluation_log_loss": float(
                    np.mean(_log_losses(predictions, outcomes))
                ),
            }
        )
    return output


def _team_side_audit(
    training: tuple[MatchDraftObservation, ...],
    evaluation: tuple[MatchDraftObservation, ...],
) -> dict[str, Any]:
    rows = (*training, *evaluation)
    return {
        "representation": "metadata_only_no_explicit_model_feature",
        "observed_oriented_team_ids": sorted({row.allied_team_id for row in rows}),
        "can_represent_training_only_side_advantage": False,
        "signed_side_feature_would_preserve_team_swap_complementarity": True,
        "freeze_decision": "non_blocking_explicit_predictive_scope_limitation",
    }


def _freeze_manifest(
    *,
    spec: PlatformFreezeSpec,
    analysis_hash: str,
    dependency_lock_path: Path,
    dependency_hash: str,
    development_lineage: dict[str, Any],
    platform_analysis: dict[str, Any],
) -> dict[str, Any]:
    l2 = {
        "composition_only": spec.composition_only_l2,
        "composition_plus_lane_matchups": spec.composition_plus_matchups_l2,
    }
    flags = [
        "--training-patch",
        FROZEN_TRAINING_PATCH,
        "--evaluation-patch",
        FROZEN_EVALUATION_PATCH,
        "--frozen-composition-only-l2",
        str(spec.composition_only_l2),
        "--frozen-composition-plus-matchups-l2",
        str(spec.composition_plus_matchups_l2),
        "--seed",
        "34001",
        "--calibration-bins",
        "10",
        "--bootstrap-replicates",
        "200",
        "--bootstrap-seed",
        "34101",
        "--max-iterations",
        "500",
        "--optimizer-tolerance",
        "1e-9",
    ]
    return {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "freeze_id": FREEZE_ID,
        "status": "frozen_before_26.16_evaluation",
        "future_evaluation_outcomes_observed": False,
        "platform": spec.platform,
        "analysis_region": spec.analysis_region,
        "queue_id": QUEUE_ID,
        "patches": {
            "training": FROZEN_TRAINING_PATCH,
            "evaluation": FROZEN_EVALUATION_PATCH,
        },
        "primary_comparison": {
            "model": "composition_only",
            "reference_baseline": "fixed_0_5",
            "unit": "one_eligible_match",
            "paired_difference": "composition_log_loss_minus_fixed_0_5_log_loss",
            "primary_metric": "paired_match_level_log_loss",
            "alpha_two_sided": ALPHA,
            "platforms_evaluated_separately": True,
            "outcome_dependent_early_stopping": False,
        },
        "secondary_metrics": [
            "brier_score",
            "calibration",
            "accuracy_at_0_5",
        ],
        "accuracy_tie_convention": (
            "probability_exactly_0.5_predicts_the_oriented_allied_team_outcome_1"
        ),
        "exploratory_comparison": (
            "composition_plus_lane_matchups_minus_composition_only"
        ),
        "exact_stage3_4a_flags": flags,
        "frozen_l2_strengths": l2,
        "randomness": {
            "model_seed": 34_001,
            "metric_bootstrap_seed": 34_101,
            "metric_bootstrap_replicates": 200,
            "calibration_bins": 10,
        },
        "optimizer": {
            "implementation": "scipy.optimize.minimize",
            "method": "L-BFGS-B",
            "max_iterations": 500,
            "tolerance": 1e-9,
            "intercept": None,
            "feature_scaling": None,
            "prediction_clipping": None,
        },
        "contracts": {
            "feature_encoding": (
                "allied_plus_one_opposing_minus_one_champion_role; optional_signed_"
                "same_role_matchup"
            ),
            "eligibility": (
                "queue_420_complete_two_team_ten_participant_role_resolved_draft"
            ),
            "comparison_population": (
                "identical_platform_specific_eligible_match_subset_for_all_policies"
            ),
            "vocabulary_fit_scope": "training_patch_only",
            "hyperparameter_scope": "frozen_before_evaluation",
            "evaluation_outcomes_in_fit_or_features": False,
            "team_swap": "complete_feature_vector_negated_probability_complemented",
            "team_side": platform_analysis["team_side"],
        },
        "development_lineage": development_lineage,
        "software": {
            "python": _python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "dependency_lock_path": dependency_lock_path.as_posix(),
            "dependency_lock_sha256": dependency_hash,
        },
        "privacy_policy": {
            "aggregate_and_model_parameter_outputs_only": True,
            "raw_riot_identifiers": False,
            "player_names": False,
            "pseudonymous_player_keys": False,
            "prediction_or_match_rows_published": False,
        },
        "sample_size_analysis": {
            "path": "prospective_sample_size.json",
            "sha256": analysis_hash,
        },
    }


def _verify_development_publication(
    directory: Path, *, expected_l2: tuple[float, float]
) -> dict[str, Any]:
    metadata_path = directory / "metadata.json"
    model_path = directory / "model_artifacts.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    models = json.loads(model_path.read_text(encoding="utf-8"))["models"]
    output_hashes = metadata["output"]["sha256"]
    for name, expected in output_hashes.items():
        if _sha256_file(directory / name) != expected:
            raise Stage3ValidationError(
                "development_output_hash_conflict", f"hash mismatch for {name}"
            )
    actual_l2 = tuple(model["selected_l2_strength"] for model in models)
    if actual_l2 != expected_l2:
        raise Stage3ValidationError(
            "development_l2_conflict", "development L2 values disagree"
        )
    implementation_path = Path(__file__).with_name("composition_modeling.py")
    if metadata["code_sha256"] != _sha256_file(implementation_path):
        raise Stage3ValidationError(
            "development_code_hash_conflict",
            "development publication code hash disagrees with implementation",
        )
    return {
        "stage3_4a_metadata_sha256": _sha256_file(metadata_path),
        "stage3_4a_output_sha256": dict(sorted(output_hashes.items())),
        "stage3_4a_publication_tree_sha256": _directory_hash(directory),
        "stage3_4a_code_sha256": metadata["code_sha256"],
    }


def _patch_rows(
    corpus: DraftCorpus, patch: str
) -> tuple[MatchDraftObservation, ...]:
    return tuple(
        sorted(
            (row for row in corpus.observations if row.public_patch == patch),
            key=lambda row: row.match_id,
        )
    )


def _log_losses(probabilities: np.ndarray, outcomes: np.ndarray) -> np.ndarray:
    selected = np.where(outcomes == 1, probabilities, 1 - probabilities)
    if np.any(selected <= 0) or not np.all(np.isfinite(selected)):
        raise ValueError("finite non-boundary probabilities are required")
    return -np.log(selected)


def _feature_counts(
    drafts: tuple[MatchDraftObservation, ...], vocabulary: Any
) -> list[int]:
    counts = np.zeros(len(vocabulary.keys), dtype=np.int64)
    for draft in drafts:
        for index in vectorize_draft(draft, vocabulary):
            counts[index] += 1
    return [int(value) for value in counts]


def _unseen_feature_rates(
    drafts: tuple[MatchDraftObservation, ...], vocabulary: Any
) -> dict[str, Any]:
    include_matchups = vocabulary.include_lane_matchups
    unseen_slots = 0
    total_slots = len(drafts) * len(POSITIONS) * 2
    unseen_matchups = 0
    total_matchups = len(drafts) * len(POSITIONS) if include_matchups else 0
    matches_with_any_unseen = 0
    for draft in drafts:
        any_unseen = False
        for assignment in (*draft.allied, *draft.opposing):
            key = ("composition", assignment.role, assignment.champion_id, 0)
            if key not in vocabulary.index:
                unseen_slots += 1
                any_unseen = True
        if include_matchups:
            allied = {item.role: item.champion_id for item in draft.allied}
            opposing = {item.role: item.champion_id for item in draft.opposing}
            for role in POSITIONS:
                low, high = sorted((allied[role], opposing[role]))
                key = ("lane_matchup", role, low, high)
                if low != high and key not in vocabulary.index:
                    unseen_matchups += 1
                    any_unseen = True
        matches_with_any_unseen += int(any_unseen)
    return {
        "unseen_champion_role_slots": unseen_slots,
        "total_champion_role_slots": total_slots,
        "unseen_champion_role_slot_rate": unseen_slots / total_slots,
        "unseen_direct_matchups": unseen_matchups if include_matchups else None,
        "total_direct_matchups": total_matchups if include_matchups else None,
        "unseen_direct_matchup_rate": (
            unseen_matchups / total_matchups if include_matchups else None
        ),
        "matches_with_any_unseen_feature": matches_with_any_unseen,
        "match_rate_with_any_unseen_feature": matches_with_any_unseen / len(drafts),
    }


def _frequency_distribution(values: Any) -> dict[str, int]:
    buckets = Counter()
    for value in values:
        if value == 0:
            bucket = "0"
        elif value == 1:
            bucket = "1"
        elif value <= 4:
            bucket = "2_4"
        elif value <= 9:
            bucket = "5_9"
        elif value <= 19:
            bucket = "10_19"
        else:
            bucket = "20_plus"
        buckets[bucket] += 1
    labels = ("0", "1", "2_4", "5_9", "10_19", "20_plus")
    return {key: buckets.get(key, 0) for key in labels}


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float | None:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / denominator) if denominator else None


def _correlation(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 2 or np.std(left) == 0 or np.std(right) == 0:
        return None
    return float(np.corrcoef(left, right)[0, 1])


def _bundle_payloads(bundle: FreezeBundle) -> dict[str, bytes]:
    payloads = {"prospective_sample_size.json": _json_bytes(bundle.analysis)}
    payloads.update(
        {
            f"{region.lower()}.freeze.json": _json_bytes(manifest)
            for region, manifest in sorted(bundle.manifests.items())
        }
    )
    return payloads


def _directory_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _derived_seed(seed: int, *parts: str) -> int:
    payload = f"{seed}:" + ":".join(parts)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def _python_version() -> str:
    import sys

    return ".".join(str(value) for value in sys.version_info[:3])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    rendered = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return rendered.encode()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _reject_nonfinite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("freeze outputs cannot contain NaN or infinity")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_nonfinite(item)
