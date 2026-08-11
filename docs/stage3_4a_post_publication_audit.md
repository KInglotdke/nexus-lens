# Stage 3.4A post-publication diagnostic audit

Status: **aggregate, zero-fit diagnostic of frozen patch-26.15 development work**.
The published nested-CV bundle remains immutable. In-sample predictions in the
separate diagnostic artifact are structural diagnostics, not OOF estimates,
development-performance estimates, or final test results.

## Phase 1 correctness audit

No material correctness defect was found.

For each role `r` and champion `c`, the composition feature is
`I(Blue(r)=c) - I(Red(r)=c)`. The observation is always oriented with Riot team 100
(Blue, the lower numeric team ID) as allied, and the target is one exactly when that
team won. All 9,414 eligible drafts reconstructed with team 100 against team 200;
their target counts exactly reproduce the published class balance.

For a same-role pair `(low_id, high_id)`, the matchup feature is `+1` when Blue has
the lower champion ID and `-1` when Blue has the higher champion ID. Champion-ID
ordering defines a stable coordinate, not champion strength. Pairing uses the same
Stage 3.2 analysis role on each side. The feature builder never reads outcomes,
post-game statistics, platform, team ID, or player information.

Swapping the teams negates every composition and matchup coordinate and flips the
target. There is no intercept or side feature, so the score changes from `z` to
`-z` and the probability changes from `sigmoid(z)` to `1 - sigmoid(z)`. This proves
the expected transformation and prevents an implicit side bias, while deliberately
preventing the model from learning a Blue-side base rate.

Vocabulary keys are the sorted set of champion-role and optional unordered
same-role pair keys observed in the supplied training partition. Reordering input
or flipping outcomes cannot change them. Every outer and inner fit receives only
its training partition; held-out keys are absent and contribute zero. The final
full-data vocabulary was reconstructed exactly from the sealed corpus and matched
the saved vocabulary hashes. Platform is absent from every key.

The optimizer minimizes:

`mean logistic loss + 0.5 * L2 * sum(coefficient ** 2)`.

Consequently, the selected `L2 = 0.1` is a penalty applied to mean loss, with
gradient term `0.1 * coefficient`; it is not an inverse-strength `C` parameter.
Both candidates and the selected value match the frozen protocol and publication.

The primary matchup model does **not** transform Stage 3.3B rates, historical
outcomes, posterior probabilities, or smoothed matchup statistics. It adds direct
signed pair indicators to the composition vector. There is therefore no missing,
default, or heavily smoothed matchup-rate value to audit. The only fallback is zero
contribution for an unseen fold-local feature.

Focused tests now prove explicit Blue/Red outcome mapping, composition signs,
same-role matchup orientation, complete vector negation after a swap, deterministic
and complete vocabulary construction, held-out-key exclusion, and outcome
independence. Existing tests continue to prove training-only fold construction,
platform exclusion, probability complementarity, and publication determinism.

## Published uncertainty

The frozen 95% overall bootstrap intervals are:

| Model | Metric | Lower | Upper |
| --- | --- | ---: | ---: |
| Composition only | Log loss | 0.69202942 | 0.69289728 |
| Composition only | Brier | 0.24944136 | 0.24987511 |
| Composition only | Accuracy | 0.50859624 | 0.52724134 |
| Composition only | ECE | 0.00331982 | 0.01863559 |
| Composition + matchup | Log loss | 0.69203593 | 0.69290307 |
| Composition + matchup | Brier | 0.24944464 | 0.24987802 |
| Composition + matchup | Accuracy | 0.50698693 | 0.52826110 |
| Composition + matchup | ECE | 0.00279339 | 0.01922797 |

The matchup-minus-composition point differences are `+0.00001764897` log loss and
`+0.00000883677` Brier. Paired replicate differences were not retained, so their
paired 95% intervals cannot be recovered from the saved marginal intervals. The
bootstrap was not rerun.

## Aggregate structure

The corpus contains 10,000 accepted matches, 9,414 role-complete eligible drafts,
and 586 exclusions, all recorded as `role_analysis_ineligible`. EUNE contributes
4,700 eligible drafts and EUW 4,714. Blue won 4,738/9,414 overall (`0.503293`),
2,337/4,700 EUNE (`0.497234`), and 2,401/4,714 EUW (`0.509334`).

Both models contain the same 710 champion-role coordinates: 165 TOP, 108 JUNGLE,
159 MIDDLE, 134 BOTTOM, and 144 UTILITY. Of these, 273 have support below ten and
213 have support at least 100. For composition-only coefficients, median absolute
magnitude is `0.001570`, the 95th percentile is `0.015235`, and the maximum is
`0.045576`. Absolute magnitude is strongly associated with support (Spearman
`0.7370`), consistent with regularization suppressing weakly supported estimates.
Named high-magnitude parameters appear only in the aggregate artifact when their
predeclared training support is at least 100; they are associational parameters,
not causal champion-strength estimates.

The matchup model observes 9,393 of 51,076 possible unordered role-pairs (`18.39%`).
Coverage ranges from `15.09%` BOTTOM to `31.50%` JUNGLE. Support is highly sparse:
6,621 pairs occur 1–4 times, 1,390 occur 5–9 times, and only four occur at least
100 times. Median absolute matchup coefficient magnitude is `0.000534`, its 95th
percentile is `0.002576`, and its maximum is `0.009377`. Active pair values retain
their full `-1/+1` variance, proving that a preprocessing transform did not flatten
them; the fitted coefficients are small instead. In the saved OOF audit, 4,564 of
47,070 matchup slots (`9.70%`) were unseen in their training fold, affecting 3,732
matches (`39.64%`).

## In-sample prediction dispersion

These values apply the saved all-data models back to the same patch-26.15 corpus.
They are **in-sample structural diagnostics only**.

| Model | Mean | SD | Min | 5th | Median | 95th | Max | In 0.49–0.51 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Composition only | 0.500119 | 0.010185 | 0.465395 | 0.483328 | 0.500193 | 0.516940 | 0.534121 | 67.38% |
| Composition + matchup | 0.500109 | 0.010589 | 0.464604 | 0.482822 | 0.500199 | 0.517538 | 0.536337 | 65.16% |

Every prediction from both models lies within `0.45–0.55`. Mean absolute distance
from 0.5 is `0.008135` and `0.008471`, respectively. Their mean absolute prediction
difference is only `0.001057`; its 95th percentile is `0.002590` and maximum is
`0.005369`. Dispersion is almost identical between EUNE and EUW.

## Interpretation

The strongest explanation is a combination of a weak additive composition signal,
extremely sparse direct matchup coordinates, and regularization that appropriately
shrinks weakly supported evidence. The matchup feature values themselves are not
constant and no smoothing stage eliminates their variation. Platform prediction
dispersion is nearly identical, so platform heterogeneity is not the main observed
mechanism, although the Blue win balance differs modestly by platform.

Near-chance performance therefore does not indicate an implementation defect. It
also does not prove that draft information is intrinsically useless: the frozen
representation omits ally interactions, shared structure across related matchups,
and all leakage-safe pre-match player proficiency and role-familiarity information.
Those hypotheses require a prospectively specified experiment.

The aggregate machine-readable artifact is
`config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1/diagnostics/stage3.4a-post-publication-v1/aggregate_diagnostics.json`.
Its SHA-256 is
`3003e7697c38353ecaeeaa417938ddc95038995a2addbb3bb00454082d56e620`.

## Unrecoverable requested analyses

Fold-specific prediction dispersion and OOF structural slices cannot be recovered
because row-level OOF predictions were not retained. Fold-specific coefficient and
support diagnostics cannot be recovered because outer-fold coefficient states were
retained only as hashes and dimensions. Paired model-difference intervals cannot be
recovered because bootstrap replicate differences were not retained. Reconstructing
any of these would require forbidden nested-CV or bootstrap work.
