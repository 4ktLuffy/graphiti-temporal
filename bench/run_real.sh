#!/usr/bin/env bash
# Real-model arms (Codex CLI as Graphiti's LLM): a person with 20 or 50 past homes moves once.
# Produces results/real_llm_h{20,50}_fix-{none,1}.jsonl, the rows in README.md and FINDINGS.md.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${NEO4J_PASSWORD:?set NEO4J_PASSWORD}"
mkdir -p results/logs
for depth in 20 50; do
  for fix in none 1; do
    uv run python bench/real_llm.py --fix "$fix" --n-history "$depth" --trials 12 --parallel 2 \
      --out "results/real_llm_h${depth}_fix-$fix.jsonl" \
      > "results/logs/real_llm_h${depth}_fix-$fix.log" 2>&1 &
  done
done
wait
grep -h "current home retired" results/logs/real_llm_h*_fix-*.log
