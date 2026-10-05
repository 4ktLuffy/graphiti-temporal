#!/bin/bash
# Graphiti's own CI unit command (unit_tests.yml) on main and on a patched revision.
#   bash bench/unit_suite.sh patch:bench/patches/01+03-neighbors.patch > results/unit-suite.txt
# Five test files need optional client libraries not installed here; they fail to load on every
# revision and are skipped with --continue-on-collection-errors.
cd "$(dirname "$0")/.."
CMD=$(git -C .gate/graphiti show origin/main:.github/workflows/unit_tests.yml | sed -n '/uv run pytest tests\//,/^\s*$/p' | sed -n '1,/[^\\]$/p' | tr -d '\\' | sed 's/uv run //' | tr '\n' ' ')
for spec in main "$@"; do
  W=$(.gate/venvs/b61ef949c2740336/bin/python -c "from graphiti_gate.revisions import prepare; print(prepare('$spec').worktree)" 2>/dev/null)
  V=$(.gate/venvs/b61ef949c2740336/bin/python -c "from graphiti_gate.revisions import prepare; print(prepare('$spec').python)" 2>/dev/null)
  uv pip install -q --python "$V" pytest pytest-asyncio pytest-xdist falkordb kuzu >/dev/null 2>&1  # same extras on every revision
  echo "## $spec"
  (cd "$W" && DISABLE_NEPTUNE=1 DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_KUZU=1 eval "\"$V\" -m $CMD -q -p no:cacheprovider --continue-on-collection-errors" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed" | sed 's/ - .*//' | sort)
done
