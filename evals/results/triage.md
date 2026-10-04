Triage quality on the test half (215 complaints the tuning never saw).
Embedding model: BAAI/bge-small-en-v1.5
Settings: {"neighbours": 25, "similarity_power": 4, "min_similarity": 0.75, "min_confidence": 0.3, "severity_signal_threshold": 0.7, "sentiment_signal_threshold": 0.675, "baseline_neighbours": 15, "baseline_quantile": 0.25}

| Metric | Value | Note |
|---|---|---|
| Category accuracy | 0.717 | 'unknown' counts as wrong |
| Category accuracy, best guess | 0.761 | ignoring the 'unknown' rule |
| Category macro-F1 | 0.734 | rare classes count as much as common ones |
| Product accuracy | 0.789 |  |
| Severity accuracy | 0.639 | exact level |
| Severity within one level | 0.944 |  |
|   part 1: baseline severity | 0.717 | from similar tickets |
|   part 2: urgency signal | 0.639 | which urgency signal, or none |
| Sentiment accuracy | 0.828 |  |
| Sentiment macro-F1 | 0.851 |  |
| Known complaints flagged unknown | 0.078 | lower is better |
| New-class complaints flagged unknown | 0.050 | higher is better |
| Off-topic questions flagged unknown | 0.867 | higher is better |
