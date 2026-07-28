"""Build deterministic Stage 3.3B matchup and synergy sufficient statistics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_aggregation import (
    PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
    run_stage3_3b,
)

DEFAULT_INPUT = Path(
    "data/processed/stage3/schema=stage3.3a-v1/run=20260722T125547567196Z-population"
)
DEFAULT_OUTPUT_ROOT = Path("data/processed/stage3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build transparent directional matchup and ally-synergy sufficient "
            "statistics from immutable Stage 3.3A observations."
        )
    )
    parser.add_argument(
        "--input-run",
        type=Path,
        default=DEFAULT_INPUT,
        help="Stage 3.3A run directory (default: retained 100-match run)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root for schema/run-versioned Stage 3.3B output",
    )
    parser.add_argument(
        "--target-patch",
        help="Target public patch; defaults to newest numeric patch in the input",
    )
    parser.add_argument(
        "--minimum-practical-advantage",
        type=float,
        default=PROVISIONAL_MINIMUM_PRACTICAL_ADVANTAGE,
        help=(
            "Provisional practical advantage stored for future posterior evaluation "
            "(default: 0.01); no retained-population posterior is evaluated"
        ),
    )
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        dest="validate_only",
        help="Validate and summarize without writing output",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        dataset = run_stage3_3b(
            input_directory=args.input_run,
            output_root=args.output_root,
            validate_only=args.validate_only,
            target_patch=args.target_patch,
            minimum_practical_advantage=args.minimum_practical_advantage,
        )
    except (Stage3ValidationError, ValueError):
        print(
            "Stage 3.3B failed: local input, schema, policy, or invariant validation "
            "did not pass. No partial output was published.",
            file=sys.stderr,
        )
        return 1
    report = dataset.quality_report
    eligible = report["eligible_observations"]
    rows = report["outputs"]["row_counts"]
    samples = report["sample_size_distributions"]
    baselines = report["baseline_component_availability"]
    lineage = report["lineage_coverage"]
    print("Nexus Lens Stage 3.3B aggregation summary")
    mode = "validation only; no files written" if args.validate_only else "published"
    print(f"  mode: {mode}")
    print("  product use: visible factual evidence, not counter recommendations")
    print(f"  target patch: {report['patch_windows']['target_patch']}")
    print(
        "  eligible contributions: "
        f"matchup={eligible['matchup_directional_contributions']}, "
        f"synergy={eligible['synergy_directional_contributions']}"
    )
    print(
        "  directional aggregate rows: "
        f"matchup={rows['matchup_aggregates']}, "
        f"synergy={rows['synergy_aggregates']}, "
        f"champion-role={rows['champion_role_sufficient_statistics']}"
    )
    print(
        "  observed games per aggregate: "
        f"matchup={samples['matchup_observed_games']}, "
        f"synergy={samples['synergy_observed_games']}"
    )
    print(f"  baseline availability: {baselines}")
    print(
        "  lineage availability: "
        f"platforms={lineage['platforms']}, "
        f"explicit-region rows={lineage['explicit_region_rows']}, "
        f"rank rows={lineage['rank_bracket_rows']}, "
        f"stratum rows={lineage['collection_stratum_rows']}"
    )
    print(
        "  production posterior status: not_evaluated_policy_unresolved "
        "(baseline formula and prior strength are not approved)"
    )
    print(
        "  findings: "
        f"reconciliation={len(report['reconciliation_failures'])}, "
        f"invariants={len(report['invariant_failures'])}"
    )
    print(f"  ready for calibration: {report['ready_for_calibration']}")
    print("  ready for counter recommendations: False")
    print(f"  output directory: {dataset.output_directory.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
