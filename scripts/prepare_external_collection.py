"""Provision private external-root metadata for patch-specific collection."""

from __future__ import annotations

import argparse
from pathlib import Path

from nexus_lens.draft_aggregation import load_stage3_3a_input
from nexus_lens.population_state import atomic_write_json
from nexus_lens.private_dedup import (
    create_private_deduplication_index,
    file_sha256,
    match_set_sha256,
    verified_catalog_match_ids,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create platform-separated external collection directories, a private "
            "cross-location deduplication index, and resumable configs."
        )
    )
    parser.add_argument("--external-root", type=Path, required=True)
    parser.add_argument(
        "--write-root",
        type=Path,
        help=(
            "Optional staging root for generated metadata. Configured live paths "
            "still use --external-root."
        ),
    )
    parser.add_argument("--platform", choices=("eun1", "euw1"), required=True)
    parser.add_argument("--analysis-region", choices=("EUNE", "EUW"), required=True)
    parser.add_argument("--source-catalog", type=Path, required=True)
    parser.add_argument("--source-stage3a", type=Path, required=True)
    parser.add_argument("--expected-source-match-count", type=int, required=True)
    parser.add_argument(
        "--expected-compatible-match-count", type=int, required=True
    )
    parser.add_argument("--target-public-patch", required=True)
    parser.add_argument("--additional-target", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    live_platform_directory = (
        args.external_root / args.analysis_region.lower()
    ).resolve()
    write_platform_directory = (
        (args.write_root or args.external_root) / args.analysis_region.lower()
    ).resolve()
    paths = {
        "config": write_platform_directory / "config",
        "raw": write_platform_directory / "raw",
        "private": write_platform_directory / "private",
        "processed": write_platform_directory / "processed",
        "stage3": write_platform_directory / "processed" / "stage3",
        "snapshots": write_platform_directory / "snapshots" / "population",
        "lineage": write_platform_directory / "lineage",
        "logs": write_platform_directory / "logs",
    }
    live_paths = {
        "config": live_platform_directory / "config",
        "raw": live_platform_directory / "raw",
        "private": live_platform_directory / "private",
        "processed": live_platform_directory / "processed",
        "stage3": live_platform_directory / "processed" / "stage3",
        "snapshots": live_platform_directory / "snapshots" / "population",
        "lineage": live_platform_directory / "lineage",
        "logs": live_platform_directory / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    source = load_stage3_3a_input(
        args.source_stage3a,
        expected_participant_count=args.expected_source_match_count * 10,
        expected_team_count=args.expected_source_match_count * 2,
        expected_match_count=args.expected_source_match_count,
    )
    compatible_stage3_ids = {
        match.match_id
        for match in source.matches
        if match.platform == args.platform
        and match.queue_id == 420
        and match.public_patch == args.target_public_patch
        and match.participant_count == 10
        and match.participants_complete
    }
    compatible_catalog_ids = verified_catalog_match_ids(
        args.source_catalog,
        analysis_region=args.analysis_region,
        public_patch=args.target_public_patch,
    )
    if compatible_stage3_ids != compatible_catalog_ids:
        raise ValueError(
            "source Stage 3 and private catalog compatible match sets differ"
        )
    if len(compatible_stage3_ids) != args.expected_compatible_match_count:
        raise ValueError("compatible source match count differs from expectation")
    source_catalog_hash = file_sha256(args.source_catalog)
    source_metadata_hash = file_sha256(args.source_stage3a / "metadata.json")
    index_path = paths["private"] / "existing-26.15-dedup.sqlite3"
    create_private_deduplication_index(
        index_path,
        match_ids=compatible_stage3_ids,
        platform=args.platform,
        analysis_region=args.analysis_region,
        public_patch=args.target_public_patch,
        queue_id=420,
        source_catalog_sha256=source_catalog_hash,
        source_stage3a_metadata_sha256=source_metadata_hash,
    )
    base_config = {
        "platforms": [args.platform],
        "target_public_patch": args.target_public_patch,
        "patch_window_size": 1,
        "tiers": ["GOLD", "PLATINUM", "EMERALD", "DIAMOND"],
        "divisions": ["I", "II", "III", "IV"],
        "initial_history_batch_size": 5,
        "max_history_per_player": 20,
        "older_patch_stop_threshold": 2,
        "pages_per_stratum": 5,
        "max_start_page": 5,
        "concurrency": 1,
        "seed": 42,
        "stop_on_newer_patch": True,
        "deduplication_indexes": [
            str((live_paths["private"] / index_path.name).resolve())
        ],
        "raw_root": str(live_paths["raw"].resolve()),
        "processed_root": str(live_paths["processed"].resolve()),
        "snapshot_root": str(live_paths["snapshots"].resolve()),
    }
    smoke = {
        **base_config,
        "target_matches_per_platform": 10,
        "max_players_per_platform": 25,
        "max_match_ids_per_platform": 250,
        "max_requests_per_platform": 300,
    }
    production = {
        **base_config,
        "target_matches_per_platform": args.additional_target,
        "max_players_per_platform": 10_000,
        "max_match_ids_per_platform": 60_000,
        "max_requests_per_platform": 60_000,
    }
    batch_300 = {
        **production,
        "max_players_per_platform": 150,
        "max_match_ids_per_platform": 5_000,
        "max_requests_per_platform": 300,
    }
    batch_1000 = {
        **production,
        "max_players_per_platform": 500,
        "max_match_ids_per_platform": 5_000,
        "max_requests_per_platform": 1_000,
    }
    batch_3000 = {
        **production,
        "max_players_per_platform": 1_500,
        "max_match_ids_per_platform": 15_000,
        "max_requests_per_platform": 3_000,
    }
    batch_5000 = {
        **production,
        "max_players_per_platform": 2_500,
        "max_match_ids_per_platform": 25_000,
        "max_requests_per_platform": 5_000,
    }
    smoke_path = paths["config"] / "smoke-26.15.json"
    batch_300_path = paths["config"] / "production-batch-300.json"
    batch_1000_path = paths["config"] / "production-batch-1000.json"
    batch_3000_path = paths["config"] / "production-batch-3000.json"
    batch_5000_path = paths["config"] / "production-batch-5000.json"
    production_path = paths["config"] / "production-26.15.json"
    atomic_write_json(smoke_path, smoke)
    atomic_write_json(batch_300_path, batch_300)
    atomic_write_json(batch_1000_path, batch_1000)
    atomic_write_json(batch_3000_path, batch_3000)
    atomic_write_json(batch_5000_path, batch_5000)
    atomic_write_json(production_path, production)
    atomic_write_json(
        paths["private"] / "reconciliation.json",
        {
            "schema_version": "external-26.15-reconciliation-v1",
            "platform": args.platform,
            "analysis_region": args.analysis_region,
            "queue_id": 420,
            "public_patch": args.target_public_patch,
            "compatible_existing_matches": len(compatible_stage3_ids),
            "compatible_match_set_sha256": match_set_sha256(
                compatible_stage3_ids
            ),
            "additional_collection_target": args.additional_target,
            "combined_training_target": (
                len(compatible_stage3_ids) + args.additional_target
            ),
            "source_catalog_sha256": source_catalog_hash,
            "source_stage3a_metadata_sha256": source_metadata_hash,
            "raw_payloads_copied": False,
            "combination_policy": (
                "logical_union_after_independent_lineage_validation"
            ),
        },
    )
    print("External collection preparation complete")
    print(f"  platform: {args.platform}")
    print(f"  compatible existing matches: {len(compatible_stage3_ids)}")
    print(f"  additional target: {args.additional_target}")
    print(f"  smoke config: {smoke_path}")
    print(f"  300-request production batch config: {batch_300_path}")
    print(f"  1000-request production batch config: {batch_1000_path}")
    print(f"  3000-request production batch config: {batch_3000_path}")
    print(f"  5000-request production batch config: {batch_5000_path}")
    print(f"  production config: {production_path}")
    print(f"  private deduplication index: {index_path}")
    print(f"  configured live root: {live_platform_directory}")
    print("  raw payloads copied: False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
