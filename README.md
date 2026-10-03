# Intelligent Support Ticket Resolution Assistant

A support agent pastes a raw customer complaint and gets back:

1. **Triage**: category, product, severity and customer sentiment.
2. **Similar past tickets and knowledge-base articles**, found by meaning (semantic search), not just keywords.
3. **A drafted step-by-step resolution** written by a local LLM, with citations to the sources it used (RAG).

Built as small microservices for a telecom support desk. Runs fully on a laptop: no paid API, no API key.

> Status: work in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Run it

Requirements: Docker Desktop and [Ollama](https://ollama.com/download).

```bash
cp .env.example .env        # Windows PowerShell: copy .env.example .env
docker compose up -d
docker compose ps           # all services should show "healthy"
```

| Service | URL |
|---|---|
| Qdrant dashboard | http://localhost:6333/dashboard |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

If a port is already used on your machine, change it in `.env` (for example `REDIS_PORT=6380`).

## The dataset

Synthetic telecom support data, built from 40 hand-written issue scenarios in `data/scenarios/`.
The generated files are already in `data/generated/`, so this step is optional:

```bash
docker compose run --rm tools python scripts/generate_data.py   # same seed = same files
docker compose run --rm tools pytest                            # data quality checks
```

| File | Rows | What it is |
|---|---|---|
| `tickets.jsonl` | 1,440 | Past resolved tickets (36 scenarios x 40). These get indexed. |
| `test_queries.jsonl` | 360 | Held-out complaints in **different wording**, used only for evals. |
| `kb_articles.jsonl` | 41 | One knowledge-base article per scenario, plus 5 general ones. |
| `out_of_scope.jsonl` | 30 | Non-telecom questions the system must refuse. |
| `holdout_*.jsonl` | 160 / 40 / 4 | Two brand-new ticket classes, kept out of the index to demo evolving classes. |

Every row keeps its `scenario_id`, which is the answer key for the retrieval evals.
Test wordings never appear in the indexed tickets (checked by a test), so the evals are not cheating.

## Build progress

- [x] Project skeleton and databases (Postgres, Qdrant, Redis)
- [x] Synthetic telecom dataset (tickets + KB articles)
- [ ] Embedding service
- [ ] Ingestion and retrieval (hybrid search + reranker)
- [ ] Triage service
- [ ] Generation service (RAG with citations)
- [ ] Gateway and agent UI
- [ ] Evolving data and ticket classes
- [ ] Evals, monitoring, tests and CI
