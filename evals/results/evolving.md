# New data and new ticket classes

Embedding model: `BAAI/bge-small-en-v1.5`. 40 complaints about 4 problem types the system had never seen.

## Learning curve

| Resolved tickets added per new problem type | Category correct | Category, best guess | Sent for human review | Right past ticket in top 3 | Right article in top 2 | Seconds until searchable |
|---|---|---|---|---|---|---|
| 0 (before) | 0.000 | 0.000 | 0.100 | 0.000 | 0.000 |  |
| 1 | 0.000 | 0.000 | 0.100 | 0.200 | 0.000 | 0.6 |
| 3 | 0.000 | 0.000 | 0.150 | 0.450 | 0.000 | 3.2 |
| 5 | 0.025 | 0.075 | 0.175 | 0.500 | 0.000 | 3.7 |
| 10 | 0.275 | 0.300 | 0.100 | 0.625 | 0.000 | 3.7 |
| 20 | 0.425 | 0.425 | 0.025 | 0.675 | 0.000 | 4.5 |
| 40 | 0.475 | 0.475 | 0.025 | 0.625 | 0.000 | 4.7 |
| 40 + articles | 0.475 | 0.475 | 0.025 | 0.525 | 0.500 | 2.6 |

Labels given to the new complaints at the end (real class -> label given):

- security_fraud -> security_fraud: 18
- esim_management -> device_hardware: 9
- esim_management -> activation_provisioning: 6
- esim_management -> security_fraud: 2
- esim_management -> number_porting: 1
- esim_management -> esim_management: 1
- security_fraud -> connectivity_outage: 1
- esim_management -> unknown: 1
- security_fraud -> payment_issue: 1

## Old classes, before and after the new ones were added

| | Category correct | Right past ticket in top 3 |
|---|---|---|
| Before | 0.617 | 0.717 |
| After | 0.600 | 0.725 |

## Discovery: grouping 40 new-class complaints and 30 off-topic questions

A group needs at least 5 complaints. A 'clean' group holds one real class only.

| Similarity needed to link two complaints | Groups proposed | Clean groups | New classes found (of 2) | New complaints placed in a group | Off-topic questions in a group |
|---|---|---|---|---|---|
| 0.700 | 1 | 0 | 2 | 1.000 | 4 |
| 0.750 | 1 | 0 | 2 | 1.000 | 0 |
| 0.800 | 1 | 0 | 2 | 1.000 | 0 |
| 0.825 | 1 | 0 | 2 | 0.850 | 0 |
| 0.850 | 2 | 2 | 2 | 0.775 | 0 |
| 0.875 | 1 | 1 | 1 | 0.200 | 0 |
| 0.900 | 0 | 0 | 0 | 0.000 | 0 |
