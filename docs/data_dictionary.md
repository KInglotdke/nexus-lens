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

## Stage 3.1 canonical dataset (`stage3.1-v1`)

Canonical tables use deterministic JSONL. JSON integers, booleans, strings, and null
values are validated through explicit Pydantic schemas. UTC timestamps are ISO 8601
strings. Every table includes `processing_schema_version`.

### `matches.jsonl`

One row per approved match. `match_id`, `public_patch`, complete `game_version`,
configured `platform`, `queue_id`, `game_creation`, `game_duration_seconds`,
`winning_team_id`, `is_remake_or_short_game`, and `source_payload_reference` are
required. `game_start_timestamp` and `game_end_timestamp` are nullable because Riot
may omit them. The short-game flag is true only when duration is below 300 seconds.

### `participants.jsonl`

One row per participant. The required structural keys are `match_id`, integer
`participant_id`, integer `team_id`, champion ID/name, win, `player_key`, and
`processing_schema_version`. `player_key` is a deterministic, project-scoped
SHA-256 pseudonym; raw PUUID is never written. A payload missing the source needed
for a player key fails validation instead of emitting an unjoinable participant.

The remaining nullable raw fields are both summoner spell IDs, Riot team/individual
positions, legacy lane/role, kills/deaths/assists, lane and neutral minion counts,
gold, total/physical/magic/true champion damage, damage taken, mitigation, healing,
teammate healing/shielding, vision and ward counters, control wards
placed/purchased, champion level, item slots 0â€“6, and time played. Selected nullable
Riot challenge counters are objectives stolen, ally saves, skillshots dodged/hit,
solo kills, and turret plates taken. No derived KDA, CS, rate, share, or participation
field is present.

### `teams.jsonl`

One row per team. `match_id`, `team_id`, and `win` are required. Counts and
first-objective booleans are nullable for champions, towers, inhibitors, dragons,
Rift Heralds, and Barons when Riot omits an objective fragment.

### `bans.jsonl`

One row per supplied ban slot. `match_id` and `team_id` are required; `pick_turn` and
`champion_id` are nullable. Champion ID `-1` is retained as an explicit no-ban value.
Missing slots are not invented; they are counted in the quality report.

### Metadata and quality report

`metadata.json` records schema versions, run/input provenance, queue, accepted
patches, deterministic row counts, storage format, and pseudonymization policy.
`quality_report.json` is aggregate-only and records input/processed counts, shape,
patch distribution, position distributions, ban completeness counts and rates,
timestamp/duration quality, queue/mode anomalies, missing critical data, sanitized
skipped-payload categories, invariant failures, output paths, and Stage 3.2
readiness. Undefined rates use JSON `null`.

## Stage 3.2 analytical dataset (`stage3.2-v1`)

All Stage 3.2 rows carry `processing_schema_version`. The accompanying metadata
records `stage3.2-formulas-v1`, the complete formula contract, fractional ratio
units, Stage 3.1 file hashes, row counts, patches, storage format, and privacy policy.

### `participant_match_features.jsonl`

Exactly one row per canonical participant. Factual context includes match,
participant, pseudonymous player, patch, team, champion, win, both Riot positions,
duration, short-game state, and original combat/economy/vision/ward/healing fields.
No raw PUUID or display identity is added.

Position fields are:

- `analysis_position`: recognized team position, otherwise a recognized individual
  position fallback, otherwise null;
- `analysis_position_source`: `team_position`,
  `individual_position_fallback`, or `unresolved`;
- `position_disagreement`: true only when both recognized source positions differ;
- `role_aggregation_eligibility` and `role_exclusion_reasons`: conservative controls
  for disagreement, fallback, unresolved, short, or structurally invalid rows.

Derived fields and exact contracts are:

- `total_cs = total_minions_killed + neutral_minions_killed`;
- `kda = (kills + assists) / max(1, deaths)`;
- `kill_participation = (kills + assists) / team_kills`;
- `cs_per_minute = total_cs / (game_duration_seconds / 60)`;
- `gold_per_minute = gold_earned / duration_minutes`;
- `team_gold_share = gold_earned / team_gold`;
- `damage_to_champions_per_minute = total_damage_to_champions /
  duration_minutes`;
- `team_champion_damage_share = total_damage_to_champions /
  team_champion_damage`;
- damage-taken, mitigated, vision, total-healing, teammate-healing, and
  teammate-shielding rates use the same duration-minutes denominator.

Ratios are fractions from 0 to 1, not percentages. Values are unrounded. KDA at zero
deaths uses denominator one. Missing numerators propagate null; missing, zero, or
negative denominators produce null. No infinity or NaN is permitted.

### `team_match_features.jsonl`

Exactly two rows per complete match. Keys and context are match, patch, team, win,
duration, short-game status, analytical eligibility, reasons, and schema version.
Participant sums provide team kills, deaths, assists, gold, champion damage, and CS
only when all five members supply the source metric. Canonical champion, tower,
inhibitor, dragon, Rift Herald, and Baron counts and first-objective flags are copied
without reinterpretation.

### `match_analysis_context.jsonl`

Exactly one row per match. It contains match/patch/queue, duration, short-game flag,
participant and team counts/completeness, position completeness, disagreement,
fallback and unresolved counts, general analytical eligibility, role-aggregation
eligibility, and machine-readable exclusion reasons. It intentionally does not copy
the complete canonical match record.

Short games satisfy `0 < game_duration_seconds < 300`. They remain in all outputs,
but general analytical eligibility is false with reason `short_game`. Valid totals,
rates, and shares remain available. Invalid duration or incomplete structural inputs
also disable general eligibility. Position problems disable role aggregation without
discarding otherwise valid factual match analysis.

### Stage 3.2 quality report

The aggregate-only report includes input and output row/patch counts, eligibility,
position outcomes, null counts for every derived metric, zero/invalid denominator
events, zero-death convention usage, derived min/max values, range failures,
participant/team/share reconciliations, source flag conflicts, sanitized errors,
hard invariants, lineage hashes, privacy status, and readiness for Stage 3.3 analysis
validation.

## Stage 3.3A draft observations (`stage3.3a-v1`)

Stage 3.3A stores factual champion-select context without statistical conclusions.
All rows include Stage 3.1/3.2 schema lineage, the source run ID, and the Stage 3.3A
schema version. The only player identifier remains `player_key`.

### `participant_draft_observations.jsonl`

One row per participant-match. It contains match/participant/player/champion/team
keys, champion name when available, win and both team outcomes, patch, queue,
platform, duration, short-game state, Stage 3.2 positions and eligibility, ordered
ally/enemy champion IDs, lane-opponent resolution, matchup/synergy eligibility, and
machine-readable reasons.

`analysis_position` is copied from Stage 3.2. Position pairing requires a recognized
TOP, JUNGLE, MIDDLE, BOTTOM, or UTILITY value sourced from `team_position`, without a
team/individual disagreement. A lane opponent is populated only for one unique,
reciprocal, eligible same-position participant on the opposing team. Otherwise the
opponent participant/champion/name fields are null and status is one of the explicit
ineligible, missing, ambiguous, nonreciprocal, or invalid-structure categories.

For an allied team with five participants, `allied_champion_ids` contains the other
four champions ordered by participant ID. `enemy_champion_ids` contains observed
opponents in participant-ID order. These lists are factual relationship inputs; they
do not encode pick order, synergy strength, or counter evidence.

`platform` is available. `region`, `rank_bracket`, and `collection_stratum` are null
because those fields are not present in the approved Stage 3.1/3.2 inputs; companion
status fields make that absence explicit.

### `team_draft_observations.jsonl`

One row per team-match. It contains ordered team and opponent champion IDs, both
outcomes, own/opponent bans, patch/queue/platform/duration context, structure and
position flags, general/matchup/synergy eligibility, exclusion reasons, and lineage.
Bans preserve `pick_turn`, nullable champion IDs, and explicit champion ID `-1`
without inferring intent.

### `match_draft_context.jsonl`

One row per match. Nested team compositions are sorted by team ID and their champions
by participant ID. Nested team bans are sorted by team ID and pick turn. The row also
contains patch/queue/platform/duration, short-game and Stage 3.2 completeness flags,
position-quality counts, number of reciprocal lane pairs, whether all five pairs are
resolved, general/matchup/synergy eligibility, reasons, and lineage.

### Quality report and metadata

`draft_observation_quality_report.json` is aggregate-only. It reports exact row and
patch counts, five-versus-five structure, ally/enemy coverage, lane-resolution
statuses, complete-pair matches, matchup/synergy eligibility, factual ally
relationship count, position findings, platform/rank/region/stratum availability,
ban counts, missing champion names, reconciliations, duplicate/non-finite/invariant
findings, privacy, and current-sample limitations.

`metadata.json` records `stage3.3a-draft-policy-v1`, input directories and every
Stage 3.1/3.2 SHA-256 hash, deterministic generation configuration, a source-run UTC
timestamp, reproduction command, row counts, and deterministic hashes for the three
JSONL files and quality report. Metadata omits its own hash to avoid self-reference.

The current 100-match sample cannot establish the approved 5%/50-game champion-role
viability rules or reliable matchup/synergy evidence. No pick order, win-rate lift,
confidence interval, shrinkage, baseline, hypothesis test, counter label,
recommendation score, or champion composition tag is generated here.

## Stage 3.3B aggregate evidence (`stage3.3b-v1`)

Stage 3.3B contains directional sufficient statistics and reusable statistical
primitives. It contains no player identifier, counter label, strong-synergy label,
or production posterior evaluation. All identities use champion IDs, positions, and
factual context; champion names are nullable display attributes.

### Shared nested statistical schemas

`SufficientStatistics` contains `observed_games`, integer `wins` and `losses`,
`raw_win_rate`, `weighted_wins`, `weighted_losses`, `weighted_win_rate`,
`sum_weights`, `sum_squared_weights`, and `effective_sample_size`. Undefined rates
and zero-weight effective sample sizes are null.

`PatchSpecificStatistics` contains public patch, numeric patch age, exact
`0.8 ** patch_age` weight, and unweighted games/wins/losses/rate for observations on
that patch.

`CumulativePatchWindow` contains its oldest age, all considered patches, patches
actually observed for that group, input-wide missing patch ages/names, and the
cumulative `SufficientStatistics`. Missing patches contribute neither games nor
weight.

`BaselineComponent` identifies the excluded counterpart champion ID/position,
records availability and unresolved-sparsity statuses, and carries the same overall,
patch-specific, and cumulative statistics. No two components are combined into an
expected baseline in this schema.

`PosteriorFields` reserves the supplied baseline, supplied prior strength, provisional
practical threshold, derived pseudo-counts, posterior parameters/mean/advantage,
practical-advantage probability, and evidence tier. In the normal retained-population
run, every field except the provisional `minimum_practical_advantage` is null.

### `matchup_aggregates.jsonl`

One row per directional focal-champion-role versus opponent-champion-role and lineage
context. The logical key includes focal/opponent champion IDs and positions,
platform, explicit region/status, rank/stratum/status, queue, and target patch. Each
row contains available names, complete statistics bundles, focal
leave-opponent-out and opponent leave-focal-out components, unresolved posterior
fields, visibility/recommendation status, source observation count, Stage 3.3A
participant-file hash, run/schema lineage, and the Stage 3.3B schema version.

A single match contributes no more than once to a directional key. Reciprocal
A-versus-B and B-versus-A rows have equal observed counts and opposite wins/losses
when both are present.

### `synergy_aggregates.jsonl`

One row per directional focal-champion-role with ally-champion-role and the same
lineage context. It contains analogous statistics, source lineage, and unresolved
posterior fields. The two broader components are the focal role in eligible games
without the specific ally and the ally role in eligible games without the focal
champion. One team-match contributes no more than once to a directional relationship.
The row does not claim causality or call synergy a counter.

### `champion_role_sufficient_statistics.jsonl`

One row per observed champion ID, analysis position, and lineage context. Separate
bundles describe role-eligible, matchup-eligible, and synergy-eligible
participant-games. Opponent and ally exclusion-component lists preserve everything
needed to reproduce aggregate leave-pair-out counts.

The row also records the champion-role eligible-game share, that role's eligible
games, the champion's eligible games across roles, and the two approved future
viability checks: at least 5% and at least 50 games. The combined result is explicitly
provisional and `authoritative_role_viability` remains false for this 100-match input.

### `aggregation_quality_report.json`

This aggregate-only report records Stage 3.3A physical counts and hashes, eligible
participant and directional-contribution counts, aggregate row counts, exact observed
count and effective-sample distributions, patch-window coverage, baseline component
availability, role and lineage coverage, unresolved posterior counts, reconciliations,
invariants, non-finite checks, privacy status, and readiness for calibration. It
contains no pseudonymous player-key values.

### `metadata.json`

Metadata records `stage3.3b-aggregation-policy-v1`,
`stage3.3b-statistics-v1`, exact Stage 3.1/3.2 paths discovered from Stage 3.3A,
hashes of every Stage 3.1/3.2/3.3A input, approved and unresolved parameters,
generation configuration, deterministic ordering/serialization, SciPy's direct
`scipy.stats.beta.sf` numerical implementation, deterministic timestamp and
reproduction command, row counts, and hashes of all non-metadata outputs.

The baseline-combination formula, prior equivalent-game strength, minimum effective
sample, evidence-based patch stopping, calibration, major-change history policy, and
causal synergy controls remain null/unresolved. Therefore, these files prepare
calibration and backtesting but do not establish reliable counter recommendations.

## Collection lineage sidecar (`lineage-v1`)

This sidecar references immutable Stage 3.1, 3.2, 3.3A, and 3.3B artifacts by path
and SHA-256. It never changes or republishes those tables.

### `match_discovery_lineage.jsonl`

One row per canonical match. Run-level fields are `platform_id`,
`regional_routing`, `analysis_region`, queue 420, source run/checkpoint/schema, and
lineage schema. `participant_rank_is_match_rank` is always false.

`discovery_contexts` is a deterministically ordered list rather than a row
multiplier. Each item contains a project-scoped `seed_player_key`, platform/routing,
`collection_tier`, `collection_division`, `collection_stratum`, status
`collection_context`, independently observed seed-rank fields and observations,
discovery timestamp/source, and explicit provenance statuses. A legacy source has a
null timestamp with `not_collected` and a deterministic derived source label.
Conflicting seed-rank observations remain nested and set rank status `ambiguous`.

### `participant_rank_lineage.jsonl`

One row per canonical participant-match key. `rank_tier` and `rank_division` are
populated only when the same pseudonymous player joins a stored Ranked Solo/Duo
League-V4 entry. `rank_status` is `observed`, `ambiguous`, or `not_collected`;
`rank_source`, `rank_observed_at`, timestamp status, and all retained observations
make the evidence explicit. `collection_seed_for_match` and
`matching_discovery_context_count` identify a provable seed relationship without
calling that context the participant's rank.

### `lineage_audit_report.json` and `metadata.json`

The report is aggregate-only: field trace, routing semantics, row/status/stratum
counts, reconciliation failures, invariants, privacy assertions, and readiness. It
contains no player keys. Metadata records all input hashes, generation policy,
deterministic timestamp/serialization, output hashes, row counts, privacy method,
and reproduction command.

## Expanded collection plan (`expanded-collection-plan-v1`)

Planner output is non-sensitive JSON, not a collection artifact. It records the
requested platform IDs separately from regional routing and analysis regions;
queue 420; public-patch window newest-first; tiers, divisions, deterministic
balanced schedule and per-stratum planning allocation; accepted-match target;
player, match-ID, request, history, page, and concurrency ceilings; raw/processed
and checkpoint path templates; rate-limit/deduplication policy; lineage policy; and
scope exclusions.

`required_configuration.riot_api_key_present` tests only whether the
`NEXUS_LENS_RIOT_API_KEY` environment-variable name exists. The planner does not
load dotenv or read, serialize, or print the secret value.
`network_requests_made` is always zero.
