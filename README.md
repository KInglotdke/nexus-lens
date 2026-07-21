# Nexus Lens

Nexus Lens is a patch-aware data feasibility pipeline for League of Legends
Ranked Solo/Duo. Stage 0 collects immutable Match-V5 snapshots. Stage 1 validates,
normalizes, deduplicates, and reports on those snapshots without making additional
API requests.

This is still a feasibility prototype. It does not contain a recommendation model,
ladder sampler, production API, database server, scheduler, or UI.

## Guarantees and scope

- Match history collection always requests Riot queue ID `420`.
- Both collection and normalization independently reject non-420 matches.
- Raw snapshots are never changed or deleted by Stage 1.
- Exactly ten participants and two teams are required for normalization.
- Known team positions become `TOP`, `JUNGLE`, `MIDDLE`, `BOTTOM`, or `UTILITY`.
  Unknown values remain visible for diagnostics; the legacy role field is never
  used to invent a team position.
- Processing is deduplicated by match ID in a local SQLite catalog and is safely
  resumable. Rejected matches remain retryable.
- Reports contain aggregates only and never contain player identifiers.

The current five-match sample validates schema shape and pipeline behavior. It is
far too small for matchup, synergy, balance, population, or recommendation
conclusions.

## Setup

Python 3.12 is required.

```bash
python -m venv .venv
# Activate the environment using the command appropriate for your shell.
python -m pip install -e ".[dev]"
```

Only Stage 0 collection requires `.env`. Copy `.env.example` to `.env` and add a
development API key and consenting test Riot ID. Secrets and collected data remain
local and are ignored by Git.

## Collect raw data (Stage 0)

```bash
python scripts/feasibility_collect.py --count 5
```

This live command resolves a Riot ID, requests recent match IDs with `queue=420`,
validates every downloaded payload again, and writes an immutable timestamped
snapshot beneath `data/raw`.

## Process snapshots (Stage 1)

The processing CLI does not load settings, read `.env`, or call Riot APIs.

```bash
# Process the newest raw snapshot and write JSON and Markdown reports.
python scripts/process_snapshots.py --latest

# Process one named snapshot.
python scripts/process_snapshots.py --snapshot 20260721T134459041766Z

# Examine every snapshot; already processed match IDs are skipped.
python scripts/process_snapshots.py --all

# Select one report format or disable report generation.
python scripts/process_snapshots.py --latest --report json
python scripts/process_snapshots.py --latest --report markdown
python scripts/process_snapshots.py --latest --report none
```

Normalized output uses deterministic JSON/JSONL files partitioned by region, patch,
and queue:

```text
data/processed/
  catalog.sqlite3
  region=<region>/
    patch=<major.minor>/
      queue=420/
        matches/<match-id>.json
        participants/<match-id>.jsonl
        teams/<match-id>.jsonl
  reports/
    feasibility_report.json
    feasibility_report.md
```

Per-match files make atomic replacement and deduplication straightforward without
adding a heavy analytical dependency. JSONL is appropriate for Stage 1's small
sample; Parquet is the planned migration when population-scale collection makes
columnar scans and compression valuable.

The catalog records source snapshot, routing context, patch, queue, timestamp,
status, and categorized failure information. A `processed` match is skipped across
both repeated and overlapping snapshots. `processing` or `rejected` entries can be
retried without a destructive rebuild.

Reports are generated from normalized records, not raw assumptions. They cover
shape, missingness, positions, queue and patch coverage, win/loss consistency,
durations, catalog statistics, and processing throughput. Both report files live
under ignored processed storage.

## Privacy

Raw responses contain sensitive Riot identifiers. Normalized participant records
retain PUUID only because future consented player discovery and cross-match
deduplication require a stable internal key. They deliberately omit Riot ID,
summoner name, summoner ID, and other display identifiers. PUUID must still be
treated as sensitive: processed files, the SQLite catalog, and reports remain
Git-ignored, and reports never emit identifier values.

See [docs/data_dictionary.md](docs/data_dictionary.md) for normalized fields and
[docs/architecture.md](docs/architecture.md) for component and transaction
boundaries.

## Development checks

```bash
pytest
ruff check .
python -m compileall -q src scripts
```
