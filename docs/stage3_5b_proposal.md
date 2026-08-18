# Prospective Stage 3.5B modelling proposal

This proposal is written before Stage 3.5B fitting. Stage 3.5A does not execute any
model in this document. Hyperparameter grids, minimum-support rules, usefulness
thresholds, and the temporal holdout must be frozen in a separately authorized
protocol before any result is inspected.

## Scientific questions and endpoints

The co-primary endpoints are focal-minus-opposing-top gold and XP at 10 minutes.
Separate primary regressions will also estimate gold and XP at 5 and 15 minutes.
Lane CS, total farm, and level differences are supporting outcomes. The 20/25-minute
trajectory and interval changes are exploratory because availability selects for
matches that survive long enough.

The primary tower task is a three-class 15-minute outcome:
`enemy_top_outer_first`, `neither_by_15`, and `allied_top_outer_first`. A later
survival analysis may use exact turret time with censoring. Final match win may be a
separate secondary classifier, but it cannot relabel lane trajectories or determine
the primary usefulness decision.

## Candidate ladder

For every continuous endpoint, compare the following prospective ladder on the
same rows and chronological folds:

1. Global training mean.
2. Training-only own-champion mean with frozen shrinkage/backoff.
3. Additive own-champion plus enemy-champion Ridge regression.
4. Regularized directional matchup-pair Ridge regression.
5. Matchup Ridge plus a regularized focal-keystone main effect.
6. Matchup Ridge plus a regularized focal-champion by focal-keystone interaction,
   only if its minimum-support and shrinkage policy is frozen prospectively.
7. Elastic Net versions where scientifically justified, using the same legal
   feature scopes and comparisons.
8. A CatBoost challenger using focal champion, enemy champion, focal keystone, and
   other already authorized champion-select features.
9. Existing secondary tower and game-win models, without allowing win to redefine
   lane success.

For the tower label compare a regularized multinomial logistic model and a CatBoost
classifier against training-only global-majority/probability baselines. A separate
secondary game-win classifier may use legal champion-select features and early-lane
targets only in a distinct post-lane translation analysis; early-lane outcomes are
not champion-select features for the matchup product.

Each generic matchup candidate must be paired with a focal-history-enhanced version
using exactly the same rows and folds. The history enhancement includes only
strictly earlier focal-player fields from Stage 3.5A. No opponent account field is
permitted. A later trajectory model may add timestamp and timestamp-interaction
terms using the repeated observations nested under match group. A neural network is
not a first-line candidate.

The outcome-blind rune audit found complete keystone coverage, but highly uneven
interaction support. A focal-keystone main effect is therefore a candidate;
champion-keystone interactions require regularization and fallback. The full focal
champion by enemy champion by keystone interaction is not a primary candidate:
7,518 observed directional groups have median support 1, maximum support 44, and no
group reaches 50 focal rows. If examined at all, it is exploratory and requires
hierarchical shrinkage or fallback.

The prospective rune fallback hierarchy is:

1. Supported matchup-specific rune evidence, if a later frozen protocol justifies
   it.
2. Champion-keystone effect plus champion-matchup effect.
3. General keystone effect plus additive champion effects.
4. Champion-only or global fallback.

The 10-minute gold and XP endpoints remain co-primary. A rune candidate must add
out-of-time value over the same matchup model without rune features. The final
holdout cannot be used for rune-feature selection.

## Evaluation contract to freeze

- Rolling chronological outer evaluation with training strictly before evaluation.
- Inner selection using only the outer training period.
- `match_group_id` preserved across every split; both perspectives remain together.
- Identical membership and ordering for paired candidates.
- Any player-history state reconstructed independently inside each temporal split,
  or verified to use only source matches earlier than the evaluated row.
- Paired uncertainty resampled at match-group level, never focal-row level.
- Platform, side, champion support, matchup support, and chronological-block audits.
- Explicit interaction-versus-additive comparison to isolate matchup information.
- No row filtering based on extreme valid outcomes, champion identity, final win, or
  a model's expectations.

Continuous metrics are weighted MAE, RMSE, coefficient of determination, and sign
accuracy, with the 0.5 row weights yielding one unit per match. Categorical metrics
are weighted log loss, multiclass Brier score, calibration, and class-conditional
coverage. Undefined metrics are null; NaN and infinity fail publication.

## Usefulness decision

No recommendation is authorized unless matchup interactions improve over additive
own/enemy champion effects on both 10-minute co-primary endpoints with stable paired
out-of-time uncertainty, acceptable calibration/error behavior, and no material
platform or chronological reversal. A history-enhanced policy must likewise improve
over the identical generic matchup model; sparse-history cohorts need explicit
coverage and uncertainty reporting.

Tower, late-trajectory, intervention, and game-win findings cannot rescue a failure
on the 10-minute lane endpoints. A successful development result still requires an
untouched future temporal test before recommendation use. Patch 26.15 estimates
remain development evidence and cannot be described as final performance.

## Deferred protocol decisions

Before Stage 3.5B execution, prospectively freeze:

- the future temporal boundary and minimum preceding-training policy;
- model encodings and regularization grids;
- CatBoost version, depth/learning-rate/iteration grids, loss, and deterministic
  settings;
- champion/matchup backoff and support rules;
- history decay and shrinkage use (without tuning on the evaluation period);
- paired interval method/replicate count and multiplicity treatment;
- numeric usefulness margins and platform/chronology gates;
- handling of recorded-intervention sensitivity cohorts and late-endpoint censoring.

Stage 3.5A aggregate feasibility results may inform whether these designs are
supportable, but no candidate result may be calculated before the protocol freeze.
Rune choice is observational and potentially influenced by opponent, playstyle,
familiarity, skill, intended strategy, and external advice. Initial Stage 3.5B
language must therefore remain "rune-conditioned expected lane performance" rather
than claim that switching a rune causes an outcome change.
