#!/usr/bin/env bash
set -euo pipefail

# Index any C++ project's compile_commands.json into a SCIP index + a
# cppgraph graph store (SQLite). Generic — cppgraph works on any project that
# provides a compile_commands.json (never hard-code a project's paths here; see
# AGENTS.md). Wraps the three steps
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
#   OUT_NAME      basename (no extension) for the outputs (default: project dir name)
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
# Example — index a whole project's sources (filter to your source subtree to
# skip third_party/vendored code):
#   scripts/reindex.sh /path/to/project/compile_commands.json src/ myproject
#
# Example — one subsystem instead, for fast iteration:
#   scripts/reindex.sh /path/to/project/compile_commands.json \
#     src/subsystem/ subsystem
#
# Example — incremental update after some edits:
#   scripts/reindex.sh --update /path/to/project/.cppgraph/myproject.graph.db \
#     /path/to/project/compile_commands.json src/
#
# Outputs live in the TARGET project's own .cppgraph/ (next to its code, like
# .vscode/), gitignored via a dropped-in .gitignore of "*" so they never dirty
# the repo. PROJECT_ROOT/.cppgraph/:
#   <OUT_NAME>.compdb.json           filtered compile_commands.json subset
#   <OUT_NAME>.scip                  scip-clang index (full build)
#   <OUT_NAME>.graph.db              cppgraph build output (interned SQLite)
#   <OUT_NAME>.partial.compdb.json   changed-TU compdb subset (--update)
#   <OUT_NAME>.partial.scip          changed-TU scip-clang index (--update)
#
# GOTCHA (kept here so it isn't rediscovered on the next project): some build
# systems' generated compile_commands.json is not uniformly formatted — entries
# may mix an absolute path and a bare relative path for logically equivalent
# locations (e.g. a Bazel-generated compdb where most entries use an absolute
# bazel-out path but a handful use a bare "src/..." relative path for the same
# file). A SRC_FILTER that requires a leading "/" would silently
# drop the bare-relative ones. This script does a plain substring match
# (no anchoring), which is robust to both forms — keep it that way, and
# don't assume a compdb's "file" field is uniformly absolute or relative for
# a new project either.
#
# Prerequisites: the scip-clang binary and the `.venv` — both set up by
# scripts/setup.sh (per-machine, in the cppgraph checkout, not committed).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Per-machine tool install (the scip-clang binary and the venv) lives in the
# cppgraph checkout; per-project outputs live in the target (see out_dir_for).
SCIP_CLANG="$REPO_ROOT/scratch/bin/scip-clang"
VENV_PY="$REPO_ROOT/.venv/bin/python"
CPPGRAPH="$REPO_ROOT/.venv/bin/cppgraph"

if [[ ! -x "$SCIP_CLANG" ]]; then
  echo "error: $SCIP_CLANG not found/executable. Run scripts/setup.sh (or see INSTALL.md)." >&2
  exit 1
fi

# Per-project outputs (graph.db, .scip, filtered compdb) live in the target
# project's own .cppgraph/ — next to the code they describe, like .vscode/, and
# gitignored (a .gitignore of "*" is dropped in) so they never dirty the repo.
out_dir_for() {
  local d="$1/.cppgraph"
  mkdir -p "$d"
  [[ -f "$d/.gitignore" ]] || printf '*\n' > "$d/.gitignore"
  printf '%s' "$d"
}

num_jobs() { sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4; }

# Deterministic time estimate so neither a human nor an LLM has to guess (and
# guess "hours"). Calibrated on measured runs: ~3 CPU-seconds per translation
# unit, parallelised across -j cores, plus a small fixed overhead. Rough by
# design — machines and TUs vary — but the right order of magnitude.
print_estimate() {
  # print_estimate FILTERED_COMPDB
  local compdb="$1" jobs; jobs="$(num_jobs)"
  python3 - "$compdb" "$jobs" <<'PY'
import json, sys
compdb, jobs = sys.argv[1], max(int(sys.argv[2]), 1)
tus = len(json.load(open(compdb)))
secs = tus * 3.0 / jobs + 10          # ~3 CPU-s/TU across `jobs` cores + overhead
est = "under a minute" if secs < 60 else f"about {round(secs/60)} minute(s)"
print(f"  {tus} translation units, indexing with -j{jobs}")
print(f"  estimated time: {est} (rough, first run)")
if secs > 300:
    print("  tip: pass a subtree filter (e.g. 'src/foo/') to index less and go faster")
PY
}

run_scip_clang() {
  # run_scip_clang PROJECT_ROOT COMPDB OUT_SCIP
  local project_root="$1" compdb="$2" out_scip="$3" jobs; jobs="$(num_jobs)"
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
  if [[ ! -f "$GRAPH_DB" ]]; then
    echo "error: graph store not found: $GRAPH_DB (run a full build first)." >&2
    exit 1
  fi
  if [[ ! -f "$COMPDB" ]]; then
    echo "error: compile_commands.json not found: $COMPDB" >&2
    exit 1
  fi
  COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
  PROJECT_ROOT="${4:-$COMPDB_DIR}"
  OUT_NAME="$(basename "$GRAPH_DB")"; OUT_NAME="${OUT_NAME%.graph.db}"; OUT_NAME="${OUT_NAME%.db}"
  OUT_DIR="$(out_dir_for "$PROJECT_ROOT")"
  PART_COMPDB="$OUT_DIR/${OUT_NAME}.partial.compdb.json"
  PART_SCIP="$OUT_DIR/${OUT_NAME}.partial.scip"

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
  CHANGED_LIST="$OUT_DIR/${OUT_NAME}.changed.txt"
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
    print_estimate "$PART_COMPDB"
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
if [[ ! -f "$COMPDB" ]]; then
  echo "error: compile_commands.json not found: $COMPDB" >&2
  echo "       pass the path to your project's compile_commands.json as the first argument" >&2
  echo "       (see AGENTS.md -> 'The compilation database' for how to produce one)." >&2
  exit 1
fi
COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
OUT_NAME="${3:-$(basename "$COMPDB_DIR")}"
PROJECT_ROOT="${4:-$COMPDB_DIR}"

OUT_DIR="$(out_dir_for "$PROJECT_ROOT")"
OUT_COMPDB="$OUT_DIR/${OUT_NAME}.compdb.json"
OUT_SCIP="$OUT_DIR/${OUT_NAME}.scip"
OUT_GRAPH="$OUT_DIR/${OUT_NAME}.graph.db"

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
if src_filter and not filtered:
    sys.exit(
        f"  error: 0 entries matched filter {src_filter!r}. It is a plain "
        f"substring of each entry's \"file\" field (e.g. 'src/', no leading "
        f"slash). Check a sample path in {compdb_path} and adjust."
    )
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
print_estimate "$OUT_COMPDB"
run_scip_clang "$PROJECT_ROOT" "$OUT_COMPDB" "$OUT_SCIP"

echo "[3/3] Building the cppgraph graph ..."
"$CPPGRAPH" build --scip "$OUT_SCIP" --out "$OUT_GRAPH" \
  ${BUILD_PROVENANCE[@]+"${BUILD_PROVENANCE[@]}"}

echo "Done. Graph: $OUT_GRAPH"
echo
echo "Register it with Claude Code (then open a new session):"
echo "  scripts/register-mcp.sh \"$OUT_GRAPH\" \"$PROJECT_ROOT\""
