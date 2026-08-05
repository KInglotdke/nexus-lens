"""Build the Stage 3.4A pre-26.16 prospective freeze bundle offline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.evaluation_freeze import (
    FREEZE_ID,
    PlatformFreezeSpec,
    build_freeze_bundle,
    validate_freeze_bundle,
    write_freeze_bundle,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use only retained 26.14 -> 26.15 development data to calculate the "
            "prospective sample-size analysis and freeze untouched 26.16 settings."
        )
    )
    parser.add_argument("--eune-input-run", type=Path, required=True)
    parser.add_argument("--eune-development-output", type=Path, required=True)
    parser.add_argument("--euw-input-run", type=Path, required=True)
    parser.add_argument("--euw-development-output", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--dependency-lock", type=Path, required=True)
    parser.add_argument("--expected-match-count", type=int, default=1_000)
    parser.add_argument("--power-bootstrap-replicates", type=int, default=10_000)
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        action="store_true",
        dest="validate_only",
        help="Calculate and validate the complete bundle without writing files",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    output_directory = args.output_root / FREEZE_ID
    specs = (
        PlatformFreezeSpec(
            analysis_region="EUNE",
            platform="eun1",
            input_directory=args.eune_input_run,
            development_output_directory=args.eune_development_output,
            composition_only_l2=0.1,
            composition_plus_matchups_l2=0.1,
        ),
        PlatformFreezeSpec(
            analysis_region="EUW",
            platform="euw1",
            input_directory=args.euw_input_run,
            development_output_directory=args.euw_development_output,
            composition_only_l2=1.0,
            composition_plus_matchups_l2=1.0,
        ),
    )
    try:
        bundle = build_freeze_bundle(
            specs=specs,
            output_directory=output_directory,
            dependency_lock_path=args.dependency_lock,
            expected_match_count=args.expected_match_count,
            power_replicates=args.power_bootstrap_replicates,
        )
        validate_freeze_bundle(bundle, args.dependency_lock)
        if not args.validate_only:
            write_freeze_bundle(bundle)
    except (OSError, ValueError, KeyError, Stage3ValidationError):
        print(
            "Freeze analysis failed closed; no future-data claim or partial bundle "
            "was published.",
            file=sys.stderr,
        )
        return 1
    mode = "validation only; no files written" if args.validate_only else "published"
    print("Nexus Lens Stage 3.4A experimental freeze")
    print(f"  mode: {mode}")
    print("  development fold: 26.14 -> 26.15 only")
    print("  frozen future fold: 26.15 -> untouched 26.16")
    print("  platforms pooled: False")
    print("  primary comparison: composition-only versus fixed 0.5")
    print("  primary metric: paired match-level log loss")
    print(f"  output directory: {output_directory.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
