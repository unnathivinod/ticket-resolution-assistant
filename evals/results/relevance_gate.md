# A second check: does the best source really fit?

Cross-encoder relevance of the best source, per group (0 = unrelated, 1 = relevant).

| Group | Lowest 10% | Lowest 25% | Middle | Top 25% |
|---|---|---|---|---|
| known | 0.000 | 0.001 | 0.004 | 0.038 |
| new_class | 0.000 | 0.000 | 0.002 | 0.007 |
| off_topic | 0.000 | 0.000 | 0.000 | 0.011 |

If we accept stopping this share of answerable complaints by mistake, how much else is stopped?

| Known complaints stopped (the cost) | Relevance threshold | New-class stopped (want high) | Off-topic stopped (want high) |
|---|---|---|---|
| 0.000 | 0.000 | 0.000 | 0.000 |
| 0.000 | 0.000 | 0.000 | 0.000 |
| 0.022 | 0.000 | 0.025 | 0.500 |
| 0.022 | 0.000 | 0.025 | 0.500 |
| 0.178 | 0.000 | 0.125 | 0.600 |

Together with the similarity cut-off already in use (0.72): stopped if EITHER check fails.

| Relevance threshold | Known stopped | New-class stopped | Off-topic stopped |
|---|---|---|---|
| 0.000 | 0.014 | 0.000 | 0.767 |
| 0.000 | 0.014 | 0.000 | 0.767 |
| 0.000 | 0.036 | 0.025 | 0.767 |
| 0.000 | 0.036 | 0.025 | 0.767 |
| 0.000 | 0.186 | 0.125 | 0.767 |

**Verdict: not useful.** It does not separate new-class complaints from known ones well enough to pay for the answerable complaints it would stop.
