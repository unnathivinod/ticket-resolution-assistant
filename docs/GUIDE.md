# Guide: using and testing every part

The [README](../README.md) gets the project running and shows the results. This page is the
reference behind it: every address, every command, and what each one shows.

| Section | What it covers |
|---|---|
| [Addresses](#addresses) | Where each service can be opened |
| [Three ways to send a complaint](#three-ways-to-send-a-complaint) | Browser, command line, API |
| [What the gateway does on every request](#what-the-gateway-does-on-every-request) | The eight steps and what happens when one fails |
| [Choosing the language model](#choosing-the-language-model) | Local model, hosted model, backup model |
| [New data and new ticket classes](#new-data-and-new-ticket-classes) | Adding tickets, articles and classes while it runs |
| [The dataset](#the-dataset) | What the data is and how it was made |
| [Evals](#evals) | The five measurement scripts |
| [Monitoring](#monitoring) | Health check, dashboard, alerts, logs |
| [Tests](#tests) | Fast tests and live tests |

## Addresses

| Service | URL |
|---|---|
| **Agent web page** (sign in with `priya` / `demo1234`) | http://localhost:8501 |
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

## Three ways to send a complaint

**In the browser:** http://localhost:8501, signed in with one of the
[demo accounts](#signing-in-roles-and-the-cases-page).

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

### The reply to the customer

Under the drafted fix the page has a **Draft reply to customer** button. The same as an API call, with the
`request_id` that `/v1/resolve` returned:

```bash
curl -X POST http://localhost:8000/v1/reply \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"request_id": "PASTE-THE-ID-HERE"}'
```

| What it does | Detail |
|---|---|
| Reads the earlier answer from the audit log | The caller sends only the ID, so the reply always matches what the system really answered |
| Uses checked steps only | A step that is not backed by its source, or that repeats what the customer already tried, is left out. `steps_left_out` says how many |
| Sets the tone from the labels | An apology when the sentiment is negative, a word about the impact when the severity is high or critical |
| Never invents a fix | With no usable step the reply says the case was passed to the specialist team |
| Keeps internal details out | Ticket and article IDs are removed from the text |
| Always returns something | First model, then the backup model, then a template with the steps filled in (`mode: template`) |

Counted in `gateway_replies_total` and `generation_replies_total`, by `llm` or `template`.

### Possible service incident

One complaint is one customer's problem. Several complaints that mean the same, arriving close together,
are probably one fault that affects many customers. Every complaint is therefore compared with the
complaints of the last 30 minutes. With three or more that mean the same, the page shows a
**Possible service incident** notice and the `PossibleIncident` alert fires.

```bash
# Six customers report the same outage in their own words (no language model is used).
docker compose run --rm tools python scripts/simulate_incident.py
docker compose run --rm tools python scripts/simulate_incident.py --area "T Nagar"
```

Then resolve the complaint the script prints, on the page or with `/v1/resolve`. The answer carries:

```json
"incident": {"detected": true, "similar_recent": 7, "needed": 3, "window_minutes": 30,
             "examples": [{"text": "No internet in ...", "minutes_ago": 2, "similarity": 0.91}]}
```

| Detail | How it works |
|---|---|
| What "the same" means | Close in meaning, by the same embedding model the search uses. Not the same category: one category holds many different faults |
| The same words twice | Count once. A retry, or the page's quick call followed by the full call, is still one customer |
| What is stored | The start of the masked complaint, in its own Qdrant collection, deleted after a day. It can never come back as a search result |
| If the check fails | The complaint is answered as usual, with `"incident": null` |
| A cached answer | The fix is reused, the incident check is done fresh |
| Test traffic | Send `"track_incident": false` so it is not counted (the evals do) |
| Settings | `INCIDENT_MIN_SIMILAR`, `INCIDENT_WINDOW_MINUTES`, `INCIDENT_MIN_SIMILARITY` in `.env`, then `docker compose up -d gateway` |

Counted in `gateway_incident_checks_total`, by `clear`, `flagged` or `failed`.

## Signing in, roles and the Cases page

The web page asks for a username and a password. Three demo accounts exist straight after setup.
They share one password, `demo1234`, which is public on purpose:

| Username | Role | Menu | Cases they see |
|---|---|---|---|
| `priya` | Agent | Resolve, Cases, Feedback | Only their own |
| `arun` | Second-line expert | the same, plus Record a fix | Everyone's, with a "Handled by" column |
| `meera` | Engineer | the same, plus Monitoring | Everyone's |

Under every drafted fix there are two buttons: **Mark as resolved** and **Escalate to second line**.
The assistant only suggests; the button records what the person really did. The **Cases** page lists
each complaint with both, marks the ones where they differ, and counts how often the suggestion was
followed. A case left open can be closed later from the Cases page.

Two things travel with a request, and they answer two different questions:

| Header | Answers | Who has it |
|---|---|---|
| `X-API-Key` | Which application is calling? | The web page, a script, an eval |
| `X-User-Token` | Which person is using it? | Only someone who signed in. Valid for eight hours |

Scripts and evals send no token and work as before. Their requests belong to nobody and are never
listed as cases.

```bash
# Sign in. The answer contains a token.
curl -X POST http://localhost:8000/v1/login \
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" \
  -d '{"username": "priya", "password": "demo1234"}'

# The cases this person may see (status: all, open, resolved, escalated; hours: how far back).
curl "http://localhost:8000/v1/cases?status=open&hours=24" \
  -H "X-API-Key: dev-local-key" -H "X-User-Token: <token>"

# Record how a case ended. <request_id> comes from the /v1/resolve answer.
curl -X POST http://localhost:8000/v1/cases/<request_id>/decision \
  -H "X-API-Key: dev-local-key" -H "X-User-Token: <token>" -H "Content-Type: application/json" \
  -d '{"decision": "resolved"}'
```

An agent who asks for another agent's case gets `404 No such case`, the same answer as for a case
that does not exist.

Accounts are added, given a new password, and switched off with a script (it asks for the password
and stores only a hash). Do this for the three demo accounts before real people use the system:

```bash
docker compose run --rm tools python scripts/add_user.py kavya agent "Kavya M"
docker compose run --rm tools python scripts/add_user.py priya --off
```

| Setting in `.env` | What it does |
|---|---|
| `TOKEN_SECRET` | Signs the session tokens. Use a long random value anywhere but a local demo |

A database that existed before this feature gets the new tables with
`docker compose run --rm tools python scripts/migrate.py` (safe to run twice). A new one has them from the start.

## What the gateway does on every request

| Step | What happens | If it goes wrong |
|---|---|---|
| 1. Check the caller | API key, then a per-key limit of 30 requests a minute. If a session token is sent, it must be valid | 401 or 429 |
| 2. Mask personal details | Emails, phone and account numbers are replaced before anything else sees them | |
| 3. Cache | A complaint already answered in the last hour is returned at once | Cache down: carry on without it |
| 4. Triage | Category, product, severity, sentiment | Triage down: answer without labels |
| 5. Search | Similar tickets and articles | Search down: 503, there is nothing to answer from |
| 6. Checkpoint | Nothing similar enough found: no answer is drafted, escalation is recommended | |
| 7. Draft | Cited, checked resolution | Drafting down: return the sources, recommend escalation |
| 8. Record | Request, answer, timings and the signed-in person go to the `resolve_requests` table; agent feedback to `feedback` | Database down: still answer, count the error |

## Choosing the language model

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
| An agent sees a wrong category | The Feedback page | Stored with the feedback. "None of the categories fits" feeds the discovery job |
| Someone wants to see what agents said | `GET /v1/feedback` | The latest ratings with totals. Complaints are shown with personal details already masked |
| Someone wants to see what experts recorded | `GET /v1/tickets/recorded` | Each recorded fix and whether it is searchable yet |
| Nobody noticed a new kind of problem yet | `python scripts/discover_classes.py` | Groups similar flagged complaints and proposes a class for a person to approve |

The same from a file, with a wait until it is searchable:

```bash
docker compose run --rm tools python scripts/add_document.py data/examples/kb_broken_router.json
docker compose run --rm tools python scripts/add_document.py --retire KB-900
```

These calls need an **admin key** (`ADMIN_API_KEYS` in `.env`). The agent key used by the web page
can read and give feedback but cannot change data.

How well this works is measured by `evals/eval_evolving.py` (see [Evals](#evals)).

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

## Evals

All numbers in the README come from these scripts. Results are saved in `evals/results/`. What
the numbers showed, and the design changes they caused, are in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).

### Search quality

```bash
docker compose run --rm tools python evals/eval_retrieval.py                   # main table, about a minute
docker compose run --rm tools python evals/eval_retrieval.py --with-reranker   # adds the slow reranker row
docker compose run --rm tools python evals/eval_retrieval.py --diagnose        # experiments that explain the numbers
```

Runs the 360 held-out complaints through several search setups and prints a comparison table.
Results are saved in `evals/results/`. What the numbers showed, and the design changes they caused,
are written up in [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).

### Triage quality

```bash
docker compose run --rm tools python evals/eval_triage.py --calibrate   # tune the settings, save them, report
docker compose run --rm tools python evals/eval_triage.py               # report with the saved settings
```

Settings are tuned on one half of the eval complaints and the reported numbers come from the other
half. The eval also checks that complaints from brand-new classes and off-topic questions are
flagged as `unknown` instead of being forced into an existing class.

### Answer quality

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

### A second "does it fit?" check

```bash
docker compose run --rm tools python evals/eval_relevance_gate.py   # about 3 minutes, no language model
```

Scores the best source for each of the 430 test complaints with a cross-encoder and reports how
many new-class and off-topic complaints it would stop, for a given share of answerable complaints
stopped by mistake. The check is off (`MIN_RELEVANCE=0`) unless this eval says it is worth its cost.

### Incident detection

```bash
docker compose run --rm tools python evals/eval_incidents.py   # about a minute, no language model
```

Builds half hours of complaints from the test set: some with seven customers reporting one problem among
twenty others, some with no such burst. For every pair of settings it reports how many incidents are
flagged and how many quiet half hours are flagged by mistake. The best setting is picked on one half of
the problems and reported on the other half, and the script prints the two `.env` lines to use it.

Result on this data: similarity 0.875 with three complaints flags 71% of the incidents and 2% of the quiet
half hours. Those are the settings in use.

### New data and new classes

```bash
docker compose run --rm tools python evals/eval_evolving.py
```

Adds the two held-back ticket classes step by step, measures how quickly search and triage pick
them up, then removes them again.

## Monitoring

```bash
docker compose run --rm tools python scripts/health_check.py
```

One command that checks every service, sends a test complaint and an off-topic question through
the gateway, checks that new data is not stuck, and lists any alert that is firing.

![The Grafana dashboard](images/dashboard.png)

| Where | What you see |
|---|---|
| http://localhost:3000 | One dashboard with 28 panels in five rows: up and fast enough, answer quality, drift, language model and cache, new data |
| http://localhost:9090/alerts | 19 alert rules, each with what is wrong and what to do first |
| `docker compose logs gateway` | One JSON line per request. The same `request_id` appears in every service the request touched |

What the alerts watch, in plain words:

| Question | Examples |
|---|---|
| Is it up and fast? | a service is down, more than 5% of requests fail, answers take over two minutes |
| Is someone guessing passwords? | more than 20 failed sign-ins in ten minutes |
| Are the answers still good? | the model is not being used, steps fail the source check, agents say "not helpful", agents keep correcting the category |
| Has the world changed? | complaints are no longer similar to anything indexed, too many escalations, agents say "none of the categories fits", several customers report the same fault within minutes |
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
