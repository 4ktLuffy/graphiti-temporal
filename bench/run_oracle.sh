#!/usr/bin/env bash
# Every oracle measurement in the README, against Neo4j 5.26.2 (no model calls).
#   docker run -d --name gt-neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/$NEO4J_PASSWORD neo4j:5.26.2
set -euo pipefail
cd "$(dirname "$0")/.."
: "${NEO4J_PASSWORD:?set NEO4J_PASSWORD}"
mkdir -p results/logs
for fix in none 1 2 both; do
  uv run python bench/history_depth.py --fix "$fix" --depths 0 5 9 10 12 20 50 --seeds 30 \
    > "results/logs/history_depth_fix-$fix.log" 2>&1 &
done
for fix in none 2; do
  uv run python bench/as_of.py --fix "$fix" --depths 0 10 20 50 --seeds 20 \
    > "results/logs/as_of_fix-$fix.log" 2>&1 &
done
wait
grep -h "history=" results/logs/history_depth_fix-*.log results/logs/as_of_fix-*.log
