"""Build the private Stage 3.5A dataset and aggregate-only feasibility audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from nexus_lens.stage35a import build_stage35a_dataset, write_stage35a_dataset
from nexus_lens.timeline_collection import load_stage35_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Stage 3.5A timelines and construct symmetric top-lane "
            "trajectory rows with strictly historical familiarity features."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Run the complete transformation and audit without writing files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_stage35_config(args.config)
        dataset = build_stage35a_dataset(config)
        if not args.validate_only:
            write_stage35a_dataset(dataset, config)
    except Exception as error:
        category = _safe_category(error)
        print(
            f"Stage 3.5A processing failed closed: category={category}; "
            "no partial publication was retained."
        )
        return 1
    mode = "validation-only; no files written" if args.validate_only else "published"
    print("Nexus Lens Stage 3.5A feasibility summary")
    print(f"  mode: {mode}")
    print(f"  downloaded matches: {dataset.downloaded_match_count}")
    print(f"  eligible matches: {dataset.all_eligible_match_count}")
    print(f"  selected matches: {dataset.selected_match_count}")
    print(f"  focal rows: {len(dataset.rows)}")
    print(f"  exclusions: {dataset.exclusions}")
    print(f"  dataset sha256: {dataset.dataset_sha256}")
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
