#!/usr/bin/env bash
# Does lucene_sanitize change what Lucene searches for? Parses raw and sanitized queries with
# Lucene 9.11's classic QueryParser (Neo4j fulltext) and prints the terms each becomes.
set -euo pipefail
cd "$(dirname "$0")"
for a in lucene-core lucene-queryparser lucene-analysis-common; do
  [ -f "$a-9.11.1.jar" ] || mvn -q dependency:copy -Dartifact=org.apache.lucene:$a:9.11.1 -DoutputDirectory=.
done
CP="lucene-core-9.11.1.jar:lucene-queryparser-9.11.1.jar:lucene-analysis-common-9.11.1.jar:."
javac -cp "$CP" ParseQueries.java
uv run python - <<'PY' | tr '\n' '\0' | xargs -0 java -cp "$CP" ParseQueries
from graphiti_core.helpers import lucene_sanitize
for q in ["EBITDA growth", "Alice works at NASA", "Tom OR Jerry", "rock AND roll", "NOT guilty",
          "ORDER of DATA", "TODO list"]:
    print(q); print(lucene_sanitize(q))
PY
