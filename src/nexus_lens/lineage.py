"""Immutable collection-lineage audit and retained-corpus repair output."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from nexus_lens.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalMatch,
    CanonicalParticipant,
    Stage3ValidationError,
)
from nexus_lens.draft_aggregation import AGGREGATION_SCHEMA_VERSION
from nexus_lens.privacy import PLAYER_KEY_METHOD, pseudonymize_puuid

LINEAGE_SCHEMA_VERSION = "lineage-v1"
LINEAGE_QUALITY_SCHEMA_VERSION = "lineage-quality-v1"
LINEAGE_POLICY_VERSION = "collection-lineage-v1"
REQUIRED_STAGE3B_FILES = (
    "aggregation_quality_report.json",
    "champion_role_sufficient_statistics.jsonl",
    "matchup_aggregates.jsonl",
    "metadata.json",
    "synergy_aggregates.jsonl",
)
REQUIRED_STAGE31_FILES = (
    "bans.jsonl",
    "matches.jsonl",
    "metadata.json",
    "participants.jsonl",
    "quality_report.json",
    "teams.jsonl",
)
LINEAGE_DATA_FILES = (
    "lineage_audit_report.json",
    "match_discovery_lineage.jsonl",
    "participant_rank_lineage.jsonl",
)
APPROVED_MATCH_STATUSES = {
    "accepted",
    "already_cataloged_accepted",
    "already_cataloged_target",
}
REPRODUCTION_COMMAND = (
    ".\\.venv\\Scripts\\python.exe scripts\\repair_collection_lineage.py "
    "--stage3-3b-run data/processed/stage3/schema=stage3.3b-v1/"
    "run=20260722T125547567196Z-population "
    "--checkpoint data/snapshots/population/"
    "20260722T125547567196Z-population/checkpoint.json "
    "--manifest data/raw/20260722T125547567196Z-population/manifest.json"
)


class LineageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RankObservation(LineageModel):
    rank_tier: str
    rank_division: str
    queue_id: int
    rank_status: Literal["observed"]
    rank_source: str
    rank_observed_at: str | None
    rank_observed_at_status: Literal["observed", "not_collected"]


class DiscoveryContext(LineageModel):
    context_key: str
    seed_player_key: str
    platform_id: str
    platform_status: Literal["observed"]
    regional_routing: str
    regional_routing_status: Literal["collection_context"]
    analysis_region: str
    analysis_region_status: Literal["derived"]
    collection_tier: str
    collection_division: str
    collection_stratum: str
    collection_context_status: Literal["collection_context"]
    seed_rank_tier: str | None
    seed_rank_division: str | None
    seed_rank_status: Literal["observed", "ambiguous", "not_collected"]
    seed_rank_observations: list[RankObservation]
    discovery_timestamp: str | None
    discovery_timestamp_status: Literal["observed", "not_collected"]
    discovery_source: str
    discovery_source_status: Literal["observed", "derived"]


class MatchDiscoveryLineage(LineageModel):
    match_id: str
    platform_id: str
    platform_status: Literal["observed"]
    regional_routing: str
    regional_routing_status: Literal["collection_context"]
    analysis_region: str
    analysis_region_status: Literal["derived"]
    queue_id: int
    discovery_contexts: list[DiscoveryContext]
    discovery_context_count: int
    multiple_discovery_contexts: bool
    multiple_collection_strata: bool
    collection_lineage_status: Literal["collection_context", "unavailable"]
    participant_rank_is_match_rank: Literal[False]
    source_run_id: str
    source_checkpoint_sha256: str
    source_stage3_1_schema_version: str
    processing_schema_version: str


class ParticipantRankLineage(LineageModel):
    match_id: str
    participant_id: int
    player_key: str
    platform_id: str
    platform_status: Literal["observed"]
    regional_routing: str
    regional_routing_status: Literal["collection_context"]
    analysis_region: str
    analysis_region_status: Literal["derived"]
    queue_id: int
    rank_tier: str | None
    rank_division: str | None
    rank_status: Literal["observed", "ambiguous", "not_collected"]
    rank_source: str | None
    rank_observed_at: str | None
    rank_observed_at_status: Literal["observed", "not_collected", "ambiguous"]
    rank_observations: list[RankObservation]
    collection_seed_for_match: bool
    matching_discovery_context_count: int
    source_run_id: str
    source_stage3_1_schema_version: str
    processing_schema_version: str


@dataclass
class LineageDataset:
    run_id: str
    output_directory: Path
    match_lineage: list[MatchDiscoveryLineage]
    participant_rank_lineage: list[ParticipantRankLineage]
    audit_report: dict[str, Any]
    metadata: dict[str, Any]


def run_lineage_repair(
    *,
    stage3_3b_directory: Path,
    checkpoint_path: Path,
    manifest_path: Path,
    output_root: Path,
    validate_only: bool,
) -> LineageDataset:
    inputs = _load_inputs(
        stage3_3b_directory=stage3_3b_directory,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
    )
    output_directory = (
        output_root / f"schema={LINEAGE_SCHEMA_VERSION}" / f"run={inputs['run_id']}"
    )
    _validate_existing_lineage(output_directory, inputs)
    dataset = _build_dataset(inputs, output_directory)
    if not validate_only:
        write_lineage_dataset(dataset)
    return dataset


def write_lineage_dataset(dataset: LineageDataset) -> Path:
    if not dataset.audit_report["ready_for_forward_lineage_use"]:
        raise Stage3ValidationError(
            "lineage_invariant_failure",
            "lineage output was not published because validation failed",
        )
    target = dataset.output_directory
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
    try:
        _write_staged_dataset(dataset, staging)
        if target.exists() and _directories_equal(staging, target):
            return target
        if target.exists():
            backup = target.with_name(f".{target.name}.previous")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except BaseException:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _load_inputs(
    *,
    stage3_3b_directory: Path,
    checkpoint_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    stage3b_hashes = _hash_required(stage3_3b_directory, REQUIRED_STAGE3B_FILES)
    stage3b_metadata = _load_object(
        stage3_3b_directory / "metadata.json", "stage3_3b_metadata"
    )
    if stage3b_metadata.get("processing_schema_version") != AGGREGATION_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_3b_schema", "Stage 3.3B schema is incompatible"
        )
    for name, expected in stage3b_metadata.get("output", {}).get("sha256", {}).items():
        if stage3b_hashes.get(name) != expected:
            raise Stage3ValidationError(
                "stage3_3b_hash_conflict", "Stage 3.3B physical hashes disagree"
            )
    prior_inputs = stage3b_metadata.get("inputs")
    if not isinstance(prior_inputs, dict):
        raise Stage3ValidationError(
            "stage3_3b_lineage_missing", "Stage 3.3B prior lineage is missing"
        )
    verified_prior: dict[str, dict[str, Any]] = {}
    for stage in ("stage3_1", "stage3_2", "stage3_3a"):
        item = prior_inputs.get(stage)
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("directory"), str)
            or not isinstance(item.get("sha256"), dict)
        ):
            raise Stage3ValidationError(
                "stage3_3b_lineage_missing", f"{stage} lineage is invalid"
            )
        directory = Path(item["directory"])
        hashes = _hash_required(directory, tuple(sorted(item["sha256"])))
        if hashes != item["sha256"]:
            raise Stage3ValidationError(
                "prior_stage_hash_conflict", f"{stage} physical hashes disagree"
            )
        verified_prior[stage] = {"directory": directory, "sha256": hashes}
    stage31_directory = verified_prior["stage3_1"]["directory"]
    if set(verified_prior["stage3_1"]["sha256"]) != set(REQUIRED_STAGE31_FILES):
        raise Stage3ValidationError(
            "stage3_1_file_contract", "Stage 3.1 file contract is incompatible"
        )
    stage31_metadata = _load_object(
        stage31_directory / "metadata.json", "stage3_1_metadata"
    )
    if stage31_metadata.get("processing_schema_version") != CANONICAL_SCHEMA_VERSION:
        raise Stage3ValidationError(
            "incompatible_stage3_1_schema", "Stage 3.1 schema is incompatible"
        )
    if Path(str(stage31_metadata.get("input_manifest"))) != manifest_path:
        raise Stage3ValidationError(
            "manifest_lineage_conflict", "manifest differs from Stage 3.1 lineage"
        )
    checkpoint = _load_object(checkpoint_path, "checkpoint")
    manifest = _load_object(manifest_path, "manifest")
    run_id = stage3b_metadata.get("run_id")
    if (
        not isinstance(run_id, str)
        or checkpoint.get("run_id") != run_id
        or manifest.get("run_id") != run_id
        or stage31_metadata.get("run_id") != run_id
    ):
        raise Stage3ValidationError("run_id_conflict", "lineage input run IDs disagree")
    participants = _load_models(
        stage31_directory / "participants.jsonl",
        CanonicalParticipant,
        "stage3_1_participants",
    )
    matches = _load_models(
        stage31_directory / "matches.jsonl",
        CanonicalMatch,
        "stage3_1_matches",
    )
    if len(participants) != 1_000 or len(matches) != 100:
        raise Stage3ValidationError(
            "retained_row_count", "retained Stage 3.1 row counts disagree"
        )
    return {
        "run_id": run_id,
        "checkpoint": checkpoint,
        "checkpoint_path": checkpoint_path,
        "checkpoint_sha256": _sha256(checkpoint_path),
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256(manifest_path),
        "participants": participants,
        "matches": matches,
        "stage3b_directory": stage3_3b_directory,
        "stage3b_hashes": stage3b_hashes,
        "prior": verified_prior,
    }


def _build_dataset(inputs: dict[str, Any], output_directory: Path) -> LineageDataset:
    checkpoint = inputs["checkpoint"]
    manifest = inputs["manifest"]
    run_id = inputs["run_id"]
    config = checkpoint.get("config")
    matches_state = checkpoint.get("matches")
    sampling = checkpoint.get("sampling")
    if not all(isinstance(value, dict) for value in (config, matches_state, sampling)):
        raise Stage3ValidationError(
            "checkpoint_schema", "checkpoint lineage sections are missing"
        )
    platform = _same_string(
        config.get("platform"), manifest.get("platform"), "platform"
    )
    regional_routing = _same_string(
        config.get("regional_routing"),
        manifest.get("regional_routing"),
        "regional_routing",
    )
    analysis_region = _same_string(
        config.get("analysis_region"),
        manifest.get("routing_region"),
        "analysis_region",
    )
    if int(config.get("queue_id", 0)) != 420:
        raise Stage3ValidationError("queue_conflict", "lineage queue is not 420")
    canonical_matches = {row.match_id: row for row in inputs["matches"]}
    if any(
        row.platform.lower() != platform or row.queue_id != 420
        for row in canonical_matches.values()
    ):
        raise Stage3ValidationError(
            "canonical_routing_conflict",
            "canonical match platform or queue disagrees with collection lineage",
        )
    canonical_participants = sorted(
        inputs["participants"], key=lambda row: (row.match_id, row.participant_id)
    )
    candidate_ranks = _candidate_rank_observations(sampling)
    accepted_state = {
        match_id: record
        for match_id, record in matches_state.items()
        if isinstance(record, dict) and record.get("status") in APPROVED_MATCH_STATUSES
    }
    if set(accepted_state) != set(canonical_matches):
        raise Stage3ValidationError(
            "approval_lineage_conflict",
            "checkpoint accepted matches differ from Stage 3.1",
        )

    match_lineage: list[MatchDiscoveryLineage] = []
    contexts_by_match: dict[str, list[DiscoveryContext]] = {}
    for match_id in sorted(canonical_matches):
        source_records = accepted_state[match_id].get("sources", [])
        if not isinstance(source_records, list):
            raise Stage3ValidationError(
                "checkpoint_source_schema", "match sources are invalid"
            )
        contexts = _discovery_contexts(
            source_records,
            candidate_ranks=candidate_ranks,
            platform=platform,
            regional_routing=regional_routing,
            analysis_region=analysis_region,
        )
        contexts_by_match[match_id] = contexts
        strata = {(row.collection_tier, row.collection_division) for row in contexts}
        match_lineage.append(
            MatchDiscoveryLineage(
                match_id=match_id,
                platform_id=platform,
                platform_status="observed",
                regional_routing=regional_routing,
                regional_routing_status="collection_context",
                analysis_region=analysis_region,
                analysis_region_status="derived",
                queue_id=420,
                discovery_contexts=contexts,
                discovery_context_count=len(contexts),
                multiple_discovery_contexts=len(contexts) > 1,
                multiple_collection_strata=len(strata) > 1,
                collection_lineage_status=(
                    "collection_context" if contexts else "unavailable"
                ),
                participant_rank_is_match_rank=False,
                source_run_id=run_id,
                source_checkpoint_sha256=inputs["checkpoint_sha256"],
                source_stage3_1_schema_version=CANONICAL_SCHEMA_VERSION,
                processing_schema_version=LINEAGE_SCHEMA_VERSION,
            )
        )

    participant_lineage: list[ParticipantRankLineage] = []
    for participant in canonical_participants:
        observations = candidate_ranks.get(participant.player_key, [])
        rank_pairs = {(row.rank_tier, row.rank_division) for row in observations}
        if len(rank_pairs) == 1:
            rank_tier, rank_division = next(iter(rank_pairs))
            rank_status = "observed"
            rank_source = "league_v4_ranked_solo_ladder_entry_retained_checkpoint"
        elif rank_pairs:
            rank_tier = rank_division = None
            rank_status = "ambiguous"
            rank_source = "multiple_conflicting_league_v4_ladder_entries"
        else:
            rank_tier = rank_division = None
            rank_status = "not_collected"
            rank_source = None
        timestamps = {
            row.rank_observed_at
            for row in observations
            if row.rank_observed_at is not None
        }
        if not observations or not timestamps:
            observed_at = None
            observed_at_status = "not_collected"
        elif len(timestamps) == 1 and all(
            row.rank_observed_at is not None for row in observations
        ):
            observed_at = next(iter(timestamps))
            observed_at_status = "observed"
        else:
            observed_at = None
            observed_at_status = "ambiguous"
        matching_contexts = [
            context
            for context in contexts_by_match[participant.match_id]
            if context.seed_player_key == participant.player_key
        ]
        participant_lineage.append(
            ParticipantRankLineage(
                match_id=participant.match_id,
                participant_id=participant.participant_id,
                player_key=participant.player_key,
                platform_id=platform,
                platform_status="observed",
                regional_routing=regional_routing,
                regional_routing_status="collection_context",
                analysis_region=analysis_region,
                analysis_region_status="derived",
                queue_id=420,
                rank_tier=rank_tier,
                rank_division=rank_division,
                rank_status=rank_status,
                rank_source=rank_source,
                rank_observed_at=observed_at,
                rank_observed_at_status=observed_at_status,
                rank_observations=observations,
                collection_seed_for_match=bool(matching_contexts),
                matching_discovery_context_count=len(matching_contexts),
                source_run_id=run_id,
                source_stage3_1_schema_version=CANONICAL_SCHEMA_VERSION,
                processing_schema_version=LINEAGE_SCHEMA_VERSION,
            )
        )
    validation = _validate_outputs(
        match_lineage, participant_lineage, canonical_matches, contexts_by_match
    )
    audit_report = _audit_report(
        inputs=inputs,
        output_directory=output_directory,
        match_lineage=match_lineage,
        participant_lineage=participant_lineage,
        validation=validation,
        platform=platform,
        regional_routing=regional_routing,
        analysis_region=analysis_region,
    )
    output_hashes = _output_hashes(match_lineage, participant_lineage, audit_report)
    metadata = _metadata(
        inputs=inputs,
        output_directory=output_directory,
        match_count=len(match_lineage),
        participant_count=len(participant_lineage),
        output_hashes=output_hashes,
    )
    return LineageDataset(
        run_id=run_id,
        output_directory=output_directory,
        match_lineage=match_lineage,
        participant_rank_lineage=participant_lineage,
        audit_report=audit_report,
        metadata=metadata,
    )


def _candidate_rank_observations(
    sampling: dict[str, Any],
) -> dict[str, list[RankObservation]]:
    by_player: dict[str, dict[tuple[Any, ...], RankObservation]] = defaultdict(dict)
    observed_at = sampling.get("candidate_observed_at", {})
    for stratum, entries in sampling.get("candidates", {}).items():
        timestamp = observed_at.get(stratum)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            puuid = entry.get("puuid")
            tier = entry.get("tier")
            division = entry.get("rank")
            if (
                not isinstance(puuid, str)
                or not isinstance(tier, str)
                or not isinstance(division, str)
                or entry.get("queueType") != "RANKED_SOLO_5x5"
            ):
                continue
            player_key = pseudonymize_puuid(puuid)
            if player_key is None:
                continue
            timestamp_value = str(timestamp) if isinstance(timestamp, str) else None
            observation = RankObservation(
                rank_tier=tier.upper(),
                rank_division=division.upper(),
                queue_id=420,
                rank_status="observed",
                rank_source="league_v4_ranked_solo_ladder_entry_retained_checkpoint",
                rank_observed_at=timestamp_value,
                rank_observed_at_status=(
                    "observed" if timestamp_value else "not_collected"
                ),
            )
            identity = (
                observation.rank_tier,
                observation.rank_division,
                observation.rank_source,
                observation.rank_observed_at,
            )
            by_player[player_key][identity] = observation
    return {
        player_key: [observations[key] for key in sorted(observations)]
        for player_key, observations in by_player.items()
    }


def _discovery_contexts(
    sources: list[Any],
    *,
    candidate_ranks: dict[str, list[RankObservation]],
    platform: str,
    regional_routing: str,
    analysis_region: str,
) -> list[DiscoveryContext]:
    contexts: dict[str, DiscoveryContext] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise Stage3ValidationError(
                "checkpoint_source_schema", "a discovery source is invalid"
            )
        expected_routing = {
            "platform_id": platform,
            "regional_routing": regional_routing,
            "analysis_region": analysis_region,
        }
        if any(
            field in source
            and (
                not isinstance(source[field], str) or source[field].lower() != expected
            )
            for field, expected in expected_routing.items()
        ):
            raise Stage3ValidationError(
                "checkpoint_source_routing",
                "a discovery source conflicts with collection routing",
            )
        seed_key = source.get("seed_player_key")
        if not isinstance(seed_key, str):
            seed_key = pseudonymize_puuid(source.get("puuid"))
        if not isinstance(seed_key, str):
            raise Stage3ValidationError(
                "checkpoint_source_identity", "a seed source cannot be pseudonymized"
            )
        tier = source.get("collection_tier") or source.get("tier")
        division = source.get("collection_division") or source.get("division")
        if not isinstance(tier, str) or not isinstance(division, str):
            raise Stage3ValidationError(
                "checkpoint_source_stratum", "a source stratum is missing"
            )
        observations_by_key = {
            _rank_observation_identity(row): row
            for row in candidate_ranks.get(seed_key, [])
        }
        source_observations = source.get("seed_rank_observations", [])
        if isinstance(source_observations, list):
            for item in source_observations:
                if not isinstance(item, dict) or item.get("rank_status") != "observed":
                    continue
                try:
                    observation = RankObservation.model_validate(item)
                except ValidationError:
                    raise Stage3ValidationError(
                        "checkpoint_source_rank",
                        "a discovery source rank observation is invalid",
                    ) from None
                observations_by_key[_rank_observation_identity(observation)] = (
                    observation
                )
        observations = [observations_by_key[key] for key in sorted(observations_by_key)]
        pairs = {(row.rank_tier, row.rank_division) for row in observations}
        if len(pairs) == 1:
            rank_tier, rank_division = next(iter(pairs))
            rank_status = "observed"
        elif pairs:
            rank_tier = rank_division = None
            rank_status = "ambiguous"
        else:
            rank_tier = rank_division = None
            rank_status = "not_collected"
        discovery_timestamp = source.get("discovery_timestamp")
        if not isinstance(discovery_timestamp, str):
            discovery_timestamp = None
        discovery_source = source.get("discovery_source")
        if not isinstance(discovery_source, str):
            discovery_source = "legacy_checkpoint_match_source"
            discovery_source_status = "derived"
        else:
            discovery_source_status = "observed"
        identity = (
            seed_key,
            tier.upper(),
            division.upper(),
            platform,
            regional_routing,
            analysis_region,
        )
        context_key = hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()
        contexts[context_key] = DiscoveryContext(
            context_key=context_key,
            seed_player_key=seed_key,
            platform_id=platform,
            platform_status="observed",
            regional_routing=regional_routing,
            regional_routing_status="collection_context",
            analysis_region=analysis_region,
            analysis_region_status="derived",
            collection_tier=tier.upper(),
            collection_division=division.upper(),
            collection_stratum=f"{tier.upper()} {division.upper()}",
            collection_context_status="collection_context",
            seed_rank_tier=rank_tier,
            seed_rank_division=rank_division,
            seed_rank_status=rank_status,
            seed_rank_observations=observations,
            discovery_timestamp=discovery_timestamp,
            discovery_timestamp_status=(
                "observed" if discovery_timestamp else "not_collected"
            ),
            discovery_source=discovery_source,
            discovery_source_status=discovery_source_status,
        )
    return [contexts[key] for key in sorted(contexts)]


def _rank_observation_identity(
    observation: RankObservation,
) -> tuple[str, str, str, str]:
    return (
        observation.rank_tier,
        observation.rank_division,
        observation.rank_source,
        observation.rank_observed_at or "",
    )


def _validate_outputs(
    match_rows: list[MatchDiscoveryLineage],
    participant_rows: list[ParticipantRankLineage],
    canonical_matches: dict[str, CanonicalMatch],
    contexts_by_match: dict[str, list[DiscoveryContext]],
) -> dict[str, Counter[str]]:
    reconciliation: Counter[str] = Counter()
    invariants: Counter[str] = Counter()
    match_ids = [row.match_id for row in match_rows]
    participant_keys = [(row.match_id, row.participant_id) for row in participant_rows]
    if len(match_ids) != len(set(match_ids)):
        invariants["duplicate_match_lineage_key"] += 1
    if len(participant_keys) != len(set(participant_keys)):
        invariants["duplicate_participant_lineage_key"] += 1
    if set(match_ids) != set(canonical_matches):
        reconciliation["canonical_match_ids"] += 1
    for row in match_rows:
        if row.discovery_context_count != len(row.discovery_contexts):
            reconciliation["discovery_context_count"] += 1
        if [item.context_key for item in row.discovery_contexts] != sorted(
            item.context_key for item in row.discovery_contexts
        ):
            invariants["discovery_context_order"] += 1
        if row.queue_id != 420:
            invariants["queue_id"] += 1
    context_keys = {
        match_id: Counter(item.seed_player_key for item in contexts)
        for match_id, contexts in contexts_by_match.items()
    }
    for row in participant_rows:
        expected = context_keys[row.match_id][row.player_key]
        if row.matching_discovery_context_count != expected:
            reconciliation["participant_seed_context"] += 1
        if row.collection_seed_for_match != bool(expected):
            reconciliation["participant_seed_flag"] += 1
        if row.rank_status == "observed" and (
            row.rank_tier is None
            or row.rank_division is None
            or not row.rank_observations
        ):
            invariants["observed_rank_incomplete"] += 1
        if row.rank_status == "not_collected" and row.rank_observations:
            invariants["uncollected_rank_has_observation"] += 1
        if row.queue_id != 420:
            invariants["queue_id"] += 1
    nonfinite = sum(
        _count_nonfinite(row.model_dump()) for row in [*match_rows, *participant_rows]
    )
    if nonfinite:
        invariants["nonfinite_values"] += nonfinite
    return {"reconciliation": reconciliation, "invariants": invariants}


def _audit_report(
    *,
    inputs: dict[str, Any],
    output_directory: Path,
    match_lineage: list[MatchDiscoveryLineage],
    participant_lineage: list[ParticipantRankLineage],
    validation: dict[str, Counter[str]],
    platform: str,
    regional_routing: str,
    analysis_region: str,
) -> dict[str, Any]:
    rank_statuses = Counter(row.rank_status for row in participant_lineage)
    context_counts = Counter(row.discovery_context_count for row in match_lineage)
    stratum_counts = Counter(
        context.collection_stratum
        for row in match_lineage
        for context in row.discovery_contexts
    )
    field_trace = [
        {
            "field": "platform_id",
            "origin": "Match-V5 info.platformId and Stage 2 platform configuration",
            "scope": "match and collection",
            "preserved_through": "Stage 3.1 CanonicalMatch.platform and later stages",
            "loss_point": None,
            "retained_recovery": "observed eun1 for all retained matches",
        },
        {
            "field": "regional_routing",
            "origin": "platform route table used for Match-V5",
            "scope": "collection run",
            "preserved_through": "Stage 2 checkpoint and manifest",
            "loss_point": "Stage 3.1 schema omitted the manifest field",
            "retained_recovery": "collection_context europe for all retained matches",
        },
        {
            "field": "analysis_region",
            "origin": "platform route table and Stage 2 configuration",
            "scope": "collection run and storage partition",
            "preserved_through": (
                "checkpoint, manifest.routing_region, catalog, normalized path"
            ),
            "loss_point": "Stage 3.1 validated but did not emit it",
            "retained_recovery": (
                "derived/collection-context eune for all retained matches"
            ),
        },
        {
            "field": "collection_tier/division/stratum",
            "origin": "League-V4 ladder sampling schedule",
            "scope": "seed discovery context, not every match participant",
            "preserved_through": "checkpoint players and per-match sources",
            "loss_point": "Stage 3.1 reduced checkpoint matches to ID/patch approvals",
            "retained_recovery": "all unique retained match-source contexts",
        },
        {
            "field": "seed_player_key",
            "origin": "checkpoint source PUUID",
            "scope": "match-to-seed discovery relationship",
            "preserved_through": "raw PUUID in sensitive checkpoint",
            "loss_point": "Stage 3.1 did not transform source relationships",
            "retained_recovery": f"derived with {PLAYER_KEY_METHOD}",
        },
        {
            "field": "seed observed Solo/Duo rank",
            "origin": "persisted League-V4 ladder candidate entry",
            "scope": "the observed player only",
            "preserved_through": "checkpoint sampling.candidates",
            "loss_point": (
                "player records kept schedule tier/division only; "
                "Stage 3.1 omitted rank"
            ),
            "retained_recovery": (
                "observed only for canonical players joining a stored entry"
            ),
        },
        {
            "field": "participant-specific rank",
            "origin": "League-V4 candidate entry when that participant was observed",
            "scope": "participant/player, never universal match rank",
            "preserved_through": "candidate entry only",
            "loss_point": "not collected for most match participants",
            "retained_recovery": (
                "observed, ambiguous, or not_collected per participant"
            ),
        },
        {
            "field": "rank_observed_at",
            "origin": "future League-V4 response timestamp",
            "scope": "rank observation",
            "preserved_through": "not present in legacy checkpoint",
            "loss_point": "Stage 2 checkpoint v3 never recorded it",
            "retained_recovery": "not_collected for the retained run",
        },
        {
            "field": "discovery_timestamp",
            "origin": "future Match-V5 history discovery event",
            "scope": "match-to-seed relationship",
            "preserved_through": "not present in legacy match sources",
            "loss_point": "Stage 2 checkpoint v3 never recorded it",
            "retained_recovery": "not_collected for the retained run",
        },
        {
            "field": "multiple discovery contexts",
            "origin": "deduplicated match histories from multiple seeds",
            "scope": "match",
            "preserved_through": "checkpoint match.sources list",
            "loss_point": "Stage 3.1 omitted the list",
            "retained_recovery": (
                "all contexts retained and sorted; no retained accepted overlap"
            ),
        },
    ]
    reconciliation = validation["reconciliation"]
    invariants = validation["invariants"]
    return {
        "processing_schema_version": LINEAGE_SCHEMA_VERSION,
        "quality_report_schema_version": LINEAGE_QUALITY_SCHEMA_VERSION,
        "lineage_policy_version": LINEAGE_POLICY_VERSION,
        "input": {
            "checkpoint": inputs["checkpoint_path"].as_posix(),
            "checkpoint_sha256": inputs["checkpoint_sha256"],
            "manifest": inputs["manifest_path"].as_posix(),
            "manifest_sha256": inputs["manifest_sha256"],
            "stage3_3b": inputs["stage3b_directory"].as_posix(),
            "stage3_3b_sha256": dict(sorted(inputs["stage3b_hashes"].items())),
        },
        "routing_semantics": {
            "platform_id": platform,
            "regional_routing": regional_routing,
            "analysis_region": analysis_region,
            "queue_id": 420,
        },
        "field_level_trace": field_trace,
        "recovery": {
            "match_lineage_rows": len(match_lineage),
            "discovery_context_rows": sum(
                row.discovery_context_count for row in match_lineage
            ),
            "matches_by_discovery_context_count": _sorted_counter(context_counts),
            "matches_with_multiple_seeds": sum(
                row.multiple_discovery_contexts for row in match_lineage
            ),
            "matches_with_multiple_strata": sum(
                row.multiple_collection_strata for row in match_lineage
            ),
            "collection_strata": _sorted_counter(stratum_counts),
            "participant_rank_rows": len(participant_lineage),
            "participant_rank_statuses": _sorted_counter(rank_statuses),
            "unique_players_with_observed_rank": len(
                {
                    row.player_key
                    for row in participant_lineage
                    if row.rank_status == "observed"
                }
            ),
            "rank_observation_timestamps_recovered": sum(
                row.rank_observed_at_status == "observed" for row in participant_lineage
            ),
            "discovery_timestamps_recovered": sum(
                context.discovery_timestamp_status == "observed"
                for row in match_lineage
                for context in row.discovery_contexts
            ),
        },
        "semantic_guardrails": {
            "collection_context_is_observed_rank": False,
            "seed_rank_applies_to_all_participants": False,
            "match_has_one_true_rank": False,
            "multiple_contexts_duplicate_analytics_rows": False,
        },
        "output": {
            "directory": output_directory.as_posix(),
            "row_counts": {
                "match_discovery_lineage": len(match_lineage),
                "participant_rank_lineage": len(participant_lineage),
            },
        },
        "reconciliation_failures": _sorted_counter(reconciliation),
        "invariant_failures": _sorted_counter(invariants),
        "privacy": {
            "raw_or_encrypted_riot_identifiers": False,
            "pseudonymous_seed_keys_limited_to_lineage_table": True,
            "aggregate_report_contains_player_keys": False,
        },
        "prior_stage_artifacts_modified": False,
        "ready_for_forward_lineage_use": not reconciliation and not invariants,
    }


def _metadata(
    *,
    inputs: dict[str, Any],
    output_directory: Path,
    match_count: int,
    participant_count: int,
    output_hashes: dict[str, str],
) -> dict[str, Any]:
    prior = {
        stage: {
            "directory": item["directory"].as_posix(),
            "sha256": dict(sorted(item["sha256"].items())),
        }
        for stage, item in sorted(inputs["prior"].items())
    }
    prior["stage3_3b"] = {
        "directory": inputs["stage3b_directory"].as_posix(),
        "sha256": dict(sorted(inputs["stage3b_hashes"].items())),
    }
    return {
        "processing_schema_version": LINEAGE_SCHEMA_VERSION,
        "quality_report_schema_version": LINEAGE_QUALITY_SCHEMA_VERSION,
        "lineage_policy_version": LINEAGE_POLICY_VERSION,
        "run_id": inputs["run_id"],
        "generation_timestamp": _deterministic_timestamp(inputs["run_id"]),
        "timestamp_policy": "deterministic UTC timestamp parsed from source run ID",
        "inputs": {
            "checkpoint": {
                "path": inputs["checkpoint_path"].as_posix(),
                "sha256": inputs["checkpoint_sha256"],
            },
            "manifest": {
                "path": inputs["manifest_path"].as_posix(),
                "sha256": inputs["manifest_sha256"],
            },
            "immutable_stages": prior,
        },
        "generation_policy": {
            "collection_context_never_promoted_to_observed_rank": True,
            "participant_rank_requires_retained_league_v4_entry": True,
            "conflicting_rank_observations": "preserve all and mark ambiguous",
            "discovery_context_order": "context_key ascending",
            "analytics_deduplication": "one match row and one participant-match row",
            "unavailable_values": "null with explicit status",
        },
        "row_counts": {
            "match_discovery_lineage": match_count,
            "participant_rank_lineage": participant_count,
        },
        "output": {
            "directory": output_directory.as_posix(),
            "sha256": dict(sorted(output_hashes.items())),
            "metadata_hash_excluded_reason": "metadata cannot contain its own hash",
        },
        "storage_format": "deterministic-jsonl",
        "privacy": {
            "player_key_method": PLAYER_KEY_METHOD,
            "raw_puuid_written": False,
            "aggregate_report_contains_player_keys": False,
        },
        "reproduction_command": REPRODUCTION_COMMAND,
    }


def _output_hashes(
    match_rows: list[MatchDiscoveryLineage],
    participant_rows: list[ParticipantRankLineage],
    audit_report: dict[str, Any],
) -> dict[str, str]:
    content = {
        "lineage_audit_report.json": _json_bytes(audit_report),
        "match_discovery_lineage.jsonl": _rows_bytes(match_rows),
        "participant_rank_lineage.jsonl": _rows_bytes(participant_rows),
    }
    return {
        name: hashlib.sha256(value).hexdigest()
        for name, value in sorted(content.items())
    }


def _write_staged_dataset(dataset: LineageDataset, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write_bytes(
        directory / "match_discovery_lineage.jsonl",
        _rows_bytes(dataset.match_lineage),
    )
    _write_bytes(
        directory / "participant_rank_lineage.jsonl",
        _rows_bytes(dataset.participant_rank_lineage),
    )
    _write_bytes(
        directory / "lineage_audit_report.json",
        _json_bytes(dataset.audit_report),
    )
    _write_bytes(directory / "metadata.json", _json_bytes(dataset.metadata))


def _validate_existing_lineage(output_directory: Path, inputs: dict[str, Any]) -> None:
    if not output_directory.exists():
        return
    metadata_path = output_directory / "metadata.json"
    if not metadata_path.is_file():
        raise Stage3ValidationError(
            "existing_lineage_metadata", "existing lineage output lacks metadata"
        )
    metadata = _load_object(metadata_path, "existing_lineage_metadata")
    recorded_inputs = metadata.get("inputs", {})
    expected_prior = {
        stage: {
            "directory": item["directory"].as_posix(),
            "sha256": dict(sorted(item["sha256"].items())),
        }
        for stage, item in sorted(inputs["prior"].items())
    }
    expected_prior["stage3_3b"] = {
        "directory": inputs["stage3b_directory"].as_posix(),
        "sha256": dict(sorted(inputs["stage3b_hashes"].items())),
    }
    expected_inputs = {
        "checkpoint": {
            "path": inputs["checkpoint_path"].as_posix(),
            "sha256": inputs["checkpoint_sha256"],
        },
        "manifest": {
            "path": inputs["manifest_path"].as_posix(),
            "sha256": inputs["manifest_sha256"],
        },
        "immutable_stages": expected_prior,
    }
    if (
        metadata.get("processing_schema_version") != LINEAGE_SCHEMA_VERSION
        or metadata.get("run_id") != inputs["run_id"]
        or recorded_inputs != expected_inputs
    ):
        raise Stage3ValidationError(
            "existing_lineage_conflict", "existing lineage input hashes differ"
        )
    recorded_output = metadata.get("output", {}).get("sha256")
    if not isinstance(recorded_output, dict):
        raise Stage3ValidationError(
            "existing_lineage_hashes", "existing lineage hashes are missing"
        )
    physical = _hash_required(output_directory, LINEAGE_DATA_FILES)
    if recorded_output != physical:
        raise Stage3ValidationError(
            "existing_lineage_hashes", "existing lineage physical hashes disagree"
        )


def _same_string(left: Any, right: Any, field: str) -> str:
    if not isinstance(left, str) or not left or left != right:
        raise Stage3ValidationError(
            "routing_semantics_conflict", f"{field} values disagree"
        )
    return left.lower()


def _hash_required(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    if any(not (directory / name).is_file() for name in names):
        raise Stage3ValidationError(
            "lineage_input_missing", "a required lineage input is missing"
        )
    return {name: _sha256(directory / name) for name in sorted(names)}


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise Stage3ValidationError(
            "lineage_input_read", "a lineage input could not be read"
        ) from None


def _load_object(path: Path, category: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise Stage3ValidationError(
            category, "an input JSON object is invalid"
        ) from None
    if not isinstance(value, dict):
        raise Stage3ValidationError(category, "an input JSON value must be an object")
    return value


def _load_models(path: Path, model: type[BaseModel], category: str) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise Stage3ValidationError(
            category, "an input table could not be read"
        ) from None
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(
                model.model_validate(json.loads(line, object_pairs_hook=_unique_object))
            )
        except (ValidationError, json.JSONDecodeError):
            raise Stage3ValidationError(category, "an input row is invalid") from None
    return rows


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _rows_bytes(rows: list[BaseModel]) -> bytes:
    return "".join(
        json.dumps(
            row.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _deterministic_timestamp(run_id: str) -> str:
    prefix = run_id.split("-", 1)[0]
    try:
        return (
            datetime.strptime(prefix, "%Y%m%dT%H%M%S%fZ")
            .replace(tzinfo=UTC)
            .isoformat()
        )
    except ValueError:
        raise Stage3ValidationError(
            "invalid_run_id", "run ID has no deterministic timestamp"
        ) from None


def _sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return dict(sorted((str(key), value) for key, value in counter.items()))


def _count_nonfinite(value: Any) -> int:
    if isinstance(value, float):
        return int(not math.isfinite(value))
    if isinstance(value, dict):
        return sum(_count_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_nonfinite(item) for item in value)
    return 0


def _directories_equal(left: Path, right: Path) -> bool:
    left_files = sorted(
        path.relative_to(left) for path in left.rglob("*") if path.is_file()
    )
    right_files = sorted(
        path.relative_to(right) for path in right.rglob("*") if path.is_file()
    )
    return left_files == right_files and all(
        (left / relative).read_bytes() == (right / relative).read_bytes()
        for relative in left_files
    )
