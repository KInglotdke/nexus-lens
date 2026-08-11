from __future__ import annotations

from pathlib import Path

import pytest

from nexus_lens.data_seal import INVENTORY_ALGORITHM, inventory_tree, sha256_file


def test_inventory_is_content_based_root_independent_and_deterministic(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "nested").mkdir(parents=True)
        (root / "a.txt").write_bytes(b"alpha\n")
        (root / "nested" / "b.bin").write_bytes(bytes((0, 1, 2, 255)))

    expected_sha256 = (
        "da22b42eaa6587a673c76bc818540c844ffad324cc254294947abf3e1a66c097"
    )
    result = inventory_tree(first)

    assert result.algorithm == INVENTORY_ALGORITHM
    assert result.file_count == 2
    assert result.byte_count == 10
    assert result.sha256 == expected_sha256
    assert inventory_tree(first) == result
    assert inventory_tree(second) == result

    (second / "nested" / "b.bin").write_bytes(bytes((0, 1, 3, 255)))
    assert inventory_tree(second).sha256 != result.sha256


def test_hash_and_inventory_reject_non_regular_targets(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(ValueError, match="regular"):
        sha256_file(directory)
    with pytest.raises(FileNotFoundError):
        inventory_tree(tmp_path / "missing")
