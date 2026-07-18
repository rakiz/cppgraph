#!/usr/bin/env bash
# Register the cppgraph MCP server in Claude Code — ONCE, globally.
#
# Serena-style: the server auto-discovers each project's graph from the current
# directory's `.cppgraph/` at launch, so a single registration serves every
# indexed project with no collision. Open Claude Code *from a project directory*
# and the cppgraph tools use that project's graph. Re-run any time (idempotent).
#
# Usage: scripts/register-mcp.sh   (no arguments)
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"

command -v claude >/dev/null || { echo "Claude Code CLI 'claude' not found." >&2; exit 1; }
bin="$repo_root/.venv/bin/cppgraph-mcp"
[[ -x "$bin" ]] || { echo "cppgraph-mcp not found — run scripts/setup.sh first." >&2; exit 1; }

# Remove any existing registration before adding, so re-running is idempotent.
claude mcp remove cppgraph --scope user >/dev/null 2>&1 || true
claude mcp add cppgraph --scope user -- "$bin"

echo "Registered cppgraph globally (project-aware, auto-discovers <project>/.cppgraph/)."
echo "Now open Claude Code FROM an indexed project directory (a new session) and ask e.g.:"
echo "  \"what calls X?\"   \"impact of changing Y?\"   \"show the dependency graph of Z\""
