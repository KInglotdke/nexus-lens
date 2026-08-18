# Nexus Lens Stage 3.5B rolling-origin development results

These are patch-26.15 development estimates, not final-holdout performance.
No product, matchup, or rune recommendation is authorized.

## Co-primary MAE

| Model | Gold 10 | XP 10 |
|---|---:|---:|
| global_mean | 797.280616 | 743.745499 |
| focal_champion_mean | 781.293607 | 736.940852 |
| additive_ridge | 763.972191 | 728.179721 |
| matchup_ridge | 762.612853 | 727.945790 |
| matchup_keystone_ridge | 761.875619 | 727.656943 |
| champion_keystone_ridge | 761.805527 | 727.675101 |
| additive_elastic_net | 768.950025 | 729.381731 |
| matchup_elastic_net | 768.028394 | 728.656272 |
| matchup_keystone_elastic_net | 766.513333 | 727.919269 |
| champion_keystone_elastic_net | 765.525395 | 727.516175 |
| catboost | 767.669548 | 730.613612 |

## Frozen gates

- Matchup gate: `False`
- Keystone gate: `False`
- Champion-keystone gate: `False`
- CatBoost gate: `False`
- Product recommendation authorized: `False`

## Operation reconciliation

- Analytic operations: `128`
- Optimizer fits: `700`
- Bootstrap model fits: `0`
- Retries: `0`

Rune results, if any, are observational rune-conditioned expected lane performance and are not causal switching effects.
