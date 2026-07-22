# Nexus Lens normalized data dictionary

## Match record

- `match_id`, `data_version`: Match-V5 identity and response schema provenance.
- `game_creation`, `game_start`, `game_end`: UTC-aware ISO 8601 timestamps.
- `game_duration_seconds`: Riot-reported duration.
- `api_game_version`: complete internal/client version, for example
  `16.14.792.1234`.
- `api_patch`: parsed internal major/minor, for example `16.14`.
- `public_patch`: canonical public patch, for example `26.14`, or null when no rule
  can resolve it safely.
- `patch_resolution_method`, `patch_resolution_status`: mapping provenance and
  uncertainty.
- `queue_id`: always 420 after normalization.
- `game_mode`, `game_type`, `map_id`, `platform_id`: match context.
- `participant_count`, `team_count`: structural quality fields.

## Participant record

- `match_id`, `participant_id`, `team_id`: within-match keys.
- `champion_id`, `champion_name`: champion selection.
- `team_position`, `individual_position`: normalized known positions; unknown values
  remain visible. `legacy_role` and `legacy_lane` are diagnostic only.
- `win`, kills/deaths/assists, KDA, kill participation, CS, gold, vision, warding,
  damage, mitigation, healing, crowd control, summoner spell IDs, rune/perk IDs, and
  item slots 0–6 support initial analyses.
- `puuid`: sensitive internal key retained only for consented cross-match discovery
  and deduplication. It must never enter reports, console output, or Git.

## Team record

- `match_id`, `team_id`, `win`, champion kills.
- Baron, dragon, herald, tower, and inhibitor totals and first-objective flags.
- Champion ban IDs when Riot supplies them.

## Processing catalog

The SQLite catalog uniquely keys match ID and stores routing region, complete API
version, API patch, public patch, resolution method/status, queue, source snapshot,
processing time, terminal/resumable status, and categorized failure information.
Legacy `patch` remains only as a migration compatibility column.

## Population checkpoint and manifest

The ignored checkpoint may contain PUUIDs/encrypted identifiers, sampled
tier/division provenance, discovered match IDs, overlap sources, and per-match
statuses. It is sensitive local operational state.

The raw run manifest contains no player identifiers. It records run ID, UTC time,
platform/routing, target public patch, configured strata/seed/safety bounds, accepted
raw file paths, aggregate counters, request metrics, and completion status.
