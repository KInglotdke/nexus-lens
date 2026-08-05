"""Build deterministic Stage 3.2 analytical features without network access."""

import argparse
from pathlib import Path

from nexus_lens.analytics import EXPECTED_MATCH_COUNT, AnalyticalDataset, run_stage3_2
from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.formulas import FORMULA_CONTRACT_VERSION

DEFAULT_INPUT_RUN = Path(
    "data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one immutable Stage 3.1 run and build deterministic Stage "
            "3.2 participant, team, and match analytical features. This command "
            "is offline and does not load .env."
        )
    )
    parser.add_argument(
        "--input-run",
        type=Path,
        default=DEFAULT_INPUT_RUN,
        help=(
            "Stage 3.1 run directory (default: the retained completed "
            "20260722T125547567196Z-population run)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/stage3"),
        help="Versioned Stage 3.2 output root (default: data/processed/stage3).",
    )
    parser.add_argument(
        "--expected-match-count",
        type=int,
        default=EXPECTED_MATCH_COUNT,
        help="Required input match count (default: 100).",
    )
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        help="Derive and validate every row without writing output files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset = run_stage3_2(
            input_directory=args.input_run,
            output_root=args.output_dir,
            validate_only=args.validate_only,
            expected_match_count=args.expected_match_count,
            expected_participant_count=args.expected_match_count * 10,
            expected_team_count=args.expected_match_count * 2,
        )
    except Stage3ValidationError as error:
        print(f"Stage 3.2 validation failed: category={error.category}.")
        print("No analytical output was published; Stage 3.1 was not changed.")
        return 1
    except OSError:
        print("Stage 3.2 failed: category=local_storage.")
        print("Stage 3.1 was not changed.")
        return 1
    _print_summary(dataset, validate_only=args.validate_only)
    return 0


def _print_summary(dataset: AnalyticalDataset, *, validate_only: bool) -> None:
    report = dataset.quality_report
    output = report["output"]
    eligibility = report["eligibility"]
    positions = report["positions"]
    mode = "validation only; no files written" if validate_only else "published"
    print("Nexus Lens Stage 3.2 analytical feature summary")
    print(f"  mode: {mode}")
    print(f"  formula contract: {FORMULA_CONTRACT_VERSION}")
    print("  ratio unit: fraction (0 to 1), not percentage")
    print("  total CS: lane minions + neutral minions")
    print("  per-minute denominator: game_duration_seconds / 60")
    print("  KDA: (kills + assists) / max(1, deaths); zero deaths uses 1")
    print("  kill participation: (kills + assists) / own-team kills")
    print("  gold share: participant gold / own-team gold")
    print("  damage share: participant champion damage / own-team champion damage")
    print("  undefined or non-positive division: JSON null")
    print(
        "  analytical eligibility: false for invalid duration, short game, "
        "or incomplete participant/team structure"
    )
    print(
        "  role eligibility: additionally false for position disagreement, "
        "fallback, or unresolved position"
    )
    rows = output["row_counts"]
    print(
        "  output rows: "
        f"participants={rows['participant_match_features']}, "
        f"teams={rows['team_match_features']}, "
        f"matches={rows['match_analysis_context']}"
    )
    patches = ", ".join(
        f"{patch}={count}"
        for patch, count in output["match_counts_by_public_patch"].items()
    )
    print(f"  public patches: {patches}")
    print(
        "  match eligibility: "
        f"eligible={eligibility['analytically_eligible_matches']}, "
        f"ineligible={eligibility['analytically_ineligible_matches']}, "
        f"short={eligibility['short_games']}"
    )
    print(
        "  role position quality: "
        f"disagreements={positions['disagreements']}, "
        f"fallbacks={positions['fallbacks']}, "
        f"unresolved={positions['unresolved']}"
    )
    print(
        "  findings: "
        f"reconciliation={sum(report['reconciliation_failures'].values())}, "
        f"range={sum(report['range_validation_failures'].values())}, "
        f"invariants={sum(report['invariant_failures'].values())}"
    )
    reconciliation = report["reconciliation_failures"]
    if reconciliation:
        rendered = ", ".join(
            f"{category}={count}" for category, count in reconciliation.items()
        )
        print(f"  reconciliation categories: {rendered}")
    else:
        print("  reconciliation categories: none")
    print(
        "  ready for Stage 3.3 analysis validation: "
        f"{report['ready_for_stage_3_3_analysis_validation']}"
    )
    print(f"  output directory: {dataset.output_directory.as_posix()}")


if __name__ == "__main__":
    raise SystemExit(main())
