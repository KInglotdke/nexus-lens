# Stage 3.4A pre-26.16 experimental freeze

This freeze was calculated entirely from the retained `26.14 -> 26.15`
development fold. No 26.16 observation was available or inspected. EUNE and EUW
remain separate throughout.

## Scientific contract

The primary prospective comparison is composition-only versus fixed 0.5 using one
paired log-loss difference per eligible match. Log loss is primary; Brier score is
secondary. Calibration and accuracy are descriptive secondary metrics. At an exact
probability of 0.5, the implementation's `>= 0.5` convention predicts outcome 1 for
the oriented allied team; accuracy is not used for selection or sample sizing.

Every policy is scored on the same platform-specific role-complete match subset.
One match creates exactly one observation. Queue 420 is mandatory. Vocabulary,
regularization selection, preprocessing, and fitting use training data only. A team
swap negates the full modeled feature vector and complements probability. The
composition-plus-direct-matchup comparison is exploratory.

Team IDs are metadata only. There is no signed side coefficient, so the model cannot
represent a training-only blue/team-100 advantage. Such a signed feature could
preserve swap complementarity, but it was not added: the frozen estimand is
predictive draft-only improvement over fixed 0.5, not a causal or side-adjusted
composition effect. This explicit limitation is non-blocking for that narrow
contract and blocks broader interpretation.

## Prospective power analysis

For each comparison, the analysis forms `model loss - comparator loss`; a negative
mean favors the first named model. It centers the observed paired differences, adds
each prespecified true improvement, and uses a deterministic 10,000-replicate
empirical-residual bootstrap with a two-sided paired Student t test at alpha 0.05.
Counts are eligible matches. Accepted-match counts divide by the observed platform
eligibility rate (EUNE 0.927007; EUW 0.935335), then apply a separate 10% operational
reserve. The bootstrap result is prospective planning evidence, not a promise that
future variance is unchanged.

### Composition-only versus fixed 0.5 (primary)

| Platform | True improvement | Power | Eligible | Eligibility-inflated accepted | With 10% reserve |
| --- | ---: | ---: | ---: | ---: | ---: |
| EUNE | 0.0025 | 80% | 6,116 | 6,598 | 7,258 |
| EUNE | 0.0025 | 90% | 8,179 | 8,824 | 9,707 |
| EUNE | 0.0050 | 80% | 1,539 | 1,661 | 1,828 |
| EUNE | 0.0050 | 90% | 2,032 | 2,192 | 2,412 |
| EUNE | 0.0100 | 80% | 388 | 419 | 461 |
| EUNE | 0.0100 | 90% | 518 | 559 | 615 |
| EUW | 0.0025 | 80% | 73 | 79 | 87 |
| EUW | 0.0025 | 90% | 96 | 103 | 114 |
| EUW | 0.0050 | 80% | 20 | 22 | 25 |
| EUW | 0.0050 | 90% | 27 | 29 | 32 |
| EUW | 0.0100 | 80% | 7 | 8 | 9 |
| EUW | 0.0100 | 90% | 9 | 10 | 11 |

The paired development sample standard deviation was 0.070016 for EUNE and
0.007508 for EUW. The large difference is why the requirements differ; the
platforms must not be pooled or assigned a common variance.

### Matchup augmentation versus composition-only (exploratory)

| Platform | True improvement | Power | Eligible | Eligibility-inflated accepted | With 10% reserve |
| --- | ---: | ---: | ---: | ---: | ---: |
| EUNE | 0.0025 | 80% / 90% | 116 / 157 | 126 / 170 | 139 / 188 |
| EUNE | 0.0050 | 80% / 90% | 31 / 41 | 34 / 45 | 38 / 50 |
| EUNE | 0.0100 | 80% / 90% | 10 / 13 | 11 / 15 | 13 / 17 |
| EUW | 0.0025 | 80% / 90% | 4 / 4 | 5 / 5 | 6 / 6 |
| EUW | 0.0050 | 80% / 90% | 3 / 3 | 4 / 4 | 5 / 5 |
| EUW | 0.0100 | 80% / 90% | 3 / 3 | 4 / 4 | 5 / 5 |

These small EUW exploratory counts reflect the unusually small development-fold
paired variance and do not make direct-matchup features primary or reliable.

### Expected 95% confidence-interval full widths

| Comparison/platform | 1,000 | 2,500 | 5,000 | 10,000 eligible |
| --- | ---: | ---: | ---: | ---: |
| Primary, EUNE | 0.008690 | 0.005492 | 0.003882 | 0.002745 |
| Primary, EUW | 0.000932 | 0.000589 | 0.000416 | 0.000294 |
| Exploratory, EUNE | 0.001178 | 0.000745 | 0.000526 | 0.000372 |
| Exploratory, EUW | 0.000118 | 0.000074 | 0.000053 | 0.000037 |

No performance-based early stopping is permitted during 26.16.

## Training adequacy

Only 381 eligible EUNE and 405 eligible EUW patch-26.15 drafts are currently
available. Their champion-role vocabularies contain 373 and 397 coefficients,
respectively: about 0.98 primary coefficients per eligible match. The exploratory
composition-plus-matchup vocabularies contain 1,793 and 1,936 coefficients, or 4.71
and 4.78 per match. EUNE/EUW champion-role frequency buckets (`1`, `2-4`, `5-9`,
`10-19`, `20+`) are `90/82/58/81/62` and `105/78/62/82/70`.

In the development direction, unseen champion-role slots were 2.44% EUNE and 1.75%
EUW, affecting 21.00% and 16.30% of matches. Direct matchup features were unseen in
52.55% and 52.94% of lanes and in 97.38% and 98.27% of matches. At 75% of the 26.14
training set, primary coefficient cosine similarity to the full model was only
0.884 EUNE and 0.870 EUW; prediction mean absolute differences were 0.0147 and
0.0020. Learning-curve log loss was non-monotonic, so it does not justify a future
performance projection.

The planning target for primary-model training is therefore 5,000 accepted
patch-26.15 matches per platform, evaluated again with outcome-blind feature-density
and stability diagnostics before fitting. Relative to the currently available
patch-26.15 totals, that means 4,589 additional EUNE and 4,567 additional EUW
accepted matches. This is a transparent heuristic: at current eligibility and
vocabulary size it supplies roughly 12 eligible matches per current primary
coefficient. It does not establish adequacy for the exploratory matchup model.

Increasing only 26.16 evaluation cannot change the 26.15 vocabulary, coefficient
estimates, or unseen-feature rate; it only narrows evaluation uncertainty. For the
untouched 26.16 primary evaluation, use fixed operational targets of 10,000 accepted
EUNE matches (rounding the conservative 9,707 requirement) and 1,000 accepted EUW
matches (a prespecified floor above the fragile variance-derived 114). Use the same
10,000 EUNE / 1,000 EUW targets for a later untouched 26.17 replication unless a
new prospective plan is frozen before observing 26.17 outcomes.

## Frozen execution settings

The machine-readable manifests contain the exact argv tokens. Both platforms use
training patch 26.15, evaluation patch 26.16, seed 34001, 10 calibration bins, 200
metric-bootstrap replicates with seed 34101, 500 L-BFGS-B iterations, and tolerance
`1e-9`. EUNE freezes both L2 values at 0.1; EUW freezes both at 1.0.

Artifacts:

- `config/evaluation/stage3.4a-pre-26.16-v1/eune.freeze.json`;
- `config/evaluation/stage3.4a-pre-26.16-v1/euw.freeze.json`;
- `config/evaluation/stage3.4a-pre-26.16-v1/prospective_sample_size.json`;
- `config/evaluation/stage3.4a-pre-26.16-v1.dependency-lock.txt`.

## Storage preflight

The proposed root is `E:\NexusLens\data` on the external ADATA SC610. The current
NTFS volume has 1,871,574,839,296 bytes free. Because USB drive letters can be
reassigned, automation should use its volume-GUID path
`\\?\Volume{89516a36-4750-44a8-a9bc-5784b90dfa7b}\NexusLens\data` or first assign a
persistent letter.

At 10,000 accepted matches per platform the existing audit projects 4,439,777,020
bytes permanent for EUNE and 4,587,391,580 for EUW. Temporary derived-publication
peaks are 2,884,256,510 and 3,007,777,520 bytes. Its conservative combined
additional-headroom estimate is 15,418,134,347 bytes, far below current free space.
Raw, checkpoint, and processed roots therefore fit safely when kept in separate
platform subtrees.

No migration was performed. A later authorized migration should copy (never move
first), hash every source and destination file, compare file counts/bytes/tree
manifests, exercise checkpoint resume and validate-only processing from the new
roots, retain the source until independent verification, then change configuration
paths in a separate reviewed operation.
