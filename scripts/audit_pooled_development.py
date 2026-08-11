"""Run zero-fit aggregate diagnostics on the frozen pooled Stage 3.4A result."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.pooled_development import RuntimeSource
from nexus_lens.pooled_diagnostics import (
    diagnostic_sha256,
    run_post_publication_diagnostic,
    write_post_publication_diagnostic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create deterministic aggregate-only, zero-fit diagnostics for the "
            "frozen pooled patch-26.15 Stage 3.4A publication."
        )
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--results-directory", type=Path, required=True)
    parser.add_argument("--eune-external", type=Path, required=True)
    parser.add_argument("--eune-retained", type=Path, required=True)
    parser.add_argument("--euw-external", type=Path, required=True)
    parser.add_argument("--euw-retained", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--validate-only", action="store_true", help="Build and hash without writing"
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        diagnostic = run_post_publication_diagnostic(
            protocol_path=args.protocol,
            results_directory=args.results_directory,
            runtime_sources=(
                RuntimeSource("eune", "external", args.eune_external),
                RuntimeSource("eune", "retained_private", args.eune_retained),
                RuntimeSource("euw", "external", args.euw_external),
                RuntimeSource("euw", "retained_private", args.euw_retained),
            ),
        )
        if not args.validate_only:
            write_post_publication_diagnostic(diagnostic, args.output_directory)
    except Stage3ValidationError as error:
        print(
            "Pooled post-publication diagnostic failed closed: "
            f"category={error.category}; no partial artifact was published.",
            file=sys.stderr,
        )
        return 1
    except (OSError, KeyError, TypeError, ValueError):
        print(
            "Pooled post-publication diagnostic failed closed; no partial artifact "
            "was published.",
            file=sys.stderr,
        )
        return 1
    mode = "validation only; no files written" if args.validate_only else "published"
    print("Nexus Lens Stage 3.4A post-publication diagnostic")
    print(f"  mode: {mode}")
    print("  scope: patch 26.15 in-sample structural diagnostics, not performance")
    print("  model fits: 0")
    eligible = diagnostic["dataset_and_target"]["eligible_counts"]["overall"]
    print(f"  eligible drafts: {eligible}")
    print(f"  diagnostic sha256: {diagnostic_sha256(diagnostic)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
