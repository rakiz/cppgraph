#!/usr/bin/env bash
# Register the cppgraph MCP server in Claude Code, pointed at a built graph.
# After this, open a NEW Claude Code session and the `cppgraph` tools appear.
#
# Usage:
#   scripts/register-mcp.sh GRAPH_DB [PROJECT_ROOT]
#
#   GRAPH_DB       a store built by `cppgraph build` / `scripts/reindex.sh`
#   PROJECT_ROOT   (optional) the source checkout, for `status` drift checks and
#                  source snippets in `explain`/`visualize`
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/register-mcp.sh GRAPH_DB [PROJECT_ROOT]" >&2
  exit 2
fi
GRAPH="$1"
ROOT="${2:-}"

command -v claude >/dev/null || { echo "Claude Code CLI 'claude' not found." >&2; exit 1; }
[ -f "$GRAPH" ] || { echo "graph not found: $GRAPH" >&2; exit 1; }

bin="$(pwd)/.venv/bin/cppgraph-mcp"
[ -x "$bin" ] || { echo "cppgraph-mcp not found — run scripts/setup.sh first." >&2; exit 1; }

# absolute path so the server resolves the graph regardless of cwd
graph_abs="$(cd "$(dirname "$GRAPH")" && pwd)/$(basename "$GRAPH")"

args=(--graph "$graph_abs")
[ -n "$ROOT" ] && args+=(--root "$ROOT")

claude mcp add cppgraph --scope user -- "$bin" "${args[@]}"
echo "Registered. Open a NEW Claude Code session, then ask e.g.:"
echo "  \"what calls X?\"   \"impact of changing Y?\"   \"show the dependency graph of Z\""
