# Nexus Lens

Nexus Lens is a privacy-conscious, patch-aware foundation for a League of Legends
champion-select recommendation system. It collects only Ranked Solo/Duo
(`queueId=420`), keeps raw responses immutable, and builds reproducible observations
for future matchup, ally-synergy, and team-composition reasoning.

It does not contain recommendation logic, professional esports data, a production
service, scheduled collection, or a UI.

## Patch identity

Match-V5 `info.gameVersion` is an internal/client version. In known 2026 data, API
patch `16.14` corresponds to public League patch `26.14`. Public patch names are
canonical for partitions, reports, analysis, and future user-facing recommendations.

Every normalized match therefore stores:

- `api_game_version`: complete provenance such as `16.14.792.1234`;
- `api_patch`: internal major/minor such as `16.14`;
- `public_patch`: canonical public value such as `26.14`;
- `patch_resolution_method` and `patch_resolution_status`.

The mapping lives in one versioned rule table in `nexus_lens.patches`. The current
rule applies only to 2026 API-major 16 data. Unsupported years, malformed versions,
and unexpected API majors remain explicitly unresolved—public-looking input such as
`26.14` is never converted a second time.

## Setup and secrets

Python 3.12 is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and supply a development key and consenting test
account. `.env` is used only by live collection/inspection commands. Offline
processing and dry-run commands do not load it.

When a development key expires, replace only `NEXUS_LENS_RIOT_API_KEY` in the local
`.env` using an editor. Never paste the value into source, documentation, reports,
command arguments, issue trackers, or chat, and never commit `.env`.

## Stage 0: bounded account collection

```powershell
.\.venv\Scripts\python.exe scripts\feasibility_collect.py --count 5
```

This requests match history with `queue=420`, revalidates downloaded payloads, and
writes an immutable raw snapshot.

To inspect at most five recent matches without printing any account or participant
identifier:

```powershell
.\.venv\Scripts\python.exe scripts\inspect_recent.py --count 5
```

The output is limited to sequence, UTC date, public/API patch, complete API version,
queue, and the configured account's champion. Preview the zero-request plan with
`--plan`.

## Stage 1: offline normalization

```powershell
.\.venv\Scripts\python.exe scripts\process_snapshots.py --latest
.\.venv\Scripts\python.exe scripts\process_snapshots.py --snapshot 20260721T134459041766Z
.\.venv\Scripts\python.exe scripts\process_snapshots.py --all
```

Old Stage 1 records used an ambiguous internal `patch` field. The report reader can
resolve those records without mutating them. To explicitly regenerate current
public-patch partitions from immutable raw snapshots and migrate catalog rows:

```powershell
.\.venv\Scripts\python.exe scripts\process_snapshots.py --all --migrate-stage1
```

This is an explicit compatibility operation, not the normal workflow. Raw data is
never removed; legacy derived files may remain, and report readers deduplicate them
in favor of resolved Stage 2 records.

Current output is deterministic JSON/JSONL:

```text
data/processed/
  catalog.sqlite3
  region=eune/
    patch=26.14/
      queue=420/
        matches/<match-id>.json
        participants/<match-id>.jsonl
        teams/<match-id>.jsonl
  reports/
```

JSONL keeps dependencies small for feasibility-scale data. Parquet remains the
planned migration once population-scale analytical scans justify it.

## Stage 3.1: canonical analysis tables

Stage 3.1 is an offline, deterministic transformation of the completed Stage 2
population. It accepts only the checkpoint-approved 100-match set, cross-checks the
manifest, read-only catalog, actual-patch normalized partition, and retained raw
payload for every match, then creates four canonical JSONL tables plus metadata and
an aggregate quality report. It never calls Riot or loads `.env`.

Validate the retained inputs and transformed rows without writing anything:

```powershell
.\.venv\Scripts\python.exe scripts\build_canonical_tables.py --manifest data/raw/20260722T125547567196Z-population/manifest.json --validate-only
```

Publish the canonical dataset:

```powershell
.\.venv\Scripts\python.exe scripts\build_canonical_tables.py --manifest data/raw/20260722T125547567196Z-population/manifest.json
```

If `--manifest` is omitted, the command selects the newest completed population
manifest that has exactly 100 accepted matches with the required 26.14/26.13 patch
split. `--checkpoint` is normally derived from the manifest run ID. Output is kept
separate under:

```text
data/processed/stage3/schema=stage3.1-v1/run=<stage-2-run-id>/
  matches.jsonl
  participants.jsonl
  teams.jsonl
  bans.jsonl
  metadata.json
  quality_report.json
```

Rows and columns are sorted deterministically. All files are staged before the run
directory is published, and an identical rerun is byte-equivalent and creates no
duplicates. The quality report contains aggregates and sanitized reason categories
only. A short/remake candidate is defined as `game_duration_seconds < 300`.

Stage 3.1 stores no raw PUUID or display identity. Its `player_key` is a stable,
project-scoped SHA-256 pseudonym truncated to 128 bits so a player can be joined
across matches without exposing the source identifier. This is pseudonymization,
not anonymization; canonical files remain local and Git-ignored.

Stage 3.1 deliberately does not calculate KDA, CS/min, gold/min, damage share, kill
participation, matchup statistics, rank baselines, recommendations, or scores. Those
analysis choices are deferred to Stage 3.2 or later.

## Stage 3.2: deterministic analytical features

Stage 3.2 reads only a completed, compatible Stage 3.1 run. It validates all six
Stage 3.1 files, records their SHA-256 hashes, and creates participant-, team-, and
match-level analytical tables under a separate schema directory. It never contacts
Riot, loads `.env`, or modifies Stage 3.1.

Validate all inputs, formulas, output rows, and reconciliations without writing:

```powershell
.\.venv\Scripts\python.exe scripts\build_analytical_features.py --input-run data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population --validate-only
```

Publish the analytical dataset:

```powershell
.\.venv\Scripts\python.exe scripts\build_analytical_features.py --input-run data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population
```

Output uses deterministic JSONL and staged atomic publication:

```text
data/processed/stage3/schema=stage3.2-v1/run=<stage-2-run-id>/
  participant_match_features.jsonl
  team_match_features.jsonl
  match_analysis_context.jsonl
  metadata.json
  quality_report.json
```

The centralized `stage3.2-formulas-v1` contract uses these rules:

- ratios are fractions, not percentages;
- `total_cs = total_minions_killed + neutral_minions_killed`;
- per-minute values divide by `game_duration_seconds / 60`;
- `KDA = (kills + assists) / max(1, deaths)`, so zero deaths uses denominator 1;
- kill participation divides by the participant's own-team kills;
- gold and champion-damage shares divide by the participant's own-team totals;
- a missing numerator or missing/non-positive denominator produces JSON `null`;
- derived values are not rounded internally, and NaN/infinity are forbidden.

For positions, a recognized `team_position` is authoritative. A disagreement never
silently replaces it with `individual_position`; that participant remains usable for
general factual analysis but is ineligible for role aggregation. If team position is
missing or invalid, a recognized individual position is recorded as an explicit,
role-ineligible fallback. No champion-based role guessing is used.

All matches and participants remain present. Matches below 300 seconds retain totals
and valid rates/shares but set `analytical_eligibility=false` with reason
`short_game`. Other position-quality exclusions are represented separately through
role-aggregation eligibility; a short game is not labeled a confirmed remake.

Stage 3.2 still does not implement subjective scores, matchup/counterpick or synergy
statistics, recommendations, baselines, model training, agents, or a dashboard.

## Stage 3.3A: draft observation foundation

Stage 3.3A turns Stage 3.1 canonical facts plus Stage 3.2 eligibility/position policy
into factual champion-select observations. It is not an expansion of post-match
performance scoring and does not calculate matchup win rates, synergy lifts,
confidence, counters, recommendations, or composition scores.

Validate without writing:

```powershell
.\.venv\Scripts\python.exe scripts\build_draft_observations.py --stage3-1-run data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population --stage3-2-run data/processed/stage3/schema=stage3.2-v1/run=20260722T125547567196Z-population --validate-only
```

Publish the immutable observation run:

```powershell
.\.venv\Scripts\python.exe scripts\build_draft_observations.py --stage3-1-run data/processed/stage3/schema=stage3.1-v1/run=20260722T125547567196Z-population --stage3-2-run data/processed/stage3/schema=stage3.2-v1/run=20260722T125547567196Z-population
```

Output is deterministic JSONL plus aggregate quality and lineage metadata:

```text
data/processed/stage3/schema=stage3.3a-v1/run=<stage-2-run-id>/
  participant_draft_observations.jsonl
  team_draft_observations.jsonl
  match_draft_context.jsonl
  draft_observation_quality_report.json
  metadata.json
```

An opponent is assigned only when exactly one position-eligible opposing participant
has the same recognized Stage 3.2 `analysis_position` and that pairing is reciprocal.
Disagreement, fallback, missing, duplicate, or ambiguous positions produce a null
opponent and an explicit reason; the code never guesses from champion identity.

For a valid five-player team, each participant row lists four allied champion IDs in
participant-ID order. Enemy compositions and team compositions use the same stable
ordering. These are observations, not inferred synergy effects. Bans retain Riot's
pick-turn ordering and explicit `-1` no-ban values, without claims about intended
picks, roles, targets, or counters.

Every factual row remains present. Stage 3.2 general and role eligibility is reused;
short games remain observable but are excluded from future matchup/synergy
aggregation. Position disagreements remain factual and do not silently change roles.
Match-V5 does not reliably expose historical champion-select pick order, so Nexus
Lens does not reconstruct or claim it.

The retained 100 matches are sufficient to validate observation structure only. They
cannot establish authoritative champion-role viability under the approved 5% and
50-game rules, reliable counters, synergy conclusions, or full-roster profiles.
Confidence intervals, shrinkage, baseline adjustment, hypothesis tests, champion tag
taxonomy, and tag-to-score rules remain deliberately unresolved.

## Stage 3.3B: transparent matchup and synergy aggregation

Stage 3.3B converts the factual Stage 3.3A rows into directional matchup,
directional ally-synergy, and champion-role sufficient statistics. It remains an
offline evidence foundation: every observed relationship stays visible, but the
retained population is not labeled with counters, strong synergies, or
recommendation eligibility.

Validate the retained input without writing:

```powershell
.\.venv\Scripts\python.exe scripts\build_draft_aggregates.py --input-run data/processed/stage3/schema=stage3.3a-v1/run=20260722T125547567196Z-population --validate-only
```

Publish the immutable aggregate run:

```powershell
.\.venv\Scripts\python.exe scripts\build_draft_aggregates.py --input-run data/processed/stage3/schema=stage3.3a-v1/run=20260722T125547567196Z-population
```

The Stage 3.1 and Stage 3.2 paths are discovered from Stage 3.3A metadata and every
recorded physical hash is rechecked. Output is written separately:

```text
data/processed/stage3/schema=stage3.3b-v1/run=<stage-2-run-id>/
  matchup_aggregates.jsonl
  synergy_aggregates.jsonl
  champion_role_sufficient_statistics.jsonl
  aggregation_quality_report.json
  metadata.json
```

A matchup is directional: A-in-TOP against B-in-TOP and B-in-TOP against A-in-TOP
answer opposite questions and carry opposite outcomes. An ally relationship is also
directional: A-in-TOP with C-in-JUNGLE and C-in-JUNGLE with A-in-TOP are distinct.
Each source match or team-match may contribute at most once to the same directional
key. Roles, queue, platform, available region/rank lineage, and collection stratum
remain part of the grouping context.

Raw observed counts are kept separately from recency-weighted quantities. The target
is the newest input patch unless `--target-patch` is supplied. Cumulative windows
start at that target and add at most five prior patches with weight `0.8 ** patch_age`.
Every window records considered patches, observed patches, and missing input patch
ages. Effective sample size is `(sum_weights ** 2) / sum_squared_weights`. It is
reported alongside observed game count and is null when total squared weight is zero.
Missing patches are not zero-result observations.

For each matchup, Stage 3.3B separately preserves the focal champion-role performance
against other opponents and the opposing champion-role performance against other
focal champions. Synergy rows similarly preserve the focal champion-role without the
specific ally and the ally champion-role without the focal champion. The direct focal
pair is excluded from these broader components, preventing it from being counted
both as direct evidence and as its own baseline evidence.

The reusable beta-binomial primitive requires callers to supply both a baseline
probability and prior equivalent-game strength. The prior mean will eventually come
from broader champion-role evidence; calibrated prior strength will determine the
equivalent pseudo-games. Pseudo-wins and pseudo-losses are calculated prior
quantities, not additional direct observations. Fractional weighted counts are
supported. `scipy.stats.beta.sf` directly calculates the regularized incomplete-beta
survival probability for numerical stability near one; Nexus Lens does not implement
a custom numerical approximation.

The provisional minimum practical advantage is configurable and defaults to `0.01`.
Evidence tiers use the approved 0.90 and 0.95 posterior-probability boundaries.
However, the baseline-combination formula, prior strength, minimum effective sample,
calibration method, adaptive-window stopping rule, major-change history policy, and
causal synergy controls remain unresolved. Consequently, the normal Stage 3.3B run
leaves every posterior field null with
`statistical_status=not_evaluated_policy_unresolved`.

The retained 100 EUNE games validate aggregation mechanics only. Visible raw
observations are not recommendation eligibility, weak samples are not counters, and
the current data cannot establish authoritative full-roster role viability or
reliable counter claims.

## Collection-lineage repair

The retained run's missing Stage 3.3B dimensions were traced to specific schema
boundaries, not missing sampler behavior:

- `platform_id=eun1` survived normalization and Stage 3.1;
- `regional_routing=europe` and analysis region `eune` survived in the Stage 2
  checkpoint/manifest but Stage 3.1 did not emit them;
- seed tier/division and the match-to-seed relationship survived in
  `checkpoint.matches[*].sources`, then Stage 3.1 reduced that state to match
  approval and patch;
- League-V4 ranked Solo/Duo tier/division survived in retained
  `sampling.candidates`, but it applied only to the player in that entry and was
  never joined into Stage 3.1;
- rank-observation and match-discovery timestamps were never collected in the
  legacy checkpoint.

The immutable Stage 3.1/3.2/3.3A/3.3B directories are not rewritten. A separate
`lineage-v1` sidecar verifies their hashes and the source checkpoint/manifest, then
publishes one match-lineage row and one participant-match rank-lineage row per
canonical row. Collection contexts are never presented as observed participant or
match rank. Multiple discovery contexts are nested and sorted under one match row,
so they cannot multiply analytical contributions.

Validate without writing, then publish:

```powershell
.\.venv\Scripts\python.exe scripts\repair_collection_lineage.py --validate-only
.\.venv\Scripts\python.exe scripts\repair_collection_lineage.py
```

The retained sidecar contains 100 match rows, 100 discovery contexts, and 1,000
participant rows. Stored League-V4 responses prove rank for 104 participant-match
rows representing 15 unique players; 896 rows remain `not_collected`. No retained
rank or discovery timestamp can be recovered. See
[docs/lineage_audit.md](docs/lineage_audit.md) for the complete trace.

Future checkpoints use schema version 4. They record platform, regional route,
analysis region, pseudonymous seed key, collection context, independently observed
seed rank and timestamp, discovery timestamp/source, and every match-to-seed
relationship. The aggregate manifest records only the policy/version flag; it does
not expose player keys.

## Stage 2: controlled population sampling

EUNE uses platform route `eun1` for League/Summoner endpoints and regional route
`europe` for Account/Match endpoints. EUW uses `euw1` plus the same `europe`
regional route. Routing is configured centrally so regions do not duplicate logic.

The collector:

- samples configurable tier/division strata from League-V4;
- defaults to deterministic tier-interleaved `balanced` sampling, while `fast`
  retains the one-stratum-at-a-time target-reaching mode;
- persists the seeded schedule, candidate offsets, player history offsets, and
  tier/division provenance for exact resume behavior;
- requests only queue 420 histories;
- requests small newest-first history pages and fetches another page only while it
  remains useful;
- stops a player after a configurable number of consecutive older-patch payloads;
- deduplicates IDs before full match downloads;
- reuses known accepted and wrong-patch catalog observations without downloading
  their payloads again;
- associates overlapping matches with multiple sampled-player provenance records;
- accepts only matches resolving inside the configured public patch window;
- stores sensitive discovery/checkpoint state only under ignored local directories;
- stops at the exact unique target or a player, match-ID, or request bound;
- uses conservative concurrency, bounded retry/backoff, `Retry-After`, and a hard
  request budget.

Match-V5 can filter match history by queue, but its history response contains only
match IDs and offers no patch filter or patch metadata. Full payload validation is
therefore still mandatory. The early stop is a conservative optimization based on
the API's reverse-chronological history ordering: after consecutive validated older
patches, collection moves to another player. It never derives patch from a match ID
and never accepts a match without validating the payload.

The default `--patch-window-size 2` accepts the target public patch and its
immediately preceding same-season patch. For example, target `26.14` accepts
`26.14` and `26.13`; it does not accept `26.12`. The window never crosses a public
major/season boundary, so targets `26.1` and `27.1` accept only themselves. Use
`--patch-window-size 1` for exact-patch behavior. Every accepted match is normalized
under its actual `patch=<public-patch>` partition, and the unique target count spans
all accepted patches.

Zero-request dry run:

```powershell
.\.venv\Scripts\python.exe scripts\collect_population.py --platform eun1 --target-public-patch 26.14 --patch-window-size 2 --tiers GOLD PLATINUM EMERALD DIAMOND --divisions I II III IV --target-matches 10 --max-players 25 --initial-history-batch-size 5 --max-history-per-player 20 --older-patch-stop-threshold 2 --sampling-strategy balanced --minimum-players-per-tier 0 --max-match-ids 250 --max-requests 300 --seed 42 --concurrency 1 --smoke-test --dry-run
```

For a later multi-platform run, the dedicated planner is safer and more explicit.
It does not load `.env`, construct an API client, or make requests. It only checks
whether the `NEXUS_LENS_RIOT_API_KEY` environment-variable name is present and
never reads or prints its value:

```powershell
.\.venv\Scripts\python.exe scripts\plan_expanded_collection.py --config config/collection/eune-pilot.example.json
.\.venv\Scripts\python.exe scripts\plan_expanded_collection.py --config config/collection/euw-pilot.example.json
```

The checked-in examples use retained patch `26.14` solely to remain executable.
Replace `target_public_patch` with the verified current public patch immediately
before an authorized run; `patch_window_size=2` then means current plus one previous
same-season patch. Planning output separates `eun1/eune` from `euw1/euw`, reports
all 16 tier/division strata, even planning allocations, newest-first traversal,
explicit ceilings and output/checkpoint templates, queue 420, and lineage status.

Pilot examples target 1,000 accepted matches per platform. The corresponding
`eune-serious.example.json` and `euw-serious.example.json` examples target 10,000.
Those sizes, per-stratum allocations, and request ceilings are operational examples,
not approved statistical thresholds. Later expansion must depend on repeated-matchup
coverage and held-out/backtest stability. No planning command starts collection.

Ten-match EUNE smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\collect_population.py --platform eun1 --target-public-patch 26.14 --patch-window-size 2 --tiers GOLD PLATINUM EMERALD DIAMOND --divisions I II III IV --target-matches 10 --max-players 25 --initial-history-batch-size 5 --max-history-per-player 20 --older-patch-stop-threshold 2 --sampling-strategy balanced --minimum-players-per-tier 0 --max-match-ids 250 --max-requests 300 --seed 42 --concurrency 1 --smoke-test
```

Default-limit 100-match EUNE run:

```powershell
.\.venv\Scripts\python.exe scripts\collect_population.py --platform eun1 --target-public-patch 26.14 --patch-window-size 2 --tiers GOLD PLATINUM EMERALD DIAMOND --divisions I II III IV --target-matches 100 --max-players 100 --initial-history-batch-size 5 --max-history-per-player 20 --older-patch-stop-threshold 2 --sampling-strategy balanced --minimum-players-per-tier 3 --max-match-ids 1000 --max-requests 1000 --seed 42 --concurrency 1
```

Resume with the saved run ID:

```powershell
.\.venv\Scripts\python.exe scripts\collect_population.py --platform eun1 --target-public-patch 26.14 --patch-window-size 2 --tiers GOLD PLATINUM EMERALD DIAMOND --divisions I II III IV --target-matches 100 --max-players 100 --initial-history-batch-size 5 --max-history-per-player 20 --older-patch-stop-threshold 2 --sampling-strategy balanced --minimum-players-per-tier 3 --max-match-ids 1000 --max-requests 1000 --seed 42 --concurrency 1 --resume <RUN_ID>
```

Resume permits monotonic extensions: a larger match target, larger player/match-ID/
request budgets, higher minimum tier coverage, and widening an exact target patch to
the same target's patch window. Platform, queue, target patch, rank configuration,
seed, sampling schedule controls, and history controls must remain compatible.
Incompatible fields produce a sanitized field-specific error. Existing exact-patch
accepted matches remain valid; newly in-window cached raw payloads are revalidated,
normalized into their actual patch partition, and credited once.

Older interrupted checkpoints that contain work but lost request metrics are charged
a conservative upper bound before resume. This may leave less request budget than
was actually consumed, but it preserves the configured hard ceiling without asking
the user to edit the checkpoint.

Targets above 100 require `--allow-over-default`. Concurrency is restricted to 1–4
and defaults to 1. Development-key limits remain the controlling constraint.

### Collection-efficiency counters

Efficiency values are scoped explicitly to the current CLI invocation unless their
name says `total`. A retained checkpoint therefore does not credit an already
accepted match a second time on resume.

- `total_accepted_matches_credited` is the unique cumulative accepted-window count
  used by exact stopping. `accepted_matches_credited_this_run` is its unique
  invocation-local increment. Target and accepted-previous-patch counts are also
  reported separately.
- `newly_downloaded_accepted_matches` and
  `newly_downloaded_wrong_patch_matches` are disjoint outcomes among
  `payloads_downloaded`, which counts unique successfully parsed Match-V5 payloads
  newly fetched during this invocation.
- `accepted_matches_reused_this_run` is split into disjoint catalog, retained-raw,
  and checkpoint-state counts. A pending checkpoint record with an existing raw
  payload takes the raw-cache path; otherwise terminal catalog metadata is used.
- `known_terminal_matches_reused_without_download` includes accepted and rejected
  terminal knowledge. `known_wrong_patch_matches_reused_without_download` is its
  explicitly named wrong-patch subset, not an additional match count.
- `new_download_acceptance_rate` is newly downloaded accepted divided by downloaded
  payloads. `overall_examined_match_acceptance_rate` is matches credited this run
  divided by unique terminal matches examined through either downloads or reuse.
- `new_payloads_per_newly_downloaded_accepted_match` and
  `new_payloads_per_accepted_match_credited_this_run` expose the two useful cost
  denominators separately. Undefined ratios are JSON `null` and print as `None`.

`accepted_matches_by_contributing_stratum` assigns each accepted match to its first
discovery-provenance stratum, so its total plus `unattributed_accepted_matches`
equals `total_accepted_matches_credited`. Additional overlapping provenance remains in
the ignored checkpoint but is not double-counted in the aggregate contribution.

## Sampling limitations

Balanced sampling provides structural diversity: it visits tiers in interleaved
order and can enforce a minimum number of examined players per tier. That is not
statistical representativeness. A small target is allowed to stop without every
configured stratum contributing, and every manifest explicitly records
`rank_representative: false`.

This is a controlled feasibility sample, not a claim about the complete ranked
population. Bias can arise from selected tiers/divisions, ladder page ordering,
active-player histories, repeated or premade players, the timing of patch rollout,
regional differences, and stopping once a target is reached. Page randomization and
deduplication reduce some mechanical bias but do not make the sample representative.

The existing five-match sample validates structure only. It cannot support matchup,
synergy, balance, population, or recommendation conclusions.

## Privacy and local data

Raw responses and population checkpoints contain sensitive encrypted identifiers or
PUUIDs needed for official API calls, provenance, and deduplication. Legacy Stage 1/2
normalized records retain PUUID as an internal cross-match key; Stage 3.1 canonical
tables replace it with `player_key`. Console summaries and reports contain aggregate
counts only and never emit Riot IDs, names, PUUIDs, summoner IDs, or sampled-player
identifiers.

Everything under `data/raw`, `data/processed`, and `data/snapshots` is Git-ignored.
See [docs/data_dictionary.md](docs/data_dictionary.md) and
[docs/architecture.md](docs/architecture.md).

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
```
