# Answer quality, end to end

Language model: llama3.2:3b, prompt v3. Every complaint went through the gateway, as an agent's would.

(checkpoint part skipped)
## The drafted answers

| Group | Complaints | Cites the right problem | Right, mixed with another | Cites a wrong problem | Escalated, no answer | Every step backed by its source | Written by the model |
|---|---|---|---|---|---|---|---|
| Known problems | 20 | 0 | 0 | 0 | 20 | 0.000 | 0.000 |
| New class (no fix exists) | 6 | 0 | 0 | 0 | 6 | 0.000 | 0.000 |
| Off topic | 30 | 0 | 0 | 0 | 30 | 0.000 | 0.000 |

## Details

- Known problems: 0% of the answers cite the right problem.
- Of the 0 wrong answers, the search had found a right source for 0 (the model picked the wrong one) and had not for 0 (the search missed it).
- The customer said what they already tried in 0 complaints: the answer listed it in 0, and repeated it as a step in 0.
- Steps not backed by their cited source: 0.0% of the steps in answers to known problems.
- Off-topic questions: 30 of 30 refused. The model was asked for 7 of them.
- New-class complaints: an answer was drafted for 0 of 6, although the knowledge base holds no fix for them.
- Answers the model itself withheld because the sources were about another problem: 20 known, 6 new-class, 7 off-topic.
- Time per answer written by the model: typical 35s, slowest 46s.
