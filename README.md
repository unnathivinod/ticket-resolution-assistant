# Intelligent Support Ticket Resolution Assistant

A support agent pastes a raw customer complaint and gets back:

1. **Triage**: category, product, severity and customer sentiment.
2. **Similar past tickets and knowledge-base articles**, found by meaning (semantic search), not just keywords.
3. **A drafted step-by-step resolution** written by a local LLM, with citations to the sources it used (RAG).

Built as small microservices for a telecom support desk. Runs fully on a laptop: no paid API, no API key.

> Status: work in progress. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design
> and [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) for the choices made and the evidence behind them.

## Run it

Requirements: Docker Desktop and [Ollama](https://ollama.com/download).

```bash
cp .env.example .env        # Windows PowerShell: copy .env.example .env
docker compose up -d --build
docker compose ps                                        # all services should show "healthy"
docker compose run --rm tools python scripts/seed.py     # load the data (about a minute)
```

Then open **http://localhost:8501**, paste a complaint and press **Resolve**.

| Service | URL |
|---|---|
| **Agent web page** | http://localhost:8501 |
| **Gateway API** (the only API a client needs, docs) | http://localhost:8000/docs |
| Qdrant dashboard | http://localhost:6333/dashboard |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |
| Embedding service (API docs) | http://localhost:8004/docs |
| Retrieval service (API docs) | http://localhost:8002/docs |
| Triage service (API docs) | http://localhost:8001/docs |
| Generation service (API docs) | http://localhost:8003/docs |

If a port is already used on your machine, change it in `.env` (for example `REDIS_PORT=6380`).

## Try it

**In the browser:** http://localhost:8501

**From the command line:**

```bash
docker compose run --rm tools python scripts/demo.py
docker compose run --rm tools python scripts/demo.py "I was charged twice this month"
```

**As an API call** (every request needs the `X-API-Key` header; the local key is in `.env`):

```bash
curl -X POST http://localhost:8000/v1/resolve \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"complaint": "My broadband drops every evening around 8"}'
```

You get the labels, the sources found, and the drafted resolution with a citation on every step.
With a small local model on a CPU the resolution takes up to a minute; asking the same thing again
is answered from the cache at once. If no model is running, the answer is quoted directly from the
best matching source, so the demo still works.

### What the gateway does on every request

| Step | What happens | If it goes wrong |
|---|---|---|
| 1. Check the caller | API key, then a per-key limit of 30 requests a minute | 401 or 429 |
| 2. Mask personal details | Emails, phone and account numbers are replaced before anything else sees them | |
| 3. Cache | A complaint already answered in the last hour is returned at once | Cache down: carry on without it |
| 4. Triage | Category, product, severity, sentiment | Triage down: answer without labels |
| 5. Search | Similar tickets and articles | Search down: 503, there is nothing to answer from |
| 6. Checkpoint | Nothing similar enough found: no answer is drafted, escalation is recommended | |
| 7. Draft | Cited, checked resolution | Drafting down: return the sources, recommend escalation |
| 8. Record | Request, answer and timings go to the `resolve_requests` table; agent feedback to `feedback` | Database down: still answer, count the error |

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

## Search quality (evals)

```bash
docker compose run --rm tools python evals/eval_retrieval.py                   # main table, about a minute
docker compose run --rm tools python evals/eval_retrieval.py --with-reranker   # adds the slow reranker row
docker compose run --rm tools python evals/eval_retrieval.py --diagnose        # experiments that explain the numbers
```

Runs the 360 held-out complaints through several search setups and prints a comparison table.
Results are saved in `evals/results/`. What the numbers showed, and the design changes they caused,
are written up in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

## Triage quality (evals)

```bash
docker compose run --rm tools python evals/eval_triage.py --calibrate   # tune the settings, save them, report
docker compose run --rm tools python evals/eval_triage.py               # report with the saved settings
```

Settings are tuned on one half of the eval complaints and the reported numbers come from the other
half. The eval also checks that complaints from brand-new classes and off-topic questions are
flagged as `unknown` instead of being forced into an existing class.

## Tests

```bash
docker compose run --rm tools pytest                  # fast tests, no services needed
docker compose run --rm tools pytest -m integration   # checks against the running services
```

## Build progress

- [x] Project skeleton and databases (Postgres, Qdrant, Redis)
- [x] Synthetic telecom dataset (tickets + KB articles)
- [x] Embedding service
- [x] Data loading and retrieval (hybrid search, tuned from eval results)
- [x] Triage service (category, product, severity, sentiment)
- [x] Generation service (RAG with citations, checked answers, fallback without a model)
- [x] Gateway (auth, rate limit, cache, checkpoints, audit log, feedback) and agent web page
- [ ] Evolving data and ticket classes
- [ ] Evals, monitoring, tests and CI
