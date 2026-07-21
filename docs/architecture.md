# Nexus Lens architecture

Nexus Lens has two deliberately separate local stages. Stage 0 is the only network
boundary. Stage 1 is deterministic and operates exclusively on immutable files.

## Data flow

```text
Stage 0 (live, explicit)                 Stage 1 (offline, resumable)

.env -> RiotClient -> Riot API           data/raw/<snapshot>/
             |                                    |
             v                                    v
     FeasibilityCollector                 Match-V5 schemas
             |                                    |
             v                                    v
 data/raw/<timestamp>/                    pure normalization
                                                  |
                                  +---------------+---------------+
                                  |                               |
                                  v                               v
                           partitioned JSON/JSONL          SQLite catalog
                                  |                               |
                                  +---------------+---------------+
                                                  |
                                                  v
                                      aggregate-only reports
```

## Raw and processed boundaries

Raw snapshots preserve Riot responses and collection provenance exactly as the
collector accepted them. Neither the processor nor report generator edits or
deletes raw files.

Normalization validates queue `420`, ten participants, and two teams before
producing a match row, ten participant rows, and two team rows. Optional values are
nullable or receive documented arithmetic defaults. Timestamps are UTC-aware. Patch
is derived from the leading major/minor game-version components.

Known `teamPosition` values are canonicalized without consulting legacy `role` or
`lane`. Unknown positions remain visible so upstream changes are diagnosable.

## Storage and transactions

Stage 1 uses standard-library SQLite and deterministic JSON/JSONL. Records are
partitioned as:

```text
region=<routing-region>/patch=<major.minor>/queue=420/<record-type>/
```

Each match owns one match file, one participant JSONL file, and one team JSONL file.
Files are written to same-directory temporary files, flushed, and atomically
replaced. Only after all three replacements succeed does the catalog transaction
mark the match `processed`. If execution stops earlier, a later run safely replaces
the same deterministic files. Reports read only match IDs marked `processed`, so an
orphaned file from an interrupted three-file replacement cannot enter an analysis.
No append-only partial batch is possible.

The catalog's unique match ID prevents duplicates across repeated or overlapping
snapshots. Statuses are `processing`, `processed`, and `rejected`. Rejected and
interrupted entries are retried; successfully processed entries are skipped. Failure
reasons are categorized without placing raw payloads or participant identifiers in
console summaries or reports.

JSONL avoids a new binary dependency for a five-match feasibility sample. Parquet
should replace it once the dataset is large enough that columnar scans, compression,
and schema evolution justify PyArrow or an equivalent dependency.

## Privacy

Raw payloads contain player identifiers. Normalized records omit display names,
Riot IDs, summoner names, and summoner IDs. PUUID is retained only as a sensitive
internal key for future consented player discovery and cross-match analysis.
Everything beneath `data/raw` and `data/processed`, including the SQLite catalog and
reports, is ignored by Git. Human and JSON reports expose aggregates only.

The processing CLI never creates application settings, loads `.env`, or contacts
Riot. Live collection remains an explicit separate command.

## Deferred work

- Population and ladder sampling
- Parquet migration and analytical query engine
- Scheduled or distributed processing
- Database server or production API
- Recommendation, matchup, and synergy logic
- UI and observability platform

The validated five-match sample proves structural feasibility only. Patch-aware
partitions make future incremental collection possible, but no statistical claim is
appropriate until a much broader, intentionally sampled population exists.
