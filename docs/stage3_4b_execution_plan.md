# Stage 3.4B-1 execution and publication plan

This plan is frozen before any Stage 3.4B-1 model is fitted to the real patch-26.15
development corpus. Executing it requires a separate explicit authorization.

## Preflight

1. Verify the repository commit and the immutable Stage 3.4A result/diagnostic hashes.
2. Validate the protocol and schema with:

   ```powershell
   $env:PYTHONPATH='src'
   .\.venv\Scripts\python.exe scripts\validate_stage34b_protocol.py `
     --protocol config/evaluation/stage3.4b-1-patch26.15-protocol-v1/protocol.json `
     --schema config/evaluation/stage3.4b-1-patch26.15-protocol-v1/protocol.schema.json
   ```

3. Load only the four already sealed patch-26.15 Stage 3.3A components used by the
   published pooled development bundle. Join their eligible drafts, in memory, to
   their immutable Stage 3.1 `game_creation` values on private `(platform, match)`
   identity. Verify exactly 9,414 unique queue-420 patch-26.15 drafts, platform counts
   4,700/4,714, the frozen source-bundle hash, UTC timestamp span, and outer counts.
4. Re-run tests, Ruff, compilation, secret/privacy scanning, path checks, sealed-source
   inventories, and free-space checks. Loading `.env`, contacting Riot, or loading a
   future dataset is prohibited.

The private timestamp adapter must return `TimedDraft` objects to
`nexus_lens.stage34b.evaluate_stage34b`. The evaluator itself accepts no paths and
cannot access a data source, environment file, or network service.

## One authorized development run

Use one controlled offline invocation that loads and validates the sealed inputs,
calls `evaluate_stage34b(..., enforce_frozen_counts=True)`, and writes to a new
staging directory. Record privacy-safe phase progress outside the scientific output.
Never print private join keys or row-level predictions.

Audit the fixed operation budget: 171 predictive training operations, comprising 163
predictive optimizer fits and eight analytic baseline estimates. The 63 calibration
evaluations are metrics only and add at most 63 optimizer fits because constant-logit
scopes are handled analytically. The 2,000 paired bootstrap replicates perform zero
fits. Fail closed on count drift, convergence failure, non-finite values, fold-count
drift, or source-hash drift.

The candidate feature vocabulary is constructed from each training partition only.
All nine policies predict the same 6,368 outer rows exactly once. Later outer folds
may train on earlier blocks, but no outer observation is used by the model that scores
its own block. The final all-development fit occurs only after OOF metrics and frozen
selection logic are complete.

## Publication gate

Before atomic publication, validate:

- deterministic fold, protocol, source, aggregate-result, and final-model hashes;
- exact fit/operation accounting and successful optimizer statuses;
- paired row reconciliation without persisting OOF rows;
- every candidate-versus-baseline log-loss and Brier interval;
- overall, platform, and chronological-block metric reconciliation;
- finite values, fixed ECE bins, coverage, and support suppression;
- absence of identifiers, names, external paths, credentials, and row-level data;
- unchanged Stage 3.4A and sealed-source hashes;
- deterministic rerun equivalence in a non-publishing staging location.

Publish only aggregate metrics, interval summaries, support-qualified summaries,
the protocol/lineage manifest, and the bounded final fitted-model representation
per repository policy. Do not publish diagnostics or OOF rows. Describe all results
as patch-26.15 rolling-origin development estimates, not final test performance and
not recommendation reliability.

If no candidate passes every material-usefulness gate, publish that negative
development result and return to prospective design. Do not access a future sealed
temporal holdout. If a candidate passes, freeze its exact model, support rules, and
evaluation command before separately deciding whether any later data should be
designated as that holdout.
