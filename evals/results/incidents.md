# Incident detection: a burst of similar complaints

An incident is 7 customers reporting the same problem among 20 other complaints in one half hour.
A quiet half hour is 27 complaints with at most two about the same problem.
Measured on the test half: 360 half hours of each kind, 180 complaints about 18 problems the tuning never saw.

How similar two complaints are (1 = same meaning):

| Pair | Lowest 10% | Middle | Top 10% |
|---|---|---|---|
| Same problem | 0.671 | 0.768 | 0.909 |
| Different problems | 0.600 | 0.680 | 0.755 |

| Setting | Similarity needed | Complaints needed | Incidents flagged (want high) | Quiet half hours flagged (want low) |
|---|---|---|---|---|
| In use now | 0.875 | 3 | 0.711 | 0.022 |
| Best on the dev half | 0.875 | 3 | 0.711 | 0.022 |

With the best setting, the flag is raised after 6 of the incident's 7 complaints (the middle case).

Every setting, on the test half (incidents flagged / quiet half hours flagged):

| Similarity needed | 3 complaints | 4 complaints | 5 complaints |
|---|---|---|---|
| 0.6 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 0.625 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 0.65 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 0.675 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 0.7 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| 0.725 | 1.00 / 1.00 | 1.00 / 1.00 | 0.98 / 1.00 |
| 0.75 | 1.00 / 1.00 | 0.96 / 1.00 | 0.89 / 0.99 |
| 0.775 | 1.00 / 1.00 | 0.81 / 0.92 | 0.64 / 0.63 |
| 0.8 | 0.99 / 0.84 | 0.62 / 0.43 | 0.38 / 0.16 |
| 0.825 | 0.94 / 0.39 | 0.39 / 0.08 | 0.12 / 0.01 |
| 0.85 | 0.85 / 0.08 | 0.21 / 0.01 | 0.03 / 0.00 |
| 0.875 | 0.71 / 0.02 | 0.10 / 0.00 | 0.00 / 0.00 |
| 0.9 | 0.41 / 0.00 | 0.03 / 0.00 | 0.00 / 0.00 |
