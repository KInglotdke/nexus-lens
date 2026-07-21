# Nexus Lens

Nexus Lens is currently a **Stage 0 feasibility prototype**. This repository does
one small experiment: given a Riot ID and a development API key, can we resolve the
account, retrieve a short list of recent League of Legends matches, validate a few
stable fields, and preserve the raw payloads for inspection?

The output is evidence for deciding what to build next. It is not a complete
recommendation system, and it intentionally contains no scoring model, database,
production service, or user interface.

## Feasibility experiment

The collection script performs this path:

1. Load a Riot API key, Riot ID, routing region, and match count from `.env`.
2. Resolve the Riot ID through Account-V1 to obtain a PUUID.
3. Fetch recent match IDs and Match-V5 payloads.
4. Write a timestamped directory under `data/raw` with a manifest and one JSON file
   per match.

Stage 0 is successful when a developer can run that path for a consenting test
account, inspect complete raw match payloads, and identify whether the available
fields are sufficient for a later recommendation hypothesis. API reliability,
rate-limit behavior, missing fields, and regional assumptions should be recorded
as experiment findings rather than hidden behind production abstractions.

## Setup

Python 3.12 is required.

```bash
python -m venv .venv
# Activate the environment using the command appropriate for your shell.
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env`, then replace the example values with a Riot
development API key and a test Riot ID. Riot API keys and collected match data must
remain local; both are ignored by Git.

Run the experiment:

```bash
python scripts/feasibility_collect.py
# Or override the configured sample size:
python scripts/feasibility_collect.py --count 10
```

Run the checks:

```bash
pytest
ruff check .
```

See [docs/architecture.md](docs/architecture.md) for component boundaries and
explicitly deferred work.
