"""Normalize raw snapshots and generate privacy-safe Stage 1 reports."""

import argparse
from pathlib import Path

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.processing import SnapshotProcessor, select_snapshot_dirs
from nexus_lens.reporting import build_feasibility_report, write_feasibility_reports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize immutable raw Match-V5 snapshots with persistent "
            "deduplication. This command does not call Riot APIs."
        )
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--latest",
        action="store_true",
        help="Process only the newest timestamped raw snapshot.",
    )
    selection.add_argument(
        "--snapshot",
        metavar="NAME",
        help="Process one specifically named directory under the raw root.",
    )
    selection.add_argument(
        "--all",
        dest="process_all",
        action="store_true",
        help="Examine all snapshots; the catalog skips processed matches.",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("data/raw"),
        help="Raw snapshot root (default: data/raw).",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=Path("data/processed"),
        help="Normalized output root (default: data/processed).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="SQLite catalog path (default: <processed-dir>/catalog.sqlite3).",
    )
    parser.add_argument(
        "--report",
        choices=("both", "json", "markdown", "none"),
        default="both",
        help="Feasibility report format (default: both).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="Report directory (default: <processed-dir>/reports).",
    )
    parser.add_argument(
        "--migrate-stage1",
        action="store_true",
        help=(
            "Re-normalize legacy catalog entries from immutable raw snapshots "
            "using public-patch fields; legacy derived files remain compatible."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        snapshots = select_snapshot_dirs(
            args.raw_dir,
            latest=args.latest,
            snapshot=args.snapshot,
            process_all=args.process_all,
        )
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error

    catalog_path = args.catalog or args.processed_dir / "catalog.sqlite3"
    report_dir = args.report_dir or args.processed_dir / "reports"
    with ProcessingCatalog(catalog_path) as catalog:
        summary = SnapshotProcessor(
            processed_root=args.processed_dir,
            catalog=catalog,
            migrate_stage1=args.migrate_stage1,
        ).process(snapshots)
        if args.report != "none":
            formats = (
                {"json", "markdown"} if args.report == "both" else {args.report}
            )
            report = build_feasibility_report(
                processed_root=args.processed_dir,
                catalog=catalog,
                processing_summary=summary,
            )
            write_feasibility_reports(report, report_dir, formats)

    _print_summary(summary.as_dict())
    return 0


def _print_summary(summary: dict[str, object]) -> None:
    print("Nexus Lens snapshot processing summary")
    print(f"  snapshots examined: {summary['snapshots_examined']}")
    print(f"  matches discovered: {summary['matches_discovered']}")
    print(f"  newly processed: {summary['newly_processed_matches']}")
    print(f"  already processed: {summary['already_processed_matches']}")
    print(f"  rejected: {summary['rejected_matches']}")
    print(f"  participant rows written: {summary['participant_rows_written']}")
    print(f"  team rows written: {summary['team_rows_written']}")
    patches = summary["public_patches_encountered"] or []
    patch_summary = ", ".join(str(item) for item in patches) or "none"
    print(f"  public patches encountered: {patch_summary}")
    print(f"  elapsed seconds: {summary['elapsed_seconds']}")
    failures = summary["failure_reasons"] or {}
    if isinstance(failures, dict) and failures:
        rendered = ", ".join(
            f"{category}={count}" for category, count in failures.items()
        )
    else:
        rendered = "none"
    print(f"  failure reasons: {rendered}")


if __name__ == "__main__":
    raise SystemExit(main())
