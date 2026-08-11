"""Preflight or execute the frozen Stage 3.4B-1 experiment offline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from nexus_lens.canonical import Stage3ValidationError
from nexus_lens.stage34b import evaluate_stage34b, load_stage34b_protocol
from nexus_lens.stage34b_operations import (
    OperationalSource,
    build_publication_payloads,
    build_stage34b_preflight,
    load_operational_amendment,
    load_stage34b_operational_input,
    reconstruct_bundle_hash,
    validate_public_payload,
    write_publication_bundle,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run zero-fit preflight or exactly one authorized offline Stage 3.4B-1 "
            "patch-26.15 development publication invocation."
        )
    )
    parser.add_argument("--scientific-protocol", type=Path, required=True)
    parser.add_argument("--scientific-schema", type=Path, required=True)
    parser.add_argument("--operational-amendment", type=Path, required=True)
    parser.add_argument("--operational-schema", type=Path, required=True)
    parser.add_argument("--stage34a-protocol", type=Path, required=True)
    parser.add_argument("--eune-external", type=Path, required=True)
    parser.add_argument("--eune-retained", type=Path, required=True)
    parser.add_argument("--euw-external", type=Path, required=True)
    parser.add_argument("--euw-retained", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--diagnostic-log", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Verify lineage, counts, timestamps, folds and paths with zero fits",
    )
    mode.add_argument(
        "--publish",
        action="store_true",
        help="Perform exactly one complete real-data evaluation and atomic publication",
    )
    parser.add_argument(
        "--authorize-real-fit",
        action="store_true",
        help="Required with --publish; records explicit external authorization",
    )
    return parser


class _DiagnosticLog:
    def __init__(self, path: Path, *, maximum_events: int, maximum_bytes: int) -> None:
        self.path = path
        self.maximum_events = maximum_events
        self.maximum_bytes = maximum_bytes
        self.bytes_written = 0
        self.started_wall = time.perf_counter()
        self.started_cpu = time.process_time()
        self.events: list[dict[str, Any]] = []
        self.handle = path.open("x", encoding="utf-8", newline="\n")

    def __call__(self, event: dict[str, Any]) -> None:
        validate_public_payload(event)
        if len(self.events) >= self.maximum_events:
            raise Stage3ValidationError(
                "stage34b_diagnostic_event_limit", "diagnostic event limit exceeded"
            )
        safe = {
            **event,
            "elapsed_wall_seconds": time.perf_counter() - self.started_wall,
            "elapsed_process_cpu_seconds": time.process_time() - self.started_cpu,
        }
        rendered = json.dumps(safe, sort_keys=True, allow_nan=False) + "\n"
        encoded_size = len(rendered.encode("utf-8"))
        if self.bytes_written + encoded_size > self.maximum_bytes:
            raise Stage3ValidationError(
                "stage34b_diagnostic_size_limit", "diagnostic size limit exceeded"
            )
        self.events.append(safe)
        self.handle.write(rendered)
        self.handle.flush()
        self.bytes_written += encoded_size

    def close(self) -> None:
        self.handle.close()


def main() -> int:
    args = _parser().parse_args()
    diagnostic = None
    try:
        _verify_git_state(args.repository_commit)
        sources = _sources(args)
        _validate_locations(
            output_directory=args.output_directory,
            diagnostic_path=args.diagnostic_log,
            source_directories=tuple(row.input_directory for row in sources),
        )
        protocol = load_stage34b_protocol(
            args.scientific_protocol, schema_path=args.scientific_schema
        )
        amendment = load_operational_amendment(
            args.operational_amendment, schema_path=args.operational_schema
        )
        operational_input = load_stage34b_operational_input(
            scientific_protocol=protocol,
            stage34a_protocol_path=args.stage34a_protocol,
            sources=sources,
        )
        preflight = build_stage34b_preflight(
            operational_input=operational_input,
            scientific_protocol=protocol,
            operational_amendment=amendment,
        )
        if args.preflight:
            if args.authorize_real_fit:
                raise ValueError("real-fit authorization is invalid in preflight mode")
            _print_preflight(preflight.summary)
            return 0
        if not args.authorize_real_fit:
            raise ValueError("publish mode requires explicit real-fit authorization")

        diagnostic = _DiagnosticLog(
            args.diagnostic_log.resolve(),
            maximum_events=amendment["diagnostics"]["maximum_events"],
            maximum_bytes=amendment["diagnostics"]["maximum_bytes"],
        )
        diagnostic(
            {
                "event": "execution_started",
                "phase": "execution",
                "repository_commit": args.repository_commit,
                "expected_predictive_training_operations": amendment[
                    "fit_reconciliation"
                ]["predictive_training_operations"],
                "expected_predictive_optimizer_fits": amendment[
                    "fit_reconciliation"
                ]["predictive_optimizer_fits"],
                "expected_calibration_evaluations": amendment[
                    "fit_reconciliation"
                ]["calibration_evaluations"],
                "expected_bootstrap_model_fits": amendment["fit_reconciliation"][
                    "bootstrap_model_fits"
                ],
            }
        )
        diagnostic(
            {
                "event": "source_preflight_completed",
                "phase": "preflight",
                "eligible_drafts": preflight.summary["eligible_counts"]["overall"],
                "outer_evaluation_drafts": preflight.summary[
                    "outer_evaluation_drafts"
                ],
            }
        )
        pairs = tuple(
            (row["candidate"], row["comparator"])
            for row in amendment["bootstrap_expansion"]["comparison_pairs"]
        )
        evaluation = evaluate_stage34b(
            operational_input.rows,
            protocol,
            enforce_frozen_counts=True,
            progress_callback=diagnostic,
            additional_paired_comparisons=pairs,
        )
        diagnostic({"event": "publication_started", "phase": "publication"})
        payloads, bundle_hash = build_publication_payloads(
            evaluation=evaluation,
            preflight=preflight,
            scientific_protocol=protocol,
            operational_amendment=amendment,
            repository_commit=args.repository_commit,
            diagnostic_events=tuple(diagnostic.events),
        )
        output = write_publication_bundle(payloads, args.output_directory)
        reconstructed = reconstruct_bundle_hash(output)
        if reconstructed != bundle_hash:
            raise Stage3ValidationError(
                "stage34b_postpublication_hash", "published bundle hash differs"
            )
        diagnostic(
            {
                "event": "publication_completed",
                "phase": "publication",
                "artifact_count": len(payloads),
                "bundle_sha256": bundle_hash,
            }
        )
        _print_publication(evaluation.artifact, bundle_hash)
        return 0
    except (
        Stage3ValidationError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        category = getattr(error, "category", "operational_preflight_or_execution")
        if diagnostic is not None:
            with suppress(Stage3ValidationError):
                diagnostic(
                    {
                        "event": "execution_failed",
                        "phase": "execution",
                        "failure_category": category,
                    }
                )
        print(
            f"Stage 3.4B-1 failed closed: category={category}; "
            "no partial scientific result was published.",
            file=sys.stderr,
        )
        return 1
    finally:
        if diagnostic is not None:
            diagnostic.close()


def _sources(args: argparse.Namespace) -> tuple[OperationalSource, ...]:
    return (
        OperationalSource("eune", "external", args.eune_external),
        OperationalSource("eune", "retained_private", args.eune_retained),
        OperationalSource("euw", "external", args.euw_external),
        OperationalSource("euw", "retained_private", args.euw_retained),
    )


def _verify_git_state(expected_commit: str) -> None:
    if len(expected_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_commit
    ):
        raise ValueError("repository commit is invalid")
    head = subprocess.run(
        ["git", "-c", "safe.directory=*", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    origin = subprocess.run(
        ["git", "-c", "safe.directory=*", "rev-parse", "origin/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-c", "safe.directory=*", "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if head != expected_commit or origin != expected_commit or status:
        raise ValueError("repository is not clean at the expected remote commit")


def _validate_locations(
    *,
    output_directory: Path,
    diagnostic_path: Path,
    source_directories: tuple[Path, ...],
) -> None:
    repository = Path.cwd().resolve()
    output = output_directory.resolve()
    diagnostic = diagnostic_path.resolve()
    sources = tuple(path.resolve() for path in source_directories)
    expected_output = (
        repository
        / "config/evaluation/stage3.4b-1-patch26.15-protocol-v1/development-v1"
    ).resolve()
    if output != expected_output or output.exists():
        raise ValueError("output must be the new protocol development-v1 directory")
    if (
        diagnostic.exists()
        or diagnostic in (repository, output)
        or repository in diagnostic.parents
        or output in diagnostic.parents
        or any(
            diagnostic == source or source in diagnostic.parents for source in sources
        )
    ):
        raise ValueError("diagnostic location violates isolation policy")
    if not output.parent.exists() or not diagnostic.parent.exists():
        raise ValueError("output and diagnostic parents must already exist")
    if any(
        output == source
        or source in output.parents
        or output in source.parents
        for source in sources
    ):
        raise ValueError("scientific output and sealed inputs must be disjoint")
    _preflight_writable_directory(output.parent)
    _preflight_writable_directory(diagnostic.parent)


def _preflight_writable_directory(directory: Path) -> None:
    probe_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".stage34b-write-probe-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as handle:
            probe_path = Path(handle.name)
            handle.write("stage34b-write-probe\n")
        if probe_path.read_text(encoding="utf-8") != "stage34b-write-probe\n":
            raise OSError("write probe content differs")
        with probe_path.open("a", encoding="utf-8") as handle:
            handle.write("append-probe\n")
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


def _print_preflight(summary: dict[str, Any]) -> None:
    print("Nexus Lens Stage 3.4B-1 zero-fit preflight")
    print("  mode: preflight only; zero model fits; zero scientific writes")
    print(f"  eligible drafts: {summary['eligible_counts']['overall']}")
    print(f"  initial training drafts: {summary['initial_training_drafts']}")
    print(f"  outer evaluation drafts: {summary['outer_evaluation_drafts']}")
    print(f"  outer blocks: {len(summary['outer_blocks'])}")
    print(f"  combined input sha256: {summary['combined_input_sha256']}")
    print(f"  timestamp join sha256: {summary['timestamp_join_sha256']}")
    print(f"  outer fold sha256: {summary['outer_fold_sha256']}")
    print("  paired evaluation rows identical: True")


def _print_publication(artifact: dict[str, Any], bundle_hash: str) -> None:
    print("Nexus Lens Stage 3.4B-1 patch-26.15 development publication")
    print("  interpretation: rolling-origin development estimate, not final test")
    print(
        "  predictive optimizer fits: "
        f"{artifact['fit_accounting']['observed_predictive_optimizer_fits']}"
    )
    print(
        "  calibration evaluations: "
        f"{artifact['fit_accounting']['observed_calibration_evaluations']}"
    )
    print(f"  future holdout gate passed: {artifact['future_holdout_gate_passed']}")
    print(f"  scientific deterministic bundle sha256: {bundle_hash}")
    print("  recommendation policy authorized: False")


if __name__ == "__main__":
    raise SystemExit(main())
