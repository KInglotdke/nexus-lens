# Nexus Lens architecture

## Stages and network boundaries

```text
Stage 0 account collection ─┐
                           ├─> immutable data/raw snapshots
Stage 2 population sample ─┘              |
                                          v
                              schemas + patch resolver
                                          |
                                          v
                               pure normalization
                                          |
                         +----------------+----------------+
                         v                                 v
             public-patch JSON/JSONL             SQLite match catalog
                         |                                 |
                         +----------------+----------------+
                                          v
                         Stage 3.1 approval/provenance gate
                                          |
                                          v
                  canonical matches/participants/teams/bans JSONL
                                          |
                                          v
                              aggregate-only quality report
                                          |
                                          v
                     Stage 3.2 deterministic formula contract
                                          |
                                          v
             participant/team features + match analysis context
                                          |
                                          v
                    Stage 3.3A factual draft observations
                                          |
                                          v
             Stage 3.3B matchup/synergy sufficient statistics
                                          |
                                          v
              Stage 3.3C patch-forward offline backtesting
                                          |
                                          v
          Stage 3.4A composition-aware match-level modelling
                                          |
                                          v
             future calibration and recommendation policy
```

Only explicit collection and inspection commands load `.env` or contact Riot.
Snapshot processing, migration, reporting, tests, and population dry-run are offline.

## Stage 3.1 canonicalization

The completed Stage 2 manifest establishes run completion and aggregate patch
counts, while its matching sensitive checkpoint supplies the exact approved match
set. A raw cache hit is not approval by itself. For every approved ID, Stage 3.1
requires a processed catalog row, an actual-public-patch normalized match record,
and an immutable retained Match-V5 payload. Conflicting IDs, queues, public patches,
or duplicate retained payloads fail closed before publication.

The current raw population layout stores newly downloaded payloads in each run's
`downloads/` directory. Reused accepted payloads remain in earlier retained run
directories. Stage 2 derived match records are partitioned by actual public patch.
Stage 3.1 resolves those locations from the retained state instead of assuming every
approved payload was copied into the final continuation run.

Canonical output uses the project's established deterministic JSONL format because
Parquet is not otherwise supported and this feasibility dataset is small. The four
tables are sorted by stable keys and written to a schema- and run-versioned directory
separate from both raw data and older normalized partitions. Files are fully staged
before publication; byte-identical reruns leave the existing directory untouched.

Transformation reads raw statistics without adding analysis features. In
particular, it does not calculate KDA, rates, shares, kill participation, matchup
results, baselines, or scores. A narrowly selected set of Riot challenge counters is
retained as nullable raw source data. The aggregate quality report records shape,
position, ban, timestamp, duration, queue, mode, missingness, sanitized skip, and
invariant categories.

## Stage 3.2 analytical feature flow

Stage 3.2 begins with a strict compatibility gate over `stage3.1-v1` metadata,
quality status, physical row counts, unique keys, patch counts, and cross-table match
IDs. It hashes all six Stage 3.1 artifacts before derivation. Those hashes become
lineage in Stage 3.2 metadata and the aggregate quality report; the input directory
is opened only for reads.

Participant rows are grouped by their own `(match_id, team_id)` before team
denominators are calculated. Complete five-player source vectors produce team kills,
deaths, assists, gold, champion damage, and CS. A missing component propagates to the
team total and dependent shares instead of being imputed. Participant features then
receive only their own team's denominators. Team features also retain the canonical
objective counts and first-objective flags for reconciliation and later factual
analysis.

All formulas live in one versioned contract. Ratios use fractional units; undefined
division returns null; KDA uses denominator one at zero deaths; output serialization
rejects NaN and infinity. Feature values remain unrounded. The quality layer records
derived null counts, zero-denominator events, ranges, share-sum checks, canonical
team-kill comparisons, opposing-death comparisons, and hard invariants.

Position policy is deliberately conservative. Recognized team position remains the
analysis position. A recognized individual position can fill a missing/invalid team
position only as an explicit fallback, and both fallback and disagreement rows are
ineligible for role aggregation. Champion identity is never used to infer role.

The 300-second Stage 3.1 threshold is reused without relabeling short games as proven
remakes. Short games remain in all tables and retain factual totals plus valid
rates/shares, but they are excluded from population-baseline eligibility through a
machine-readable reason. Match-wide analytical eligibility and role-position
eligibility remain separate so a position disagreement does not discard otherwise
valid match facts.

Output is staged as a complete schema/run directory before publication. An identical
rerun compares byte content and leaves the existing output untouched. Stage 3.2 is a
feature foundation for later analysis validation, not recommendation logic or a
subjective performance model.

## Stage 3.3A draft observation flow

Nexus Lens is primarily a champion-select recommendation project. Stage 3.3A pivots
the processed match foundation toward factual inputs for future matchup, ally-synergy,
and composition analysis while preserving Stage 3.2 rather than extending
post-match performance scoring.

The stage reads immutable Stage 3.1 and Stage 3.2 directories, hashes every physical
input, and verifies that Stage 3.2's recorded Stage 3.1 hashes still match. It then
reconciles participant, match, patch, outcome, position, eligibility, team, and ban
keys before producing observations. Existing Stage 3.3A lineage rejects changed
prior-stage hashes instead of silently replacing the run.

Participant and team compositions are ordered by match-local participant ID. An ally
list is emitted only for a team with exactly five participants; a complete team gives
each participant four factual ally relationships. Bans are ordered by Riot
`pick_turn`, with null turns last and champion ID as a deterministic tie-break.

Lane pairing uses only the approved Stage 3.2 `analysis_position`. A participant is
position-pairing eligible when the analysis position is recognized, its source is
`team_position`, and the canonical team/individual positions do not disagree. A lane
opponent is emitted only for a unique reciprocal same-position pair. Missing,
duplicated, ambiguous, fallback, disagreement, or structurally invalid cases remain
as observations with null opponent fields and machine-readable reasons. Champion
identity is never used to guess a lane.

General, role, matchup, and synergy eligibility remain separate. Short games and
other Stage 3.2 exclusions are carried forward. Matchup eligibility additionally
requires a reliable lane opponent; synergy eligibility requires a structurally valid
five-player allied team. No row is deleted because it is inconvenient.

Stage 3.1 supplies platform lineage (`eun1`) but the approved Stage 3.1/3.2 tables do
not contain explicit analysis-region, rank-bracket, or collection-stratum fields.
Stage 3.3A marks those values unavailable rather than reopening sensitive checkpoints
or inferring them. Match-V5 also cannot support a reliable historical pick-order
claim, so pick order is absent by policy.

The later `lineage-v1` repair is a companion dataset rather than an edit to any
existing Stage 3 run. It validates the physical hashes recorded through Stage 3.3B,
then joins the retained checkpoint only where an exact pseudonymous player or
match-source relationship proves the value. Its match rows nest all discovery
contexts; its participant rows remain one per canonical participant. Downstream
consumers therefore join lineage onto existing unique keys instead of re-expanding
one match for each seed.

Metadata contains a deterministic UTC timestamp parsed from the source run ID,
generation policy, reproduction command, all prior-stage hashes, and hashes of every
non-metadata output. Metadata excludes its own hash to avoid self-reference. Files
are staged before atomic publication and identical reruns are byte-equivalent.

The output is ready only for factual aggregation and coverage validation. Statistical
confidence, shrinkage, baselines, hypothesis tests, counter labels, champion-role
profile tags, and tag-to-score rules remain future decisions.

## Stage 3.3B aggregation and statistical primitives

Stage 3.3B reads the immutable Stage 3.3A run and discovers its related Stage 3.1 and
Stage 3.2 directories from Stage 3.3A metadata. It hashes all five Stage 3.3A
artifacts and rechecks every recorded Stage 3.1 and Stage 3.2 hash before aggregation.
The loader fails closed on schema, policy, quality-gate, physical-hash, key, or
cross-table lineage conflicts.

Eligible resolved lane rows become directional matchup contributions. A source match
can contribute once to A-role-versus-B-role and once to the reverse B-role-versus-A
question, with reconciled opposite outcomes, but never twice to either directional
group. Structurally valid synergy rows create four directional focal-with-ally
relationships per eligible participant. Team-match and logical-group keys prevent a
duplicate ally relationship from entering one direction. Champion ID and position,
not champion name, identify every group.

The patch target defaults to the newest numeric public patch in the input. An
explicit CLI target is also supported. Stage 3.3B builds every cumulative window from
age zero through the oldest available input age, capped at five previous patches.
Weights are exactly `0.8 ** patch_age`; no evidence-based early stop is applied.
Windows distinguish patches considered, patches observed for the group, and patch
ages absent from the entire input. An absent patch contributes no row and no weight.

Every statistics bundle keeps integer observed games/wins/losses and raw win rate
separate from weighted wins/losses, total and squared weights, weighted win rate, and
effective sample size. Effective sample size is `(sum(weights) ** 2) /
sum(weights ** 2)` and is null for a zero squared-weight denominator. It is not
treated as an observed game count.

Broader baseline components are sufficient statistics, not final baselines. For a
focal matchup, the focal champion-role component excludes the exact opponent pair;
the opponent champion-role component uses its own directional performance and
excludes the focal pair. Synergy performs the analogous calculation using
participant-games where the specific ally is absent. This prevents direct focal
evidence from also contributing to its own broader baseline. Availability is factual;
sparsity remains unevaluated because no minimum effective-sample threshold is
approved.

The pure beta-binomial primitive accepts an explicit baseline probability, explicit
prior equivalent-game strength, weighted or integral wins/losses, and a configurable
minimum practical advantage. Prior expected wins and losses are derived pseudo-counts
from the broader-data prior mean and calibrated strength; they are not direct
matchup observations. Posterior practical-advantage probability uses
`scipy.stats.beta.sf` directly, avoiding the numerical loss from `1 - cdf` near one.
Zero prior strength is supported only when positive observed wins and losses still
form a proper Beta posterior. Inputs must be finite; the baseline is strictly inside
zero and one; distribution thresholds outside the unit interval return the exact
support-boundary probability.

Normal population aggregation never calls that posterior primitive. The
baseline-combination formula and prior strength are unresolved, so every retained
posterior field remains null and the status is
`not_evaluated_policy_unresolved`. The output is suitable for parameter calibration
and historical backtesting design, not counter labels, reliable synergy claims, or
recommendation scoring.

Publication stages all five files before atomic replacement. Existing output rejects
changed Stage 3.3A lineage, validation-only mode writes nothing, and byte-identical
reruns leave the directory unchanged.

## Stage 3.3C evaluation boundary

Stage 3.3C reads one hash-verified Stage 3.3A platform at a time. Match public patch
defines rolling-origin folds: all training patches must compare numerically earlier
than the evaluation patch, and all rows from one match share its split. Evaluation
and training match-set digests are recorded and their intersection must be empty.
Queue 420, a single platform, and an explicit analysis region are required.

Reference policy fitting consumes only training participants. Prediction consumes
champion, role, directional opponent/ally context, and training sufficient
statistics; it cannot consume the evaluation outcome. Outcome joins occur only in
the metric layer. The framework retains directional matchup and focal-with-ally
semantics and exposes extension points for a later composition policy without
implementing one.

Aggregate metrics include log loss, Brier score, accuracy at the recorded 0.5
threshold, calibration bins/ECE, coverage and abstention, missingness, training
evidence buckets, champion-role frequency buckets, patch/platform/role slices, and
optional deterministic match-cluster bootstrap intervals. Clipping is never
implicit. Undefined non-finite log loss is represented as null with a reason, and no
published value may be NaN or infinite.

Stage 3.3C publications are aggregate-only, deterministic, schema/run versioned,
atomically staged, and immutable. Validation-only mode writes nothing; unequal
existing output fails rather than replacing it. Exact Stage 3.1/3.2/3.3A hashes,
configuration, schema/policy/code versions, and output hashes are recorded. Player
keys, raw Riot identifiers, names, and match identifiers are not published.

The `26.14 -> 26.15` EUNE/EUW evaluations are explicitly non-calibrating mechanical
smoke tests. Platforms and patches are never pooled, and no metric selects a
baseline, prior, policy, reliability threshold, or recommendation. Full details are
in `docs/stage3_3c.md`.

The companion storage audit is read-only. It inventories raw, checkpoint, catalog,
normalized, stage, lineage, and report bytes; computes exact canary/pilot file
overlap by SHA-256 and size; records pilot operational sources physically shared
under canary roots; and projects linear 10,000-match storage plus one temporary
derived-publication copy and a 10% margin. Moving roots, compression, and
derived-artifact retention remain proposals only.

## Stage 3.4A composition boundary

Stage 3.4A converts complete role-resolved Stage 3.3A matches into one oriented
five-versus-five draft row. Champion-role composition features are allied-minus-
opposing. Same-role matchup features use one unordered champion pair with a signed
allied orientation. A team swap negates every feature. No-intercept logistic models
therefore complement probability without fitting or correction.

Vocabulary and regularization selection are training-only. Candidate strengths use
seeded balanced match folds of 26.14, and each fold builds its own vocabulary. SciPy
L-BFGS-B fits sparse L2 models deterministically. There is no scaling or clipping;
unseen champion-role and matchup features contribute zero. EUNE/EUW models, metrics,
and artifacts never share a fit.

The match-level counterfactual boundary accepts one candidate replacement and keeps
nine role-attached slots fixed. It returns comparable probabilities and an unchanged-
slot hash, not a ranked list, recommendation, causal estimate, or lane-dominance
claim.

Publications contain aggregate metrics and privacy-safe model parameters, never
prediction rows or match/player identifiers. Input hashes, configuration, code,
model vocabulary, coefficients, and output hashes are versioned. Atomic immutable
publication, validate-only, failed-write prevention, and a pre-publication storage
reserve apply before any file appears.

The Stage 3.4A pre-26.16 freeze is a separate tracked control artifact. It records
platform-specific fixed L2 values, the exact future argv, paired-log-loss primary
hypothesis, secondary/exploratory labels, dependency and lineage hashes, and a
development-only prospective power analysis. It publishes no match-level rows and
cannot be regenerated from future outcomes without producing a different freeze.

## Forward collection lineage

Checkpoint schema 4 preserves three different scopes:

1. A collection context is the configured tier/division path used to discover a
   seed. It has status `collection_context`.
2. A rank observation is a League-V4 Ranked Solo/Duo response for one specific
   player. It has `rank_source`, `rank_observed_at`, and `observed`,
   `ambiguous`, or `not_collected` status.
3. A discovery context relates one pseudonymous seed to one match and records
   platform, regional routing, analysis region, stratum, source, and timestamp.

These scopes never collapse into a universal match rank. If histories overlap, all
unique match-to-seed contexts are retained in deterministic order. Analytics still
has one match key and ten participant-match keys, preventing double-counting.
Checkpoint state may retain source Riot identifiers because it is ignored sensitive
operational data; versioned lineage output uses the same project-scoped player
pseudonym as Stage 3.1 and aggregate reports contain no player key.

The no-network expanded planner composes independent `PopulationConfig` instances
for EUNE (`eun1`, `europe`, `eune`) and EUW (`euw1`, `europe`, `euw`). It fixes queue
420 and balanced, newest-first, resumable behavior while keeping patch window,
targets, and request ceilings explicit. Planning never authorizes or starts a
collection.

## Composition-aware future modelling

Future recommendation work must retain four distinct questions:

1. Lane or same-role matchup advantage: the direct focal-versus-opponent effect.
2. Whole-draft/team-composition win probability: interactions among both complete
   teams.
3. Player familiarity and champion-pool fit: player-specific suitability, separate
   from population matchup evidence.
4. Final champion-select recommendation: a decision layer that may combine the
   prior three with explicit policy and uncertainty.

A later evaluation should compare a composition-only model against composition plus
direct matchup effects, then compare counterfactual candidate picks while holding
the surrounding allied and enemy draft fixed. Match victory alone does not prove
lane dominance. If lane-outcome labels are eventually required, Timeline-V5 may be
needed for gold, CS, XP, kills, and plates at fixed elapsed times. Timeline
collection and composition-model implementation are intentionally outside this
stage.

## Patch resolution

`info.gameVersion` is preserved as `api_game_version`. A strict parser derives
`api_patch`, then a centralized versioned rule evaluates game year and API major.
The current `riot_public_patch_2026_api16_v1` rule maps API 16.x to public 26.x for
2026. Rules are immutable data objects and can be extended when Riot conventions
change.

The resolver never applies arithmetic to already-public-looking input and never
invents a value outside a known rule. Status and method travel with every normalized
match and into the catalog. Public patch is the partition and reporting key.

Legacy Stage 1 files receive a non-mutating compatibility view at report time. The
explicit `--migrate-stage1` path re-normalizes from raw truth and updates catalog
provenance without deleting raw or requiring a destructive rebuild.

## Platform and regional routing

- EUNE: platform `eun1`, regional route `europe`, analysis region `eune`.
- EUW: platform `euw1`, regional route `europe`, analysis region `euw`.

League-V4 and Summoner-V4 use platform routing. Account-V1 and Match-V5 use regional
routing. The route table is centralized for later platforms.

## Population flow

1. Build and checkpoint a deterministic tier-interleaved or fast
   tier/division/page schedule from the seed.
2. Read bounded League-V4 pages for Ranked Solo/Duo strata. Balanced mode consumes
   one player per stratum visit before rotating to another tier.
3. Use a returned PUUID or resolve it through Summoner-V4 when necessary.
4. Request queue-420 histories newest-first in small pages. Match-V5 history contains
   only IDs, so it cannot be filtered by patch.
5. Record match-to-player provenance in the ignored sensitive checkpoint.
6. Deduplicate match IDs before checking the normalized catalog or downloading.
7. Download bounded pending payloads with conservative concurrency. Dynamically
   request another history page only when recent results have not crossed below the
   lower accepted-window patch. Stop an individual player after the configured number of
   consecutive validated older-patch matches.
8. Validate queue, shape, resolved public patch, and same-season patch window before
   accepting.
9. Write raw payload, normalized public-patch partition, catalog row, checkpoint,
   and non-sensitive run manifest.

The default public-patch window contains the target and its immediately preceding
same-season patch. It truncates at minor 1 and never crosses the public major/season
boundary. Exact-patch mode uses a window size of one. The collection target counts
unique matches across the accepted window, while normalized storage always uses each
match's resolved actual public patch.

An already-cataloged accepted-window match counts toward the unique target without a
second download or normalized copy. A cached exact-window rejection that becomes
eligible after a safe window extension is reloaded from retained raw data,
revalidated, normalized into its actual patch partition, and marked processed.
Outside-window and unresolved patches remain categorized. No accepted games is a
valid bounded outcome.

Efficiency reporting separates invocation-local work from cumulative checkpoint
state. Downloaded accepted, downloaded wrong-patch, and other downloaded outcomes
partition newly downloaded parsed payloads. Accepted catalog, raw-cache, and
checkpoint-state reuse categories are disjoint. The known-wrong reuse count is an
explicit subset of all terminal matches reused without a download. Ratios name both
numerator and denominator, and undefined zero-denominator values are serialized as
`null`.

Exact stopping counts unique accepted-status match IDs in the checkpoint. Per-run
credit is the set difference between target IDs after and before the invocation, so
resume cannot credit an existing accepted match twice. Stratum contribution uses the
first discovery source per accepted match; overlapping provenance remains retained
without inflating the contribution total.

The checkpoint stores the schedule, cursor, shuffled candidates, candidate offsets,
per-player history offset, pending history page, consecutive-old count, and match
provenance. It also stores the target patch, window size, accepted patch list, and
accepted counts by actual public patch. Resume therefore does not reseed or restart
completed histories. Monotonic target, safety-budget, tier-coverage, and same-target
window extensions are allowed. Platform, queue, target patch, rank configuration,
seed, schedule controls, and history controls are immutable. Stage 2 checkpoints
without window metadata are treated as exact-patch checkpoints and migrated only
through these controlled compatibility rules.

If an older interrupted checkpoint contains collection state but no persisted request
count, resume charges a conservative upper bound: every possible logical League,
Summoner, history, and payload call represented by the checkpoint is multiplied by
the client's maximum attempts per call. The recovered charge is recorded in the
checkpoint before any new request, preserving the configured cumulative ceiling.

## Resilience and transactions

The Riot client handles 429 `Retry-After`, transient 5xx responses, timeouts, and
connection failures with bounded exponential backoff and jitter. Authentication and
forbidden responses fail immediately. Metrics cover attempts, successes, retries,
429s, 5xxs, authentication failures, and endpoint categories. Proactive waits for a
full response-header bucket, reactive `Retry-After`, and transient retry backoff are
reported separately and summed as `total_backoff_seconds`. API keys
remain exclusively in headers and neither headers nor identifier-bearing URLs are
logged.

CLI failures are mapped to sanitized categories for checkpoint incompatibility,
request-budget exhaustion, authentication, retry exhaustion, invalid settings, and
local storage. The original exception object, request URL, headers, and identifiers
are never rendered; the message always confirms checkpoint retention when available.

Collection bounds include unique target, sampled players, discovered match IDs, and
total HTTP attempts. Concurrency defaults to one and cannot exceed four.

The sensitive checkpoint is atomically replaced after progress. Raw downloads are
written atomically and never modified afterward. Normalized records use atomic
per-file replacement; the catalog is marked processed only afterward. Report readers
include only processed catalog IDs and deduplicate legacy compatibility files.

## Privacy

Raw Match-V5 data and discovery checkpoints are ignored and may contain encrypted
identifiers required for API calls. Run manifests, console output, JSON reports, and
Markdown reports contain configuration and aggregate counts only. Legacy normalized
records may retain PUUID as an explicitly sensitive internal key. Current normalized
and Stage 3.1 canonical participant rows replace it with a stable project-scoped
hashed `player_key`; no reversal mapping is written.

## Sampling bias and deferred work

Tier interleaving and optional minimum examined-player coverage improve structural
diversity; they do not make a bounded convenience sample representative. Small
targets need not cover every tier or division, and manifests never claim otherwise.

Tier/division selection, randomized ladder pages, active-history availability,
repeated and premade players, patch timing, stopping rules, and regional differences
all influence the sample. A bounded sample is not the complete ladder population.

Deferred work includes broader sampling validation, Parquet migration, statistical
power criteria, production-key operations, scheduling, recommendation logic, and a
user-facing product.
