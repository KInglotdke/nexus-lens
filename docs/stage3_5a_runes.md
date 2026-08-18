# Stage 3.5A rune-feasibility addendum

This addendum asks only whether focal-keystone data have enough coverage to be a
prospective Stage 3.5B champion-select feature. It does not compare rune outcomes,
fit a model, rank runes, or authorize a recommendation.

## Source and mapping contract

The addendum is a derived lineage beside the sealed `stage3.5a-v1` dataset. Its
parent is the unchanged 20,000-row focal-perspective table with SHA-256
`91dd6f0130dbc47734089b067e55b0b75350228205390a0458a1f4b67b9868b6`.
For each parent row, the private derived table retains its row index, a hash of the
complete parent row, the existing match-group key and scientific weight, and only
these new values:

- focal keystone ID and patch-correct name;
- focal primary and secondary rune-tree IDs;
- an explicit mapping status.

The table does not copy targets and contains no opponent rune, raw match ID, Riot
identifier, player name, or external path. Reversing perspective selects the newly
focal participant's own keystone.

Public patch 26.15 resolves through the repository patch map to API patch 16.15 and
then to the newest exact matching official Data Dragon revision, `16.15.1`. The
cached `runesReforged.json` SHA-256 is
`36070dbfec7c958641a588da7df5dc18da4ae12a87c5858dfa5c82d4045c8d4d`;
the version-list SHA-256 is
`c2eede780ca5a85ab58877fcadcd6af3b43bafaddc4f6169840c18142e5ac382`.
Keystones are identified by membership in the first slot of the patch-specific
rune-tree definition, not by a payload array position or a current-version lookup.

## Outcome-blind support result

All 20,000 focal rows in 10,000 two-perspective match groups have a mapped
keystone, spanning 17 observed keystones. Of those rows, 19,999 also have an
ordinary complete tree mapping. One row has the explicit status
`mapped_secondary_tree_unavailable`: its primary keystone and tree are valid, but
its secondary tree cannot be stated reliably. No row was removed.

Champion-keystone support is uneven but usable with regularization and fallback:
627 combinations have a median of 3 focal rows, an interquartile range of 1-14,
and a maximum of 786. Counts meeting thresholds 2/5/10/20/50 are respectively
413/265/188/132/80. Among 165 observed focal champions, 16 have one observed
keystone, 40 have two, and 109 have three or more; only 41 champions have at least
two keystones with 20 or more focal rows each.

Directional focal-champion/enemy-champion/keystone support is much sparser. The
7,518 combinations have a median of 1 focal row, an interquartile range of 1-3,
and a maximum of 44. Counts meeting thresholds 2/5/10/20/50 are
3,250/1,113/356/48/0. This does not support a primary unregularized three-way
interaction.

Both platforms have complete focal-row coverage. Five equal chronological blocks
of 2,000 match groups each also have 100% coverage; they contain 16, 16, 14, 16,
and 16 observed keystones, with no material coverage-rate change from 2026-07-29
through 2026-08-11. Full combination tables remain private. The public aggregate
audit is in
`config/evaluation/stage3.5a-rune-addendum-patch26.15-v1/results/`.

## Scientific interpretation

The support evidence permits a regularized focal-keystone main-effect candidate
with a rare-category fallback. Focal champion by keystone interactions are only
partially supported and require shrinkage plus the documented hierarchy.
Champion-by-enemy-by-keystone interactions are exploratory at most. Complete rune
pages were not extracted and remain outside scope. CatBoost cannot create absent
support and can overfit rare combinations.

Focal keystone is selected before match outcomes and can be supplied at champion
select. Rune selection is nevertheless observational, not randomized: enemy
champion, playstyle, familiarity, skill, expected strategy, and external advice can
all influence it. A future model may initially describe only
"rune-conditioned expected lane performance," not the causal effect of switching
runes. It must not derive the feature from performance, win/loss, targets, opponent
accounts, or future matches.

The existing intervention proxies were not used to filter this audit or create rune
features. The earlier 77.81% five-minute proxy rate and zero Herald-linked top-tower
events still require separate validation and cannot become rune-model inputs or
exclusion rules.
