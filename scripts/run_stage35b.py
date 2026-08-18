"""Run the frozen Stage 3.5B zero-fit preflight or one guarded publication."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from nexus_lens.stage35b import (
    build_preflight,
    build_publication_payloads,
    evaluate_development,
    load_execution_config,
    load_protocol,
    reconstruct_bundle_hash,
    write_publication,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or execute the frozen, offline Stage 3.5B rolling-origin "
            "top-lane development experiment."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Validate sealed inputs, folds, schemas and budgets with zero fits/writes."
        ),
    )
    mode.add_argument(
        "--publish",
        action="store_true",
        help="Execute exactly one guarded real-data run and publish atomically.",
    )
    parser.add_argument(
        "--authorize-real-fit",
        action="store_true",
        help="Required with --publish; records explicit authorization.",
    )
    parser.add_argument(
        "--diagnostic-log",
        type=Path,
        help=(
            "Required with --publish and must remain outside the repository and inputs."
        ),
    )
    return parser


class DiagnosticLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.perf_counter()
        self.handle = path.open("x", encoding="utf-8", newline="\n")
        self.events = 0

    def __call__(self, event: dict[str, Any]) -> None:
        if self.events >= 5000:
            raise ValueError("Stage 3.5B diagnostic event ceiling exceeded")
        safe = {**event, "elapsed_seconds": time.perf_counter() - self.started}
        rendered = json.dumps(safe, sort_keys=True, allow_nan=False) + "\n"
        if self.handle.tell() + len(rendered.encode("utf-8")) > 5_000_000:
            raise ValueError("Stage 3.5B diagnostic byte ceiling exceeded")
        self.handle.write(rendered)
        self.handle.flush()
        self.events += 1

    def close(self) -> None:
        self.handle.close()


def main() -> int:
    args = _parser().parse_args()
    diagnostic = None
    try:
        config = load_execution_config(args.config)
        protocol = load_protocol(args.protocol, args.schema)
        files = (
            Path(__file__).resolve(),
            Path(__file__).resolve().parents[1] / "src/nexus_lens/stage35b.py",
        )
        preflight = build_preflight(
            config,
            protocol,
            protocol_path=args.protocol,
            schema_path=args.schema,
            executable_files=files,
        )
        if args.preflight:
            if args.authorize_real_fit or args.diagnostic_log is not None:
                raise ValueError(
                    "preflight cannot accept fit authorization or diagnostics"
                )
            _print_preflight(preflight.summary)
            return 0
        if not args.authorize_real_fit:
            raise ValueError("publish mode requires --authorize-real-fit")
        if args.diagnostic_log is None:
            raise ValueError("publish mode requires --diagnostic-log")
        _verify_clean_repository(args.repository_commit)
        _validate_publication_locations(config, args.diagnostic_log)
        diagnostic = DiagnosticLog(args.diagnostic_log.resolve())
        diagnostic(
            {
                "event": "execution_started",
                "phase": "execution",
                "expected_optimizer_fits": protocol["operation_budget"][
                    "optimizer_fits"
                ],
                "expected_analytic_operations": protocol["operation_budget"][
                    "analytic_training_operations"
                ],
                "expected_bootstrap_model_fits": 0,
            }
        )
        result, ledger, private_rows = evaluate_development(
            preflight, protocol, progress_callback=diagnostic
        )
        diagnostic({"event": "model_evaluation_completed", "phase": "evaluation"})
        public_payloads, private_payloads, bundle_hash = build_publication_payloads(
            preflight=preflight,
            protocol=protocol,
            protocol_path=args.protocol,
            schema_path=args.schema,
            repository_commit=args.repository_commit,
            result=result,
            operation_ledger=ledger,
            private_rows=private_rows,
        )
        diagnostic({"event": "publication_started", "phase": "publication"})
        write_publication(
            public_payloads=public_payloads,
            private_payloads=private_payloads,
            config=config,
        )
        if reconstruct_bundle_hash(config.aggregate_output_directory) != bundle_hash:
            raise ValueError("Stage 3.5B post-publication bundle hash differs")
        diagnostic(
            {
                "event": "publication_completed",
                "phase": "publication",
                "scientific_result_bundle_sha256": bundle_hash,
            }
        )
        _print_result(result, ledger, bundle_hash)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if diagnostic is not None:
            with suppress(OSError, ValueError):
                diagnostic(
                    {
                        "event": "execution_failed",
                        "phase": "execution",
                        "failure_category": type(error).__name__,
                    }
                )
        print(
            "Stage 3.5B failed closed; no partial scientific result was authorized.",
            file=sys.stderr,
        )
        return 1
    finally:
        if diagnostic is not None:
            diagnostic.close()


def _verify_clean_repository(expected_commit: str) -> None:
    if len(expected_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_commit
    ):
        raise ValueError("repository commit is invalid")
    commands = {
        "head": ["git", "-c", "safe.directory=*", "rev-parse", "HEAD"],
        "origin": ["git", "-c", "safe.directory=*", "rev-parse", "origin/main"],
        "status": ["git", "-c", "safe.directory=*", "status", "--porcelain"],
    }
    values = {
        name: subprocess.run(
            command, check=True, capture_output=True, text=True
        ).stdout.strip()
        for name, command in commands.items()
    }
    if (
        values["head"] != expected_commit
        or values["origin"] != expected_commit
        or values["status"]
    ):
        raise ValueError("repository is not clean at the authorized commit")


def _validate_publication_locations(config: Any, diagnostic_path: Path) -> None:
    repository = Path.cwd().resolve()
    diagnostic = diagnostic_path.resolve()
    protected = (
        config.parent_dataset_path.resolve(),
        config.rune_dataset_path.resolve(),
        config.rune_manifest_path.resolve(),
        config.stage34b_bundle_manifest_path.resolve(),
    )
    if (
        diagnostic.exists()
        or repository in diagnostic.parents
        or diagnostic == repository
        or any(
            path == diagnostic or path.parent in diagnostic.parents
            for path in protected
        )
        or not diagnostic.parent.exists()
    ):
        raise ValueError("diagnostic location violates isolation policy")
    if (
        config.aggregate_output_directory.exists()
        or config.private_output_directory.exists()
    ):
        raise ValueError("Stage 3.5B output already exists")


def _print_preflight(summary: dict[str, Any]) -> None:
    print("Nexus Lens Stage 3.5B zero-fit preflight")
    print("  model fits: 0; optimizer fits: 0; bootstrap fits: 0; files written: 0")
    print(f"  focal rows: {summary['focal_rows']}")
    print(f"  match groups: {summary['match_groups']}")
    print(
        f"  development evaluation groups: {summary['development_evaluation_groups']}"
    )
    print(f"  final holdout groups: {summary['final_holdout_groups']}")
    print(f"  protocol sha256: {summary['protocol_sha256']}")
    print(f"  fold sha256: {summary['fold_sha256']}")
    print(f"  feature schema sha256: {summary['feature_schema_sha256']}")
    print(f"  executable bundle sha256: {summary['executable_bundle_sha256']}")
    print(f"  scientific preflight sha256: {summary['scientific_preflight_sha256']}")
    operations = summary["operation_budget"]["total_training_operations"]
    print(f"  expected operations: {operations}")


def _print_result(
    result: dict[str, Any], ledger: dict[str, Any], bundle_hash: str
) -> None:
    print("Nexus Lens Stage 3.5B rolling-origin development publication")
    print("  interpretation: development estimates; final holdout not evaluated")
    print(f"  optimizer fits: {ledger['actual']['optimizer_fits']}")
    print(f"  analytic operations: {ledger['actual']['analytic_training_operations']}")
    print("  bootstrap model fits: 0")
    print(f"  matchup gate: {result['gate_decisions']['matchup']['passed']}")
    print(f"  keystone gate: {result['gate_decisions']['keystone']['passed']}")
    champion_keystone = result["gate_decisions"]["champion_keystone"]["passed"]
    print(f"  champion-keystone gate: {champion_keystone}")
    print(f"  CatBoost gate: {result['gate_decisions']['catboost']['passed']}")
    print(f"  scientific result bundle sha256: {bundle_hash}")
    print("  product recommendation authorized: False")


if __name__ == "__main__":
    raise SystemExit(main())
