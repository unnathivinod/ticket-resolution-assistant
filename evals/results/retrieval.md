Retrieval quality on 360 held-out test complaints (wording never seen by the index).
Embedding model: BAAI/bge-small-en-v1.5

| Setup | Hit@1 | Hit@3 | Context hit | Category@1 | MRR@10 | KB Hit@1 | KB Recall@3 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| Keyword only (BM25) - today's baseline | 0.397 | 0.525 | 0.608 | 0.489 | 0.475 | 0.222 | 0.408 | 50 | 88 |
| Dense only (meaning) | 0.542 | 0.714 | 0.822 | 0.644 | 0.650 | 0.450 | 0.733 | 50 | 73 |
| Hybrid, equal weights | 0.489 | 0.697 | 0.789 | 0.581 | 0.613 | 0.372 | 0.672 | 53 | 80 |
| Hybrid, dense counts 3x (our default) | 0.550 | 0.714 | 0.831 | 0.656 | 0.654 | 0.478 | 0.708 | 54 | 82 |
