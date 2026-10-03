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

Decisions taken:

| Decision | Chosen | Why |
|---|---|---|
| Category and product | Similarity-weighted vote of the nearest past tickets | No training step, new classes work as soon as labelled tickets exist, a few milliseconds per request |
| Severity and sentiment | Example sentences per signal, matched by meaning, kept in a YAML file | Catches new wordings, every decision comes with a reason, support staff can edit the examples |
| Thresholds | Fitted by `eval_triage.py --calibrate` on a dev half, reported on a test half | Avoids hand-picked numbers and avoids grading on the data used for tuning |
| Detecting new classes | Not by a per-request threshold. Use agent corrections plus a clustering job over recent tickets | Measurement 3 above: a threshold cannot see them |

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
