# Stage 3.4B-1 operational refreeze

Status: **operationally frozen before any real Stage 3.4B-1 model fit**.

This amendment repairs execution infrastructure only. The scientific protocol,
folds, equations, features, grids, support thresholds, seeds, optimizer settings,
selection rules, calibration policy, bootstrap settings, and usefulness thresholds
remain byte-identical to commit
`cc5dd3ab764cf69eff4488890bbcd361e220b8df`.

## Implemented controls

- A deterministic source adapter verifies the four Stage 3.3A components against the
  frozen Stage 3.4A source contract and the combined input fingerprint.
- Each component's Stage 3.1 `matches.jsonl` is resolved only through hash-verified
  Stage 3.3A metadata. The private match join is never published.
- Zero-fit preflight verifies 9,414 eligible drafts, platform counts 4,700/4,714,
  3,046 initial training drafts, 6,368 shared evaluation rows, exact block counts,
  UTC chronology, timestamp and fold fingerprints, and source hashes.
- Full mode is exactly one load/evaluate/publish invocation. It has no validation-only
  mode and requires `--publish --authorize-real-fit`.
- Diagnostics record privacy-safe phase, fit, fold, configuration, duration,
  parameter-count, optimizer-iteration, optimizer-status, calibration, bootstrap,
  and publication events in a unique external JSONL file.
- Publication stages nine bounded aggregate files and atomically renames the complete
  directory. Existing unequal output fails closed.
- The manifest verifies every published artifact. Its scientific deterministic
  fingerprint covers only scientific-content files; separately hashed observational
  timing and environment files cannot perturb that fingerprint.

No diagnostic or public artifact may contain a match ID, player identifier,
pseudonymous player key, outcome label, row-level prediction, coefficient, credential,
or raw external path.

## Prospectively expanded comparisons

Before the real run, the paired-bootstrap matrix is expanded with five ablations:

1. shared synergy versus side-intercept composition;
2. shared lane counter versus side-intercept composition;
3. combined interactions versus side-intercept composition;
4. combined interactions versus shared synergy;
5. combined interactions versus shared lane counter.

Each reports candidate-minus-comparator log-loss and Brier differences using the same
2,000 match-level replicates, seed 34201, and platform × outer-block strata. Negative
differences indicate improvement. Bootstrap performs zero fits.

## Zero-fit preflight command

Run from a clean operational-refreeze commit whose `HEAD` equals `origin/main`.
Replace the four source placeholders with the already sealed Stage 3.3A directories;
do not record those external paths in a tracked file.

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe scripts\run_stage34b.py `
  --scientific-protocol config/evaluation/stage3.4b-1-patch26.15-protocol-v1/protocol.json `
  --scientific-schema config/evaluation/stage3.4b-1-patch26.15-protocol-v1/protocol.schema.json `
  --operational-amendment config/evaluation/stage3.4b-1-patch26.15-protocol-v1/operational-amendment-v1.json `
  --operational-schema config/evaluation/stage3.4b-1-patch26.15-protocol-v1/operational-amendment-v1.schema.json `
  --stage34a-protocol config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1/protocol.json `
  --eune-external '<sealed-eune-external-stage3.3a>' `
  --eune-retained '<sealed-eune-retained-stage3.3a>' `
  --euw-external '<sealed-euw-external-stage3.3a>' `
  --euw-retained '<sealed-euw-retained-stage3.3a>' `
  --output-directory config/evaluation/stage3.4b-1-patch26.15-protocol-v1/development-v1 `
  --diagnostic-log '<writable-external-temp>\stage34b-preflight-unused.jsonl' `
  --repository-commit '<operational-refreeze-commit>' `
  --preflight
```

Preflight validates both path locations but creates neither the diagnostic file nor
the scientific output directory.

## Future single publication invocation

After separate authorization, use the same arguments and paths, replace `--preflight`
with `--publish --authorize-real-fit`, and invoke the command exactly once. Do not run
a fitting validation pass first. If it fails, preserve its external diagnostic log,
do not restart automatically, and investigate without changing the frozen scientific
or operational contracts.

The output consists of aggregate development results, input and fit reconciliation,
final-model configuration summaries without coefficients, timing, environment,
quality, scientific interpretation, and a deterministic scientific-content
manifest. It remains
a patch-26.15 rolling-origin development estimate—not final test performance,
recommendation reliability, or a causal counterfactual claim. A future sealed
temporal holdout remains untouched.
