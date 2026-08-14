"""Prepare a private, deduplicated patch-26.15 Stage 3.5A extension run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from nexus_lens.population_state import atomic_write_json
from nexus_lens.private_dedup import create_private_deduplication_index
from nexus_lens.timeline_collection import (
    load_stage35_config,
    verify_stage31_sources,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a platform-isolated, exact-patch population-extension config "
            "whose private deduplication index contains every current Stage 3.5A "
            "source match. This command is offline and does not load .env."
        )
    )
    parser.add_argument("--stage35a-config", type=Path, required=True)
    parser.add_argument("--platform", choices=("eun1", "euw1"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--additional-target", type=int, required=True)
    parser.add_argument("--max-players", type=int, default=1_000)
    parser.add_argument("--max-match-ids", type=int, default=20_000)
    parser.add_argument("--max-requests", type=int, default=15_000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.additional_target < 1:
            raise ValueError("additional target must be positive")
        config = load_stage35_config(args.stage35a_config)
        matches = verify_stage31_sources(config)
        selected = {row.match_id for row in matches if row.platform == args.platform}
        if not selected:
            raise ValueError("platform has no Stage 3.5A source matches")
        analysis_region = "eune" if args.platform == "eun1" else "euw"
        platform_root = args.output_root.resolve() / analysis_region
        private_root = platform_root / "private"
        config_root = platform_root / "config"
        index = private_root / "existing-stage35a-dedup.sqlite3"
        source_hashes = {
            f"{source.label}:{name}": digest
            for source in config.sources
            if source.platform == args.platform
            for name, digest in source.file_sha256.items()
        }
        bundle_hash = _sha256_json(source_hashes)
        metadata_hash = _sha256_json(
            {
                key: value
                for key, value in source_hashes.items()
                if key.endswith(":metadata.json")
            }
        )
        create_private_deduplication_index(
            index,
            match_ids=selected,
            platform=args.platform,
            analysis_region=analysis_region,
            public_patch=config.public_patch,
            queue_id=config.queue_id,
            source_catalog_sha256=bundle_hash,
            source_stage3a_metadata_sha256=metadata_hash,
        )
        population_config = {
            "platforms": [args.platform],
            "target_public_patch": config.public_patch,
            "patch_window_size": 1,
            "tiers": ["GOLD", "PLATINUM", "EMERALD", "DIAMOND"],
            "divisions": ["I", "II", "III", "IV"],
            "target_matches_per_platform": args.additional_target,
            "max_players_per_platform": args.max_players,
            "initial_history_batch_size": 5,
            "max_history_per_player": 20,
            "older_patch_stop_threshold": 2,
            "pages_per_stratum": 5,
            "max_start_page": 5,
            "max_match_ids_per_platform": args.max_match_ids,
            "max_requests_per_platform": args.max_requests,
            "concurrency": 1,
            "seed": 35001,
            "stop_on_newer_patch": False,
            "deduplication_indexes": [str(index.resolve())],
            "raw_root": str((platform_root / "raw").resolve()),
            "processed_root": str((platform_root / "processed").resolve()),
            "snapshot_root": str(
                (platform_root / "snapshots" / "population").resolve()
            ),
        }
        config_path = config_root / "population-extension-26.15.json"
        atomic_write_json(config_path, population_config)
        atomic_write_json(
            private_root / "preparation-summary.json",
            {
                "schema_version": "stage3.5a-extension-preparation-v1",
                "platform": args.platform,
                "analysis_region": analysis_region,
                "queue_id": config.queue_id,
                "public_patch": config.public_patch,
                "existing_match_count": len(selected),
                "existing_match_set_sha256": _match_set_sha256(selected),
                "source_bundle_sha256": bundle_hash,
                "additional_target": args.additional_target,
                "balanced_tier_division_sampling": True,
                "newest_first_history": True,
                "continue_through_newer_patch": True,
                "network_requests": 0,
                "credentials_loaded": False,
            },
        )
        print("Nexus Lens Stage 3.5A extension preparation")
        print(f"  platform: {args.platform}")
        print(f"  existing unique matches: {len(selected)}")
        print(f"  additional target: {args.additional_target}")
        print("  patch: 26.15 exact")
        print("  queue: 420")
        print("  network requests: 0")
        return 0
    except (OSError, ValueError):
        print("Stage 3.5A extension preparation failed: category=validation_or_storage")
        return 1


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _match_set_sha256(match_ids: set[str]) -> str:
    digest = hashlib.sha256()
    for match_id in sorted(match_ids):
        digest.update(match_id.encode())
        digest.update(b"\n")
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
