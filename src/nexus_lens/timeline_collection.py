"""Bounded, resumable Match-V5 timeline collection for Stage 3.5A."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_lens.config import Settings
from nexus_lens.data_seal import sha256_file
from nexus_lens.riot_client import RiotApiError, RiotClient
from nexus_lens.schemas import RANKED_SOLO_QUEUE_ID

TIMELINE_COLLECTION_SCHEMA_VERSION = "stage3.5a-timeline-collection-v1"
STAGE35_CONFIG_SCHEMA_VERSION = "stage3.5a-config-v1"
REQUIRED_STAGE31_FILES = (
    "bans.jsonl",
    "matches.jsonl",
    "metadata.json",
    "participants.jsonl",
    "quality_report.json",
    "teams.jsonl",
)


class Stage35ConfigModel(BaseModel):
    """Strict base for the checked Stage 3.5A execution configuration."""

    model_config = ConfigDict(extra="forbid")


class Stage31SourceConfig(Stage35ConfigModel):
    label: str = Field(min_length=1)
    platform: str = Field(pattern=r"^(eun1|euw1)$")
    source_kind: str = Field(pattern=r"^(external|retained_private|extension)$")
    directory: Path
    file_sha256: dict[str, str]

    @model_validator(mode="after")
    def validate_file_hashes(self) -> Stage31SourceConfig:
        if set(self.file_sha256) != set(REQUIRED_STAGE31_FILES):
            raise ValueError("Stage 3.1 hash declaration is incomplete")
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.file_sha256.values()
        ):
            raise ValueError("Stage 3.1 hash declaration is invalid")
        return self


class TimelinePlatformConfig(Stage35ConfigModel):
    platform: str = Field(pattern=r"^(eun1|euw1)$")
    routing_region: str = Field(pattern=r"^europe$")
    raw_timeline_directory: Path
    checkpoint_database: Path
    maximum_requests: int = Field(ge=1)


class HistoryPolicyConfig(Stage35ConfigModel):
    recent_days: int = Field(default=30, ge=1)
    exponential_decay: float = Field(default=0.8, gt=0.0, lt=1.0)
    shrinkage_prior_mean: float = Field(default=0.5, ge=0.0, le=1.0)
    shrinkage_prior_strength: float = Field(default=10.0, gt=0.0)
    limited_history_threshold: int = Field(default=5, ge=0)
    proposed_maximum_prior_matches: int = Field(default=100, ge=1, le=100)
    proposed_maximum_prior_days: int = Field(default=180, ge=1)


class Stage35Config(Stage35ConfigModel):
    schema_version: str = Field(pattern=r"^stage3\.5a-config-v1$")
    template_only: bool = False
    public_patch: str = Field(pattern=r"^26\.15$")
    queue_id: int = Field(default=RANKED_SOLO_QUEUE_ID)
    target_eligible_matches: int = Field(default=10_000, ge=1)
    primary_timestamps_minutes: tuple[int, ...] = (5, 10, 15)
    exploratory_timestamps_minutes: tuple[int, ...] = (20, 25)
    minimum_game_duration_seconds: int = Field(default=300, ge=0)
    maximum_frame_lateness_ms: int = Field(default=60_000, ge=0)
    concurrency: int = Field(default=4, ge=1, le=4)
    private_output_directory: Path
    aggregate_output_directory: Path
    maximum_aggregate_publication_bytes: int = Field(default=5_000_000, ge=1)
    sources: tuple[Stage31SourceConfig, ...]
    platforms: tuple[TimelinePlatformConfig, ...]
    history_policy: HistoryPolicyConfig = Field(default_factory=HistoryPolicyConfig)

    @model_validator(mode="after")
    def validate_contract(self) -> Stage35Config:
        if self.queue_id != RANKED_SOLO_QUEUE_ID:
            raise ValueError("Stage 3.5A requires Ranked Solo/Duo queue 420")
        if self.primary_timestamps_minutes != (5, 10, 15):
            raise ValueError("Stage 3.5A primary timestamps are frozen at 5/10/15")
        if self.exploratory_timestamps_minutes != (20, 25):
            raise ValueError("Stage 3.5A exploratory timestamps are 20/25")
        source_platforms = {source.platform for source in self.sources}
        storage_platforms = [platform.platform for platform in self.platforms]
        if len(storage_platforms) != len(set(storage_platforms)):
            raise ValueError("timeline platform configuration is duplicated")
        if source_platforms != set(storage_platforms):
            raise ValueError("source and timeline platform sets differ")
        labels = [source.label for source in self.sources]
        if len(labels) != len(set(labels)):
            raise ValueError("source labels are duplicated")
        return self

    def scientific_payload(self) -> dict[str, Any]:
        """Return path-free policy content suitable for aggregate publication."""

        return {
            "schema_version": self.schema_version,
            "public_patch": self.public_patch,
            "queue_id": self.queue_id,
            "target_eligible_matches": self.target_eligible_matches,
            "primary_timestamps_minutes": list(self.primary_timestamps_minutes),
            "exploratory_timestamps_minutes": list(self.exploratory_timestamps_minutes),
            "minimum_game_duration_seconds": self.minimum_game_duration_seconds,
            "maximum_frame_lateness_ms": self.maximum_frame_lateness_ms,
            "concurrency": self.concurrency,
            "maximum_aggregate_publication_bytes": (
                self.maximum_aggregate_publication_bytes
            ),
            "sources": [
                {
                    "label": source.label,
                    "platform": source.platform,
                    "source_kind": source.source_kind,
                    "file_sha256": dict(sorted(source.file_sha256.items())),
                }
                for source in sorted(self.sources, key=lambda item: item.label)
            ],
            "platforms": [
                {
                    "platform": platform.platform,
                    "routing_region": platform.routing_region,
                    "maximum_requests": platform.maximum_requests,
                }
                for platform in sorted(self.platforms, key=lambda item: item.platform)
            ],
            "history_policy": self.history_policy.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class TimelineMatchRef:
    platform: str
    match_id: str


@dataclass(frozen=True)
class TimelineCollectionSummary:
    platform: str
    source_matches: int
    already_downloaded: int
    downloaded_this_run: int
    failed_this_run: int
    unavailable_this_run: int
    cumulative_downloaded: int
    cumulative_failed: int
    cumulative_unavailable: int
    remaining: int
    timeline_set_sha256: str
    request_metrics: dict[str, object]
    request_ceiling: int
    cumulative_request_attempts: int
    target_complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TIMELINE_COLLECTION_SCHEMA_VERSION,
            "platform": self.platform,
            "source_matches": self.source_matches,
            "already_downloaded": self.already_downloaded,
            "downloaded_this_run": self.downloaded_this_run,
            "failed_this_run": self.failed_this_run,
            "unavailable_this_run": self.unavailable_this_run,
            "cumulative_downloaded": self.cumulative_downloaded,
            "cumulative_failed": self.cumulative_failed,
            "cumulative_unavailable": self.cumulative_unavailable,
            "remaining": self.remaining,
            "timeline_set_sha256": self.timeline_set_sha256,
            "request_metrics": self.request_metrics,
            "request_ceiling": self.request_ceiling,
            "cumulative_request_attempts": self.cumulative_request_attempts,
            "target_complete": self.target_complete,
        }


def load_stage35_config(path: Path, *, allow_template: bool = False) -> Stage35Config:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = Stage35Config.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Stage 3.5A configuration is invalid") from error
    if config.template_only and not allow_template:
        raise ValueError("template Stage 3.5A configuration is not executable")
    return config


def verify_stage31_sources(
    config: Stage35Config,
) -> tuple[TimelineMatchRef, ...]:
    """Verify immutable source files and return unique private match references."""

    matches: list[TimelineMatchRef] = []
    seen: set[tuple[str, str]] = set()
    for source in sorted(config.sources, key=lambda item: item.label):
        for name in REQUIRED_STAGE31_FILES:
            path = source.directory / name
            if sha256_file(path) != source.file_sha256[name]:
                raise ValueError("sealed Stage 3.1 source hash differs")
        metadata = _load_json(source.directory / "metadata.json")
        if (
            metadata.get("processing_schema_version") != "stage3.1-v1"
            or metadata.get("queue_id") != RANKED_SOLO_QUEUE_ID
            or not isinstance(
                metadata.get("match_counts_by_public_patch", {}).get(
                    config.public_patch
                ),
                int,
            )
        ):
            raise ValueError("Stage 3.1 source declaration differs")
        selected_count = 0
        with (source.directory / "matches.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("public_patch") != config.public_patch:
                    continue
                key = (str(row["platform"]).lower(), str(row["match_id"]))
                if key[0] != source.platform:
                    raise ValueError("Stage 3.1 source platform differs")
                if row.get("queue_id") != RANKED_SOLO_QUEUE_ID:
                    raise ValueError("Stage 3.1 match is outside the frozen scope")
                if key in seen:
                    raise ValueError("Stage 3.1 source match is duplicated")
                seen.add(key)
                matches.append(TimelineMatchRef(*key))
                selected_count += 1
        if (
            selected_count
            != metadata["match_counts_by_public_patch"][config.public_patch]
        ):
            raise ValueError("Stage 3.1 public-patch count differs")
    return tuple(sorted(matches, key=lambda item: (item.platform, item.match_id)))


class TimelineCatalog:
    """Private SQLite state for exact-once timeline downloads."""

    def __init__(self, path: Path, *, config_sha256: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS timelines (
                match_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                content_sha256 TEXT,
                relative_path TEXT,
                failure_category TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._bind_config(config_sha256)
        self.connection.commit()

    def __enter__(self) -> TimelineCatalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def _bind_config(self, config_sha256: str) -> None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='config_sha256'"
        ).fetchone()
        if row is not None and row[0] != config_sha256:
            raise ValueError("timeline checkpoint configuration differs")
        self.connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('config_sha256',?)",
            (config_sha256,),
        )

    def successful_ids(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT match_id FROM timelines WHERE status='downloaded'"
            )
        }

    def terminal_ids(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT match_id FROM timelines "
                "WHERE status IN ('downloaded','unavailable')"
            )
        }

    def record_download(
        self, match_id: str, content_sha256: str, relative_path: str
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO timelines(
                match_id,status,content_sha256,relative_path,failure_category,updated_at
            ) VALUES(?,?,?,?,NULL,?)
            ON CONFLICT(match_id) DO UPDATE SET
                status=excluded.status,
                content_sha256=excluded.content_sha256,
                relative_path=excluded.relative_path,
                failure_category=NULL,
                updated_at=excluded.updated_at
            """,
            (
                match_id,
                "downloaded",
                content_sha256,
                relative_path,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def record_failure(self, match_id: str, category: str) -> None:
        self.connection.execute(
            """
            INSERT INTO timelines(
                match_id,status,content_sha256,relative_path,failure_category,updated_at
            ) VALUES(?, 'failed', NULL, NULL, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                status='failed',failure_category=excluded.failure_category,
                updated_at=excluded.updated_at
            """,
            (match_id, category, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def record_unavailable(self, match_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO timelines(
                match_id,status,content_sha256,relative_path,failure_category,updated_at
            ) VALUES(?, 'unavailable', NULL, NULL, 'not_found', ?)
            ON CONFLICT(match_id) DO UPDATE SET
                status='unavailable',content_sha256=NULL,relative_path=NULL,
                failure_category='not_found',updated_at=excluded.updated_at
            """,
            (match_id, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    def request_attempts(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='request_attempts'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_request_attempt(self, _category: str) -> None:
        attempted = self.request_attempts() + 1
        self.connection.execute(
            """
            INSERT INTO metadata(key,value) VALUES('request_attempts',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(attempted),),
        )
        self.connection.commit()

    def status_counts(self) -> Counter[str]:
        return Counter(
            dict(
                self.connection.execute(
                    "SELECT status, COUNT(*) FROM timelines GROUP BY status"
                )
            )
        )

    def timeline_set_sha256(self) -> str:
        digest = hashlib.sha256()
        rows = self.connection.execute(
            """
            SELECT match_id,content_sha256 FROM timelines
            WHERE status='downloaded' ORDER BY match_id
            """
        )
        for match_id, content_sha256 in rows:
            private_key = hashlib.sha256(match_id.encode()).hexdigest()
            digest.update(f"{private_key}\0{content_sha256}\n".encode("ascii"))
        return digest.hexdigest()


async def collect_platform_timelines(
    *,
    config: Stage35Config,
    platform_config: TimelinePlatformConfig,
    matches: tuple[TimelineMatchRef, ...],
) -> TimelineCollectionSummary:
    """Download one platform's missing timelines under a hard request ceiling."""

    selected = tuple(row for row in matches if row.platform == platform_config.platform)
    config_hash = _timeline_policy_sha256(config, platform_config)
    with TimelineCatalog(
        platform_config.checkpoint_database, config_sha256=config_hash
    ) as catalog:
        completed = catalog.successful_ids()
        terminal = catalog.terminal_ids()
        pending = tuple(row for row in selected if row.match_id not in terminal)
        already_downloaded = len(set(row.match_id for row in selected) & completed)
        remaining_budget = max(
            0, platform_config.maximum_requests - catalog.request_attempts()
        )
        settings = Settings(  # type: ignore[call-arg]
            platform_region=platform_config.platform,
            routing_region=platform_config.routing_region,
        )
        downloaded = 0
        failed = 0
        unavailable = 0
        semaphore = asyncio.Semaphore(config.concurrency)
        write_lock = asyncio.Lock()
        async with RiotClient(
            settings,
            max_requests=remaining_budget,
            request_observer=catalog.record_request_attempt,
        ) as client:

            async def fetch(row: TimelineMatchRef) -> None:
                nonlocal downloaded, failed, unavailable
                async with semaphore:
                    try:
                        payload = await client.get_match_timeline(row.match_id)
                        _validate_timeline_identity(payload, row.match_id)
                        encoded = _canonical_json_bytes(payload)
                        digest = hashlib.sha256(encoded).hexdigest()
                        relative = Path(digest[:2]) / f"{digest}.json"
                        async with write_lock:
                            _atomic_write_bytes(
                                platform_config.raw_timeline_directory / relative,
                                encoded,
                            )
                            catalog.record_download(
                                row.match_id, digest, relative.as_posix()
                            )
                            downloaded += 1
                    except RiotApiError as error:
                        if error.status_code == 404:
                            async with write_lock:
                                catalog.record_unavailable(row.match_id)
                                unavailable += 1
                            return
                        async with write_lock:
                            catalog.record_failure(
                                row.match_id, _failure_category(error)
                            )
                            failed += 1
                        raise
                    except Exception as error:
                        async with write_lock:
                            catalog.record_failure(
                                row.match_id, _failure_category(error)
                            )
                            failed += 1
                        raise

            tasks = [asyncio.create_task(fetch(row)) for row in pending]
            try:
                for task in asyncio.as_completed(tasks):
                    await task
            except Exception:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            counts = catalog.status_counts()
            cumulative_downloaded = counts["downloaded"]
            return TimelineCollectionSummary(
                platform=platform_config.platform,
                source_matches=len(selected),
                already_downloaded=already_downloaded,
                downloaded_this_run=downloaded,
                failed_this_run=failed,
                unavailable_this_run=unavailable,
                cumulative_downloaded=cumulative_downloaded,
                cumulative_failed=counts["failed"],
                cumulative_unavailable=counts["unavailable"],
                remaining=max(
                    0,
                    len(selected) - cumulative_downloaded - counts["unavailable"],
                ),
                timeline_set_sha256=catalog.timeline_set_sha256(),
                request_metrics=client.metrics.as_dict(),
                request_ceiling=platform_config.maximum_requests,
                cumulative_request_attempts=catalog.request_attempts(),
                target_complete=(
                    cumulative_downloaded + counts["unavailable"] == len(selected)
                ),
            )


def load_timeline_payloads(
    platform_config: TimelinePlatformConfig,
) -> dict[str, dict[str, Any]]:
    """Load checksummed private timelines indexed by private match ID."""

    reader = TimelinePayloadReader(platform_config)
    return {
        match_id: payload
        for match_id in reader.match_ids()
        if (payload := reader.load(match_id)) is not None
    }


class TimelinePayloadReader:
    """Read and verify one timeline at a time from a small catalog index."""

    def __init__(self, platform_config: TimelinePlatformConfig) -> None:
        self._platform_config = platform_config
        database_uri = (
            f"file:{platform_config.checkpoint_database.resolve().as_posix()}?mode=ro"
        )
        with sqlite3.connect(database_uri, uri=True) as connection:
            records = connection.execute(
            """
            SELECT match_id,content_sha256,relative_path FROM timelines
            WHERE status='downloaded' ORDER BY match_id
            """
            )
            self._records = {
                str(match_id): (str(expected_hash), str(relative_path))
                for match_id, expected_hash, relative_path in records
            }

    def __len__(self) -> int:
        return len(self._records)

    def match_ids(self) -> tuple[str, ...]:
        return tuple(self._records)

    def load(self, match_id: str) -> dict[str, Any] | None:
        record = self._records.get(match_id)
        if record is None:
            return None
        expected_hash, relative_path = record
        path = self._platform_config.raw_timeline_directory / relative_path
        if sha256_file(path) != expected_hash:
            raise ValueError("timeline payload checksum differs")
        payload = _load_json(path)
        _validate_timeline_identity(payload, match_id)
        return payload

    def verify_all(self) -> int:
        for match_id in self.match_ids():
            self.load(match_id)
        return len(self)


def _validate_timeline_identity(payload: dict[str, Any], match_id: str) -> None:
    metadata = payload.get("metadata")
    info = payload.get("info")
    if (
        not isinstance(metadata, dict)
        or metadata.get("matchId") != match_id
        or not isinstance(info, dict)
        or not isinstance(info.get("frames"), list)
    ):
        raise ValueError("timeline payload identity or shape differs")


def _failure_category(error: Exception) -> str:
    name = type(error).__name__.lower()
    if "budget" in name:
        return "request_budget"
    if "api" in name:
        return "riot_api"
    if isinstance(error, (OSError, sqlite3.Error)):
        return "local_storage"
    if isinstance(error, (TypeError, ValueError)):
        return "payload_validation"
    return "unexpected"


def _canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON document was not an object")
    return payload


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _timeline_policy_sha256(
    config: Stage35Config, platform: TimelinePlatformConfig
) -> str:
    return _sha256_json(
        {
            "schema_version": config.schema_version,
            "public_patch": config.public_patch,
            "queue_id": config.queue_id,
            "minimum_game_duration_seconds": config.minimum_game_duration_seconds,
            "maximum_frame_lateness_ms": config.maximum_frame_lateness_ms,
            "primary_timestamps_minutes": config.primary_timestamps_minutes,
            "exploratory_timestamps_minutes": config.exploratory_timestamps_minutes,
            "platform": platform.platform,
            "routing_region": platform.routing_region,
            "raw_storage_method": "sha256-content-addressed-canonical-json-v1",
        }
    )
