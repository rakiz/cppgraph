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
# Two modes:
#
#   Full build (default):
#     scripts/reindex.sh COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
#
#   Incremental update of an existing store:
#     scripts/reindex.sh --update GRAPH_DB COMPDB_PATH [SRC_FILTER] [PROJECT_ROOT]
#
# --- Full build arguments ---
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
# --- Incremental update (--update) ---
#   Re-indexes only the translation units that changed since the store was
#   built and applies them in place, instead of rebuilding from scratch. The
#   changed-file set comes for free from the store's provenance: it diffs
#   `meta.source_commit` (recorded at the last build) against the project's
#   current working tree — no mtime/hash guessing. Then it filters the compdb
#   to the changed TUs, re-indexes just those into a partial .scip, and calls
#   `cppgraph update` (passing deleted files via --deleted). See DESIGN.md §
#   "Keeping the graph up to date".
#
#   GRAPH_DB      an existing store built by `cppgraph build` (required); must
#                 carry a `meta.source_commit` (built with git provenance)
#   COMPDB_PATH   the project's current compile_commands.json (required) —
#                 refresh it first if the build *structure* changed (new
#                 files/targets/includes; see INSTALL.md/AGENTS.md)
#   SRC_FILTER    same substring filter as full build (default: "")
#   PROJECT_ROOT  the git checkout to diff + index from (default: compdb dir)
#
#   Limitation (header changes): a changed header has no compdb entry of its
#   own — it's only re-indexed when a re-indexed TU includes it. So a header
#   edit whose dependent TUs are not themselves in the diff won't fully
#   propagate. The script warns when the diff contains headers; for a
#   structural or widely-included header change, prefer a full rebuild.
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
# Example — incremental update after some edits:
#   scripts/reindex.sh --update scratch/mongo_full.graph.db \
#     /Users/sebastien.mendez/code/mongo/compile_commands.json src/mongo/
#
# Outputs (all under scratch/, gitignored — never committed, see AGENTS.md
# "Large artifacts"):
#   scratch/<OUT_NAME>.compdb.json           filtered compile_commands.json subset
#   scratch/<OUT_NAME>.scip                  scip-clang index (full build)
#   scratch/<OUT_NAME>.graph.db              cppgraph build output (interned SQLite)
#   scratch/<OUT_NAME>.partial.compdb.json   changed-TU compdb subset (--update)
#   scratch/<OUT_NAME>.partial.scip          changed-TU scip-clang index (--update)
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCIP_CLANG="$REPO_ROOT/scratch/bin/scip-clang"
VENV_PY="$REPO_ROOT/.venv/bin/python"
CPPGRAPH="$REPO_ROOT/.venv/bin/cppgraph"

if [[ ! -x "$SCIP_CLANG" ]]; then
  echo "error: $SCIP_CLANG not found/executable. See INSTALL.md section 2." >&2
  exit 1
fi

run_scip_clang() {
  # run_scip_clang PROJECT_ROOT COMPDB OUT_SCIP
  local project_root="$1" compdb="$2" out_scip="$3" jobs
  jobs="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
  echo "  running scip-clang (-j $jobs, cwd=$project_root) ..." >&2
  (
    cd "$project_root"
    "$SCIP_CLANG" \
      --compdb-path "$compdb" \
      --index-output-path "$out_scip" \
      -j "$jobs" \
      --no-progress-report
  )
}

# ---------------------------------------------------------------------------
# Incremental update mode
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--update" ]]; then
  shift
  if [[ $# -lt 2 ]]; then
    echo "usage: $0 --update GRAPH_DB COMPDB_PATH [SRC_FILTER] [PROJECT_ROOT]" >&2
    exit 2
  fi
  GRAPH_DB="$1"
  COMPDB="$2"
  SRC_FILTER="${3:-}"
  COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
  PROJECT_ROOT="${4:-$COMPDB_DIR}"
  OUT_NAME="$(basename "$GRAPH_DB")"; OUT_NAME="${OUT_NAME%.graph.db}"; OUT_NAME="${OUT_NAME%.db}"
  PART_COMPDB="$REPO_ROOT/scratch/${OUT_NAME}.partial.compdb.json"
  PART_SCIP="$REPO_ROOT/scratch/${OUT_NAME}.partial.scip"

  if [[ ! -f "$GRAPH_DB" ]]; then
    echo "error: graph store $GRAPH_DB not found. Run a full build first." >&2
    exit 1
  fi
  if [[ ! -f "$COMPDB" ]]; then
    echo "error: $COMPDB not found." >&2
    exit 1
  fi

  # The store's provenance anchor: the commit whose sources it reflects.
  BASE_COMMIT="$("$VENV_PY" - "$GRAPH_DB" <<'PYEOF'
import sys
from cppgraph.store import GraphStore
print(GraphStore(sys.argv[1]).meta().get("source_commit", ""))
PYEOF
)"
  if [[ -z "$BASE_COMMIT" ]]; then
    echo "error: $GRAPH_DB has no meta.source_commit — can't diff for an" >&2
    echo "       incremental update. Rebuild it with git provenance, or do a" >&2
    echo "       full build." >&2
    exit 1
  fi
  echo "[1/4] Diffing working tree against stored commit $BASE_COMMIT ..."

  # commit -> working tree: captures committed AND uncommitted changes since
  # the indexed state. --diff-filter=d excludes deletions (they can't be
  # re-indexed); =D selects only deletions (passed to `update --deleted`).
  CHANGED="$(git -C "$PROJECT_ROOT" diff --name-only --diff-filter=d "$BASE_COMMIT" -- || true)"
  DELETED="$(git -C "$PROJECT_ROOT" diff --name-only --diff-filter=D "$BASE_COMMIT" -- || true)"

  if [[ -n "$SRC_FILTER" ]]; then
    CHANGED="$(printf '%s\n' "$CHANGED" | grep -F "$SRC_FILTER" || true)"
    DELETED="$(printf '%s\n' "$DELETED" | grep -F "$SRC_FILTER" || true)"
  fi
  CHANGED="$(printf '%s\n' "$CHANGED" | grep -v '^$' || true)"
  DELETED="$(printf '%s\n' "$DELETED" | grep -v '^$' || true)"

  if [[ -z "$CHANGED" && -z "$DELETED" ]]; then
    echo "  nothing changed since $BASE_COMMIT — store is up to date."
    exit 0
  fi
  echo "  changed: $(printf '%s\n' "$CHANGED" | grep -c . || true) file(s), deleted: $(printf '%s\n' "$DELETED" | grep -c . || true) file(s)"

  # Warn on header changes: a header isn't a TU, so it's only re-indexed when a
  # changed TU includes it (see the limitation note in the header comment).
  if printf '%s\n' "$CHANGED" | grep -qE '\.(h|hpp|hh|hxx|ipp|inl)$'; then
    echo "  WARNING: the diff contains header files. Headers are only refreshed" >&2
    echo "           when a re-indexed TU includes them; a widely-included header" >&2
    echo "           change may not fully propagate. Consider a full rebuild." >&2
  fi

  echo "[2/4] Filtering compile_commands.json to the changed TUs ..."
  # `python -` reads the *program* from stdin (the heredoc), so the changed-file
  # list can't also go on stdin — pass it via a file. Match compdb entries whose
  # "file" contains any changed path (substring, to tolerate the absolute/
  # relative mix — see GOTCHA above).
  CHANGED_LIST="$REPO_ROOT/scratch/${OUT_NAME}.changed.txt"
  printf '%s\n' "$CHANGED" > "$CHANGED_LIST"
  MATCHED="$("$VENV_PY" - "$COMPDB" "$PART_COMPDB" "$CHANGED_LIST" <<'PYEOF'
import json, sys
compdb_path, out_path, changed_path = sys.argv[1:4]
with open(changed_path) as f:
    changed = [line for line in f.read().splitlines() if line]
with open(compdb_path) as f:
    data = json.load(f)
filtered = [e for e in data if any(c in e["file"] for c in changed)]
with open(out_path, "w") as f:
    json.dump(filtered, f)
print(len(filtered))
PYEOF
)"
  echo "  $MATCHED changed TU(s) matched in the compdb"

  echo "[3/4] Re-indexing the changed TUs ..."
  if [[ "$MATCHED" -gt 0 ]]; then
    run_scip_clang "$PROJECT_ROOT" "$PART_COMPDB" "$PART_SCIP"
  else
    # Deletions only (or headers with no matching TU): produce an empty partial
    # index so `cppgraph update` can still drop the deleted files' contributions.
    echo "  no TU to re-index; writing an empty partial index for deletions."
    "$VENV_PY" - "$PART_SCIP" "$PROJECT_ROOT" <<'PYEOF'
import sys
from cppgraph.proto import scip_pb2
index = scip_pb2.Index()
index.metadata.project_root = f"file://{sys.argv[2]}"
with open(sys.argv[1], "wb") as f:
    f.write(index.SerializeToString())
PYEOF
  fi

  echo "[4/4] Applying the partial re-index to $GRAPH_DB ..."
  UPDATE_ARGS=(update --graph "$GRAPH_DB" --scip "$PART_SCIP")
  while IFS= read -r f; do
    [[ -n "$f" ]] && UPDATE_ARGS+=(--deleted "$f")
  done <<< "$DELETED"
  # New provenance anchor: the current HEAD (+ dirty flag).
  if NEW_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null)"; then
    UPDATE_ARGS+=(--source-commit "$NEW_COMMIT")
    if [[ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ]]; then
      UPDATE_ARGS+=(--source-dirty)
    fi
  fi
  "$CPPGRAPH" "${UPDATE_ARGS[@]}"

  echo "Done."
  echo "  $PART_COMPDB"
  echo "  $PART_SCIP"
  echo "  $GRAPH_DB (updated in place)"
  exit 0
fi

# ---------------------------------------------------------------------------
# Full build mode
# ---------------------------------------------------------------------------
if [[ $# -lt 1 ]]; then
  echo "usage: $0 COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]" >&2
  echo "       $0 --update GRAPH_DB COMPDB_PATH [SRC_FILTER] [PROJECT_ROOT]" >&2
  exit 2
fi

COMPDB="$1"
SRC_FILTER="${2:-}"
COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
OUT_NAME="${3:-$(basename "$COMPDB_DIR")}"
PROJECT_ROOT="${4:-$COMPDB_DIR}"

OUT_COMPDB="$REPO_ROOT/scratch/${OUT_NAME}.compdb.json"
OUT_SCIP="$REPO_ROOT/scratch/${OUT_NAME}.scip"
OUT_GRAPH="$REPO_ROOT/scratch/${OUT_NAME}.graph.db"

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

echo "[2/3] Running scip-clang ..."
run_scip_clang "$PROJECT_ROOT" "$OUT_COMPDB" "$OUT_SCIP"

echo "[3/3] Building the cppgraph graph ..."
"$CPPGRAPH" build --scip "$OUT_SCIP" --out "$OUT_GRAPH" \
  ${BUILD_PROVENANCE[@]+"${BUILD_PROVENANCE[@]}"}

echo "Done."
echo "  $OUT_COMPDB"
echo "  $OUT_SCIP"
echo "  $OUT_GRAPH"
