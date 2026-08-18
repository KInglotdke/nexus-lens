# Stage 3.5B frozen rolling-origin development protocol

Stage 3.5B is an offline, patch-26.15 development experiment for generic top-lane
champion matchup and focal-keystone prediction. It models focal-minus-opposing-top
gold and XP at 10 minutes separately as co-primary outcomes. It does not create a
subjective lane score, access a future holdout, or authorize a recommendation.

## Sealed inputs and temporal boundary

The immutable inputs are the 20,000 Stage 3.5A focal rows and their aligned rune
addendum. Each match group retains two perspectives with weight 0.5 each. The first
3,185 chronological match groups are initial training data. Four rolling-origin
development blocks contain 1,342, 1,844, 1,980, and 999 groups. Training expands
strictly forward and both platforms occur in every training, inner-validation, and
outer-validation partition.

The 650 groups at or after `2026-08-10T00:00:00Z` form the frozen final holdout.
The Stage 3.5B adapter identifies them by timestamp and does not project their
features or targets into the development table. No model selection, scoring,
metric, bootstrap, or publication consumes those rows. Evaluation of that holdout
requires separate authorization.

## Candidate and feature contract

The fixed continuous-outcome ladder is:

1. Training-fold global mean.
2. Training-fold focal-champion mean with support-10 global fallback.
3. Additive focal-champion, enemy-champion, and side Ridge.
4. Ridge plus a directional champion matchup.
5. Matchup Ridge plus focal keystone.
6. Matchup/keystone Ridge plus focal-champion by keystone interaction.
7. Elastic Net counterparts for each of the four linear scopes.
8. A native-categorical ordered CatBoost challenger using focal champion, enemy
   champion, side, and focal keystone.

Interactions retain their main effects. One-hot linear models ignore an unseen
category so unsupported interactions contribute zero while lower-level features
remain. Keystone categories with fewer than 20 training rows use a training-only
rare fallback. CatBoost receives raw categorical values and never receives manual
target encoding.

The only predictive concepts are focal champion, enemy top champion, their
directional pair, focal keystone, focal champion by focal keystone, and side.
Platform is a reporting stratum, not a predictor. Rank, focal history, opponent
account information, opponent runes, minor runes, intervention signals, match
identifiers, and every post-selection event or target are forbidden at schema and
runtime boundaries.

## Frozen optimization and operation budget

Ridge alphas are 1 and 10. Elastic Net configurations are `(alpha=0.01,
l1_ratio=0.1)` and `(alpha=0.1, l1_ratio=0.25)`, with 5,000 iterations and tolerance
`1e-4`. CatBoost configurations use depth 4/6, 150/200 iterations, learning rate
0.05/0.04, L2 3/5, ordered boosting, Bayesian bootstrap, one CPU thread, and seed
35501. Early stopping is disabled.

The execution environment is frozen to Python 3.12, NumPy 2.5.1, SciPy 1.18.0,
scikit-learn 1.9.0, and CatBoost 1.2.10. Preflight rejects a version mismatch.

Within each outer training period, the last 24 hours form an inner chronological
validation period. Each candidate configuration is scored by mean normalized MAE
across the two co-primary targets. Lowest score wins; ties within `1e-12` use the
lexicographically smallest configuration ID. That outer-fold configuration is then
reused for all secondary continuous endpoints.

The exact budget is 828 training operations: 128 analytic estimates and 700
optimizer fits. The 154 paired comparison evaluations resample existing OOF
predictions for 2,000 deterministic match-group bootstrap replicates and perform
zero model fits. Failures are fail-closed; retries are forbidden.

## Metrics and gates

Continuous metrics are match-weighted MAE, RMSE, R-squared, sign accuracy, and
coverage, overall and by platform, chronological block, side, and training-only
focal-champion support tier. Secondary endpoints include 5/15-minute gold and XP,
5/10/15-minute lane-CS, total-farm and level differences, tower outcome at 15
minutes, and final game win. Tower and win use separate classification pipelines
and cannot rescue a co-primary failure.

Paired MAE differences are complex minus simpler, so negative favours complexity.
The matchup, keystone, champion-keystone, and CatBoost gates each require both
co-primary point estimates and 95% interval upper bounds below zero, favourable
directions on EUNE and EUW, at least three favourable chronological blocks, and
complete prediction coverage. Passing only creates a hypothesis for separately
authorized final-holdout evaluation.

Rune choice remains observational. The strongest permitted wording is
"rune-conditioned expected lane performance"; the experiment cannot identify a
causal benefit from switching runes.

## Commands and privacy

The checked-in execution configuration is a non-executable path template. A private
configuration supplies the sealed paths. Preflight writes nothing and performs no
fit:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe scripts\run_stage35b.py `
  --config data\stage35b\private\production-config.json `
  --protocol config\evaluation\stage3.5b-patch26.15-protocol-v1\protocol.json `
  --schema config\evaluation\stage3.5b-patch26.15-protocol-v1\protocol.schema.json `
  --repository-commit <PROTOCOL_FREEZE_COMMIT> --preflight
```

Real execution additionally requires `--publish --authorize-real-fit` and a new
diagnostic JSONL outside the repository, sealed inputs, private OOF directory, and
scientific result directory. Row-level OOF predictions remain private. Public files
contain only aggregate metrics, comparisons, gates, operation counts, and path-free
fingerprints; they contain no match/player identifier, external path, prediction
row, or coefficient.

## Frozen development checkpoint

The one authorized development execution completed from protocol commit
`862151531d71ed1ca9c05f9ac5852dc6903c3624`. It reconciled 128 analytic
operations and 700 optimizer fits, with no retry, failure, or bootstrap fit. All
four usefulness gates failed, so only the additive Ridge structure remains
eligible for a separately authorized final-holdout evaluation. No holdout score or
product recommendation was produced. The aggregate checkpoint and interpretation
are documented in [stage3_5b_results.md](stage3_5b_results.md).
