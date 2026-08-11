"""Privacy-safe deterministic inventories for sealed external data trees."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

INVENTORY_ALGORITHM = "sha256-relative-path-size-content-sha256-v1"
_READ_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TreeInventory:
    """Aggregate-only description of a content tree."""

    algorithm: str
    file_count: int
    byte_count: int
    sha256: str

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def inventory_tree(root: Path) -> TreeInventory:
    """Hash every regular file without retaining or emitting individual paths.

    Files are ordered by their root-relative POSIX path. Each aggregate digest
    record is ``relative-path NUL byte-count NUL content-sha256 LF``. Timestamps,
    drive letters, and the absolute root are deliberately excluded.
    """

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("inventory root must be a directory")

    paths: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("inventory trees may not contain symbolic links")
        if path.is_file():
            paths.append((path.relative_to(root).as_posix(), path))
    paths.sort(key=lambda item: item[0])

    aggregate = hashlib.sha256()
    byte_count = 0
    for relative_path, path in paths:
        size = path.stat().st_size
        content_hash = _sha256_file(path)
        aggregate.update(relative_path.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(content_hash.encode("ascii"))
        aggregate.update(b"\n")
        byte_count += size

    return TreeInventory(
        algorithm=INVENTORY_ALGORITHM,
        file_count=len(paths),
        byte_count=byte_count,
        sha256=aggregate.hexdigest(),
    )


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for one regular file."""

    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("hash target must be a regular, non-symlink file")
    return _sha256_file(resolved)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()
