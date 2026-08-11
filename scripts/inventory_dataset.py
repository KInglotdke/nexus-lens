"""Calculate an aggregate-only deterministic inventory of a sealed data tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nexus_lens.data_seal import inventory_tree


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read every regular file below a data root and print only a "
            "privacy-safe aggregate content inventory. No files are written."
        )
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--scope-label",
        required=True,
        help="Non-sensitive logical label printed instead of the absolute root",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--expected-files", type=int)
    parser.add_argument("--expected-bytes", type=int)
    parser.add_argument("--expected-sha256")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.repeat < 1:
        print("Inventory failed: repeat must be positive.", file=sys.stderr)
        return 2
    try:
        inventories = [inventory_tree(args.root) for _ in range(args.repeat)]
    except (OSError, ValueError):
        print(
            "Inventory failed while reading the requested tree; no path or "
            "file-level details were emitted.",
            file=sys.stderr,
        )
        return 1
    first = inventories[0]
    if any(item != first for item in inventories[1:]):
        print("Inventory failed: repeated calculations differed.", file=sys.stderr)
        return 1
    expected = {
        "file_count": args.expected_files,
        "byte_count": args.expected_bytes,
        "sha256": args.expected_sha256.lower() if args.expected_sha256 else None,
    }
    actual = first.as_dict()
    if any(
        value is not None and actual[field] != value
        for field, value in expected.items()
    ):
        print("Inventory failed: aggregate expectation mismatch.", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "scope": args.scope_label,
                **actual,
                "repeat_count": args.repeat,
                "repeated_results_identical": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
