import sqlite3
from pathlib import Path

from nexus_lens.catalog import ProcessingCatalog
from nexus_lens.processing import SnapshotProcessor
from nexus_lens.reporting import build_feasibility_report, write_feasibility_reports
from tests.factories import make_match_payload, write_snapshot


def make_processor(
    tmp_path: Path,
) -> tuple[ProcessingCatalog, SnapshotProcessor, Path]:
    processed = tmp_path / "processed"
    catalog = ProcessingCatalog(processed / "catalog.sqlite3")
    processor = SnapshotProcessor(processed_root=processed, catalog=catalog)
    return catalog, processor, processed


def test_duplicate_snapshot_processing_is_idempotent(tmp_path: Path) -> None:
    snapshot = write_snapshot(
        tmp_path / "raw",
        "20260101T000000000000Z",
        [make_match_payload(match_id="TEST_DUPLICATE")],
    )
    catalog, processor, processed = make_processor(tmp_path)
    try:
        first = processor.process([snapshot])
        match_file = next(processed.glob("region=*/patch=*/queue=*/matches/*.json"))
        first_content = match_file.read_bytes()
        second = processor.process([snapshot])

        assert first.newly_processed_matches == 1
        assert first.participant_rows_written == 10
        assert first.team_rows_written == 2
        assert second.newly_processed_matches == 0
        assert second.already_processed_matches == 1
        assert match_file.read_bytes() == first_content
        assert catalog.stats()["total_entries"] == 1
    finally:
        catalog.close()


def test_overlapping_snapshots_do_not_duplicate_matches(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    first_snapshot = write_snapshot(
        raw,
        "20260101T000000000000Z",
        [make_match_payload(match_id="TEST_SHARED")],
    )
    second_snapshot = write_snapshot(
        raw,
        "20260102T000000000000Z",
        [
            make_match_payload(match_id="TEST_SHARED"),
            make_match_payload(match_id="TEST_NEW"),
        ],
    )
    catalog, processor, processed = make_processor(tmp_path)
    try:
        summary = processor.process([first_snapshot, second_snapshot])
        match_files = list(
            processed.glob("region=*/patch=*/queue=*/matches/*.json")
        )

        assert summary.matches_discovered == 3
        assert summary.newly_processed_matches == 2
        assert summary.already_processed_matches == 1
        assert len(match_files) == 2
        assert catalog.stats()["total_entries"] == 2
    finally:
        catalog.close()


def test_rejected_match_is_retryable_without_reprocessing_success(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    valid = make_match_payload(match_id="TEST_VALID")
    retryable = make_match_payload(match_id="TEST_RETRY", queue_id=440)
    snapshot = write_snapshot(
        raw,
        "20260101T000000000000Z",
        [valid, retryable],
    )
    catalog, processor, _ = make_processor(tmp_path)
    try:
        first = processor.process([snapshot])
        assert first.newly_processed_matches == 1
        assert first.rejected_matches == 1
        assert catalog.stats()["by_status"] == {"processed": 1, "rejected": 1}

        retryable["info"]["queueId"] = 420
        write_snapshot(raw, snapshot.name, [valid, retryable])
        second = processor.process([snapshot])

        assert second.already_processed_matches == 1
        assert second.newly_processed_matches == 1
        assert second.rejected_matches == 0
        assert catalog.stats()["by_status"] == {"processed": 2}
    finally:
        catalog.close()


def test_catalog_match_id_is_unique(tmp_path: Path) -> None:
    with ProcessingCatalog(tmp_path / "catalog.sqlite3") as catalog:
        details = {
            "match_id": "TEST_UNIQUE",
            "routing_region": "test-region",
            "api_game_version": "16.12.788.4269",
            "api_patch": "16.12",
            "public_patch": "26.12",
            "patch_resolution_method": "test-rule",
            "patch_resolution_status": "resolved",
            "queue_id": 420,
            "source_snapshot": "snapshot-a",
        }
        catalog.record_processed(**details)
        catalog.record_processed(**{**details, "source_snapshot": "snapshot-b"})
        assert catalog.stats()["total_entries"] == 1


def test_legacy_catalog_entry_can_be_migrated_from_raw(tmp_path: Path) -> None:
    catalog_path = tmp_path / "processed" / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(catalog_path)
    connection.execute(
        """
        CREATE TABLE processed_matches (
            match_id TEXT PRIMARY KEY,
            routing_region TEXT NOT NULL,
            patch TEXT,
            queue_id INTEGER,
            source_snapshot TEXT NOT NULL,
            processing_timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            failure_code TEXT,
            failure_reason TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO processed_matches VALUES (
            'TEST_LEGACY', 'test-region', '16.14', 420, 'old',
            '2026-01-01T00:00:00Z', 'processed', NULL, NULL
        )
        """
    )
    connection.commit()
    connection.close()
    snapshot = write_snapshot(
        tmp_path / "raw",
        "20260701T000000000000Z",
        [
            make_match_payload(
                match_id="TEST_LEGACY",
                game_version="16.14.792.1234",
            )
        ],
    )

    with ProcessingCatalog(catalog_path) as catalog:
        assert catalog.needs_patch_migration("TEST_LEGACY") is True
        summary = SnapshotProcessor(
            processed_root=tmp_path / "processed",
            catalog=catalog,
            migrate_stage1=True,
        ).process([snapshot])
        assert summary.newly_processed_matches == 1
        assert catalog.needs_patch_migration("TEST_LEGACY") is False
        assert catalog.processed_public_patch("TEST_LEGACY") == "26.14"

    files = list(
        (tmp_path / "processed").glob(
            "region=test-region/patch=26.14/queue=420/matches/*.json"
        )
    )
    assert len(files) == 1


def test_report_integrity_and_privacy(tmp_path: Path) -> None:
    snapshot = write_snapshot(
        tmp_path / "raw",
        "20260101T000000000000Z",
        [make_match_payload(match_id="TEST_REPORT")],
    )
    catalog, processor, processed = make_processor(tmp_path)
    try:
        summary = processor.process([snapshot])
        report = build_feasibility_report(
            processed_root=processed,
            catalog=catalog,
            processing_summary=summary,
        )
        paths = write_feasibility_reports(
            report,
            processed / "reports",
            {"json", "markdown"},
        )

        assert report["counts"] == {"matches": 1, "participants": 10, "teams": 2}
        assert report["queue_ids"] == [420]
        assert report["public_patches"] == ["26.12"]
        assert report["api_patches"] == ["16.12"]
        assert report["api_game_versions"] == ["16.12.788.4269"]
        assert report["shape_checks"]["duplicate_match_records"] == 0
        assert report["shape_checks"]["teams_with_one_of_each_position"] == 2
        assert report["win_loss_checks"][
            "matches_with_exactly_one_winner_and_one_loser"
        ] == 1
        assert report["sample_warning"] is not None
        human_report = next(path for path in paths if path.suffix == ".md")
        content = human_report.read_text(encoding="utf-8")
        machine_report = next(path for path in paths if path.suffix == ".json")
        machine_content = machine_report.read_text(encoding="utf-8")
        for report_content in (content, machine_content):
            assert "synthetic-player" not in report_content
            assert "TEST_REPORT" not in report_content
            assert "puuid" not in report_content.lower()
            assert "summoner" not in report_content.lower()
        assert "Public patches (canonical): 26.12" in content
    finally:
        catalog.close()
