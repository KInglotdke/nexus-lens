"""Run the frozen pooled patch-26.15 development experiment offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.pooled_development import (
    PooledDevelopmentConfig,
    RuntimeSource,
    build_pooled_development_result,
    construct_fold_plan,
    load_pooled_input,
    load_protocol,
    write_pooled_development_result,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the prospectively frozen pooled EUNE/EUW patch-26.15 nested-CV "
            "development baseline without loading .env or contacting Riot."
        )
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--eune-external", type=Path, required=True)
    parser.add_argument("--eune-retained", type=Path, required=True)
    parser.add_argument("--euw-external", type=Path, required=True)
    parser.add_argument("--euw-retained", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--protocol-tag-object", required=True)
    parser.add_argument("--protocol-tag-commit", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan-only",
        action="store_true",
        help="Verify sources, counts, union hash, and folds without fitting or writing",
    )
    mode.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--diagnostic-log",
        type=Path,
        help=(
            "Write privacy-safe phase/counter JSONL outside the scientific output "
            "directory; the path must not already exist"
        ),
    )
    return parser


class _DiagnosticLog:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("x", encoding="utf-8", newline="\n")

    def __call__(self, event: dict[str, object]) -> None:
        self._handle.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _diagnostic_callback(
    path: Path | None, output_directory: Path
) -> _DiagnosticLog | None:
    if path is None:
        return None
    resolved = path.resolve()
    output = output_directory.resolve()
    if resolved == output or output in resolved.parents:
        raise ValueError("diagnostic log must be outside the scientific output")
    if resolved.exists():
        raise ValueError("diagnostic log already exists")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return _DiagnosticLog(resolved)


def main() -> int:
    args = _parser().parse_args()
    diagnostic = None
    try:
        diagnostic = _diagnostic_callback(
            args.diagnostic_log, args.output_directory
        )
        protocol = load_protocol(args.protocol)
        pooled = load_pooled_input(
            protocol=protocol,
            runtime_sources=(
                RuntimeSource("eune", "external", args.eune_external),
                RuntimeSource("eune", "retained_private", args.eune_retained),
                RuntimeSource("euw", "external", args.euw_external),
                RuntimeSource("euw", "retained_private", args.euw_retained),
            ),
        )
        if args.plan_only:
            outer = construct_fold_plan(
                pooled.observations,
                fold_count=5,
                seed=34_001,
                scope_label="outer",
            )
            final = construct_fold_plan(
                pooled.observations,
                fold_count=5,
                seed=34_001,
                scope_label="final-selection",
            )
            print("Nexus Lens pooled patch-26.15 development plan")
            print("  mode: plan only; no fitting and no files written")
            print(f"  accepted matches: {pooled.accepted_counts['overall']}")
            print(f"  eligible drafts: {pooled.eligible_counts['overall']}")
            print(f"  combined input sha256: {pooled.combined_input_sha256}")
            print(f"  outer fold sha256: {outer.fingerprint_sha256}")
            print(f"  final-selection fold sha256: {final.fingerprint_sha256}")
            return 0
        result = build_pooled_development_result(
            pooled=pooled,
            protocol=protocol,
            protocol_path=args.protocol,
            output_directory=args.output_directory,
            config=PooledDevelopmentConfig(
                repository_commit=args.repository_commit,
                protocol_tag_object=args.protocol_tag_object,
                protocol_tag_commit=args.protocol_tag_commit,
            ),
            progress_callback=diagnostic,
        )
        if not args.validate_only:
            write_pooled_development_result(
                result, progress_callback=diagnostic
            )
    except Stage3ValidationError as error:
        print(
            f"Pooled development failed closed: category={error.category}; "
            "no partial result was published.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, KeyError):
        print(
            "Pooled development failed closed; no partial result was published.",
            file=sys.stderr,
        )
        return 1
    finally:
        if diagnostic is not None:
            diagnostic.close()
    mode = "validation only; no files written" if args.validate_only else "published"
    print("Nexus Lens pooled patch-26.15 development baseline")
    print(f"  mode: {mode}")
    print("  interpretation: nested-CV development estimate, not final test")
    print(f"  accepted matches: {result.metrics['accepted_counts']['overall']}")
    print(f"  eligible drafts: {result.metrics['eligible_counts']['overall']}")
    print(
        "  combined input sha256: "
        f"{result.experiment_manifest['combined_input_sha256']}"
    )
    print(
        "  outer fold sha256: "
        f"{result.metrics['outer_fold_fingerprint_sha256']}"
    )
    for variant, values in result.metrics["out_of_fold_metrics"].items():
        overall = values["overall"]
        print(
            f"  {variant}: log_loss={overall['log_loss']}, "
            f"brier={overall['brier_score']}, "
            f"accuracy={overall['accuracy_at_0_5']}, "
            f"ece={overall['expected_calibration_error']}"
        )
    for row in result.metrics["final_l2_selection"]:
        print(f"  final L2 {row['variant']}: {row['selected_l2']}")
    print(f"  deterministic bundle sha256: {result.deterministic_bundle_sha256}")
    print("  patch 26.16 used: False")
    print("  recommendation policy authorized: False")
    print(f"  output directory: {args.output_directory.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
