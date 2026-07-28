"""Publish or validate retained-corpus lineage without changing prior stages."""

import argparse
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.lineage import run_lineage_repair

DEFAULT_RUN_ID = "20260722T125547567196Z-population"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and optionally publish an immutable lineage-v1 sidecar "
            "for the retained Stage 3.3B population."
        )
    )
    parser.add_argument(
        "--stage3-3b-run",
        type=Path,
        default=(
            Path("data/processed/stage3/schema=stage3.3b-v1") / f"run={DEFAULT_RUN_ID}"
        ),
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=(
            Path("data/snapshots/population") / DEFAULT_RUN_ID / "checkpoint.json"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/raw") / DEFAULT_RUN_ID / "manifest.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/processed/lineage"),
    )
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        dest="validate_only",
        help="Run every audit and reconciliation without writing output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        dataset = run_lineage_repair(
            stage3_3b_directory=args.stage3_3b_run,
            checkpoint_path=args.checkpoint,
            manifest_path=args.manifest,
            output_root=args.output_root,
            validate_only=args.validate_only,
        )
    except Stage3ValidationError as error:
        print(f"Lineage validation failed [{error.category}]: {error}")
        return 1
    report = dataset.audit_report
    recovery = report["recovery"]
    print("Nexus Lens collection-lineage audit")
    print(f"  mode: {'validation-only' if args.validate_only else 'published'}")
    print(f"  output: {dataset.output_directory}")
    print(f"  match lineage rows: {recovery['match_lineage_rows']}")
    print(f"  discovery contexts: {recovery['discovery_context_rows']}")
    print(f"  participant rank statuses: {recovery['participant_rank_statuses']}")
    print(
        "  unique players with observed rank: "
        f"{recovery['unique_players_with_observed_rank']}"
    )
    print(f"  ready for forward lineage use: {report['ready_for_forward_lineage_use']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
