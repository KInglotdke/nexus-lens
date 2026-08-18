"""Build the zero-model Stage 3.5A focal-keystone feasibility addendum."""

from __future__ import annotations

import argparse
from pathlib import Path

from nexus_lens.stage35a_runes import (
    build_rune_addendum_dataset,
    load_rune_addendum_config,
    write_rune_addendum_dataset,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an outcome-blind, patch-correct focal-keystone feature lineage "
            "and aggregate support audit without fitting a model."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Run all joins, mapping, alignment, and audit checks without writing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_rune_addendum_config(args.config)
        dataset = build_rune_addendum_dataset(config)
        if not args.validate_only:
            write_rune_addendum_dataset(dataset, config)
    except Exception as error:
        print(
            "Stage 3.5A rune addendum failed closed: "
            f"category={_safe_category(error)}; no partial publication was retained."
        )
        return 1
    mode = "validation-only; no files written" if args.validate_only else "published"
    coverage = dataset.audit["mapping_coverage"]
    print("Nexus Lens Stage 3.5A rune-feasibility summary")
    print(f"  mode: {mode}")
    print(f"  parent focal rows: {len(dataset.features)}")
    print(f"  match groups: {dataset.quality_report['match_groups']}")
    print(f"  mapped rows: {coverage['mapped_focal_rows']}")
    print(f"  unmapped rows: {coverage['unmapped_focal_rows']}")
    print(f"  observed keystones: {coverage['observed_keystones']}")
    print(f"  derived dataset sha256: {dataset.derived_dataset_sha256}")
    print("  outcome-conditioned statistics: 0")
    print("  predictive model fits: 0")
    return 0


def _safe_category(error: Exception) -> str:
    if isinstance(error, (TypeError, ValueError)):
        return "validation"
    if isinstance(error, OSError):
        return "local_storage"
    return "unexpected"


if __name__ == "__main__":
    raise SystemExit(main())
