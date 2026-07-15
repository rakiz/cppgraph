#!/usr/bin/env bash
set -euo pipefail

# Index any C++ project's compile_commands.json into a SCIP index + a
# cppgraph graph store (SQLite). Generic — cppgraph works on any project that provides
# a compile_commands.json (MongoDB is only our current test target, see
# AGENTS.md; never hard-code a project's paths here). Wraps the three steps
# documented in INSTALL.md as one command — also the script to hand to an
# LLM/agent if you want it to redo or adjust an indexing run, since it embeds
# the one non-obvious compdb gotcha inline (see below).
#
# Usage:
#   scripts/reindex.sh COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
#
#   COMPDB_PATH   path to a compile_commands.json (required)
#   SRC_FILTER    substring filter applied to each compdb entry's "file"
#                 field; omit or pass "" to index everything in the compdb
#                 (default: "")
#   OUT_NAME      basename (no extension) for the outputs under scratch/
#                 (default: basename of COMPDB_PATH's parent directory)
#   PROJECT_ROOT  directory to run scip-clang from — matters for the
#                 project_root recorded in the SCIP index (see DESIGN.md
#                 "Project root is a query-time parameter, never stored")
#                 (default: directory containing COMPDB_PATH)
#
# Example — MongoDB, our current test target, all of src/mongo (excludes
# third_party):
#   scripts/reindex.sh /Users/sebastien.mendez/code/mongo/compile_commands.json \
#     src/mongo/ mongo_full
#
# Example — one MongoDB subsystem instead, for fast iteration:
#   scripts/reindex.sh /Users/sebastien.mendez/code/mongo/compile_commands.json \
#     src/mongo/db/pipeline/ pipeline
#
# Outputs (all under scratch/, gitignored — never committed, see AGENTS.md
# "Large artifacts"):
#   scratch/<OUT_NAME>.compdb.json   filtered compile_commands.json subset
#   scratch/<OUT_NAME>.scip          scip-clang index
#   scratch/<OUT_NAME>.graph.db      cppgraph build output (interned SQLite)
#
# GOTCHA (cost a debugging session the first time on MongoDB's compdb — kept
# here so it isn't rediscovered on the next project): some build systems'
# generated compile_commands.json is not uniformly formatted — entries may
# mix an absolute path and a bare relative path for logically equivalent
# locations (observed on MongoDB's Bazel-generated compdb: most entries use
# an absolute bazel-out path, a handful use a bare "src/..." relative path
# for the same file). A SRC_FILTER that requires a leading "/" would silently
# drop the bare-relative ones. This script does a plain substring match
# (no anchoring), which is robust to both forms — keep it that way, and
# don't assume a compdb's "file" field is uniformly absolute or relative for
# a new project either.
#
# Prerequisites: `scratch/bin/scip-clang` and the `.venv` set up per
# INSTALL.md. Both are per-machine and gitignored, not provided by this repo.

if [[ $# -lt 1 ]]; then
  echo "usage: $0 COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]" >&2
  exit 2
fi

COMPDB="$1"
SRC_FILTER="${2:-}"
COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
OUT_NAME="${3:-$(basename "$COMPDB_DIR")}"
PROJECT_ROOT="${4:-$COMPDB_DIR}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCIP_CLANG="$REPO_ROOT/scratch/bin/scip-clang"
OUT_COMPDB="$REPO_ROOT/scratch/${OUT_NAME}.compdb.json"
OUT_SCIP="$REPO_ROOT/scratch/${OUT_NAME}.scip"
OUT_GRAPH="$REPO_ROOT/scratch/${OUT_NAME}.graph.db"

if [[ ! -x "$SCIP_CLANG" ]]; then
  echo "error: $SCIP_CLANG not found/executable. See INSTALL.md section 2." >&2
  exit 1
fi
if [[ ! -f "$COMPDB" ]]; then
  echo "error: $COMPDB not found." >&2
  exit 1
fi

mkdir -p "$REPO_ROOT/scratch"

echo "[1/3] Filtering compile_commands.json (substring: '${SRC_FILTER:-<none, indexing everything>}') ..."
python3 - "$COMPDB" "$SRC_FILTER" "$OUT_COMPDB" <<'PYEOF'
import json
import sys

compdb_path, src_filter, out_path = sys.argv[1:4]
with open(compdb_path) as f:
    data = json.load(f)
filtered = [e for e in data if src_filter in e["file"]] if src_filter else data
with open(out_path, "w") as f:
    json.dump(filtered, f)
print(f"  {len(filtered)} of {len(data)} compdb entries matched")
PYEOF

# Capture the source commit NOW, before indexing — this is the accurate moment
# (the state scip-clang actually reads), and it becomes the store's provenance
# anchor for incremental updates. Best-effort: silently skipped if PROJECT_ROOT
# isn't a git checkout (cppgraph stays general, not git-only).
BUILD_PROVENANCE=()
if SRC_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null)"; then
  BUILD_PROVENANCE+=(--source-commit "$SRC_COMMIT")
  if [[ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ]]; then
    BUILD_PROVENANCE+=(--source-dirty)
  fi
  echo "  source commit: $SRC_COMMIT"
fi

JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
echo "[2/3] Running scip-clang (-j $JOBS, cwd=$PROJECT_ROOT) ..."
(
  cd "$PROJECT_ROOT"
  "$SCIP_CLANG" \
    --compdb-path "$OUT_COMPDB" \
    --index-output-path "$OUT_SCIP" \
    -j "$JOBS" \
    --no-progress-report
)

echo "[3/3] Building the cppgraph graph ..."
"$REPO_ROOT/.venv/bin/cppgraph" build --scip "$OUT_SCIP" --out "$OUT_GRAPH" \
  ${BUILD_PROVENANCE[@]+"${BUILD_PROVENANCE[@]}"}

echo "Done."
echo "  $OUT_COMPDB"
echo "  $OUT_SCIP"
echo "  $OUT_GRAPH"
