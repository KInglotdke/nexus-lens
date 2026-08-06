from pathlib import Path

import pytest

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.private_dedup import (
    create_private_deduplication_index,
    file_sha256,
    load_private_deduplication_index,
    verified_catalog_match_ids,
)


def test_source_catalog_is_read_only_and_index_round_trips(tmp_path: Path) -> None:
    catalog_path = tmp_path / "source.sqlite3"
    with ProcessingCatalog(catalog_path) as catalog:
        catalog.record_processed(
            match_id="PRIVATE-ONE",
            routing_region="eune",
            api_game_version="16.15.1.1",
            api_patch="16.15",
            public_patch="26.15",
            patch_resolution_method="synthetic",
            patch_resolution_status="resolved",
            queue_id=420,
            source_snapshot="private-source",
        )
        catalog.record_processed(
            match_id="OTHER-PATCH",
            routing_region="eune",
            api_game_version="16.14.1.1",
            api_patch="16.14",
            public_patch="26.14",
            patch_resolution_method="synthetic",
            patch_resolution_status="resolved",
            queue_id=420,
            source_snapshot="private-source",
        )
    before = file_sha256(catalog_path)

    match_ids = verified_catalog_match_ids(
        catalog_path,
        analysis_region="EUNE",
        public_patch="26.15",
    )

    assert match_ids == {"PRIVATE-ONE"}
    assert file_sha256(catalog_path) == before
    index_path = tmp_path / "private" / "dedup.sqlite3"
    create_private_deduplication_index(
        index_path,
        match_ids=match_ids,
        platform="eun1",
        analysis_region="EUNE",
        public_patch="26.15",
        queue_id=420,
        source_catalog_sha256=before,
        source_stage3a_metadata_sha256="a" * 64,
    )
    loaded = load_private_deduplication_index(
        index_path,
        platform="eun1",
        analysis_region="EUNE",
        public_patch="26.15",
    )
    assert loaded == match_ids


def test_private_index_scope_and_immutable_content_are_validated(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "dedup.sqlite3"
    values = {
        "index_path": index_path,
        "match_ids": {"PRIVATE-ONE"},
        "platform": "eun1",
        "analysis_region": "EUNE",
        "public_patch": "26.15",
        "queue_id": 420,
        "source_catalog_sha256": "b" * 64,
        "source_stage3a_metadata_sha256": "c" * 64,
    }
    create_private_deduplication_index(**values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="scope or integrity"):
        load_private_deduplication_index(
            index_path,
            platform="euw1",
            analysis_region="EUW",
            public_patch="26.15",
        )
    with pytest.raises(ValueError, match="differs"):
        create_private_deduplication_index(
            **{**values, "match_ids": {"PRIVATE-TWO"}}  # type: ignore[arg-type]
        )
