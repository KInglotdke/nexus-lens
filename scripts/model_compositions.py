"""Run offline Stage 3.4A composition-aware smoke modelling."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.composition_modeling import CompositionConfig, run_stage3_4a


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fit and evaluate experimental match-level composition models using "
            "strictly chronological, platform-isolated offline data."
        )
    )
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--analysis-region", required=True)
    parser.add_argument("--training-patch", default="26.14")
    parser.add_argument("--evaluation-patch", default="26.15")
    parser.add_argument(
        "--l2-grid",
        type=float,
        nargs="+",
        default=(0.01, 0.1, 1.0),
        help="Candidate L2 strengths selected only inside training-patch CV",
    )
    parser.add_argument("--cv-folds", type=int, default=3)
    parser.add_argument(
        "--frozen-composition-only-l2",
        type=float,
        help="Skip tuning and use a strength frozen before evaluation",
    )
    parser.add_argument(
        "--frozen-composition-plus-matchups-l2",
        type=float,
        help="Skip tuning and use a strength frozen before evaluation",
    )
    parser.add_argument("--seed", type=int, default=34_001)
    parser.add_argument("--calibration-bins", type=int, default=10)
    parser.add_argument("--bootstrap-replicates", type=int, default=200)
    parser.add_argument("--bootstrap-seed", type=int, default=34_101)
    parser.add_argument("--max-iterations", type=int, default=500)
    parser.add_argument("--optimizer-tolerance", type=float, default=1e-9)
    parser.add_argument("--expected-match-count", type=int, required=True)
    parser.add_argument("--max-publication-bytes", type=int, default=5_000_000)
    parser.add_argument("--minimum-free-space-reserve-bytes", type=int, required=True)
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        dest="validate_only",
        help="Run all offline checks and modelling without writing output",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = CompositionConfig(
        analysis_region=args.analysis_region,
        training_patch=args.training_patch,
        evaluation_patch=args.evaluation_patch,
        l2_grid=tuple(args.l2_grid),
        composition_only_l2=args.frozen_composition_only_l2,
        composition_plus_lane_matchups_l2=(args.frozen_composition_plus_matchups_l2),
        cv_folds=args.cv_folds,
        seed=args.seed,
        calibration_bins=args.calibration_bins,
        bootstrap_replicates=args.bootstrap_replicates,
        bootstrap_seed=args.bootstrap_seed,
        max_iterations=args.max_iterations,
        optimizer_tolerance=args.optimizer_tolerance,
        max_publication_bytes=args.max_publication_bytes,
        minimum_free_space_reserve_bytes=args.minimum_free_space_reserve_bytes,
    )
    try:
        dataset = run_stage3_4a(
            input_directory=args.input_run,
            output_root=args.output_root,
            config=config,
            validate_only=args.validate_only,
            expected_match_count=args.expected_match_count,
        )
    except (Stage3ValidationError, ValueError):
        print(
            "Stage 3.4A failed: offline lineage, model, invariant, metric, storage, "
            "or publication validation did not pass. No partial output was published.",
            file=sys.stderr,
        )
        return 1
    mode = "validation only; no files written" if args.validate_only else "published"
    print("Nexus Lens Stage 3.4A composition summary")
    print(f"  mode: {mode}")
    print("  status: experimental, non-calibrating, policy unresolved")
    print(f"  platform: {dataset.metrics['platform']}")
    print(f"  analysis region: {dataset.metrics['analysis_region']}")
    print(
        "  match fold: "
        f"{dataset.metrics['training_patch']}="
        f"{dataset.metrics['training_match_count']} "
        f"-> {dataset.metrics['evaluation_patch']}="
        f"{dataset.metrics['evaluation_match_count']} "
        f"(eligible={dataset.metrics['evaluation_eligible_draft_count']})"
    )
    for result in dataset.metrics["policy_results"]:
        overall = result["overall"]
        print(
            f"    {result['policy']}: evaluated={overall['evaluated_rows']}, "
            f"coverage={overall['coverage']}, log_loss={overall['log_loss']}, "
            f"brier={overall['brier_score']}"
        )
    for model in dataset.model_artifacts["models"]:
        print(
            f"  model configuration: {model['variant']} "
            f"L2={model['selected_l2_strength']}"
        )
    storage = dataset.storage_preflight
    print(
        "  storage preflight: "
        f"publication={storage.estimated_publication_bytes}, "
        f"free={storage.observed_free_bytes}, reserve={storage.minimum_reserve_bytes}, "
        f"material_reduction={storage.materially_reduces_collection_headroom}"
    )
    print("  winning model selected: False")
    print("  recommendations authorized: False")
    print(f"  output directory: {dataset.output_directory.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
