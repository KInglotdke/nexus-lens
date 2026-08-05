# Stage 3.4A: offline composition-aware modelling

Stage 3.4A is a match-level experimental harness. It compares a fixed 0.5
prediction, a training-only champion-role baseline, a composition-only logistic
model, and composition plus direct same-role matchup features. It does not implement
player familiarity, champion-pool personalization, candidate ranking, causal claims,
or recommendations.

The retained smoke evaluation trains only on public patch 26.14 and treats 26.15 as
development data. EUNE and EUW are fitted and published independently. Every output
is labelled experimental, non-calibrating, and policy-unresolved.

## Match representation

One eligible observation represents one entire draft from the lower numeric team ID
as the allied side:

- exactly five allied `(role, champion_id)` assignments;
- exactly five opposing `(role, champion_id)` assignments;
- one assignment for each TOP, JUNGLE, MIDDLE, BOTTOM, and UTILITY role per team;
- platform, queue 420, public patch, team-side IDs, and binary allied-team outcome.

Incomplete or ambiguous role drafts abstain and reduce coverage; they are never
silently repaired. Participant order is discarded only after the role remains
attached. Duplicate match or participant keys fail the run.

Composition features are signed champion-role indicators: allied slots contribute
`+1` and opposing slots `-1`. Optional lane-matchup features use an unordered
champion pair per role and a sign indicating which champion is allied. Swapping the
two teams therefore negates the complete feature vector. Both learned models have no
intercept, so `sigmoid(-z) = 1 - sigmoid(z)` makes team-swap complementarity a
structural property.

The champion-role baseline uses raw training-only champion-role rates. An unseen
champion-role receives 0.5. The match probability is `0.5 + (mean allied rate - mean
opposing rate) / 2`, which also complements under a team swap.

## Models and training boundary

The learned models are deterministic, CPU-compatible L2 logistic regressions fitted
with `scipy.optimize.minimize(method="L-BFGS-B")`. Sparse matrices are used; there
is no feature scaling, intercept, evaluation-fit vocabulary, or implicit clipping.
Unseen evaluation features have a defined zero-coefficient contribution.

When strengths are not frozen, each candidate L2 value is evaluated only inside
seeded, balanced, match-grouped folds of the configured training patch. Every fold
builds its vocabulary from its own training subset. Evaluation-patch outcomes never
participate in vocabulary construction, preprocessing, or strength selection.

For the 26.14 training data, the development selections were:

| Platform | Composition-only L2 | Composition + matchup L2 |
| --- | ---: | ---: |
| EUNE | 0.1 | 0.1 |
| EUW | 1.0 | 1.0 |

These values are not globally calibrated. They are the platform-specific settings
to freeze before a genuinely untouched 26.16 evaluation. The future fold should
train on 26.15, evaluate 26.16, and pass the frozen values explicitly; it must not
rerun selection using 26.16 outcomes.

## Counterfactual interface

`evaluate_counterfactual` takes a fitted composition model, one draft, side, role,
and candidate champion ID. It replaces exactly that slot, hashes the other nine
unchanged role-attached slots, and returns the original probability, candidate
probability, and difference.

The result is always marked `mechanical_non_causal_not_a_recommendation`. The
interface evaluates one caller-supplied candidate; it has no ranking operation and
does not claim that a probability difference is causal, a counter, or lane
dominance.

## Metrics and publication

Stage 3.4A reuses the Stage 3.3C definitions for log loss, Brier score, accuracy at
0.5, equal-width calibration tables/ECE, coverage/abstention, and deterministic
match-cluster bootstrap intervals. Descriptive slices use minimum training
champion-role frequency and, for lane features, minimum direct feature frequency.

Log loss is primary and Brier score is secondary. Calibration and accuracy are
descriptive secondary metrics. The accuracy implementation treats an exact
probability of 0.5 as predicting the oriented allied outcome 1 because it uses
`probability >= 0.5`; accuracy is not used for model selection or sample sizing.
Every policy uses the identical eligible match subset.

Team IDs are retained as observation metadata but are not a model feature. The
current model cannot fit a blue/team-100 base advantage. Adding a signed side term
would preserve team-swap complementarity, but it would define a different model;
the pre-26.16 freeze keeps the draft-only contract and records this limitation.

No prediction-level file is published. Each immutable directory contains:

```text
schema=stage3.4a-v1/run=<source-run>__stage3.4a__<config-hash>/
  composition_metrics.json
  model_artifacts.json
  quality_report.json
  composition_report.md
  metadata.json
```

`model_artifacts.json` contains aggregate champion-role counts, training-only CV
scores, feature vocabulary, fitted coefficients, per-feature training counts, and
optimizer facts. Champion IDs and roles are model dimensions, not player data.
Metadata records exact Stage 3.1/3.2/3.3A hashes, configuration, code hash, and output
hashes. Publication is staged atomically; byte-identical reruns are no-ops and an
unequal existing run fails. Validate-only and failed validation write nothing.

## Storage gate

The caller supplies a free-space reserve and publication byte cap. The complete
serialized publication is measured before writing. Publication fails if it exceeds
the cap, would cross the reserve, or would consume more than 0.1% of observed free
space (with a 1 MB lower materiality threshold). Actual free space remains a runtime
preflight value rather than deterministic metadata.

The retained smoke commands use a 5,000,000-byte publication cap and the prior
15,371,520,659-byte dual-10k collection reserve. Current publications are about 1.1
MB per platform and do not materially change collection headroom.

## Smoke command

```powershell
.\.venv\Scripts\python.exe scripts\model_compositions.py --input-run data/pilot/26.15/eune/processed/stage3/schema=stage3.3a-v1/run=20260803T141321876182Z-population --output-root data/pilot/26.15/eune/processed/stage3 --analysis-region EUNE --training-patch 26.14 --evaluation-patch 26.15 --l2-grid 0.01 0.1 1.0 --cv-folds 3 --seed 34001 --calibration-bins 10 --bootstrap-replicates 200 --bootstrap-seed 34101 --max-iterations 500 --optimizer-tolerance 1e-9 --expected-match-count 1000 --max-publication-bytes 5000000 --minimum-free-space-reserve-bytes 15371520659
```

EUW uses the corresponding `euw` input/output roots and `--analysis-region EUW`.
Add `--validate-only` to execute the complete offline harness without writing.

## Frozen 26.16 evaluation configuration

After a future platform-specific Stage 3.3A input containing 26.15 and 26.16 exists,
use `--training-patch 26.15 --evaluation-patch 26.16` and:

- EUNE: `--frozen-composition-only-l2 0.1
  --frozen-composition-plus-matchups-l2 0.1`;
- EUW: `--frozen-composition-only-l2 1.0
  --frozen-composition-plus-matchups-l2 1.0`.

All other feature, optimizer, seed, metric, eligibility, privacy, and storage
parameters remain exactly those in the smoke command. This configuration must be
frozen before inspecting any 26.16 outcomes.

The versioned manifests, deterministic power analysis, training-adequacy evidence,
collection targets, and SSD preflight are documented in
[stage3_4a_freeze.md](stage3_4a_freeze.md).

## Deferred work

Do not interpret the development metrics as selecting a model. Broader patch
replication, sample-size/power policy, storage headroom, and a genuinely untouched
26.16 fold remain necessary. Player features, timelines, composition interactions
beyond the transparent additive harness, causal modelling, candidate ranking, and
recommendation policy remain out of scope.
