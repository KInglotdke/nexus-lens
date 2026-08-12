# Nexus Lens Stage 3.4B-1 patch-26.15 development report

Status: **rolling-origin development estimate, not final test performance**.

- Eligible drafts: `9414`
- Outer evaluation drafts: `6368`
- Predictive optimizer fits: `163`
- Candidate contrasts: mechanical and non-causal.
- Recommendation reliability authorized: `False`.

## Overall development metrics

| Policy | Log loss | Brier | Calibration intercept | Calibration slope | ECE | Dispersion | Coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `constant_0_5` | 0.69314718 | 0.25000000 | 0.02387048 | null | 0.00596734 | 0.00000000 | 1.00000000 |
| `training_fold_blue_win_rate_intercept` | 0.69317285 | 0.25001283 | 0.04779292 | 2.99782896 | 0.00796206 | 0.00173018 | 1.00000000 |
| `stage3_4a_composition_only_l2_0_1_no_intercept` | 0.69269034 | 0.24977164 | 0.02249867 | 1.12390558 | 0.00565763 | 0.01342375 | 1.00000000 |
| `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | 0.69270546 | 0.24977922 | 0.02251424 | 1.07480109 | 0.00564744 | 0.01374390 | 1.00000000 |
| `fold_local_champion_role_rate_minimum_support_10` | 0.69288978 | 0.24987108 | 0.02952490 | 0.81153713 | 0.00770268 | 0.01563370 | 1.00000000 |
| `composition_with_side_intercept` | 0.69371606 | 0.25028070 | 0.02617843 | 0.32194801 | 0.00944629 | 0.02695524 | 1.00000000 |
| `shared_allied_synergy` | 0.69271622 | 0.24978455 | 0.03278486 | 1.16318083 | 0.00787694 | 0.01350685 | 1.00000000 |
| `shared_lane_counter` | 0.69271681 | 0.24978485 | 0.03272969 | 1.16160117 | 0.00786760 | 0.01351111 | 1.00000000 |
| `combined_shared_interactions` | 0.69271739 | 0.24978514 | 0.03282047 | 1.16238140 | 0.00788583 | 0.01350313 | 1.00000000 |

## Fit reconciliation

- Predictive training operations: `171`
- Predictive optimizer fits: `163`
- Analytic baseline estimates: `8`
- Calibration evaluations: `63`
- Calibration optimizer fits: `52`
- Bootstrap model fits: `0`
- All frozen counts and statuses reconciled: `True`

## Paired candidate comparisons

Differences are candidate minus comparator; negative loss differences indicate improvement.

| Candidate | Comparator | Metric | Point | 2.5% | 97.5% | Replicates | Seed |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| `composition_with_side_intercept` | `constant_0_5` | `log_loss` | 0.00056888 | -0.00078999 | 0.00186670 | 2000 | 34201 |
| `composition_with_side_intercept` | `constant_0_5` | `brier_score` | 0.00028070 | -0.00039447 | 0.00092607 | 2000 | 34201 |
| `composition_with_side_intercept` | `training_fold_blue_win_rate_intercept` | `log_loss` | 0.00054321 | -0.00084046 | 0.00183128 | 2000 | 34201 |
| `composition_with_side_intercept` | `training_fold_blue_win_rate_intercept` | `brier_score` | 0.00026786 | -0.00042030 | 0.00090832 | 2000 | 34201 |
| `composition_with_side_intercept` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `log_loss` | 0.00102572 | 0.00020777 | 0.00184268 | 2000 | 34201 |
| `composition_with_side_intercept` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `brier_score` | 0.00050905 | 0.00010311 | 0.00091333 | 2000 | 34201 |
| `composition_with_side_intercept` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `log_loss` | 0.00101060 | 0.00019432 | 0.00183323 | 2000 | 34201 |
| `composition_with_side_intercept` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `brier_score` | 0.00050148 | 0.00009717 | 0.00090856 | 2000 | 34201 |
| `composition_with_side_intercept` | `fold_local_champion_role_rate_minimum_support_10` | `log_loss` | 0.00082628 | -0.00011781 | 0.00182787 | 2000 | 34201 |
| `composition_with_side_intercept` | `fold_local_champion_role_rate_minimum_support_10` | `brier_score` | 0.00040961 | -0.00005831 | 0.00090560 | 2000 | 34201 |
| `shared_allied_synergy` | `constant_0_5` | `log_loss` | -0.00043096 | -0.00109450 | 0.00023352 | 2000 | 34201 |
| `shared_allied_synergy` | `constant_0_5` | `brier_score` | -0.00021545 | -0.00054693 | 0.00011648 | 2000 | 34201 |
| `shared_allied_synergy` | `training_fold_blue_win_rate_intercept` | `log_loss` | -0.00045663 | -0.00112892 | 0.00019703 | 2000 | 34201 |
| `shared_allied_synergy` | `training_fold_blue_win_rate_intercept` | `brier_score` | -0.00022828 | -0.00056441 | 0.00009808 | 2000 | 34201 |
| `shared_allied_synergy` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `log_loss` | 0.00002588 | -0.00011083 | 0.00016588 | 2000 | 34201 |
| `shared_allied_synergy` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `brier_score` | 0.00001291 | -0.00005535 | 0.00008282 | 2000 | 34201 |
| `shared_allied_synergy` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `log_loss` | 0.00001075 | -0.00014643 | 0.00016968 | 2000 | 34201 |
| `shared_allied_synergy` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `brier_score` | 0.00000533 | -0.00007320 | 0.00008457 | 2000 | 34201 |
| `shared_allied_synergy` | `fold_local_champion_role_rate_minimum_support_10` | `log_loss` | -0.00017356 | -0.00066092 | 0.00031587 | 2000 | 34201 |
| `shared_allied_synergy` | `fold_local_champion_role_rate_minimum_support_10` | `brier_score` | -0.00008653 | -0.00033006 | 0.00015800 | 2000 | 34201 |
| `shared_lane_counter` | `constant_0_5` | `log_loss` | -0.00043037 | -0.00109388 | 0.00023481 | 2000 | 34201 |
| `shared_lane_counter` | `constant_0_5` | `brier_score` | -0.00021515 | -0.00054662 | 0.00011712 | 2000 | 34201 |
| `shared_lane_counter` | `training_fold_blue_win_rate_intercept` | `log_loss` | -0.00045604 | -0.00113068 | 0.00019896 | 2000 | 34201 |
| `shared_lane_counter` | `training_fold_blue_win_rate_intercept` | `brier_score` | -0.00022799 | -0.00056493 | 0.00009905 | 2000 | 34201 |
| `shared_lane_counter` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `log_loss` | 0.00002646 | -0.00011011 | 0.00016534 | 2000 | 34201 |
| `shared_lane_counter` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `brier_score` | 0.00001320 | -0.00005500 | 0.00008255 | 2000 | 34201 |
| `shared_lane_counter` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `log_loss` | 0.00001134 | -0.00014525 | 0.00016985 | 2000 | 34201 |
| `shared_lane_counter` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `brier_score` | 0.00000563 | -0.00007260 | 0.00008470 | 2000 | 34201 |
| `shared_lane_counter` | `fold_local_champion_role_rate_minimum_support_10` | `log_loss` | -0.00017298 | -0.00066021 | 0.00031678 | 2000 | 34201 |
| `shared_lane_counter` | `fold_local_champion_role_rate_minimum_support_10` | `brier_score` | -0.00008624 | -0.00032970 | 0.00015836 | 2000 | 34201 |
| `combined_shared_interactions` | `constant_0_5` | `log_loss` | -0.00042979 | -0.00109240 | 0.00023607 | 2000 | 34201 |
| `combined_shared_interactions` | `constant_0_5` | `brier_score` | -0.00021486 | -0.00054605 | 0.00011775 | 2000 | 34201 |
| `combined_shared_interactions` | `training_fold_blue_win_rate_intercept` | `log_loss` | -0.00045546 | -0.00112801 | 0.00019914 | 2000 | 34201 |
| `combined_shared_interactions` | `training_fold_blue_win_rate_intercept` | `brier_score` | -0.00022770 | -0.00056396 | 0.00009914 | 2000 | 34201 |
| `combined_shared_interactions` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `log_loss` | 0.00002705 | -0.00011056 | 0.00016706 | 2000 | 34201 |
| `combined_shared_interactions` | `stage3_4a_composition_only_l2_0_1_no_intercept` | `brier_score` | 0.00001349 | -0.00005520 | 0.00008341 | 2000 | 34201 |
| `combined_shared_interactions` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `log_loss` | 0.00001192 | -0.00014553 | 0.00017110 | 2000 | 34201 |
| `combined_shared_interactions` | `stage3_4a_composition_plus_direct_matchup_l2_0_1_no_intercept` | `brier_score` | 0.00000592 | -0.00007275 | 0.00008536 | 2000 | 34201 |
| `combined_shared_interactions` | `fold_local_champion_role_rate_minimum_support_10` | `log_loss` | -0.00017239 | -0.00065933 | 0.00031690 | 2000 | 34201 |
| `combined_shared_interactions` | `fold_local_champion_role_rate_minimum_support_10` | `brier_score` | -0.00008595 | -0.00032901 | 0.00015852 | 2000 | 34201 |
| `shared_allied_synergy` | `composition_with_side_intercept` | `log_loss` | -0.00099984 | -0.00180098 | -0.00019176 | 2000 | 34201 |
| `shared_allied_synergy` | `composition_with_side_intercept` | `brier_score` | -0.00049614 | -0.00089446 | -0.00009498 | 2000 | 34201 |
| `shared_lane_counter` | `composition_with_side_intercept` | `log_loss` | -0.00099925 | -0.00180038 | -0.00019252 | 2000 | 34201 |
| `shared_lane_counter` | `composition_with_side_intercept` | `brier_score` | -0.00049585 | -0.00089385 | -0.00009526 | 2000 | 34201 |
| `combined_shared_interactions` | `composition_with_side_intercept` | `log_loss` | -0.00099867 | -0.00180118 | -0.00019047 | 2000 | 34201 |
| `combined_shared_interactions` | `composition_with_side_intercept` | `brier_score` | -0.00049556 | -0.00089371 | -0.00009446 | 2000 | 34201 |
| `combined_shared_interactions` | `shared_allied_synergy` | `log_loss` | 0.00000117 | -0.00000037 | 0.00000262 | 2000 | 34201 |
| `combined_shared_interactions` | `shared_allied_synergy` | `brier_score` | 0.00000058 | -0.00000019 | 0.00000131 | 2000 | 34201 |
| `combined_shared_interactions` | `shared_lane_counter` | `log_loss` | 0.00000058 | -0.00000073 | 0.00000191 | 2000 | 34201 |
| `combined_shared_interactions` | `shared_lane_counter` | `brier_score` | 0.00000029 | -0.00000037 | 0.00000095 | 2000 | 34201 |

## Selected configurations

| Model | Scope | Selected configuration |
| --- | --- | --- |
| `composition_with_side_intercept` | `outer-0` | `side-l2-0.03` |
| `composition_with_side_intercept` | `outer-1` | `side-l2-0.03` |
| `composition_with_side_intercept` | `outer-2` | `side-l2-0.3` |
| `composition_with_side_intercept` | `outer-3` | `side-l2-0.1` |
| `shared_allied_synergy` | `outer-0` | `synergy-k2-l2-1` |
| `shared_allied_synergy` | `outer-1` | `synergy-k2-l2-1` |
| `shared_allied_synergy` | `outer-2` | `synergy-k4-l2-3` |
| `shared_allied_synergy` | `outer-3` | `synergy-k2-l2-1` |
| `shared_lane_counter` | `outer-0` | `counter-k4-l2-3` |
| `shared_lane_counter` | `outer-1` | `counter-k4-l2-3` |
| `shared_lane_counter` | `outer-2` | `counter-k4-l2-3` |
| `shared_lane_counter` | `outer-3` | `counter-k4-l2-3` |
| `combined_shared_interactions` | `outer-0` | `combined-k4-l2-3` |
| `combined_shared_interactions` | `outer-1` | `combined-k2-l2-1` |
| `combined_shared_interactions` | `outer-2` | `combined-k4-l2-3` |
| `combined_shared_interactions` | `outer-3` | `combined-k4-l2-3` |
| `composition_with_side_intercept` | `final all-development` | `side-l2-0.03` |
| `shared_allied_synergy` | `final all-development` | `synergy-k2-l2-1` |
| `shared_lane_counter` | `final all-development` | `counter-k4-l2-3` |
| `combined_shared_interactions` | `final all-development` | `combined-k4-l2-3` |

## Material-usefulness gate

### `composition_with_side_intercept` — passes all: `False`

| Criterion | Required | Observed | Pass | Field |
| --- | --- | --- | ---: | --- |
| `log_loss_improvement_vs_blue_intercept` | `>=0.002` | `-0.0005432110496597087` | `False` | `metrics and coverage_reconciliation` |
| `log_loss_improvement_vs_composition` | `>=0.002` | `-0.0010257180823096679` | `False` | `metrics and coverage_reconciliation` |
| `paired_log_loss_upper_vs_blue_below_zero` | `<0` | `0.0018312835332694015` | `False` | `metrics and paired_bootstrap_intervals` |
| `paired_log_loss_upper_vs_composition_below_zero` | `<0` | `0.0018426767785935838` | `False` | `metrics and paired_bootstrap_intervals` |
| `brier_improvement_vs_blue_intercept` | `>=0.001` | `-0.00026786147539115124` | `False` | `metrics and coverage_reconciliation` |
| `brier_improvement_vs_composition` | `>=0.001` | `-0.0005090536578755411` | `False` | `metrics and coverage_reconciliation` |
| `each_platform_improves_vs_composition` | `>=0.001 for each platform` | `{"eun1": -0.0007095333279082405, "euw1": -0.0013519916929460862}` | `False` | `metrics and coverage_reconciliation` |
| `no_platform_worse_than_blue_intercept` | `<=0 for each platform` | `{"eun1": 4.766866472449838e-05, "euw1": 0.0010545652528762828}` | `False` | `metrics and coverage_reconciliation` |
| `ece` | `<=0.02` | `0.009446294132408769` | `True` | `metrics and coverage_reconciliation` |
| `calibration_slope` | `0.8..1.2` | `0.32194800558254066` | `False` | `metrics and coverage_reconciliation` |
| `calibration_intercept` | `<=0.02 absolute` | `0.026178433809488977` | `False` | `metrics and coverage_reconciliation` |
| `coverage` | `>=0.95` | `1.0` | `True` | `metrics and coverage_reconciliation` |
| `role_coverage` | `<=0.05 drop` | `0.0` | `True` | `metrics and coverage_reconciliation` |
| `chronological_direction_repeats` | `>=2 blocks` | `1` | `False` | `metrics and coverage_reconciliation` |

### `shared_allied_synergy` — passes all: `False`

| Criterion | Required | Observed | Pass | Field |
| --- | --- | --- | ---: | --- |
| `log_loss_improvement_vs_blue_intercept` | `>=0.002` | `0.0004566319025414156` | `False` | `metrics and coverage_reconciliation` |
| `log_loss_improvement_vs_composition` | `>=0.002` | `-2.5875130108543587e-05` | `False` | `metrics and coverage_reconciliation` |
| `paired_log_loss_upper_vs_blue_below_zero` | `<0` | `0.000197025663379349` | `False` | `metrics and paired_bootstrap_intervals` |
| `paired_log_loss_upper_vs_composition_below_zero` | `<0` | `0.00016588084019769628` | `False` | `metrics and paired_bootstrap_intervals` |
| `brier_improvement_vs_blue_intercept` | `>=0.001` | `0.00022828293372587072` | `False` | `metrics and coverage_reconciliation` |
| `brier_improvement_vs_composition` | `>=0.001` | `-1.2909248758519176e-05` | `False` | `metrics and coverage_reconciliation` |
| `each_platform_improves_vs_composition` | `>=0.001 for each platform` | `{"eun1": 5.481548471331088e-05, "euw1": -0.00010914042951315484}` | `False` | `metrics and coverage_reconciliation` |
| `no_platform_worse_than_blue_intercept` | `<=0 for each platform` | `{"eun1": -0.000716680147897053, "euw1": -0.00018828601055664862}` | `True` | `metrics and coverage_reconciliation` |
| `ece` | `<=0.02` | `0.007876935678074104` | `True` | `metrics and coverage_reconciliation` |
| `calibration_slope` | `0.8..1.2` | `1.1631808334169693` | `True` | `metrics and coverage_reconciliation` |
| `calibration_intercept` | `<=0.02 absolute` | `0.03278485592164987` | `False` | `metrics and coverage_reconciliation` |
| `coverage` | `>=0.95` | `1.0` | `True` | `metrics and coverage_reconciliation` |
| `role_coverage` | `<=0.05 drop` | `0.0` | `True` | `metrics and coverage_reconciliation` |
| `chronological_direction_repeats` | `>=2 blocks` | `2` | `True` | `metrics and coverage_reconciliation` |

### `shared_lane_counter` — passes all: `False`

| Criterion | Required | Observed | Pass | Field |
| --- | --- | --- | ---: | --- |
| `log_loss_improvement_vs_blue_intercept` | `>=0.002` | `0.00045604297166734753` | `False` | `metrics and coverage_reconciliation` |
| `log_loss_improvement_vs_composition` | `>=0.002` | `-2.646406098261167e-05` | `False` | `metrics and coverage_reconciliation` |
| `paired_log_loss_upper_vs_blue_below_zero` | `<0` | `0.00019895592071851706` | `False` | `metrics and paired_bootstrap_intervals` |
| `paired_log_loss_upper_vs_composition_below_zero` | `<0` | `0.00016534170166446403` | `False` | `metrics and paired_bootstrap_intervals` |
| `brier_improvement_vs_blue_intercept` | `>=0.001` | `0.00022798883757207955` | `False` | `metrics and coverage_reconciliation` |
| `brier_improvement_vs_composition` | `>=0.001` | `-1.3203344912310344e-05` | `False` | `metrics and coverage_reconciliation` |
| `each_platform_improves_vs_composition` | `>=0.001 for each platform` | `{"eun1": 5.4427436764981074e-05, "euw1": -0.00010993665310632394}` | `False` | `metrics and coverage_reconciliation` |
| `no_platform_worse_than_blue_intercept` | `<=0 for each platform` | `{"eun1": -0.0007162920999487232, "euw1": -0.0001874897869634795}` | `True` | `metrics and coverage_reconciliation` |
| `ece` | `<=0.02` | `0.007867600054926194` | `True` | `metrics and coverage_reconciliation` |
| `calibration_slope` | `0.8..1.2` | `1.1616011704650122` | `True` | `metrics and coverage_reconciliation` |
| `calibration_intercept` | `<=0.02 absolute` | `0.03272968652738412` | `False` | `metrics and coverage_reconciliation` |
| `coverage` | `>=0.95` | `1.0` | `True` | `metrics and coverage_reconciliation` |
| `role_coverage` | `<=0.05 drop` | `0.0` | `True` | `metrics and coverage_reconciliation` |
| `chronological_direction_repeats` | `>=2 blocks` | `2` | `True` | `metrics and coverage_reconciliation` |

### `combined_shared_interactions` — passes all: `False`

| Criterion | Required | Observed | Pass | Field |
| --- | --- | --- | ---: | --- |
| `log_loss_improvement_vs_blue_intercept` | `>=0.002` | `0.00045546081620617684` | `False` | `metrics and coverage_reconciliation` |
| `log_loss_improvement_vs_composition` | `>=0.002` | `-2.7046216443782356e-05` | `False` | `metrics and coverage_reconciliation` |
| `paired_log_loss_upper_vs_blue_below_zero` | `<0` | `0.0001991411565447731` | `False` | `metrics and paired_bootstrap_intervals` |
| `paired_log_loss_upper_vs_composition_below_zero` | `<0` | `0.0001670642268198739` | `False` | `metrics and paired_bootstrap_intervals` |
| `brier_improvement_vs_blue_intercept` | `>=0.001` | `0.00022769796832794453` | `False` | `metrics and coverage_reconciliation` |
| `brier_improvement_vs_composition` | `>=0.001` | `-1.3494214156445361e-05` | `False` | `metrics and coverage_reconciliation` |
| `each_platform_improves_vs_composition` | `>=0.001 for each platform` | `{"eun1": 5.490862251755768e-05, "euw1": -0.00011161607898391157}` | `False` | `metrics and coverage_reconciliation` |
| `no_platform_worse_than_blue_intercept` | `<=0 for each platform` | `{"eun1": -0.0007167732857012998, "euw1": -0.00018581036108589188}` | `True` | `metrics and coverage_reconciliation` |
| `ece` | `<=0.02` | `0.007885826929108896` | `True` | `metrics and coverage_reconciliation` |
| `calibration_slope` | `0.8..1.2` | `1.1623814046094973` | `True` | `metrics and coverage_reconciliation` |
| `calibration_intercept` | `<=0.02 absolute` | `0.03282047325443188` | `False` | `metrics and coverage_reconciliation` |
| `coverage` | `>=0.95` | `1.0` | `True` | `metrics and coverage_reconciliation` |
| `role_coverage` | `<=0.05 drop` | `0.0` | `True` | `metrics and coverage_reconciliation` |
| `chronological_direction_repeats` | `>=2 blocks` | `2` | `True` | `metrics and coverage_reconciliation` |

## Interpretation

The best observed development candidate by overall log loss was `shared_allied_synergy`; this is not a model-selection decision. Its observed log-loss differences were -0.00045663 versus the Blue-rate baseline and 0.00002588 versus the composition baseline. Mechanically, the available draft-only data were insufficient for advancement. Failed frozen criteria for that candidate: `log_loss_improvement_vs_blue_intercept, log_loss_improvement_vs_composition, paired_log_loss_upper_vs_blue_below_zero, paired_log_loss_upper_vs_composition_below_zero, brier_improvement_vs_blue_intercept, brier_improvement_vs_composition, each_platform_improves_vs_composition, calibration_intercept`. These draft contrasts are non-causal and do not authorize recommendations.

No candidate passed the complete prospectively frozen gate; Stage 3.4B-1 produced no model suitable for advancement.
