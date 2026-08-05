# Patch 26.15 dual-platform pilot lifecycle

This operational pilot resumes the schema-4 collection checkpoints created by the
100-match canary. It does not create unrelated fresh collections:

- EUNE collection run: `20260803T141321876182Z-population`
- EUW collection run: `20260803T141945176809Z-population`
- accepted operational window: public patches `26.15` and `26.14`, newest first
- queue: Ranked Solo/Duo (`420`)
- final collection target: exactly 1,000 accepted matches per platform

The explicitly authorized resume grows the ignored raw snapshot, checkpoint, and
appendable normalized collection state for each existing run. The former 100-match
Stage 3.1, Stage 3.2, Stage 3.3A, Stage 3.3B, and lineage-v1 publications remain in
their original `data/canary/26.15/<region>/processed` paths and are not rewritten.

The 1,000-match derivations are separate processing snapshots:

- `data/pilot/26.15/eune/processed`
- `data/pilot/26.15/euw/processed`

Each platform is normalized and then processed through Stage 3.1, Stage 3.2,
Stage 3.3A, Stage 3.3B, and lineage-v1. Every stage is validated without writing
before publication. Stage 3.3B records target patch `26.15`; accepting the adjacent
patch operationally does not assert statistical interchangeability. EUNE and EUW
remain physically and logically separate, and the pilot does not select statistical
policy or produce recommendations.
