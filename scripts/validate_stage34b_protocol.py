"""Validate and summarize the frozen Stage 3.4B-1 protocol without fitting."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.stage34b import (
    expected_fit_accounting,
    load_stage34b_protocol,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the prospectively frozen Stage 3.4B-1 patch-26.15 protocol "
            "and print its zero-fit execution budget."
        )
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        protocol = load_stage34b_protocol(
            args.protocol, schema_path=args.schema
        )
    except Stage3ValidationError as error:
        print(
            f"Stage 3.4B-1 protocol failed closed: category={error.category}",
            file=sys.stderr,
        )
        return 1
    budget = expected_fit_accounting(protocol)
    print("Nexus Lens Stage 3.4B-1 prospective protocol")
    print("  mode: protocol validation only; zero model fits; zero files written")
    print(f"  eligible drafts: {protocol['data_scope']['eligible_drafts']}")
    print(f"  outer blocks: {len(protocol['validation']['outer_blocks'])}")
    print(
        "  predictive training operations: "
        f"{budget['expected_predictive_training_operations']}"
    )
    print(f"  predictive optimizer fits: {budget['predictive_optimizer_fits']}")
    print(
        "  metric-only calibration evaluations: "
        f"{budget['calibration_metric_evaluations']}"
    )
    print(
        "  total optimizer invocations upper bound: "
        f"{budget['total_optimizer_invocations_upper_bound_including_metric_regressions']}"
    )
    print("  real development metrics calculated: False")
    print("  future sealed temporal holdout accessed: False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
