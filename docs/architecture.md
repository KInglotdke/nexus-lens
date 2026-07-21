# Stage 0 architecture

Nexus Lens begins with a deliberately narrow vertical slice. It proves that a Riot
ID can be resolved and that recent Match-V5 payloads can be collected reliably
enough for later product discovery. It does not define the recommendation engine.

## Data flow

```text
.env configuration
       |
       v
feasibility_collect.py
       |
       v
FeasibilityCollector ---> RiotClient ---> Account-V1 / Match-V5
       |
       v
data/raw/<timestamp>/
  manifest.json
  matches/<match-id>.json
```

The client owns HTTP transport and API error translation. The collector owns the
experiment workflow and persistence. Pydantic schemas validate only the stable
fields needed to confirm that useful match records arrived; additional Riot fields
are retained in raw snapshots for inspection.

## Boundaries and later decisions

- Raw snapshots are local experiment artifacts and are excluded from Git.
- No database, feature extraction, ranking, champion recommendation, user-facing
  API, or UI is included at this stage.
- Retries, rate-limit coordination, routing across multiple regions, storage
  migrations, and production observability are deferred until feasibility evidence
  justifies them.
- `data/processed` is reserved for later exploratory transformations.
- `data/snapshots` is reserved for versioned, reproducible analytical fixtures;
  Stage 0 writes live responses only to `data/raw`.
