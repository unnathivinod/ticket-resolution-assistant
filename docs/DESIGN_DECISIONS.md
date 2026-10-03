# Design decisions

Each decision lists what was chosen, what else was considered, and why.
Where a decision was changed by a measurement, the numbers are shown.

## Retrieval: what the evals changed

The first design was "hybrid search + reranker", the common default for RAG systems.
We measured it before building on top of it, and the measurements changed the design.

### Measurement 1: four search setups

360 held-out test complaints, embedding model `BAAI/bge-small-en-v1.5`.
The test complaints deliberately use wording that never appears in the indexed tickets.

| Setup | Hit@1 | Precision@5 | MRR@10 | KB Hit@1 | KB Recall@3 | p50 ms |
|---|---|---|---|---|---|---|
| Keyword only (BM25) | 0.397 | 0.350 | 0.475 | 0.222 | 0.408 | 34 |
| Dense only (meaning) | 0.542 | 0.497 | 0.650 | 0.450 | 0.733 | 38 |
| Hybrid, equal weights | 0.475 | 0.452 | 0.606 | 0.400 | 0.667 | 39 |
| Hybrid, equal weights + reranker | 0.458 | 0.459 | 0.577 | 0.408 | 0.669 | 949 |

What this showed:

1. **Semantic search beats keyword search** (Hit@1 0.54 vs 0.40, KB Recall@3 0.73 vs 0.41).
   This is the core claim of the project, now backed by numbers.
2. **Equal-weight hybrid was worse than dense alone.** When the wording is different, the keyword
   half mostly contributes wrong candidates, and equal weighting lets them push good ones down.
3. **The reranker did not improve accuracy and made search about 25 times slower.**
   The reranker (`ms-marco-MiniLM-L-6-v2`) is trained to match a short question to a passage that
   answers it. Our task is different: matching one complaint to another complaint.

### Measurement 2: why is Hit@1 only 0.54?

Hypothesis: the extra sentences in a complaint (greeting, what was tried, impact, tone) blur the search.
Test: search with only the one sentence that describes the fault ("core problem only").

| Setup | Hit@1 | Hit@3 | MRR@10 | KB Hit@1 | KB Recall@3 |
|---|---|---|---|---|---|
| Dense, full complaint | 0.542 | 0.714 | 0.650 | 0.450 | 0.733 |
| Dense, core problem only | 0.542 | 0.753 | 0.660 | 0.569 | 0.772 |
| Keyword, core problem only | 0.147 | 0.208 | 0.187 | 0.189 | 0.358 |
| Hybrid, dense counts 3x, full complaint | 0.550 | 0.711 | 0.654 | 0.478 | 0.714 |

What this showed:

1. **The hypothesis was wrong for tickets.** Removing the extra sentences did not change Hit@1 at all.
   It helped only for knowledge-base articles (KB Hit@1 0.45 to 0.57).
2. **The extra sentences carry useful signal.** Keyword search drops from 0.40 to 0.15 without them,
   because "what the customer already tried" is itself a clue to the problem.
3. **The limit is the embedding model on hard paraphrases**, not noise in the query. Examples of what
   it has to match: "Everything crawls from sunrise to bedtime" with "My broadband is very slow all day".
4. **Weighting dense 3x in hybrid recovers dense-level accuracy** (0.550 vs 0.542; with 360 queries a
   difference this small is within noise) while keeping keyword matching for exact codes such as `E-102`.

### Decisions taken

| Decision | Chosen | Why |
|---|---|---|
| Search mode | Hybrid, dense weighted 3x | Same accuracy as dense alone, and still finds exact codes and plan names |
| Reranker | Off by default, still available per request | No accuracy gain on this data for about 900 ms extra per search |
| Query focusing step | Not built | Measured upper bound showed no gain for tickets, so it would add latency for nothing |
| "No confident match" signal | Cosine similarity of the closest result | The fusion score is rank-based: the top result always scores about the same, even for nonsense queries |
| Quality bar | Report Context hit and Category@1 next to Hit@1 | They measure what the next stages actually use: the 5 sources given to the LLM, and the category triage votes on |

### Known limits and next experiments

- Hit@1 of about 0.55 means the single best ticket is right only a bit more than half the time.
  The system does not depend on that: it passes 3 tickets and 2 articles to the LLM, cites sources,
  and refuses when the closest match is weak.
- The test set is the hardest case by design (no shared wording). Real traffic would be a mix of
  easy and hard wording, so real accuracy would be higher than these numbers and lower than a
  same-wording test.
- Next experiments, in order of expected value:
  1. A stronger embedding model (set `EMBEDDING_DENSE_MODEL`, rebuild, reseed, rerun the eval).
  2. Asking the LLM to restate the complaint in plain technical language before searching.
  3. A reranker trained for sentence similarity instead of question answering.

## Triage: what the evals showed

Triage labels a complaint by letting the most similar past tickets vote (category, product), and by
matching sentences against a short list of example sentences for urgency and tone (severity, sentiment).
Its settings are tuned on one half of the eval complaints and reported on the other half.

First measurement, on 215 test-half complaints, embedding model `BAAI/bge-small-en-v1.5`:

| Metric | Value |
|---|---|
| Category accuracy ('unknown' counts as wrong) | 0.711 |
| Category accuracy, best guess | 0.756 |
| Product accuracy | 0.783 |
| Severity accuracy, exact level | 0.489 |
| Severity within one level | 0.894 |
| Sentiment accuracy | 0.828 |
| Known complaints flagged unknown | 0.078 |
| Off-topic questions flagged unknown | 0.867 |
| New-class complaints flagged unknown | 0.050 |

Similarity of the closest past ticket, per group:

| Group | 25% | middle | 75% |
|---|---|---|---|
| Known classes | 0.798 | 0.827 | 0.853 |
| New classes (eSIM, fraud) | 0.792 | 0.831 | 0.846 |
| Off-topic questions | 0.559 | 0.624 | 0.702 |

What this showed:

1. **Voting over 25 neighbours beats trusting the single closest ticket** for category
   (0.756 vs 0.656 for the top search result alone).
2. **Sentiment from example sentences works well** (0.83) even though the test complaints use
   tone phrases the examples do not contain.
3. **The 'unknown' rule catches off-topic questions (87%) but not new problem types (5%).**
   A complaint about a new class is still a telecom complaint, so its closest past ticket is just as
   similar as for a known class (middle value 0.83 in both groups). A similarity threshold cannot
   separate them. This is an honest limit of the approach, not a tuning problem.
4. **Exact severity is the weakest label (0.49)**, although it is within one level 89% of the time.
   Severity depends on two things being right at once: the baseline for the problem type and the
   urgency signal. The eval now scores the two parts separately so the weaker one can be fixed.
   Follow-up: with the two parts scored separately, the baseline settings (how many similar tickets,
   and which point of their severities) were added to the tuning. On the test half, exact severity
   rose from 0.489 to **0.594** and within one level from 0.894 to **0.933**. The parts: baseline
   0.706, urgency signal 0.678. Severity stays the weakest label.

Decisions taken:

| Decision | Chosen | Why |
|---|---|---|
| Category and product | Similarity-weighted vote of the nearest past tickets | No training step, new classes work as soon as labelled tickets exist, a few milliseconds per request |
| Severity and sentiment | Example sentences per signal, matched by meaning, kept in a YAML file | Catches new wordings, every decision comes with a reason, support staff can edit the examples |
| Thresholds | Fitted by `eval_triage.py --calibrate` on a dev half, reported on a test half | Avoids hand-picked numbers and avoids grading on the data used for tuning |
| Detecting new classes | Not by a per-request threshold. Use agent corrections plus a clustering job over recent tickets | Measurement 3 above: a threshold cannot see them |

## Generation: drafting an answer that can be trusted

The model used is `llama3.2:3b` through Ollama: free, runs on a laptop, no API key. It is a small
model, so the design assumes it will make mistakes and checks its work instead of trusting it.

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Output format | The model must return JSON that follows a schema, and a citation can only be one of the source IDs it was shown | Free text with "[1]" style references | The reply is always machine-readable, and an invented source ID is impossible, not just unlikely. |
| Checking the answer | Each step is compared (by meaning) with the lines of the source it cites. Low match: the step is marked "not verified". No real citation: the step is dropped. | Trust the model, or ask a second model to judge | A citation only proves the model pointed at a source, not that the source says so. This check costs milliseconds; a second model call would cost another minute. |
| "Already tried" | The model lists what the customer already did, and any step that repeats it is flagged | Leave it to the prompt | The brief's example ("already restarted the router twice") is exactly where a small model slips. |
| When the model is down, slow or returns rubbish | Quote the resolution steps of the best matching source, clearly labelled as quoted | Return an error | The system always returns something useful, and it runs for a reviewer with no model installed. |
| Duplicate sources | Sources that say the same thing are grouped, the model sees one of each, citations credit all of them | Send all five | Shorter prompt (faster on a CPU) and less repeated text for the model to get lost in. |
| Prompt injection | Complaint and sources are fenced off as data, and the prompt says to ignore instructions inside them | Nothing | A complaint is untrusted text typed by anyone. |
| Provider | Any OpenAI-compatible API, set by three environment variables | Code against Ollama directly | Moving to a hosted model in production is a configuration change, not a code change. |
| Prompt versioning | `PROMPT_VERSION` is stored with every answer | Not tracked | A drop in quality can be tied to the prompt change that caused it. |

The first real answers changed the prompt (v1 to v2): the model wrote the source ID inside the step
text, its summary only repeated the complaint, and it padded the answer with a step from a less
relevant source. v2 tells it not to, and the code strips a trailing ID as a second layer.

Measured on a laptop with no GPU: about 63 seconds per answer. That is why the gateway caches
answers and why the page shows labels and sources first. Answer quality has not been scored yet;
that needs its own eval (groundedness, refusal on off-topic questions) and is the next thing to measure.

### Measured: the final answers (prompt v2)

`evals/eval_answers.py`, model `llama3.2:3b`, through the gateway. Because every ticket and article
in the dataset belongs to a scenario, a citation can be checked against the scenario the complaint
was written from, with no human grading.

| Group | Complaints | Cites the right problem | Right, mixed with another | Cites a wrong problem | Escalated, no answer |
|---|---|---|---|---|---|
| Known problems | 20 | 11 | 3 | 6 | 0 |
| New class (no fix exists) | 6 | 0 | 0 | 6 | 0 |
| Off topic | 30 | 0 | 0 | 7 | 23 |

The "nothing similar enough" cut-off, over all 430 test complaints (share stopped):

| Cut-off | Off-topic (want high) | Known complaints (want low) | New-class complaints |
|---|---|---|---|
| 0.60 | 0.433 | 0.003 | 0.000 |
| 0.70 | 0.733 | 0.006 | 0.000 |
| 0.72 (in use) | 0.767 | 0.014 | 0.000 |
| 0.75 | 0.867 | 0.042 | 0.050 |
| 0.80 | 1.000 | 0.250 | 0.275 |

What this showed:

1. **70% of answers to known problems cite the right problem** (14 of 20). Of the 6 wrong ones,
   5 were search misses (no source about the right problem was among the five handed to the
   model) and 1 was the model choosing badly. When the search found the right source, the model
   used it 14 times out of 15. **Search is the bottleneck, not the model.**
2. **"Every step is backed by its source" was 100%, and that is not the same as correct.** The
   model copies steps faithfully, including from a source about the wrong problem. The check
   catches invented steps. It cannot catch a real step that answers a different question.
3. **The model never said "I do not know".** It drafted a fix for all 6 new-class complaints and for
   all 7 off-topic questions that got past the cut-off. The prompt rule "escalate if the sources do
   not cover the problem" was ignored 13 times out of 13.
4. **The cut-off stops what is clearly unrelated and nothing else.** At 0.72 it stops 77% of
   off-topic questions and 1.4% of real complaints. Raising it to 0.80 would stop all off-topic
   questions but also a quarter of real complaints. It cannot help with new classes at all:
   their closest match (0.83) looks exactly like a known complaint's.
5. **"Already tried" works**: the answer listed what the customer tried in 11 of 11 complaints,
   and repeated it as a step once.
6. **A typical answer takes 31 seconds** on a laptop CPU (slowest 44).

### Measured: making the model say "I do not know" (prompt v3, rejected)

Because of finding 3, the decision was made part of the answer format: before writing any step
the model had to name the customer's problem, name the best source's problem, and say whether
they are the same. If it said no, the code removed the steps and escalated. Same eval, same
complaints:

| Group | Prompt v2: answer drafted | Prompt v3: answer drafted |
|---|---|---|
| Known problems (20) | 20, of which 14 cite the right problem | **0** |
| New class, no fix exists (6) | 6, all wrong | 0 |
| Off topic, past the cut-off (7) | 7, all wrong | 0 |

**The model went from never refusing to always refusing.** It said "not the same problem" for all
33 complaints it was asked about, including the 14 it answers correctly without the check. So it
is not judging the match at all: asked to decide, a 3-billion-parameter model picks the safe
answer every time.

Decision: v2 stays the default. The check is kept behind a setting (`LLM_MATCH_CHECK`, off) so
it can be tried again with a larger model, and the eval that rejected it is the eval that would
accept it. The honest state of the system is therefore:

- it is useful when the knowledge base covers the problem (70% of answers cite the right one),
- it does not know when it does not know, so **a person must review every answer**. The page is
  built for that: it shows the sources, their similarity, and what the answer cites.

What would fix it properly, in order of expected value: a larger model for the match decision
only (one short call), a reranker used as a relevance gate with a threshold taken from this eval,
and better search (5 of the 6 wrong answers were search misses).

## Gateway: one front door

The agent web page and any other client only ever talk to the gateway. It runs the steps in order
(triage, search, draft) and owns everything that is not "AI": who may call, how often, caching,
the audit log and feedback.

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Who calls the services | The gateway calls triage, search and drafting in order | The web page calls each service | One place for auth, limits, caching and logging. The inner services are not exposed to clients. |
| Which failures stop a request | Only search. Triage down: answer without labels. Drafting down: return the sources and recommend escalation. | Fail the request if anything fails | An agent with five relevant past tickets and no draft is still better off than an agent with an error page. |
| "No confident match" checkpoint | If the closest source is below 0.72 similarity, the model is not asked and escalation is recommended | Always draft an answer | A confident wrong fix costs more than no fix. In the triage eval, three quarters of off-topic questions are below 0.70 and three quarters of real complaints are above 0.80. It also saves a minute of model time. |
| Cache | Exact-match on the masked, normalised complaint, one hour, in Redis | Semantic cache (similar complaints share an answer) | Safe: the same text always got the same sources. A semantic cache can serve the wrong customer's answer and needs its own eval. |
| What is cached | Only complete answers produced with every service healthy | Cache everything | A degraded answer must not be replayed for an hour after the service has recovered. |
| Rate limiter when Redis is down | Let requests through, count the error | Block everything | The callers are our own agents. Blocking the whole desk because the limiter is down is the worse failure. |
| Audit log | Every answer is stored with the masked complaint, source IDs, model, prompt version and timings | Logs only | Any answer can be traced back to exactly what produced it, and feedback is attached to that record. |
| Audit log when the database is down | Still answer, count the error, raise an alert from the metric | Fail the request | Availability for the agent first. In a regulated setting this would be flipped to "fail". |
| Personal data | Masked in the gateway before any other service, the cache or the database sees it | Mask in each service | One place to get right. The inner services mask again as a second layer. |
| Two-speed response | The page first asks for labels and sources only (under a second), then for the full answer | Wait for everything | The agent can start reading past tickets while a slow local model is still writing. |
| API key check | Constant-time comparison, only a hash prefix of the key is logged | Plain `==`, log the key | Avoids leaking key contents through timing or logs. |

Known limits, to be honest about: the limiter is a fixed one-minute window (a burst at the window
edge can reach twice the limit), API keys live in an environment variable (a secret manager in
production), and the 0.72 checkpoint has not yet been measured end to end.

## New data and new ticket classes

The brief asks for a system that handles "evolving data and ticket classes". Two different things
change, and they need different answers.

**New data** (a ticket is resolved, an article is edited, a fix becomes outdated):

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Where a change is written first | PostgreSQL, then a note on a queue | Straight into the search index | PostgreSQL is the source of truth. The index can always be rebuilt from it, never the other way round. |
| Who updates the index | A separate worker reading a Redis Stream | The gateway does it during the request | Turning text into vectors is slow. The API answers at once, a burst of new tickets cannot slow agents down, and workers can be added. |
| What the note contains | Only "ticket T-123 changed" | The whole document | The worker always reads the newest version from PostgreSQL, so notes can arrive twice or out of order without harm. |
| A lost note (Redis down, crash between the two writes) | A sweep every minute compares `indexed_at` with `updated_at` and repairs anything behind | Trust the queue | Writing to two systems can never be made atomic. The sweep turns "usually consistent" into "always consistent within a minute or two". |
| A note that keeps failing | Retried, then moved to a dead-letter list after 5 tries, with a metric | Retry forever | One broken document must not block everything behind it. |
| Editing an article | Write the new chunks first, then delete leftover chunks of the old version | Delete, then write | The article never disappears from search during the update. |
| Outdated fixes | Marked inactive and removed from the index, kept in PostgreSQL | Delete the row | Old answers that cited the document can still be traced. |
| Cached answers after a change | The worker raises an index version, which is part of every cache key | Wait for the cache to expire | An answer cached before a knowledge-base fix must not be served after it. The cost is fewer cache hits on busy days. |
| Who may change data | Separate admin keys, with a higher rate limit for bulk loads | One kind of key | The agent web page should not be able to rewrite the knowledge base. |

**New classes** (a kind of problem that did not exist when the system was built):

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Where classes live | Rows in a `taxonomy` table, managed through the API | A list in the code | Adding a class is a data change, not a release. |
| How triage learns a new class | Nothing to do: it votes over the nearest labelled tickets, so the class appears as soon as tickets carry it | Retrain a classifier | No training step, no deployment, no "model version" to roll back. `eval_evolving.py` measures how many tickets it takes. |
| Typos creating classes | A ticket with a class that does not exist is refused (422) | Accept anything | Otherwise "biling_dispute" silently becomes a class. |
| Spotting a new class | Not per request. Agents mark "none of the categories fits" (and triage marks "unknown"); a job groups similar flagged complaints and proposes a class | A similarity threshold per request | Measured in the triage eval: a new-class complaint looks exactly as "familiar" as a known one (5% flagged). One complaint cannot be spotted, a group of them can. |
| Who decides | A person approves or rejects each proposal and gives it its name | Create classes automatically | A class changes routing and reporting. The job suggests, a human decides. |
| Grouping method | Link complaints whose meaning is close enough, take the linked groups | k-means, HDBSCAN | No need to guess the number of classes, a few lines of code, easy to explain. The threshold comes from the eval. |
| Agent corrections | Stored with the feedback and counted as a metric | Ignore | Corrections divided by feedback is a live estimate of triage accuracy on real traffic. |

### Measured: two classes the system had never seen

`evals/eval_evolving.py`, embedding model `BAAI/bge-small-en-v1.5`. The dataset holds back two
classes (eSIM problems and fraud: 4 problem types, 40 test complaints in unseen wording). Resolved
tickets were added through the gateway API a few at a time, and the complaints re-scored each time.

| Resolved tickets added per new problem type | Category correct | Sent for human review | Right past ticket in top 3 | Right article in top 2 | Seconds until searchable |
|---|---|---|---|---|---|
| 0 (before) | 0.000 | 0.100 | 0.000 | 0.000 | |
| 1 | 0.000 | 0.100 | 0.100 | 0.000 | 0.6 |
| 3 | 0.000 | 0.125 | 0.450 | 0.000 | 3.2 |
| 5 | 0.025 | 0.175 | 0.525 | 0.000 | 2.2 |
| 10 | 0.275 | 0.100 | 0.625 | 0.000 | 2.3 |
| 20 | 0.425 | 0.025 | 0.625 | 0.000 | 3.4 |
| 40 | 0.475 | 0.025 | 0.575 | 0.000 | 4.9 |
| 40 + articles | 0.475 | 0.025 | 0.625 | 0.500 | 2.1 |

Old classes (120 complaints), before and after: category 0.633 and 0.600, right ticket in top 3
0.717 and 0.717.

Grouping the 40 new complaints with 30 off-topic questions (a group needs 5 members):

| Similarity needed to link two complaints | Groups | Clean groups | New classes found (of 2) | New complaints placed | Off-topic questions in a group |
|---|---|---|---|---|---|
| 0.70 | 1 | 0 | 2 | 1.000 | 4 |
| 0.75 | 1 | 0 | 2 | 1.000 | 0 |
| 0.80 | 1 | 0 | 2 | 1.000 | 0 |
| 0.85 | 2 | 2 | 2 | 0.775 | 0 |
| 0.90 | 0 | 0 | 0 | 0.000 | 0 |

What this showed:

1. **New data is live in seconds, with no retraining and no restart.** 80 tickets were searchable
   4.9 seconds after they were sent.
2. **Search learns a new problem type from a handful of tickets**: the right past ticket is in the
   top 3 for 45% of complaints after 3 tickets, and about 60% after 10. It then levels off below
   the old classes (72%).
3. **Triage learns the category more slowly and stops short**: 47% with 40 tickets per type,
   against 63% for the old classes on the same run. Almost none of the misses are sent for review
   (2.5%), so they are given a wrong existing label with confidence. The eval now prints which
   labels they get, which is the next thing to look at.
   A second run printed the labels, and the average hides two very different results:
   **fraud was learned well (18 of 20 correct), eSIM almost not at all (1 of 20).** The eSIM
   complaints were labelled `device_hardware` (9) and `activation_provisioning` (6). Those are
   not random mistakes: "my eSIM will not activate" really is close to both existing classes.
   A new class that overlaps old ones is hard for a nearest-neighbour vote, because the old
   classes have many more tickets nearby. Search is less affected (it finds the right eSIM ticket
   in the top 3 for about 60%), so the agent still sees the right fix under the wrong label.
   What would help, not yet built: let the agent's corrections move such tickets, or give the
   class definitions themselves a vote.
4. **The old classes lost a little**: category 0.633 to 0.600, which is 4 complaints out of 120.
   That is too few to be sure it is real, but it is the direction to expect, because new tickets
   compete for the same votes.
5. **Discovery works, inside a narrow range.** At 0.85 the two classes come out as two clean groups
   with sensible keywords ("sim, handset, digital, code" and "card, claims, dodgy, firm") and no
   off-topic questions. At 0.80 both classes merge into one group, and at 0.90 nothing groups.
   The default was changed from 0.80 to 0.85 because of this. A range this narrow will move with
   the embedding model, so the eval has to be re-run when the model changes.
6. **What the discovery test does not show**: it assumes agents flagged these complaints. Triage on
   its own flagged only 10% of them, so without the agent's "none of the categories fits" the job
   would have had almost nothing to group.

Known limits: the discovery job only sees complaints that were flagged, so a new class that agents
keep filing under an old category stays invisible until someone notices. Suggested names are
keywords, not real names. And the worker updates the live collection in place; a change of
embedding model still needs a full re-index into a new collection and an alias switch.

## Monitoring: knowing when it stops working

An AI system can fail without any error: every request returns 200 while the answers quietly get
worse. So the monitoring watches four things, not one.

| Question | What is measured | Why it matters |
|---|---|---|
| Is it up and fast? | `up`, error share, time per stage | The usual service health. Time per stage shows that the language model is where the time goes. |
| Are the answers good? | share drafted by the model against quoted, steps that failed the source check, agent feedback, category corrections | These catch a bad prompt change or a broken model while every request still "succeeds". |
| Has the world changed? | similarity of the closest match, share escalated, share triage could not label, mix of categories | Drift. When customers start asking about something new, similarity falls before anyone complains. |
| Is new data arriving? | queue length, time since the last indexed document, dead letters | A stuck worker is invisible to agents: search keeps working, only on old data. |

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Quality signals without labels | Things the system can check itself (citations, similarity) plus agent feedback | Wait for labelled data | Live traffic has no answer key. These signals are available on every request. |
| Live triage accuracy | Category corrections divided by ratings | Only offline evals | The offline eval says how good triage was on test data. Corrections say how good it is today. |
| Alert text | Every alert carries a summary and a first action | Name only | The person who gets the alert may not be the person who built the system. |
| Alert rules | Tested with made-up numbers (`promtool test rules`), in CI | Trust the expression | An alert that never fires looks exactly like a healthy system. |
| Dashboard | Built from a short Python list, checked by a test against the metric names in the code | Edit JSON by hand in Grafana | A renamed metric would otherwise leave an empty panel that nobody notices. |
| Counters | Every known label starts at 0 | Appear on first use | Prometheus cannot see a rise from "does not exist" to 1, so the first event of each kind would be lost. |
| Health check | A script that sends real requests through the gateway | Only `/ready` endpoints | Every service can be "ready" while the whole path is broken. |
| Alert delivery | Shown in Prometheus and Grafana | Alertmanager with paging | Enough for a single machine. In production the same rules would go to Alertmanager. |
| Logs | One JSON line per request with a shared request ID | Plain text | One customer request can be followed through every service by its ID. |

Known limits: the numbers live on one machine and are lost with it, there is no tracing beyond
the shared request ID, and the drift alerts use thresholds taken from the evals (for example a
normal closest-match similarity of about 0.83) that have not been tuned on real traffic.

## Other decisions

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Dataset | Synthetic telecom tickets from 40 hand-written scenarios | Public support datasets | The public sets are not telecom or have no labels. Synthetic data gives an answer key for evals. |
| Train/test wording | Test complaints use held-out wording, checked by a test | Random split | A random split would reuse the same sentences, and every search method would look perfect. |
| What gets embedded for a ticket | The customer's problem text | Problem + resolution | A new complaint should match old complaints. The resolution is stored next to it for the LLM. |
| KB articles | Split by heading, full article stored with each chunk | Whole article as one vector | Small chunks match more precisely, and the LLM still receives the full steps. |
| Personal data | Masked before embedding and storing in the index | Store as-is | Phone numbers and account numbers are not needed for search and must not reach the LLM. |
| Model files | Baked into the image at build time, loaded offline, checked during the build | Download at start-up | Fast, repeatable starts with no internet, and a broken model fails the build, not production. |
| Embedding service | One service owns all small models | Load the model in each service | One copy in memory, one model version everywhere, scales on its own. |
| Collection naming | Services search an alias that points at a versioned collection | Fixed collection name | A new embedding model can be indexed in the background and switched in with no downtime. |
| Point IDs | Derived from the document ID | Random IDs | Loading the same document twice updates it, never duplicates it. |
