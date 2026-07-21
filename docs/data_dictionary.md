# Stage 1 data dictionary

All normalized files are deterministic UTF-8 JSON or JSONL. Nullable fields reflect
optional or missing Riot values rather than invented replacements.

## Match record

- `match_id`, `data_version`: Match-V5 identity and schema version.
- `game_creation`, `game_start`, `game_end`: UTC-aware ISO 8601 timestamps.
- `game_duration_seconds`: Riot-reported duration.
- `game_version`, `patch`: complete version and normalized major/minor patch.
- `queue_id`: always 420 after normalization.
- `game_mode`, `game_type`, `map_id`, `platform_id`: game context.
- `participant_count`, `team_count`: retained for quality reporting.

## Participant record

- `match_id`, `participant_id`, `team_id`: within-match keys.
- `champion_id`, `champion_name`: champion selection.
- `team_position`, `individual_position`: normalized known positions; unknown values
  are preserved. `legacy_role` and `legacy_lane` are diagnostics only.
- `win`, `kills`, `deaths`, `assists`: outcome and combat totals.
- `kda`: `(kills + assists) / deaths`; when deaths are zero, the numerator is used
  rather than infinity.
- `kill_participation`: `(kills + assists) / team champion kills`; zero when team
  kills are known to be zero and null when a team total is unavailable.
- `total_minions_killed`, `neutral_minions_killed`, `cs`: lane CS, neutral CS, and
  their sum.
- Gold, vision, warding, champion damage, damage taken, mitigation, healing, and
  crowd-control fields retain Riot totals when available.
- Summoner spell IDs, rune style/perk IDs, and item slots 0–6 describe the build.
- `puuid`: sensitive internal stable key retained for future consented participant
  discovery and cross-match analysis. It must never appear in reports or Git.

## Team record

- `match_id`, `team_id`, `win`: team key and result.
- `champion_kills`: team champion objective kills.
- Baron, dragon, herald, tower, and inhibitor kill totals and first-objective flags.
- `bans`: champion IDs in the ban list when Riot provides them.

## Catalog

The SQLite catalog stores match ID, routing region, patch, queue, source snapshot,
processing timestamp, status, failure category, and a non-sensitive failure reason.
Match ID is unique. `processed` entries are deduplicated; `processing` and `rejected`
entries are safe to retry.
