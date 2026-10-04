| scenario | main | pr:1729 | pr:1772 | pr:1940 | pr:1867 | patch:upstream/01-invalidation-candidates.patch |
|---|---|---|---|---|---|---|
| `intervals/backfill-ended` | pass | pass | pass | pass | pass | pass |
| `intervals/backfill-open` | pass | pass | pass | pass | pass | pass |
| `intervals/end-only-release` | FAIL | FAIL | FAIL | pass | FAIL | FAIL |
| `intervals/reassertion` | FAIL | FAIL | FAIL | FAIL | pass | FAIL |
| `invalidation/depth-20` | FAIL | FAIL | FAIL | FAIL | FAIL | pass |
| `invalidation/depth-5-control` | pass | pass | pass | pass | pass | pass |
| `invalidation/same-slot-over-eager-control` | pass | pass | pass | pass | pass | pass |
| `invalidation/unrelated-over-eager` | FAIL | pass | FAIL | FAIL | FAIL | FAIL |
