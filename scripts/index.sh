#!/usr/bin/env bash
set -euo pipefail
#
# Index the current project into a cppgraph graph — the interactive wizard.
#
# Run this from your project directory (where compile_commands.json lives, or any
# directory above it). The wizard finds the compilation database, shows what's
# indexable, asks the scope questions (subtree / tests / attribution), and — when
# an index or graph already exists — shows its details and asks whether to reuse
# or recompute it. Nothing expensive is overwritten without your say-so.
#
#   scripts/index.sh                 guided, from the current project
#   scripts/index.sh --from-scratch  re-walk every stage from the top
#
# The tool venv must already exist (created by scripts/setup.sh).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CPPGRAPH="$REPO_ROOT/.venv/bin/cppgraph"

if [[ ! -x "$CPPGRAPH" ]]; then
  echo "error: the cppgraph venv is not set up (looked for $CPPGRAPH)." >&2
  echo "Run the tool setup first:  $REPO_ROOT/scripts/setup.sh" >&2
  exit 1
fi

exec "$CPPGRAPH" index "$@"
