# External resumable population collection

External collection keeps raw payloads, normalized rows, private catalogs,
checkpoints, lineage companions, and processed Stage 3 inputs outside the Git
working tree. Storage roots come from local JSON configuration; reusable code does
not contain a machine-specific drive path.

`scripts/prepare_external_collection.py` verifies that a retained Stage 3.3A run
and its private catalog identify the same exact-patch, queue-420 match set. It then
creates a private SQLite match-ID index and smoke, bounded-batch, and production
configurations. `--write-root` may stage this non-secret bundle when the source
artifacts and destination drive have different Windows ACLs. The configured live
paths still come from `--external-root`.

The collector applies these operational protections:

- exact accepted public-patch windows and Match-V5 `queue=420`;
- defensive payload validation before normalization;
- optional immediate checkpoint sealing on a newer resolved patch;
- deterministic cross-location match-ID deduplication using a private index;
- cumulative request ceilings, single-concurrency rate-limit handling, and atomic
  checkpoint saves;
- current request metrics persisted with each checkpoint save, with a conservative
  recovery charge after an abrupt process interruption;
- a process-crash-safe exclusive run lock that prevents overlapping resumes of one
  checkpoint.

Run the no-network planner before collection:

```powershell
.\.venv\Scripts\python.exe scripts\plan_expanded_collection.py --config <external-config.json>
```

Start a smoke run, then use the emitted run ID for monotonic bounded extensions:

```powershell
.\.venv\Scripts\python.exe scripts\collect_population.py --config <smoke-config.json>
.\.venv\Scripts\python.exe scripts\collect_population.py --config <larger-batch-config.json> --resume <run-id>
```

An interrupted run can be prepared for Stage 3 only after it reaches a quiescent
`request_budget_exhausted` checkpoint. Stage 3.1 keeps its completed-population
default; `--allow-bounded-partial` is an explicit operational opt-in and still
requires the caller's exact accepted count. Queue, patch, participant, raw-payload,
catalog, normalized-row, and lineage checks are unchanged.
Partial canonical outputs are nested under `snapshot=accepted-<count>` so a later
resume cannot collide with an earlier immutable processing snapshot. Pass that same
snapshot directory as the output root for downstream Stage 3 builders.

External collection expands future training data. It does not authorize model
fitting, parameter selection, recommendation output, cross-platform pooling, or a
change to a frozen evaluation contract.
