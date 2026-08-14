# Stage 3.5A: top-lane trajectory and personalization-data feasibility

Stage 3.5A is a data-engineering and feasibility stage. It does not fit a model,
select a policy, or reverse the negative Stage 3.4B draft-composition result. The
Stage 3.1–3.4B inputs and the Stage 3.4B scientific bundle remain immutable. Raw
timelines, checkpoints, focal rows, match groups, and pseudonymous player keys are
private and live outside the repository; only aggregate audits may be published.

## Product and information contract

The product question is whether a known top-lane player should prefer one candidate
champion over another against a selected enemy top champion, using only information
available during champion select. The prospective feature interface contains:

- platform, public patch, and focal side;
- the candidate/focal champion and enemy top champion;
- strictly pre-match focal-player familiarity and support fields.

It excludes opponent accounts, PUUIDs, player keys, levels, ranks, histories,
mastery, familiarity, win rates, and inferred skill. It also excludes trajectory
labels, tower/intervention events, and final game outcome. Opponent identifiers are
used privately only to locate the opposing top participant and are discarded from
the row contract. Final win is retained solely as a secondary target.

## Source and timeline collection

The source is the complete sealed 10,000-match EUNE/EUW patch-26.15 population,
including the external and retained-private Stage 3.1 components. Stage 3.5A does
not start from the smaller Stage 3.4B composition-eligible subset. The eligibility
audit found a 379-match shortfall, so the bounded extension added exactly 220 unique
patch-26.15 queue-420 matches per platform. The final source/timeline inventory is
10,440; 10,037 passed the technical eligibility rules and the deterministic core
stops at exactly 10,000.

`scripts/collect_timelines.py` verifies every declared Stage 3.1 file hash before
using Match-V5. It requests one timeline per unique match through the regional
`europe` route. EUNE and EUW use separate content-addressed raw directories and
SQLite checkpoints. Canonical JSON is written atomically under its SHA-256 digest.
The private catalog maps match ID to checksum and makes reruns idempotent. Offline
validation retains only the compact catalog index and reads one checksummed timeline
at a time instead of retaining the complete raw timeline corpus in memory. Request
attempts are charged transactionally against a cumulative per-platform ceiling;
the ceiling cannot reset on resume. HTTP 404 becomes a terminal unavailable record,
while authentication, exhausted retries, request budget, malformed payload, and
storage failures stop the run safely.

The checked-in example is non-executable. A live config must replace source paths
and zero hashes in ignored/private storage:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe scripts\collect_timelines.py `
  --config data\stage35a\private\production-config.json --dry-run

.\.venv\Scripts\python.exe scripts\collect_timelines.py `
  --config data\stage35a\private\production-config.json `
  --platform eun1
```

Run EUW separately with `--platform euw1`. `--verify-only` performs a zero-network
read and checksum verification. The tool loads normal Riot credentials only in live
collection mode and never prints or stores them.

## Eligibility and frame policy

A core match must be patch 26.15, queue 420, non-remake under the prospective
technical rule, contain exactly one unambiguous TOP participant on each standard
team, and expose finite/nonnegative participant-frame values at 5, 10, and 15
minutes. Level must be between 1 and 18. Twenty- and twenty-five-minute frames are
not required.

For endpoint `t`, the extractor chooses the earliest frame whose timestamp is at or
after `t` and no more than 60 seconds late. It never uses a frame before the
endpoint. The actual observed frame timestamp is retained. Missing or malformed
primary frames exclude the match deterministically; exploratory frames become
explicit nulls. If more than 10,000 matches are eligible, chronological/platform/
private-ID stable ordering selects exactly the first 10,000.

At 5, 10, 15, 20, and 25 minutes the focal-minus-opposing-top values are:

- total gold;
- XP;
- lane minions;
- total farm (lane plus jungle minions);
- level.

The 5→10, 10→15, 15→20, and 20→25 changes are differences between those endpoint
advantages. Repeated endpoints remain nested under one match group and are never
treated as independent matches.

## Perspective and weighting

Every eligible match yields two rows. Both carry the same private project-scoped
`match_group_id`; their scientific weights are 0.5, so a match totals one. Numeric
advantages negate under reversal, focal win complements, and `enemy_top_outer_first`
and `allied_top_outer_first` reverse. `neither_by_15` is self-reversing. Future
folding and resampling must operate on `match_group_id`, never on focal rows.

## Tower and intervention measurements

The first `BUILDING_KILL` event with top lane, outer-turret type, a valid destroyed
team, and timestamp supplies time to first top outer turret. The primary 15-minute
label is `enemy_top_outer_first`, `neither_by_15`, or
`allied_top_outer_first` from the focal perspective. Exact timestamp and indicators
for 10/15/20/25 minutes are retained.

Plate difference is emitted only when every relevant `TURRET_PLATE_DESTROYED` event
has a usable top-lane attribution and destroyed-team field. The feasibility rule
uses events through 14:00 and publishes support/missingness; it must be abandoned or
amended prospectively if live payloads do not support reliable attribution.

Intervention indicators are post-selection sensitivity variables, never features:

- a jungler or another role participated in a kill involving either top laner;
- such a kill occurred in the declared top-lane map proxy
  (`x <= 6000 and y >= 8000`, or `x <= 8000 and y >= 10000`);
- a Herald kill by the attacking team preceded a top-tower destruction by at most
  120 seconds.

These indicators do not identify every gank. Pressure without a supported kill or
frame event remains unobserved.

## Strict familiarity and low-history handling

Rows are processed in UTC start-time cohorts. Features are calculated for every row
in a cohort before any row from that cohort updates history. Consequently only a
strictly earlier match can contribute. The initial feasibility fields include prior
observed ranked/top games, prior/recent champion games, days since last champion
use, exponentially weighted 5/10/15 gold and XP advantages, general top baseline,
champion-specific performance relative to that baseline, and support/missingness.

Observed win rate is secondary and shrunk toward a fixed 0.5 population prior with
the configured support strength. The public audit reports support and uncertainty.
`limited_history_or_smurf_like` means insufficient observed history, not low skill.
Account level is absent. No account is discarded or penalized because history is
sparse.

Within-corpus history is necessarily left-truncated: an account may have many games
before the collection window. Stage 3.5A therefore reports coverage at 1/5/10/20
prior champion games and only estimates a future bounded expansion. The proposed
cap is the earlier of 100 prior queue-420 matches or 180 days. That expansion needs
separate authorization and must deduplicate shared historical matches.

## Publication and validation

`scripts/build_stage35a.py --validate-only` performs the complete transformation and
audit without writing. Publication atomically creates a private two-row-per-match
table and four repository-safe aggregate artifacts. Public validators reject raw
identifiers, player keys, match IDs, external paths, non-finite numbers, and partial
scientific publication.

The audit covers retention/exclusions, platform representation, endpoint
distributions, target correlations, champion/matchup support, late-endpoint survivor
bias, towers, game outcome, intervention proxies, history coverage, symmetry, and
impossible/non-finite values. Twenty- and twenty-five-minute results are always
described as survivor-conditioned exploratory observations.
