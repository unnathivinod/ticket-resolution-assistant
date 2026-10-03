Retrieval quality on 360 held-out test complaints (wording never seen by the index).

| Setup | Hit@1 | Hit@3 | Precision@5 | MRR@10 | KB Hit@1 | KB Recall@3 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| Dense, full complaint (as today) | 0.542 | 0.714 | 0.497 | 0.650 | 0.450 | 0.733 | 36 | 59 |
| Hybrid, dense counts 3x, full complaint | 0.550 | 0.711 | 0.487 | 0.654 | 0.478 | 0.714 | 41 | 62 |
| Keyword, core problem only | 0.147 | 0.208 | 0.124 | 0.187 | 0.189 | 0.358 | 26 | 47 |
| Dense, core problem only (upper bound) | 0.542 | 0.753 | 0.524 | 0.660 | 0.569 | 0.772 | 28 | 48 |
| Hybrid, core problem only | 0.372 | 0.625 | 0.336 | 0.535 | 0.442 | 0.678 | 34 | 59 |
| Hybrid, dense counts 3x, core problem only | 0.536 | 0.722 | 0.436 | 0.647 | 0.522 | 0.753 | 34 | 48 |
