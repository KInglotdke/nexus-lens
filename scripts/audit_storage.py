"""Print a read-only Nexus Lens storage-readiness audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nexus_lens.storage_audit import audit_storage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory Nexus Lens storage and project 10,000 matches per platform. "
            "This command is read-only and never moves, compresses, or deletes "
            "artifacts."
        )
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = audit_storage(args.data_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
