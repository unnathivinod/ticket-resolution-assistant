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

Updating a copy that already has data? Run `docker compose run --rm tools python scripts/migrate.py`
once after pulling, to add the newest columns to the database. It is safe to run again.

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
| Ingestion worker (metrics only) | http://localhost:8005/metrics |
| **Grafana dashboard** (no login needed to look) | http://localhost:3000 |
| Prometheus (numbers and alerts) | http://localhost:9090/alerts |

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

## New data and new ticket classes

Tickets, articles and ticket classes change while the system is running. Nothing is retrained
and nothing is restarted.

```bash
# Add a resolved ticket. It is searchable about a second later.
curl -X POST http://localhost:8000/v1/tickets \
  -H "X-API-Key: dev-admin-key" -H "Content-Type: application/json" \
  -d '{"subject": "Router overheats", "description": "The router gets very hot and restarts.",
       "resolution_steps": ["Move it to an open shelf.", "Replace it if it still overheats."],
       "category": "device_hardware", "product": "broadband"}'
```

| What changes | How | What happens |
|---|---|---|
| A new resolved ticket | `POST /v1/tickets` | Saved in PostgreSQL, a note goes on a queue, the ingestion worker adds it to the search index |
| An article is edited | `PUT /v1/kb/{id}` | Same path. The new version replaces the old one in the index, with no gap |
| A fix is outdated | `DELETE /v1/documents/{id}` | Hidden from search, kept in PostgreSQL so old answers can still be traced |
| A new ticket class | `POST /v1/taxonomy` | Classes are rows in a table. Triage learns the class from the tickets labelled with it |
| An agent sees a wrong category | Dropdown under the answer | Stored with the feedback. "None of the categories fits" feeds the discovery job |
| Nobody noticed a new kind of problem yet | `python scripts/discover_classes.py` | Groups similar flagged complaints and proposes a class for a person to approve |

These calls need an **admin key** (`ADMIN_API_KEYS` in `.env`). The agent key used by the web page
can read and give feedback but cannot change data.

Measure it (adds the two held-back classes step by step, then removes them again):

```bash
docker compose run --rm tools python evals/eval_evolving.py
```

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

## Is it healthy? (monitoring)

```bash
docker compose run --rm tools python scripts/health_check.py
```

One command that checks every service, sends a test complaint and an off-topic question through
the gateway, checks that new data is not stuck, and lists any alert that is firing.

| Where | What you see |
|---|---|
| http://localhost:3000 | One dashboard with 25 panels in five rows: up and fast enough, answer quality, drift, language model and cache, new data |
| http://localhost:9090/alerts | 16 alert rules, each with what is wrong and what to do first |
| `docker compose logs gateway` | One JSON line per request. The same `request_id` appears in every service the request touched |

What the alerts watch, in plain words:

| Question | Examples |
|---|---|
| Is it up and fast? | a service is down, more than 5% of requests fail, answers take over two minutes |
| Are the answers still good? | the model is not being used, steps fail the source check, agents say "not helpful", agents keep correcting the category |
| Has the world changed? | complaints are no longer similar to anything indexed, too many escalations, agents say "none of the categories fits" |
| Is new data arriving? | the queue is growing, nothing indexed for 10 minutes, a document was given up on |

The alert rules have their own tests (made-up numbers in, expected alerts out):

```bash
docker compose run --rm --entrypoint promtool prometheus test rules /etc/prometheus/alerts_test.yml
```

Every push to GitHub runs the code style check, the fast tests and these config checks
(`.github/workflows/ci.yml`).

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
- [x] Evolving data and ticket classes (ingestion worker, class management, discovery, eval)
- [x] Monitoring (Prometheus, alert rules with tests, Grafana dashboard, health check) and CI
- [ ] End-to-end answer quality eval, final documentation
