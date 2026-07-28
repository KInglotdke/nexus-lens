"""Build Stage 3.3A factual draft observations without network access."""

import argparse
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.draft_observations import DraftObservationDataset, run_stage3_3a

DEFAULT_STAGE3_1_RUN = Path(
    "data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population"
)
DEFAULT_STAGE3_2_RUN = Path(
    "data/processed/stage3/schema=stage3.2-v1/run=20260722T125547567196Z-population"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate immutable Stage 3.1/3.2 runs and build deterministic "
            "Stage 3.3A champion-select draft observations. This command is "
            "offline, does not load .env, and does not reconstruct pick order."
        )
    )
    parser.add_argument(
        "--stage3-1-run",
        type=Path,
        default=DEFAULT_STAGE3_1_RUN,
        help="Canonical Stage 3.1 run directory.",
    )
    parser.add_argument(
        "--stage3-2-run",
        type=Path,
        default=DEFAULT_STAGE3_2_RUN,
        help="Analytical Stage 3.2 run directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/stage3"),
        help="Versioned Stage 3.3A output root (default: data/processed/stage3).",
    )
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        help="Build and validate every observation without writing output files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset = run_stage3_3a(
            stage3_1_directory=args.stage3_1_run,
            stage3_2_directory=args.stage3_2_run,
            output_root=args.output_dir,
            validate_only=args.validate_only,
        )
    except Stage3ValidationError as error:
        print(f"Stage 3.3A validation failed: category={error.category}.")
        print("No observations were published; prior-stage artifacts were unchanged.")
        return 1
    except OSError:
        print("Stage 3.3A failed: category=local_storage.")
        print("Prior-stage artifacts were unchanged.")
        return 1
    _print_summary(dataset, validate_only=args.validate_only)
    return 0


def _print_summary(dataset: DraftObservationDataset, *, validate_only: bool) -> None:
    report = dataset.quality_report
    rows = report["outputs"]["row_counts"]
    lane = report["lane_opponents"]
    eligibility = report["eligibility"]
    positions = report["positions"]
    lineage = report["lineage_availability"]
    mode = "validation only; no files written" if validate_only else "published"
    print("Nexus Lens Stage 3.3A draft observation summary")
    print(f"  mode: {mode}")
    print(
        "  product use: factual champion-select observations, not performance scoring"
    )
    print("  pick order reconstructed: false (Match-V5 does not support that claim)")
    print(
        "  lane opponent: exactly one position-eligible opposing participant "
        "with the same Stage 3.2 analysis_position; otherwise null"
    )
    print(
        "  output rows: "
        f"participants={rows['participant_draft_observations']}, "
        f"teams={rows['team_draft_observations']}, "
        f"matches={rows['match_draft_context']}"
    )
    print(
        "  lane-opponent coverage: "
        f"resolved={lane['resolved_participant_observations']}, "
        f"unresolved={lane['unresolved_participant_observations']}, "
        f"complete-pair matches={lane['matches_with_all_five_pairs']}"
    )
    print(
        "  participant eligibility: "
        f"matchup={eligibility['matchup_eligible_participants']}, "
        f"synergy={eligibility['synergy_eligible_participants']}"
    )
    print(
        "  position quality: "
        f"disagreements={positions['disagreements']}, "
        f"fallbacks={positions['fallbacks']}, "
        f"unresolved={positions['unresolved']}"
    )
    print(
        "  lineage availability: "
        f"platforms={lineage['platforms']}, explicit-region rows="
        f"{lineage['explicit_region_rows']}, rank rows="
        f"{lineage['rank_bracket_rows']}, stratum rows="
        f"{lineage['collection_stratum_rows']}"
    )
    print(
        "  findings: "
        f"reconciliation={sum(report['reconciliation_failures'].values())}, "
        f"invariants={sum(report['invariant_failures'].values())}"
    )
    print("  authoritative role viability: false; the retained sample is too small")
    print(
        "  ready for factual matchup/synergy aggregation: "
        f"{report['ready_for_matchup_synergy_aggregation']}"
    )
    print(f"  output directory: {dataset.output_directory.as_posix()}")


if __name__ == "__main__":
    raise SystemExit(main())
