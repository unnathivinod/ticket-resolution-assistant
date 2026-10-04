# Intelligent Support Ticket Resolution Assistant

A support agent pastes a raw customer complaint and gets back:

1. **Triage**: category, product, severity and customer sentiment.
2. **Similar past tickets and knowledge-base articles**, found by meaning (semantic search), not just keywords.
3. **A drafted step-by-step resolution** written by a local LLM, with citations to the sources it used (RAG).

Built as small microservices for a telecom support desk. Runs fully on a laptop: no paid API, no API key.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design, the diagrams, and every measured result.
- [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md): each choice, the alternative, and the measurement behind it.

![The agent web page](docs/images/web-page.png)

## What was measured

All numbers come from the scripts in `evals/`, run on a laptop CPU with `bge-small-en-v1.5` and `llama3.2:3b`.

| Question | Result |
|---|---|
| Does semantic search beat keyword search? | The right past ticket comes first for 55% of complaints, against 40% for keywords. Test complaints use wording the index has never seen. |
| How good are the labels? | Category 71%, product 78%, sentiment 83%, severity 59% exact and 93% within one level. |
| How fast does new data arrive? | A new ticket is searchable a few seconds after one API call. No retraining, no restart. |
| Can it learn a new kind of problem? | Search: yes, from a handful of tickets. Category: well for one new class (18 of 20), badly for one that overlaps existing classes (1 of 20). |
| Are the drafted answers right? | 70% cite the right problem. Most misses are search misses, not model mistakes. |
| Does it refuse what it cannot answer? | Off-topic questions: 77% stopped. Telecom problems it has no fix for: **no**, it drafts a confident wrong answer. A person must review every draft. |
| How long does an answer take? | Labels and sources in under a second, the drafted answer in about 31 seconds. |

The weak spots are written up as plainly as the strong ones, in section 13 of the architecture document.

The same 56 complaints were then run through a larger hosted model (`openai/gpt-oss-20b` on Groq,
optional, with the local model as its backup):

| On the same complaints | Local `llama3.2:3b` | Hosted `gpt-oss-20b` |
|---|---|---|
| Known problems, right answer (of 20) | 11, plus 3 mixed with another problem | 15 |
| Known problems, wrong answer | 6 | 3 |
| When the search had found the right source (15 of 20) | 11 right, 3 mixed, 1 wrong | 15 of 15 right |
| When the search had missed it (5 of 20) | 5 wrong answers | 3 wrong, 2 refused |
| Problems with no fix in the knowledge base (6) | 6 wrong answers | 3 wrong, 3 refused |
| Off-topic questions that reached the model (7 of 30) | 7 answered | 7 refused |
| Typical time per drafted answer | 31 s | 1.4 s |

What it shows: with the larger model, every remaining wrong answer on known problems is a search
miss. The model is no longer the weak link; the search is. One run each, small groups: read the
table as a direction, not as exact rates.

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

### Choosing the language model

Out of the box the answers are written by `llama3.2:3b` running in Ollama on your machine: no key,
no cost, about half a minute per answer on a laptop CPU.

The model is a setting, not code. Any provider with an OpenAI-compatible API works, and a **backup
model** can be named that is asked only when the first one fails:

```
first model (for example a hosted one, a few seconds)
   └─ unreachable, rate limited, or two unusable replies
        └─ backup model (for example local Ollama)
             └─ fails too
                  └─ the steps are quoted from the best matching source, no model
```

To put a hosted model first, copy the commented block in `.env.example` into `.env`, paste your
own key and run `docker compose up -d`. The top bar of the web page shows which model wrote each
answer, and says so when the backup had to step in. With no key nothing changes: Ollama answers.

What leaves the machine with a hosted model: the complaint (personal details already masked) and
the three sources shown to the model. Nothing else.

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

## When a complaint is new

No knowledge base covers everything. What matters is what the system does when it has no fix,
and that it learns from the case. Every step of that loop is in this project:

| Step | What should happen | Where it is |
|---|---|---|
| 1. Notice | Realise that nothing known really fits | Similarity cut-off in the gateway (stops 77% of off-topic questions). Measured weak spot: a new telecom problem that looks like an old one gets through. A second check is built and measured by `evals/eval_relevance_gate.py`. |
| 2. Do not guess | Hand it to an expert instead of inventing a fix | The gateway escalates, drafts nothing, and still shows the closest sources |
| 3. Human safety net | A person reviews every draft | The page shows each step's sources and similarity. The agent can mark "not helpful" or "none of the categories fits". |
| 4. Learn | The expert's fix goes into the system | "Record the real fix" form on the page, or `POST /v1/tickets`. Searchable in seconds, nothing retrained. |
| 5. Spot a trend | Many similar unknown complaints mean a new kind of problem | `scripts/discover_classes.py` groups them and proposes a class for a person to approve |
| 6. Raise an alarm | Tell someone the world has changed | Alerts for falling similarity, rising escalations and "none of the categories fits" |

**new complaint → escalate → expert solves it → system learns it → the next customer gets the answer**

To see it: ask about something the knowledge base does not cover, open "Second-line support:
record the real fix" under the answer, save the fix, and press Resolve again.

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

The same from a file, with a wait until it is searchable:

```bash
docker compose run --rm tools python scripts/add_document.py data/examples/kb_broken_router.json
docker compose run --rm tools python scripts/add_document.py --retire KB-900
```

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

## Answer quality (evals)

```bash
docker compose run --rm tools python evals/eval_answers.py               # about 35 minutes on a CPU
docker compose run --rm tools python evals/eval_answers.py --answers 5   # a quicker look
```

Sends complaints through the gateway exactly as an agent would and checks the final answers
against the answer key in the dataset: does the answer cite the right problem, is every step
backed by its source, does it avoid repeating what the customer already tried, are off-topic
questions refused, and what happens with problems the knowledge base does not cover.
It also measures the "nothing similar enough" cut-off over all 430 test complaints.

One experiment is kept behind a setting: `LLM_MATCH_CHECK=true` makes the model confirm that the
best source is about the same problem before it writes any step. With `llama3.2:3b` it then refused
every complaint, so it is off. It is there to be tried again with a larger model.

With a hosted model on a free plan, add `--pause 20` so the run stays under the per-minute limit.
Each model writes its own result file (`answers_v2.md`, `answers_v2_openai-gpt-oss-20b.md`).

## A second "does it fit?" check (evals)

```bash
docker compose run --rm tools python evals/eval_relevance_gate.py   # about 3 minutes, no language model
```

Scores the best source for each of the 430 test complaints with a cross-encoder and reports how
many new-class and off-topic complaints it would stop, for a given share of answerable complaints
stopped by mistake. The check is off (`MIN_RELEVANCE=0`) unless this eval says it is worth its cost.

## Is it healthy? (monitoring)

```bash
docker compose run --rm tools python scripts/health_check.py
```

One command that checks every service, sends a test complaint and an off-topic question through
the gateway, checks that new data is not stuck, and lists any alert that is firing.

![The Grafana dashboard](docs/images/dashboard.png)

| Where | What you see |
|---|---|
| http://localhost:3000 | One dashboard with 25 panels in five rows: up and fast enough, answer quality, drift, language model and cache, new data |
| http://localhost:9090/alerts | 17 alert rules, each with what is wrong and what to do first |
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

## What is in the box

| Part | What it does |
|---|---|
| Gateway | API keys, rate limit, PII masking, cache, checkpoints, audit log, feedback, data and class endpoints |
| Triage | Category, product, severity, sentiment, with reasons, and "unknown" when unsure |
| Retrieval | Hybrid semantic and keyword search over tickets and knowledge-base articles |
| Generation | Cited answer drafted by an LLM (local by default, hosted optional, with a backup model), checked against its sources, with a no-model fallback |
| Embedding | The small models, in one place |
| Ingestion worker | New and edited documents reach the search index in seconds, with retries and a safety sweep |
| Web page | What the support agent uses |
| Monitoring | Prometheus with 17 tested alert rules, a Grafana dashboard, a one-command health check |
| Evals | Search, labels, new data and classes, final answers |
| Tests and CI | 252 fast tests, 21 live tests, GitHub Actions on every push |
