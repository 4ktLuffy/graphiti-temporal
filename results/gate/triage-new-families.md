| scenario | main | pr:1596 | pr:1741 | pr:1873 | pr:1914 | pr:1912 | pr:1671 | pr:1729 | pr:1940 | pr:1867 | pr:1772 | patch:upstream/01-invalidation-candidates.patch |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bulk/same-fact-two-episodes` | FAIL | FAIL | FAIL | pass | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| `nodes/exact-name-below-cosine` | FAIL | FAIL | pass | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| `nodes/exact-name-control` | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass |
| `search/date-filter-and-control` | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass | pass |
| `search/date-filter-or-groups` | FAIL | pass | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| *changed `graphiti_core` statements the gate ran* |  | 1/4 | 7/8 | 9/10 | 0/1 **untested** | 1/21 | 0/2 **untested** | 3/11 | 1/21 | 7/28 | 3/26 | 2/6 |
