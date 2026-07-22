# Nexus Lens

Nexus Lens is a privacy-conscious, patch-aware League of Legends data feasibility
pipeline. It collects only Ranked Solo/Duo (`queueId=420`), keeps raw responses
immutable, normalizes analysis-ready records, and supports bounded population
sampling through official Riot APIs.

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
PUUIDs needed for official API calls, provenance, and deduplication. Normalized data
retains PUUID only as an internal cross-match key. Console summaries and reports
contain aggregate counts only and never emit Riot IDs, names, PUUIDs, summoner IDs,
or sampled-player identifiers.

Everything under `data/raw`, `data/processed`, and `data/snapshots` is Git-ignored.
See [docs/data_dictionary.md](docs/data_dictionary.md) and
[docs/architecture.md](docs/architecture.md).

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m compileall -q src scripts tests
```
