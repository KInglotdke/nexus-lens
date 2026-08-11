# Nexus Lens pooled patch-26.15 development report

Status: **nested-CV development estimate; not an untouched final test**.

EUNE and EUW were pooled for fitting while platform remained subgroup and stratification metadata only. Patch 26.16 was not used.

- Accepted matches: `10000`
- Eligible drafts: `9414`
- Excluded drafts: `586`
- Outer fold fingerprint: `0ba47e9c44e1899a186e460375ed8b5b2613687c5d660339d04bb0e908ef4008`

| Model | Scope | Log loss | Brier | Accuracy | ECE |
| --- | --- | ---: | ---: | ---: | ---: |
| `composition_only` | overall | 0.69243175 | 0.24964241 | 0.51784576 | 0.00908727 |
| `composition_only` | EUNE | 0.69185018 | 0.24935176 | 0.53063830 | 0.02192909 |
| `composition_only` | EUW | 0.69301159 | 0.24993219 | 0.50509122 | 0.00913692 |
| `composition_plus_lane_matchups` | overall | 0.69244940 | 0.24965124 | 0.51710219 | 0.00807627 |
| `composition_plus_lane_matchups` | EUNE | 0.69187252 | 0.24936294 | 0.52914894 | 0.02015622 |
| `composition_plus_lane_matchups` | EUW | 0.69302457 | 0.24993869 | 0.50509122 | 0.00915817 |

Final all-development L2 selections:

- `composition_only`: `0.1`
- `composition_plus_lane_matchups`: `0.1`

No recommendation policy was selected or implemented. Training metrics from the all-data fit are not reported as generalization evidence.

Ready for recommendation policy: `False`.
