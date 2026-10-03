# Answer quality, end to end

Language model: `llama3.2:3b`. Every complaint went through the gateway, as an agent's would.

## The 'nothing similar enough' checkpoint

Share of each group that would be stopped at each cut-off. The gateway uses 0.72.

| Cut-off | Off-topic stopped (want high) | Known complaints stopped (want low) | New-class complaints stopped |
|---|---|---|---|
| 0.55 | 0.133 | 0.000 | 0.000 |
| 0.60 | 0.433 | 0.003 | 0.000 |
| 0.65 | 0.567 | 0.003 | 0.000 |
| 0.70 | 0.733 | 0.006 | 0.000 |
| 0.72 (in use) | 0.767 | 0.014 | 0.000 |
| 0.75 | 0.867 | 0.042 | 0.050 |
| 0.80 | 1.000 | 0.250 | 0.275 |

## The drafted answers

| Group | Complaints | Cites the right problem | Right, mixed with another | Cites a wrong problem | Escalated, no answer | Every step backed by its source | Written by the model |
|---|---|---|---|---|---|---|---|
| Known problems | 20 | 11 | 3 | 6 | 0 | 1.000 | 1.000 |
| New class (no fix exists) | 6 | 0 | 0 | 6 | 0 | 0.833 | 1.000 |
| Off topic | 30 | 0 | 0 | 7 | 23 | 1.000 | 1.000 |

## Details

- Known problems: 70% of the answers cite the right problem.
- Of the 6 wrong answers, the search had found a right source for 1 (the model picked the wrong one) and had not for 5 (the search missed it).
- The customer said what they already tried in 11 complaints: the answer listed it in 11, and repeated it as a step in 1.
- Steps not backed by their cited source: 0.0% of the steps in answers to known problems.
- Off-topic questions: 23 of 30 refused. The model was asked for 7 of them.
- New-class complaints: an answer was drafted for 6 of 6, although the knowledge base holds no fix for them.
- Time per answer written by the model: typical 31s, slowest 44s.
