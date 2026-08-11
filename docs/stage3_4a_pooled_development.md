# Stage 3.4A pooled patch-26.15 development protocol

This prospective addendum was recorded before any pooled model fitting. It permits
one development exercise over the logical union of the sealed EUNE and EUW
patch-26.15 populations. Source artifacts remain immutable and platform-isolated;
platform is retained as provenance, stratification, and subgroup metadata but is not
a predictive feature.

Patch 26.15 is development and training data. Nested cross-validation provides an
out-of-fold development estimate, not an untouched test result. Patch 26.16 remains
the untouched future temporal evaluation and is neither collected nor inspected by
this task.

## Relationship to the earlier freeze

The addendum supersedes four provisions only for pooled development and the locked
pooled model that will later face patch 26.16:

- platform-specific model fitting becomes pooled fitting with platform subgroups;
- the platform-specific fixed L2 values become the candidate set `{0.1, 1.0}`;
- composition plus same-role matchup features becomes the primary development model;
- composition-only becomes its reference instead of the earlier fixed-0.5 primary
  comparison.

It preserves queue-420 role-complete eligibility, lower-team orientation and target,
the antisymmetric feature definitions, no intercept or scaling, no team-side or
platform feature, zero contribution for unseen features, training-only vocabulary,
L-BFGS-B settings, metric definitions, seeds, privacy constraints, and the untouched
26.16 temporal boundary. Neither historical tag is moved or rewritten.

## Data and leakage boundary

Four immutable Stage 3.3A components are joined logically in memory: external and
retained EUNE, and external and retained EUW. Match identity is the deduplication and
fold-grouping key. The expected accepted/eligible counts are `5000/4700` EUNE,
`5000/4714` EUW, and `10000/9414` overall. Any mismatch stops the run.

Each match creates exactly one lower-team-oriented draft. No mirrored representation
exists. Fold assignment is stratified by `(platform, binary target)`: rows in each
stratum are ordered by a SHA-256 key containing the fixed seed, a declared scope
label, and private match identity, then assigned round-robin. Only an aggregate fold
fingerprint is published.

Outer nested-CV evaluation uses five folds. Each outer training partition performs
four-fold selection separately for both models over L2 `{0.1, 1.0}`, minimizing mean
validation log loss. Values within absolute tolerance `1e-12` tie, and the larger L2
(`1.0`) wins. Final all-development selection uses a separate deterministic
five-fold pass under the same rule.

Every fitted fold constructs its champion-role and signed same-role champion-pair
vocabulary only from that fold's training rows. Validation-only champions or pairs
therefore contribute zero. The matchup model does not consume Stage 3.3B aggregates
or any separately calculated outcome-rate feature; its pair coefficients are learned
only from the training partition. Validation outcomes cannot affect vocabulary,
transformation, selection, or fitting.

## Metrics and interpretation

Out-of-fold metrics use the existing log-loss, Brier, accuracy-at-0.5, calibration,
ECE, coverage, and abstention definitions. Results are reported overall, EUNE, EUW,
and as EUNE-minus-EUW differences for log loss, Brier, accuracy, and ECE. Overall
bootstrap resampling preserves platform sample sizes, clusters at one match, uses
200 replicates and seed 34101, and reports 95% intervals.

The final selected models are refitted on all 9,414 eligible patch-26.15 drafts.
Training metrics are not evidence of generalization. Before patch-26.16 outcomes are
inspected, the final configuration, vocabulary, coefficients, and model fingerprints
must remain locked. Patch-26.16 will be collected and sealed separately by platform,
then scored overall and by platform without retraining or tuning.

The machine-readable contract is
`config/evaluation/stage3.4a-patch26.15-pooled-dev-protocol-v1/protocol.json`.

Execution artifacts record wall and process CPU time. Whole-run Python allocation
tracing is deliberately not enabled: it is non-scientific instrumentation and can
dominate sparse optimizer runtime. The execution record marks memory measurement as
unavailable rather than reporting a misleading value.
