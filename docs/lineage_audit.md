# Retained population lineage audit

This audit covers retained run `20260722T125547567196Z-population`. It was produced
offline from the ignored Stage 2 checkpoint/manifest and immutable Stage
3.1/3.2/3.3A/3.3B outputs. It made no Riot request and did not load `.env`.

## Exact field trace

| Field | Origin and scope | Preserved through | Exact loss point | Retained recovery |
| --- | --- | --- | --- | --- |
| `platform_id` | Match-V5/config; match and run | Normalization and every Stage 3 table | Not lost | `eun1`, observed for all 100 matches |
| `regional_routing` | Route table; collection run | Stage 2 checkpoint and manifest | Stage 3.1 schema did not emit it | `europe`, collection-run context |
| `analysis_region` | Route table; run/storage partition | Checkpoint, manifest `routing_region`, catalog, normalized `region=eune` partition | Stage 3.1 validated the partition but did not emit it | `eune`, deterministic collection context |
| collection tier/division/stratum | Configured League-V4 schedule; discovery seed | Checkpoint player record and `matches[*].sources` | Stage 3.1 approval map kept match ID/patch only | All 100 retained match-source contexts |
| match-to-seed relation | History query by seed PUUID | Checkpoint `matches[*].sources` | Stage 3.1 did not transform it | Project-scoped seed pseudonym for all 100 |
| participant-specific Solo/Duo rank | Stored League-V4 ladder response; that player only | `sampling.candidates` | Never joined into Stage 3.1 | 104/1,000 participant-match rows, 15 unique players |
| `rank_observed_at` | League response time | Not recorded by checkpoint v3 | Stage 2 collection | `not_collected` for all retained observations |
| discovery timestamp | Match-ID history discovery event | Not recorded by checkpoint v3 | Stage 2 collection | `not_collected` for all retained contexts |
| multiple discovery contexts | Multiple seed histories | Checkpoint source list | Stage 3.1 omitted source list | Preserve every unique context, sorted |

Stage 3.2 copied only approved Stage 3.1 context. Stage 3.3A consequently set region,
rank bracket, and collection stratum to unavailable. Stage 3.3B grouped on those
explicit unavailable statuses; it did not independently lose values.

## Retained-corpus findings

- 100 accepted unique matches and 1,000 canonical participant rows reconcile.
- 100 match-to-seed source relationships were retained.
- Every accepted match has exactly one retained discovery context; no retained
  accepted match was found through multiple seeds or strata.
- 11 collection strata contributed.
- All 3,075 stored League-V4 candidate entries were checked. They represent 3,074
  unique candidate players.
- 104 canonical participant-match rows join a stored Ranked Solo/Duo entry and are
  `observed`; 896 are `not_collected`.
- Those 104 rows represent 15 unique observed players. No joined player has
  conflicting retained rank observations.
- No rank observation timestamp or match-discovery timestamp is recoverable.

The sampler's configured stratum is not promoted to observed participant rank, and
the seed's rank is never copied to the other nine participants or called a match
rank.

## Versioned repair

The output is:

```text
data/processed/lineage/schema=lineage-v1/
  run=20260722T125547567196Z-population/
    match_discovery_lineage.jsonl
    participant_rank_lineage.jsonl
    lineage_audit_report.json
    metadata.json
```

The migration records SHA-256 for the checkpoint, manifest, and every immutable
Stage 3 input. Publication is staged and atomic. Identical reruns are byte-equivalent.
Failed validation and `--validate-only` publish nothing.

The two JSONL tables contain only existing project-scoped pseudonyms, never Riot IDs,
names, raw PUUIDs, summoner IDs, or encrypted identifiers. The JSON audit and
metadata are aggregate-only and contain no player keys.

## Forward policy

Checkpoint schema 4 stores collection context separately from directly observed
rank, plus rank/discovery timestamps and sources, platform/routing/analysis region,
pseudonymous seed identity, and every match-to-seed relationship. Conflicts are
retained as multiple observations/contexts and marked `ambiguous`; missing facts use
`not_collected` or `unavailable`. Analytics continues to use one match and one
participant-match row regardless of discovery-path count.
