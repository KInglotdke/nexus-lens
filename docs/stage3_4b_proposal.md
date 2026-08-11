# Frozen Stage 3.4B-1 development protocol

Status: **prospectively frozen before any Stage 3.4B-1 fit on real data**.

The machine-readable contract is
`config/evaluation/stage3.4b-1-patch26.15-protocol-v1/protocol.json`. Stage
3.4B-1 is limited to the 9,414 already eligible patch-26.15 Ranked Solo/Duo
drafts. It introduces no player-history features, new collection, recommendation
policy, causal claim, or SHAP analysis.

Current planning uses the generic term **future sealed temporal holdout**. Older
Stage 3.4A freezes and published result artifacts may retain the numbered wording
that was historically frozen with them; those immutable files are intentionally not
rewritten.

## Target and interpretation

The factual target remains `P(Blue/lower-numeric team wins)` for one completed,
role-complete draft. A later candidate-champion contrast may substitute one legal
pick and rescore the otherwise fixed draft, but that contrast is mechanical and
non-causal. An unselected champion has no observed historical outcome.

## Rolling-origin design

UTC game-creation timestamps establish four adjacent outer test blocks:

| Block | Test interval `[start, end)` | EUNE | EUW | Total |
|---|---|---:|---:|---:|
| outer-0 | 2026-08-03 to 2026-08-05 | 669 | 616 | 1,285 |
| outer-1 | 2026-08-05 to 2026-08-07 | 954 | 776 | 1,730 |
| outer-2 | 2026-08-07 to 2026-08-09 | 982 | 872 | 1,854 |
| outer-3 | 2026-08-09 to 2026-08-12 | 629 | 870 | 1,499 |

The 3,046 drafts before the first boundary form initial unscored training data;
6,368 drafts are scored exactly once. Each later training partition expands to
include all earlier observations. Every fold requires at least 800 preceding drafts,
300 from each platform, and 48 hours of preceding time. EUNE and EUW use identical
boundaries without downsampling. Platform is subgroup and bootstrap-stratum metadata,
never a predictive feature.

Each selection context uses three one-day inner validation intervals immediately
before its cutoff, with all earlier observations as expanding training. Final
configuration selection uses a 2026-08-11 UTC cutoff; the final selected model is then
fit to all 9,414 development drafts. Identical timestamps remain in one partition.
Vocabularies, rates, support decisions, and model selection are fold-local. No random
cross-validation is used.

## Baselines

All policies score the identical outer rows. The required baselines are constant
0.5, the training-fold Blue rate, the two frozen Stage 3.4A specifications refit in
each outer training fold, and a fold-local champion-role-rate baseline. The latter
keeps champion-role rates with at least ten training matches, falls back to the
training Blue rate, and uses `Blue rate + (mean Blue champion rate - mean Red
champion rate) / 2`.

The Stage 3.4A baselines use L2 0.1 and no intercept. Their previously published
metric values are not reused because those values were produced on different folds.

## Candidate equations and sharing

Let `x_B - x_R` be signed champion-role indicators and `b` the unregularized
Blue-side intercept.

1. Composition with side intercept:
   `s = b + w·(x_B - x_R)`. One champion-role coefficient is reused in every
   draft and both orientations.
2. Shared allied synergy:
   `s = b + w·(x_B - x_R) + Σ(i<j)<u_Bi,u_Bj> -
   Σ(i<j)<u_Ri,u_Rj>`. A small champion-role embedding is reused across all
   allied role pairs.
3. Shared lane counter:
   `s = b + w·(x_B - x_R) + Σr(<a_Br,d_Rr> - <a_Rr,d_Br>)`.
   Attack and defense embeddings pool every same-role counter interaction; reversing
   the lane reverses its contrast.
4. Combined: the sum of the composition, allied-synergy, and lane-counter terms
   above under one fixed ablation grid.

For all candidates, `P(Blue wins) = sigmoid(s)`. The objective is mean binary
logistic loss plus `0.5 * main_l2 * ||w||²` and, where present,
`0.5 * embedding_l2` times the squared embedding norms. The side intercept is not
regularized. Swapping teams negates every draft-dependent term, so
`score(draft) + score(swapped) = 2b`.

With 710 reference champion-role features, parameter counts are 711 for composition;
2,131/3,551 for synergy dimensions 2/4; 3,551/6,391 for counter dimensions 2/4;
and 4,971/9,231 for combined dimensions 2/4.

## Frozen grids and numerical policy

- Composition: main L2 `{0.03, 0.1, 0.3}`.
- Shared synergy: `(dimension, embedding L2)` `{(2, 1), (4, 3)}`, main L2 0.1.
- Shared counter: `(dimension, embedding L2)` `{(2, 1), (4, 3)}`, main L2 0.1.
- Combined: both latent dimensions 2 with embedding L2 1, or both dimensions 4
  with embedding L2 3; main L2 0.1.

Only champion-role features with at least ten training matches receive embeddings.
Unsupported embeddings are zero; unseen linear and exact-pair features contribute
zero. L-BFGS-B uses an analytic gradient, at most 300 iterations, tolerance `1e-8`,
and deterministic centered-normal embedding initialization at scale 0.01 from seed
34401. Any non-convergence or non-finite value fails closed.

Selection minimizes the unweighted mean of three inner-fold log losses. Ties within
`1e-12` choose, in order, larger embedding L2, larger main L2, smaller total latent
dimension, then lexicographically smaller configuration ID. Calibration intercept
and slope are unregularized metric-only regressions; predictions are not recalibrated.
ECE uses ten fixed equal-width bins.

## Paired uncertainty and publication

The primary comparison is paired per-match log-loss difference. Brier score,
calibration intercept/slope, ECE, prediction dispersion, coverage, platform results,
and outer-block results are also retained. Two thousand match-level bootstrap
replicates use seed 34201 and resample within platform × outer-block strata. Every
candidate-versus-every-baseline log-loss and Brier interval is stored in aggregate.
Bootstrap performs zero model fits.

OOF rows may exist only ephemerally. Publication is aggregate-only and excludes match
IDs, player identifiers, external paths, and low-support named slices. Named summaries
require support of at least 100.

The fixed budget is 171 predictive training operations: exactly 163 optimizer fits
and eight analytic fold-local baseline estimates. Metric calculation makes 63
calibration evaluations. Constant-prediction scopes are resolved analytically, so
these add at most 63 optimizer fits and the complete run has at most 226 optimizer
invocations. Bootstrap adds none. The pre-run estimate is 15–35 CPU minutes and at
most 1 GiB peak memory.

## Material-usefulness gate

A candidate must pass every frozen gate: paired log-loss improvements of at least
0.002 versus both the Blue-rate and composition baselines with both 95% interval upper
bounds below zero; Brier improvements of at least 0.001 versus both; at least 0.001
log-loss improvement versus composition on each platform with neither platform worse
than the Blue-rate baseline; ECE at most 0.02; calibration slope 0.8–1.2; absolute
calibration intercept at most 0.02; at least 95% coverage; no role coverage drop over
five percentage points; improvement in at least two outer blocks; and all leakage,
determinism, privacy, convergence, and finite-value gates.

A future sealed temporal holdout may be evaluated only after one prospectively frozen
Stage 3.4B candidate passes every material-usefulness, leakage, determinism, privacy,
convergence and coverage gate.
