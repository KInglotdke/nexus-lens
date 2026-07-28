"""Build privacy-conscious Stage 3.1 canonical tables without network access."""

import argparse
import json
from pathlib import Path

from nexus_lens.canonical import (
    EXPECTED_MATCH_COUNT,
    EXPECTED_PATCH_COUNTS,
    CanonicalDataset,
    Stage3ValidationError,
    build_retained_population_dataset,
    write_canonical_dataset,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the completed 100-match Stage 2 population and build "
            "deterministic Stage 3.1 JSONL tables. This command is offline and "
            "does not load .env."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help=(
            "Completed Stage 2 manifest. By default, select the newest completed "
            "100-match population manifest under --raw-dir."
        ),
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help=(
            "Matching Stage 2 checkpoint. By default, derive it from the manifest "
            "run ID under --snapshot-dir."
        ),
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("data/raw"),
        help="Immutable raw snapshot root (default: data/raw).",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("data/snapshots/population"),
        help="Population checkpoint root (default: data/snapshots/population).",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=Path("data/processed"),
        help="Existing Stage 2 normalized root (default: data/processed).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("data/processed/catalog.sqlite3"),
        help="Read-only Stage 2 catalog (default: data/processed/catalog.sqlite3).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/stage3"),
        help="Separate canonical output root (default: data/processed/stage3).",
    )
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        help="Perform every input and row validation without writing output files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest_path = args.manifest or _newest_completed_manifest(args.raw_dir)
        checkpoint_path = args.checkpoint or _checkpoint_for_manifest(
            manifest_path, args.snapshot_dir
        )
        dataset = build_retained_population_dataset(
            manifest_path=manifest_path,
            checkpoint_path=checkpoint_path,
            catalog_path=args.catalog,
            raw_root=args.raw_dir,
            processed_root=args.processed_dir,
            output_root=args.output_dir,
            expected_match_count=EXPECTED_MATCH_COUNT,
            expected_patch_counts=EXPECTED_PATCH_COUNTS,
        )
        if not dataset.quality_report["ready_for_stage_3_2"]:
            categories = dataset.quality_report["invariant_failures"]
            rendered = ", ".join(sorted(categories)) or "unknown"
            raise Stage3ValidationError(
                "invariant_failure", f"row validation categories: {rendered}"
            )
        if not args.validate_only:
            write_canonical_dataset(dataset)
    except Stage3ValidationError as error:
        print(f"Stage 3.1 validation failed: category={error.category}.")
        print("No canonical output was published; retained inputs were not changed.")
        return 1
    except OSError:
        print("Stage 3.1 failed: category=local_storage.")
        print("Retained inputs were not changed.")
        return 1

    _print_summary(dataset, validate_only=args.validate_only)
    return 0


def _newest_completed_manifest(raw_root: Path) -> Path:
    for path in sorted(raw_root.glob("*-population/manifest.json"), reverse=True):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        summary = manifest.get("summary", {})
        if (
            summary.get("completion_status") == "target_reached"
            and summary.get("accepted_matches") == EXPECTED_MATCH_COUNT
            and summary.get("accepted_matches_by_public_patch") == EXPECTED_PATCH_COUNTS
        ):
            return path
    raise Stage3ValidationError(
        "manifest_missing", "no completed retained Stage 2 population was found"
    )


def _checkpoint_for_manifest(manifest_path: Path, snapshot_root: Path) -> Path:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = manifest["run_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise Stage3ValidationError(
            "malformed_manifest", "run ID is unavailable"
        ) from None
    if not isinstance(run_id, str) or not run_id:
        raise Stage3ValidationError("malformed_manifest", "run ID is unavailable")
    return snapshot_root / run_id / "checkpoint.json"


def _print_summary(dataset: CanonicalDataset, *, validate_only: bool) -> None:
    report = dataset.quality_report
    processed = report["processed"]
    shape = report["shape"]
    positions = report["positions"]
    bans = report["bans"]
    timestamps = report["timestamps_and_duration"]
    payload = report["payload_quality"]
    mode = "validation only; no files written" if validate_only else "published"
    print("Nexus Lens Stage 3.1 canonicalization summary")
    print(f"  mode: {mode}")
    approved_count = report["input"]["approved_matches"]
    print(f"  approved/processed matches: {approved_count}/{processed['matches']}")
    patches = ", ".join(
        f"{patch}={count}"
        for patch, count in processed["match_counts_by_public_patch"].items()
    )
    print(f"  public patches: {patches}")
    print(f"  participant rows: {processed['participants']}")
    print(f"  team rows: {processed['teams']}")
    print(f"  ban rows: {processed['bans']} (missing slots: {bans['missing_slots']})")
    print(
        "  position findings: "
        f"team missing={positions['missing_team_positions']}, "
        f"individual missing={positions['missing_individual_positions']}, "
        f"ambiguous={positions['ambiguous_team_vs_individual_positions']}"
    )
    print(
        "  structural findings: "
        f"duplicate matches={shape['duplicate_match_ids']}, "
        "duplicate participant IDs="
        f"{shape['duplicate_participant_ids_within_matches']}"
    )
    print(
        "  short games / missing optional timestamps: "
        f"{timestamps['remake_or_short_game_count']} / "
        f"{sum(timestamps['missing_optional_timestamps'].values())}"
    )
    print(
        "  malformed or skipped payloads: "
        f"{payload['malformed_or_skipped_payloads_total']}"
    )
    print(f"  ready for Stage 3.2: {report['ready_for_stage_3_2']}")
    print(f"  output directory: {dataset.output_directory.as_posix()}")


if __name__ == "__main__":
    raise SystemExit(main())
