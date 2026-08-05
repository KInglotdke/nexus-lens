# Stage 3.3C: offline leakage-safe backtesting

Stage 3.3C is an evaluation framework, not a recommendation stage. It tests whether
reference policies fitted on earlier public patches can be scored on a later public
patch without leakage. The current `26.14 -> 26.15` pilot runs are mechanical smoke
tests. Their sample sizes do not authorize a baseline formula, prior strength,
threshold, winning policy, matchup claim, synergy claim, or recommendation.

## Evaluation contract

The split unit is a match. Every participant row belonging to a match receives the
same split. For evaluation patch `E`, the training set contains only input patches
whose numeric `(major, minor)` tuple is strictly less than `E`. The evaluation set
contains only `E`. Match-set intersection is forbidden. Repeating
`--evaluation-patch` produces rolling-origin folds under the same rule.

Each invocation accepts exactly one Stage 3.3A run. Its platform and queue are
validated; mixed platforms and any queue other than Ranked Solo/Duo 420 fail. The
analysis region is an explicit parameter because it was unavailable in legacy Stage
3.3A rows. Platforms, analysis regions, and public patches are never pooled.

Stage 3.3A physical hashes and its recorded Stage 3.1/3.2 hashes are revalidated by
the existing loader. Stage 3.3C metadata stores those hashes plus the fold match-set
digests, parameters, schema/policy/code versions, and implementation hash. It does
not publish match IDs, raw Riot identifiers, player names, or pseudonymous player
keys.

All fitted counts and probabilities come from training matches only. Prediction
functions do not read the evaluation outcome. Evaluation outcomes enter only after
prediction, when aggregate metrics are calculated. Directional A-role-versus-B-role
and B-role-versus-A-role keys remain separate. Directional focal-with-ally synergy
keys also remain separate.

## Experimental reference policies

The framework exposes five deliberately simple interfaces:

- `naive_empirical`: global training participant win rate;
- `champion_role_baseline`: training champion-role empirical rate, abstaining when
  unseen;
- `shrunk_directional_matchup`: directional pair rate shrunk toward its training
  champion-role rate, or the global training rate when that role is unseen;
- `shrunk_directional_synergy`: the mean of directional focal-with-ally posterior
  means, using the same training-only baseline fallback;
- `matchup_synergy_combination`: an equal-weight mechanical combination of the two
  preceding predictions when both exist.

The prior equivalent-game value is a required CLI argument and is labelled
experimental. No default CLI choice can silently become an approved production
prior. The equal-weight combination is an extensibility demonstration, not a model
selection result. Future composition-aware policies can implement the same
fit-on-training/predict-without-outcome boundary.

## Metrics

For each policy and fold, `backtest_metrics.json` contains:

- candidate, evaluated, and abstained rows; coverage and abstention rate;
- log loss, Brier score, and accuracy using an explicit `0.5` threshold;
- configurable equal-width calibration bins and expected calibration error (ECE);
- explicit missing/abstention reasons;
- slices by public patch, platform, role, training champion-role frequency, and
  training evidence bucket;
- optional deterministic percentile intervals from match-cluster bootstrap samples.

Evidence and champion-frequency buckets are descriptive: unseen, 1, 2-4, 5-9,
10-19, and 20-plus training observations. They are not reliability thresholds.

Probability clipping is applied only when both `--clip-min` and `--clip-max` are
provided and is recorded in metadata/report. Without explicit clipping, an
impossible probability/outcome combination makes log loss `null` with an explicit
reason; it never emits infinity. Undefined empty-denominator metrics are `null`.
NaN and infinity are rejected.

## Publication contract

Output is schema/run versioned:

```text
<output-root>/schema=stage3.3c-v1/run=<source-run>__stage3.3c__<config-hash>/
  backtest_metrics.json
  backtest_report.md
  quality_report.json
  metadata.json
```

Files are staged and atomically renamed. Byte-identical reruns are no-ops. A
different result at the same immutable path fails instead of replacing the prior
publication. Failed validation publishes nothing. `--validate-only` performs input,
lineage, split, fitting, prediction, metric, and privacy checks in memory and writes
nothing.

## Pilot smoke commands

These use an arbitrary explicit value of 10 pseudo-games only to exercise mechanics.
That value is not calibrated or approved. EUNE and EUW use separate input and output
roots:

```powershell
.\.venv\Scripts\python.exe scripts\backtest_policies.py --input-run data/pilot/26.15/eune/processed/stage3/schema=stage3.3a-v1/run=20260803T141321876182Z-population --output-root data/pilot/26.15/eune/processed/stage3 --analysis-region EUNE --evaluation-patch 26.15 --experimental-prior-equivalent-games 10 --calibration-bins 10 --clip-min 0.01 --clip-max 0.99 --bootstrap-replicates 200 --bootstrap-seed 33003 --expected-match-count 1000 --validate-only

.\.venv\Scripts\python.exe scripts\backtest_policies.py --input-run data/pilot/26.15/euw/processed/stage3/schema=stage3.3a-v1/run=20260803T141945176809Z-population --output-root data/pilot/26.15/euw/processed/stage3 --analysis-region EUW --evaluation-patch 26.15 --experimental-prior-equivalent-games 10 --calibration-bins 10 --clip-min 0.01 --clip-max 0.99 --bootstrap-replicates 200 --bootstrap-seed 33003 --expected-match-count 1000 --validate-only
```

Remove `--validate-only` to publish the already-validated experimental report. Doing
so still makes no network request and does not read `.env`.

## Leakage failure conditions

Stage 3.3C fails closed for duplicated matches or participant keys, mixed platforms,
non-420 input, participant/match patch conflicts, missing evaluation patches, empty
earlier training windows, training/evaluation match overlap, an evaluation row sent
through a fitted training set, changed input hashes, or an unequal immutable output.
Tests additionally mutate evaluation outcomes and require predictions to remain
unchanged.

## Storage readiness

`scripts/audit_storage.py --data-root data` is read-only. It reports current bytes
and files by raw payload, checkpoint, catalog, normalized, Stage 3, lineage, and
other/report components; per-platform bytes per accepted match; exact file
duplication between canary and pilot by SHA-256 plus size; and linear 10,000-match
projections. When pilot Stage 3.1 metadata points to an operational source retained
under the canary root, the audit reports those bytes as shared rather than pretending
the pilot has another raw copy. Shared operational bytes enter the pilot scaling
basis without being added twice to current physical disk usage.

The conservative headroom estimate is:

```text
1.10 * (incremental projected permanent bytes
        + one projected normalized/Stage-3/lineage publication copy)
```

It is a point estimate, not a filesystem reservation. Raw payloads, collection
manifests/catalogs/checkpoints, configuration, code/schema versions, and lineage
hashes provide the strongest reproduction path and should be retained. Potentially
reversible future measures are lossless raw-JSON compression with new verified hash
manifests, a verified copy-and-hash move to separate raw/processed roots, and
regeneration of derived tables from retained raw truth. Stage 3.3C only proposes
these measures; it does not execute them.

## Deferred decisions

Production baseline construction, prior calibration, threshold selection,
hyperparameter search, policy selection, composition models, timeline features,
recommendation generation, and claims of matchup reliability remain deferred. A
larger future chronological study should use multiple evaluation patches and compare
stability separately by platform, patch, role, champion-frequency, and evidence
bucket before any of those decisions.
