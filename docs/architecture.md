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
                              aggregate-only reports
```

Only explicit collection and inspection commands load `.env` or contact Riot.
Snapshot processing, migration, reporting, tests, and population dry-run are offline.

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
Markdown reports contain configuration and aggregate counts only. Normalized records
omit display identities; PUUID remains an explicitly sensitive internal key.

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
