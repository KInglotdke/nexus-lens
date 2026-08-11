# Prospective Stage 3.4B development proposal

Status: **design proposal only; not implemented, fitted, tuned, or frozen**.

Patch 26.16 remains untouched. This proposal must become a versioned protocol before
any Stage 3.4B model is fitted or any new result is inspected.

## Target and decision context

The factual prediction target remains `P(Blue/lower-numeric team wins)` at completed
draft time. The recommendation-facing quantity is a conditional candidate contrast:
the change in predicted win probability when one not-yet-filled role is assigned a
legal candidate champion while observed allied picks, enemy picks, bans, role, patch,
and leakage-safe pre-match player state remain fixed.

That contrast is a model output, not an identified causal treatment effect. Stage
3.4B may evaluate predictive usefulness and ranking stability, but it must not claim
that an unchosen champion would have changed the historical result.

## Candidate feature families

Available from already collected data without new Riot collection:

- an intercept-only Blue-rate baseline and the frozen signed champion-role features;
- allied champion-role pair synergies within each team;
- same-role lane-counter pairs with shared or hierarchical shrinkage rather than one
  unrelated coefficient per exact pair;
- fold-local champion-role strength estimates;
- bans and the set of already selected champions, without reconstructed pick order;
- platform as stratification/subgroup metadata, not automatically as a predictor;
- public patch and canonical game creation time for prospectively defined recency
  weighting, after a sealed lineage join to existing canonical inputs.

Features requiring prospective pre-match player-history collection or a new sealed
history join:

- champion proficiency using only that player's matches strictly before the target
  match creation time;
- role familiarity and recent role frequency, also strictly historical;
- player-specific champion-role sample size and recency;
- prior ally familiarity or duo history when support and privacy thresholds permit;
- independently observed rank/MMR proxies. Existing rank lineage is too sparse to
  support this family.

Items, gold, kills, assists, damage, objectives, post-game ranks, timelines, and any
other information unavailable at recommendation time are prohibited.

## Models and baselines

Required baselines:

1. constant `0.5`;
2. training-fold Blue win-rate intercept;
3. frozen composition-only logistic form;
4. frozen composition-plus-direct-matchup form;
5. fold-local champion-role-rate baseline.

Candidate families should be introduced in ablation order:

1. regularized logistic regression with ally-synergy interactions;
2. logistic regression with hierarchical champion-role and lane-pair effects that
   share role-level and champion-level information;
3. a low-rank factorization-machine interaction model;
4. one bounded interaction-capable tree/boosting model, only with deterministic
   seeds, strict capacity limits, and post-hoc calibration fitted inside training
   folds.

Each family receives a fixed prospective hyperparameter grid. Complexity is retained
only when it beats every required baseline under the criteria below.

## Leakage controls and validation

- Split and score one match once; never mirror rows.
- Use deterministic rolling-origin outer folds within patch 26.15, stratified by
  platform and target where compatible with chronology.
- Build vocabularies, histories, encodings, smoothing parameters, calibration, and
  hyperparameters inside each outer training partition only.
- For player-history features, require `history_game_creation < evaluated_game_creation`.
- Purge overlapping history windows around fold boundaries when the same player can
  occur on both sides of a temporal split.
- Keep platform-specific subgroup reports and add a leave-one-platform-out stress
  test; do not silently fit platform as a feature.
- Freeze all seeds, grids, support thresholds, recency windows, missing-value rules,
  and tie-breaks before fitting.
- Preserve aggregate outputs only. OOF rows may be held ephemerally for metrics but
  must not be published.

Nested rolling-origin validation remains the default: outer folds estimate
development performance, inner chronological folds select hyperparameters. If
player-history purging makes inner folds too small, use a single prospectively fixed
configuration and repeated blocked outer evaluation rather than weakening temporal
separation.

Primary metric is paired per-match log-loss difference. Secondary metrics are Brier
score, calibration intercept/slope, ECE, coverage, abstention, and prediction
dispersion. Accuracy and ROC AUC are descriptive only. Report overall, EUNE, EUW,
role, and prospectively support-binned results; suppress low-support slices.

## Candidate-ranking evaluation

Historical data reveal an outcome only for the chosen draft, not for alternative
champions. Therefore:

- factual win-prediction evaluation can test calibration and discrimination;
- masked-pick reconstruction can test whether a model ranks the observed legal pick
  plausibly, but measures pick behavior rather than win utility;
- counterfactual rank stability can be tested across folds, seeds, nearby time
  windows, and support perturbations without claiming correctness;
- matched or propensity-weighted observational comparisons may be reported only as
  sensitivity analyses because draft choice is confounded and logging propensities
  are unavailable;
- actual recommendation benefit ultimately requires a prospective randomized or
  carefully interleaved user study with consent and predeclared outcomes.

No offline metric may relabel an unobserved alternative as a win or loss.

## Predeclared material-usefulness gate

Before patch 26.16 can be accessed, one locked Stage 3.4B candidate must satisfy all
of the following on patch-26.15 outer-fold development predictions:

- overall paired log loss improves by at least `0.0020` versus both the Blue-rate
  intercept and frozen composition-only baseline;
- the paired 95% bootstrap upper bound for each of those log-loss differences is
  below `0`;
- overall Brier score improves by at least `0.0010` versus both baselines;
- EUNE and EUW each improve log loss by at least `0.0010` versus composition-only,
  with neither platform worse than the Blue-rate intercept;
- overall ECE is at most `0.020`, calibration slope is between `0.8` and `1.2`, and
  calibration intercept magnitude is at most `0.02`;
- candidate-model coverage is at least `95%` of role-complete drafts and no
  prospectively declared role loses more than five percentage points of coverage;
- the direction of improvement repeats in at least two chronological outer blocks;
- all leakage, determinism, privacy, convergence, and finite-value gates pass.

These thresholds are design decisions recorded before Stage 3.4B results. They may
be revised only by a new prospective protocol before fitting, never after observing
the candidate result.

Patch 26.16 may be used once, without retraining or tuning, only after the complete
Stage 3.4B protocol, feature implementation, fitted final model, support policy, and
evaluation command are sealed and the material-usefulness gate above is met. Failure
to meet the gate keeps patch 26.16 untouched and returns the project to development
design rather than temporal testing.
