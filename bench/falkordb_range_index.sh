#!/bin/bash
# FalkorDB: with a range index on a string property, `<` and `<=` ignore their bound.
# Runs against the two local test containers (4.10.3 on gt-falkor4, 6.0.1 on gt-falkor6).
#   bash bench/falkordb_range_index.sh > results/falkordb_range_index.txt
for C in gt-falkor4 gt-falkor6; do
  q(){ docker exec $C redis-cli GRAPH.QUERY rangeidx "$1" | grep -v "Cached\|internal\|Graph version"; }
  docker exec $C redis-cli GRAPH.DELETE rangeidx >/dev/null 2>&1
  echo "## $C ($(docker exec $C redis-cli MODULE LIST | tr '\n' ' ' | grep -o 'name graph ver [0-9]*' | sed 's/name graph //'))"
  q "CREATE ()-[:T {v:'2001'}]->(), ()-[:T {v:'2003'}]->(), ()-[:T {v:'2005'}]->()" >/dev/null
  echo "no index:   v <= '2002' -> $(q "MATCH ()-[e:T]->() WHERE e.v <= '2002' RETURN collect(e.v)" | tail -1)"
  q "CREATE INDEX FOR ()-[e:T]-() ON (e.v)" >/dev/null; sleep 1
  for op in '<=' '<' '>=' '>'; do
    echo "with index: v $op '2002' -> $(q "MATCH ()-[e:T]->() WHERE e.v $op '2002' RETURN collect(e.v)" | tail -1)"
  done
  docker exec $C redis-cli GRAPH.DELETE rangeidx >/dev/null
done
