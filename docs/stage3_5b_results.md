# Stage 3.5B rolling-origin development checkpoint

This checkpoint records the single frozen patch-26.15 development execution from
protocol commit `862151531d71ed1ca9c05f9ac5852dc6903c3624`. The estimates are
development results, not final-holdout performance. No product, matchup, or rune
recommendation is authorized.

## Execution and lineage

- Development evaluation: 6,165 match groups and 12,330 paired perspective rows
  over four chronological blocks of 1,342, 1,844, 1,980, and 999 groups.
- Final holdout: 650 groups beginning `2026-08-10T00:00:00Z`; no holdout feature,
  target, score, or metric entered this execution.
- Operations: 128 analytic estimates, 700 optimizer fits, 828 total training
  operations, 154 paired comparisons with 2,000 bootstrap replicates each, and
  zero bootstrap fits. There were no failures or retries.
- Scientific bundle:
  `0d8ccb4512e8de0e4c6b50350d570eef6a3677899ba6eeb10efc109de27db010`.

The complete aggregate bundle is in
`config/evaluation/stage3.5b-patch26.15-protocol-v1/development-v1`. Row-level OOF
predictions remain private and outside version control.

## Co-primary development metrics

All rows had complete prediction coverage. MAE is the frozen comparison metric.

| Model | Gold 10 MAE | Gold RMSE | Gold R2 | Gold sign | XP 10 MAE | XP RMSE | XP R2 | XP sign |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Global mean | 797.281 | 985.138 | 0.000 | 0.000 | 743.745 | 945.893 | 0.000 | 0.001 |
| Focal-champion mean | 781.294 | 968.651 | 0.033 | 0.545 | 736.941 | 938.830 | 0.015 | 0.530 |
| Additive Ridge | 763.972 | 948.454 | 0.073 | 0.593 | 728.180 | 928.226 | 0.037 | 0.568 |
| Matchup Ridge | 762.613 | 948.254 | 0.073 | 0.595 | 727.946 | 928.000 | 0.037 | 0.571 |
| Matchup + keystone Ridge | 761.876 | 947.434 | 0.075 | 0.595 | 727.657 | 927.682 | 0.038 | 0.571 |
| Champion-keystone Ridge | 761.806 | 947.868 | 0.074 | 0.597 | 727.675 | 928.115 | 0.037 | 0.569 |
| Additive Elastic Net | 768.950 | 952.793 | 0.065 | 0.594 | 729.382 | 930.001 | 0.033 | 0.565 |
| Matchup Elastic Net | 768.028 | 951.905 | 0.066 | 0.594 | 728.656 | 929.249 | 0.035 | 0.571 |
| Matchup + keystone Elastic Net | 766.513 | 950.233 | 0.070 | 0.595 | 727.919 | 928.455 | 0.037 | 0.573 |
| Champion-keystone Elastic Net | 765.525 | 949.455 | 0.071 | 0.595 | 727.516 | 928.007 | 0.037 | 0.572 |
| CatBoost | 767.670 | 953.726 | 0.063 | 0.591 | 730.614 | 931.308 | 0.031 | 0.566 |

The exact 154 paired-bootstrap records, including confidence intervals, relative
effects, platform effects, and chronological-block effects, are published in
`paired_comparisons.json`. Negative MAE differences favour the more complex model.

## Secondary trajectory and classification

Secondary outcomes did not participate in or rescue a co-primary gate. The table
shows the lowest observed development MAE among the frozen candidates, solely as a
descriptive summary.

| Outcome | Lowest-MAE model | MAE | RMSE | R2 | Sign accuracy |
|---|---|---:|---:|---:|---:|
| Gold difference, 5 min | Matchup + keystone Ridge | 356.406 | 451.279 | 0.078 | 0.606 |
| Gold difference, 15 min | Matchup + keystone Ridge | 1,396.090 | 1,733.612 | 0.060 | 0.586 |
| XP difference, 5 min | Matchup + keystone Elastic Net | 298.590 | 389.021 | 0.038 | 0.554 |
| XP difference, 15 min | Matchup Ridge | 1,507.132 | 1,867.991 | 0.027 | 0.563 |
| Lane-minion CS difference, 5 min | Matchup + keystone Ridge | 7.546 | 9.475 | 0.159 | 0.628 |
| Lane-minion CS difference, 10 min | Champion-keystone Ridge | 15.332 | 19.375 | 0.147 | 0.629 |
| Lane-minion CS difference, 15 min | Champion-keystone Ridge | 23.737 | 29.656 | 0.126 | 0.615 |
| Total-farm difference, 5 min | Matchup + keystone Ridge | 7.545 | 9.472 | 0.159 | 0.629 |
| Total-farm difference, 10 min | Champion-keystone Ridge | 15.383 | 19.433 | 0.148 | 0.630 |
| Total-farm difference, 15 min | Champion-keystone Ridge | 23.949 | 29.920 | 0.128 | 0.615 |
| Level difference, 5 min | Global mean | 0.344 | 0.650 | 0.000 | 0.693 |
| Level difference, 10 min | Global mean | 0.776 | 1.058 | 0.000 | 0.372 |
| Level difference, 15 min | Global mean | 1.191 | 1.515 | 0.000 | 0.229 |

For the three-class 15-minute tower endpoint, CatBoost achieved log loss 0.998570,
Brier score 0.600823, and ECE 0.011774; regularized logistic achieved 1.009657,
0.608790, and 0.061401. For final win, the global probability baseline remained
best (log loss 0.693147, Brier 0.500000, ECE 0.000000); CatBoost yielded 0.693500,
0.500351, and 0.009061, while regularized logistic yielded 0.712813, 0.518215, and
0.070811. Coverage was 1.0 throughout.

## Mechanical gate decision

The matchup, keystone-main-effect, champion-keystone interaction, and CatBoost
gates all failed. Although some point estimates were slightly favourable, the
required joint conditions across both co-primary outcomes, paired 95% intervals,
both platforms, and chronological blocks were not met. The additive Ridge model is
therefore the only structure eligible for a separately authorized final-holdout
evaluation. Rune effects remain observational, rune-conditioned expected lane
performance and must not be interpreted as causal switching effects.
