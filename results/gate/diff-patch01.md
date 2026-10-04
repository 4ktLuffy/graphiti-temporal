### Memory-logic gate: `patch:upstream/01-invalidation-candidates.patch, base 5d47d4d, patch 215bb4f` vs `main, base 5d47d4d`

**3 fixed** (of 20 scenarios)

| scenario | base | head | change | why |
|---|---|---|---|---|
| `invalidation/depth-20` | FAIL | pass | fixed |  |
| `invalidation/depth-20-backfill` | FAIL | pass | fixed |  |
| `invalidation/depth-20-triplet` | FAIL | pass | fixed |  |
| `invalidation/additive-over-eager` | FAIL | FAIL | disputed | q7: expected open, got 2026-08-10, expired |
| `dedup/duplicate-retires-old` | pass | pass | unchanged |  |
| `dedup/semantic-duplicate` | pass | pass | unchanged |  |
| `intervals/backfill-between` | pass | pass | unchanged |  |
| `intervals/backfill-ended` | pass | pass | unchanged |  |
| `intervals/backfill-open` | pass | pass | unchanged |  |
| `intervals/born-ended-expired` | FAIL | FAIL | unchanged | stint: expected expired=True, got 2020-01-01 |
| `intervals/end-only-later-start` | pass | pass | unchanged |  |
| `intervals/end-only-no-extend` | pass | pass | unchanged |  |
| `intervals/end-only-release` | FAIL | FAIL | unchanged | assigned: expected invalid_at 2026-03-01, got open; assigned: expected expired=True, got open |
| `intervals/move-control` | pass | pass | unchanged |  |
| `intervals/reassertion` | FAIL | FAIL | unchanged | again: expected open, got merged into old; Alex/assignment: expected open ['again'], got [] |
| `intervals/reassertion-overlap-control` | pass | pass | unchanged |  |
| `invalidation/depth-5-control` | pass | pass | unchanged |  |
| `invalidation/same-slot-over-eager-control` | pass | pass | unchanged |  |
| `invalidation/unrelated-over-eager` | FAIL | FAIL | unchanged | job: expected open, got 2024-03-01, expired |
| `judge/under-resolving-silent` | pass | pass | unchanged |  |

Scenario sources and expected outcomes: `scenarios/`. Judge personas and how each outcome is checked: `graphiti_gate/judges.py`, `graphiti_gate/worker.py`. 

