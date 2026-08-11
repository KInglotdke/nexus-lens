# Patch 26.15 two-region data seal

The patch-26.15 population milestone contains 5,000 unique Ranked Solo/Duo
(`queueId=420`) match observations for EUNE and 5,000 for EUW. The regions remain
separate: this is 10,000 regional observations, not a pooled model dataset. No
patch-26.16 match is included, and no model, baseline, prior, or recommendation
policy had been fitted, evaluated, or selected when the data was sealed.

The earlier annotated tag `stage3.4a-pre-26.16-v1` is the pre-collection scientific
method freeze. Its manifests preserve the predetermined features, metrics,
regularization, optimizer, and prospective sample-size plan. This later data seal is
only a post-collection provenance and integrity milestone; it does not change that
scientific contract.

## Population record

| Region | Platform | External accepted | Retained private | Overlap | Combined |
| --- | --- | ---: | ---: | ---: | ---: |
| EUNE | `eun1` | 4,589 | 411 | 0 | 5,000 |
| EUW | `euw1` | 4,567 | 433 | 0 | 5,000 |

Both runs used routing `europe`, exact public patch `26.15`, queue 420, seed 42,
newest-first history traversal, and deterministic balanced round-robin sampling over
Gold, Platinum, Emerald, and Diamond divisions I-IV. The final external snapshots
are `accepted-4589` for EUNE and `accepted-4567` for EUW.

## Inventory contract

The EUW seal covers every regular file recursively below the logical external
`euw/` root. It has no exclusions, so the quiescent lock database and operational
logs are covered as content even though neither is committed. Empty directories are
not represented. The repository stores only aggregate values; it does not store or
print the file-level inventory, raw filenames, raw content, or identifiers.

The `sha256-relative-path-size-content-sha256-v1` algorithm sorts files by their
root-relative POSIX path and feeds this record for each file into an aggregate
SHA-256 digest:

```text
relative-path NUL decimal-byte-count NUL lowercase-content-sha256 LF
```

Absolute roots, drive letters, and timestamps are excluded. Running
`scripts/inventory_dataset.py` twice produced the same EUW result: 20,171 files,
2,767,335,433 bytes, and SHA-256
`04daf727e8f816ff95449edf2ea2a954f37c0a3062e6dafb81d83c4c9e806ba0`.

EUNE remains immutable. Its 20,031-file and 1,740,139,074-byte aggregates were
reverified, as were its 4,589 + 411 population counts and zero overlap. A full
content read remains blocked by the protected recovery subtree under the current
identity. Permissions were not weakened. Consequently, its
`37a9c2ab1def3e57da4ea56b198336d15774735a3017de9b57986a4bcd634538`
fingerprint is labeled as the previously recorded seal rather than a current full
recomputation. The accessible EUNE Stage 3 and lineage publications were hashed
read-only with the new content-tree algorithm.

Machine-readable records are in
`config/data_seals/patch26.15-10k-v1/`. They contain only logical paths, aggregate
counts, and hashes; external datasets, databases, credentials, identifiers, raw
filenames, logs, and player-level rows remain outside Git.
