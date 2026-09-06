#!/usr/bin/env bash
# Build (or rebuild) the graphify knowledge graph for one project.
# Code-only: AST extraction, no LLM calls, no API key.
# Usage: ./build-graph.sh <project-path>
set -uo pipefail
PROJ="${1:?usage: build-graph.sh <project-path>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$PROJ" && pwd)" || exit 1
NAME="$(basename "$PROJ")"
mkdir -p "$DIR/logs"
LOG="$DIR/logs/${NAME}-graphify.log"

cd "$PROJ" || exit 1
mkdir -p graphify-out
if [ ! -f graphify-out/.graphify_python ]; then
  P="$(uv tool run --from graphifyy python -c 'import sys; print(sys.executable)' 2>/dev/null)"
  [ -z "$P" ] && P="$(command -v python3)"
  printf '%s' "$P" > graphify-out/.graphify_python
fi
PY="$(cat graphify-out/.graphify_python)"
pwd > graphify-out/.graphify_root

{
echo "=== graphify build: $NAME  $(date '+%F %T') ==="
"$PY" - <<'PYEOF'
import json
from pathlib import Path
from graphify.detect import detect
from graphify.extract import collect_files, extract
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json

d = detect(Path('.'))
Path('graphify-out/.graphify_detect.json').write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
print(f"detected {d['total_files']} files")

cf = []
for f in d.get('files', {}).get('code', []):
    p = Path(f)
    cf.extend(collect_files(p) if p.is_dir() else [p])
if not cf:
    raise SystemExit('no code files graphify can parse - skipping')

r = extract(cf, cache_root=Path('.'))
ex = {'nodes': r['nodes'], 'edges': r['edges'], 'hyperedges': [],
      'input_tokens': 0, 'output_tokens': 0}
Path('graphify-out/.graphify_extract.json').write_text(json.dumps(ex, ensure_ascii=False), encoding='utf-8')
print(f"AST: {len(r['nodes'])} nodes, {len(r['edges'])} edges")

G = build_from_json(ex, root='.', directed=False)
if G.number_of_nodes() == 0:
    raise SystemExit('graph empty - nothing written')
com = cluster(G)
coh = score_all(G, com)
labels = {c: f'Community {c}' for c in com}
qs = suggest_questions(G, com, labels)
if not to_json(G, com, 'graphify-out/graph.json'):
    raise SystemExit('refused to shrink existing graph.json')
rep = generate(G, com, coh, labels, god_nodes(G), surprising_connections(G, com),
               d, {'input': 0, 'output': 0}, '.', suggested_questions=qs)
Path('graphify-out/GRAPH_REPORT.md').write_text(rep, encoding='utf-8')
print(f"graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(com)} communities")
PYEOF
graphify export html 2>&1 | grep -v '^ *warning:' || true
echo "=== done ==="
} >>"$LOG" 2>&1

tail -4 "$LOG"
