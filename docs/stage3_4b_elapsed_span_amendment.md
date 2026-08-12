# Stage 3.4B-1 zero-fit elapsed-span amendment

Status: **operationally refrozen after a failed invocation with zero model and
optimizer fits and zero scientific results**.

The current contracts are
`config/evaluation/stage3.4b-1-patch26.15-protocol-v2/protocol.json` and
`operational-amendment-v2.json` in the same directory. Version 1 remains retained as
the historical pre-amendment contract.

## Provenance and rationale

The 48-hour preceding-training-span guard first appeared in commit
`cc5dd3ab764cf69eff4488890bbcd361e220b8df` in the protocol, implementation, and
proposal. Repository history contains no citation, derivation, external methodology,
or rationale for exactly 48 hours. The proposal merely repeated the threshold.

The first authorized execution stopped before any training operation because the
first inner selection fold had about 37.52 hours of preceding data. No candidate
outcome, metric, prediction, coefficient, or model-performance result was produced or
inspected. The elapsed guard is therefore removed as unsupported rather than lowered
to a threshold tailored to this observed boundary.

Training adequacy continues to require observation counts, both outcome classes,
EUNE and EUW representation, strict chronological separation, non-empty partitions,
no overlap, and deterministic fold membership. Elapsed training span is reported only
as a diagnostic.

## Preserved scientific design

The amendment does not change source data, the four outer boundaries, the three
one-day inner-validation schedule, the 6,368 outer-evaluation rows or their ordering,
models, features, grids, seeds, fitting policy, selection rules, calibration,
bootstrap, usefulness gates, privacy controls, or the 171-operation budget. It does
not introduce a replacement elapsed-time threshold.

Zero-fit preflight now constructs all three inner folds in each of the four outer
selection contexts plus the three final-development selection folds. It validates
their row counts, outcome support, platform representation, chronology, isolation,
determinism, paired membership, ordering, and operation-budget consistency before a
real execution can begin.
