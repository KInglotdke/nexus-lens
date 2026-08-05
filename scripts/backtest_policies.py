"""Run offline Stage 3.3C rolling-origin reference-policy backtests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.backtesting import POLICIES, BacktestConfig, run_stage3_3c
from nexus_lens.canonical import Stage3ValidationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run leakage-safe, patch-forward Stage 3.3C reference-policy evaluation. "
            "Outputs are experimental and do not select a production policy."
        )
    )
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--analysis-region", required=True)
    parser.add_argument(
        "--evaluation-patch",
        action="append",
        required=True,
        dest="evaluation_patches",
        help="Chronological evaluation patch; repeat for rolling-origin folds",
    )
    parser.add_argument(
        "--policy",
        action="append",
        choices=POLICIES,
        dest="policies",
        help="Reference policy; repeat as needed (default: all)",
    )
    parser.add_argument(
        "--experimental-prior-equivalent-games",
        type=float,
        required=True,
        help="Explicit smoke-test shrinkage value; this does not approve a prior",
    )
    parser.add_argument("--calibration-bins", type=int, default=10)
    parser.add_argument("--clip-min", type=float)
    parser.add_argument("--clip-max", type=float)
    parser.add_argument("--bootstrap-replicates", type=int, default=0)
    parser.add_argument("--bootstrap-seed", type=int, default=33_003)
    parser.add_argument("--expected-match-count", type=int, required=True)
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        dest="validate_only",
        help="Validate and calculate in memory without writing files",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = BacktestConfig(
        analysis_region=args.analysis_region,
        evaluation_patches=tuple(args.evaluation_patches),
        policies=tuple(args.policies or POLICIES),
        prior_equivalent_games=args.experimental_prior_equivalent_games,
        calibration_bins=args.calibration_bins,
        clip_min=args.clip_min,
        clip_max=args.clip_max,
        bootstrap_replicates=args.bootstrap_replicates,
        bootstrap_seed=args.bootstrap_seed,
    )
    try:
        dataset = run_stage3_3c(
            input_directory=args.input_run,
            output_root=args.output_root,
            config=config,
            validate_only=args.validate_only,
            expected_match_count=args.expected_match_count,
        )
    except (Stage3ValidationError, ValueError):
        print(
            "Stage 3.3C failed: offline lineage, split, metric, or publication "
            "validation did not pass. No partial output was published.",
            file=sys.stderr,
        )
        return 1
    mode = "validation only; no files written" if args.validate_only else "published"
    print("Nexus Lens Stage 3.3C backtest summary")
    print(f"  mode: {mode}")
    print("  status: experimental smoke test; policy unresolved")
    print(f"  platform: {dataset.metrics['platform']}")
    print(f"  analysis region: {dataset.metrics['analysis_region']}")
    for fold in dataset.metrics["folds"]:
        print(
            f"  fold: train={','.join(fold['training_patches'])}, "
            f"evaluate={fold['evaluation_patch']}, "
            f"matches={fold['training_match_count']}->{fold['evaluation_match_count']}"
        )
        for result in fold["policy_results"]:
            overall = result["overall"]
            print(
                f"    {result['policy']}: evaluated={overall['evaluated_rows']}, "
                f"coverage={overall['coverage']}, log_loss={overall['log_loss']}, "
                f"brier={overall['brier_score']}"
            )
    print("  policy selection authorized: False")
    print("  recommendations authorized: False")
    print(f"  output directory: {dataset.output_directory.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
