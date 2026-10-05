# Pre-registered: the frozen gate against Graphiti fixes merged after today

**What was frozen, and how.** The 34 scenarios and the code that judges them are frozen. Their
SHA-256 fingerprint is recorded in `results/gate/prospective_commitment.json`, and the GitHub
commit that adds that file is the public timestamp.

**Which fixes count.** Every commit on getzep/graphiti `main` after `b7fc30f` whose message
contains "fix" and that touches the code the gate exercises (the files listed in the commitment).
Each one is tested by `bench/prospective.py` exactly as `bench/real_regressions.py` tests past
fixes: the frozen scenarios run on the fix's parent and on the fix.

**What counts as caught.** A frozen scenario that passes on the fix and fails or errors on its
parent. `bench/prospective.py --check-freeze` proves the gate used is the frozen one.

**Why.** Every earlier number in GATE.md was measured by the people who built the gate, on bugs
they had seen or written. These fixes did not exist when the gate was frozen, so it could not be
tuned to them.

**Results so far.** None. They will be reported as they come, misses included.
