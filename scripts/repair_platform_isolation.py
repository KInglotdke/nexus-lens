"""Validate or repair accepted Match-V5 payloads from the wrong platform."""

import argparse
from pathlib import Path

from nexus_lens.platform_repair import (
    PlatformIsolationRepairError,
    repair_platform_isolation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit and atomically reclassify accepted payloads whose platform "
            "does not match a single-platform population checkpoint."
        )
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--raw-run-dir", type=Path, required=True)
    parser.add_argument("--normalized-root", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path)
    parser.add_argument("--expected-platform", required=True)
    parser.add_argument("--expected-mismatches", type=int, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Audit every store and write nothing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = repair_platform_isolation(
            checkpoint_path=args.checkpoint,
            manifest_path=args.manifest,
            catalog_path=args.catalog,
            raw_run_dir=args.raw_run_dir,
            normalized_root=args.normalized_root,
            backup_directory=args.backup_directory,
            expected_platform=args.expected_platform,
            expected_mismatches=args.expected_mismatches,
            validate_only=args.validate_only,
        )
    except PlatformIsolationRepairError as error:
        print(f"Platform-isolation repair failed: {error}")
        return 1
    values = report.as_dict()
    print("Nexus Lens platform-isolation repair")
    for key, value in values.items():
        print(f"  {key.replace('_', ' ')}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
