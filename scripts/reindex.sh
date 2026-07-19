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
#     scripts/reindex.sh [--attributed-refs] [--no-tests] COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
#
#   Incremental update of an existing store:
#     scripts/reindex.sh --update GRAPH_DB COMPDB_PATH [SRC_FILTER] [PROJECT_ROOT]
#
#   Preview what a compdb contains before indexing (TUs, subtrees, tests):
#     cppgraph compdb-summary COMPDB_PATH [--filter SUBSTR]
#
# --- Full build arguments (leading flags, any order) ---
#   --attributed-refs  upgrade the reference index to SYMBOL granularity — records
#                 which definition uses each symbol, so "where is this type used?"
#                 answers with the functions, not just the files. Needs a #504
#                 scip-clang (emits enclosing_range); larger store. Default off
#                 (file granularity, already exact). Can also be added afterwards
#                 without re-indexing: `cppgraph enrich-refs`.
#   --no-tests    drop test TUs (is_test_file) from the index for a lighter,
#                 production-only graph. (Queries drop tests by default anyway;
#                 this also keeps them out of the store.)
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
#   SRC_FILTER    optional; normally omitted. The update reuses the *recorded*
#                 index scope (subtree filter + tests state stored in the graph),
#                 so it stays consistent with the original build. If you pass a
#                 non-empty filter that disagrees with the recorded one, it errors
#                 — changing scope requires a full rebuild, not an update. (Legacy
#                 graphs without a recorded scope fall back to this argument.)
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
# Prerequisites: the scip-clang binary (per-machine data dir) and the `.venv` (in
# the checkout) — both set up by scripts/setup.sh, per-machine, not committed.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The venv lives in the cppgraph checkout; the scip-clang binary is a per-machine
# artifact shared across projects, kept in the persistent data dir XDG_DATA_HOME
# (set up by setup.sh; override with CPPGRAPH_BIN_DIR). Per-project outputs go to
# the target project's own .cppgraph/ (see out_dir_for).
SCIP_CLANG="${CPPGRAPH_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin}/scip-clang"
VENV_PY="$REPO_ROOT/.venv/bin/python"
CPPGRAPH="$REPO_ROOT/.venv/bin/cppgraph"

# A native scip-clang isn't available on every platform (no arm64-linux binary,
# no Windows). We don't hard-fail on its absence: a full build can instead reuse
# a .scip produced elsewhere (scripts/index-in-container.sh, or copied in). Only
# incremental --update genuinely needs the binary (it indexes changed TUs).
if [[ -x "$SCIP_CLANG" ]]; then HAVE_SCIP_CLANG=1; else HAVE_SCIP_CLANG=0; fi

# The binary's variant (stock vs enclosing_range-504), from its provenance
# sidecar — stamped into the store so `cppgraph status` can tell when a graph is
# stale for the pinned indexer. Only trusted when we run this native binary; when
# reusing a .scip built elsewhere we don't know its variant, so leave it unset.
SCIP_VARIANT=""
_prov="$(dirname "$SCIP_CLANG")/scip-clang.json"
if [[ "$HAVE_SCIP_CLANG" == 1 && -f "$_prov" ]]; then
  SCIP_VARIANT="$("$VENV_PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("variant",""))' "$_prov" 2>/dev/null || true)"
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
# Indexing runs the C++ front-end once per TU, so wall time is CPU-bound. The
# per-TU cost swings widely with core speed: ~3 CPU-s on a fast x86 core, ~18
# CPU-s on an older ARM core (measured: 6482 TUs in ~4 h at -j8 on an AWS
# Graviton2 m6g). Give a range rather than one optimistic number.
lo = tus * 3.0 / jobs + 10
hi = tus * 18.0 / jobs + 10
def fmt(s):
    return "under a minute" if s < 60 else (f"~{round(s/60)} min" if s < 5400 else f"~{s/3600:.1f} h")
print(f"  {tus} translation units, indexing with -j{jobs}")
print(f"  estimated time: {fmt(lo)} to {fmt(hi)} (rough; CPU-bound —")
print(f"                  older/ARM cores like Graviton2 land at the high end)")
if hi > 600:
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
  if [[ "$HAVE_SCIP_CLANG" != 1 ]]; then
    echo "error: incremental --update needs a native scip-clang, which isn't" >&2
    echo "available on this platform. Do a full re-index instead (it can reuse a" >&2
    echo ".scip from scripts/index-in-container.sh), or run --update on an x86_64 host." >&2
    exit 1
  fi
  if [[ ! -f "$GRAPH_DB" ]]; then
    echo "error: graph store not found: $GRAPH_DB (run a full build first)." >&2
    exit 1
  fi
  if [[ ! -f "$COMPDB" ]]; then
    echo "error: compile_commands.json not found: $COMPDB" >&2
    exit 1
  fi
  COMPDB_DIR="$(cd "$(dirname "$COMPDB")" && pwd)"
  # Same project-root rule as a full build: git top-level, not the compdb dir
  # (which is often build/ — see the note in the full-build path below).
  PROJECT_ROOT="${4:-$(git -C "$COMPDB_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$COMPDB_DIR")}"
  OUT_NAME="$(basename "$GRAPH_DB")"; OUT_NAME="${OUT_NAME%.graph.db}"; OUT_NAME="${OUT_NAME%.db}"
  OUT_DIR="$(out_dir_for "$PROJECT_ROOT")"
  PART_COMPDB="$OUT_DIR/${OUT_NAME}.partial.compdb.json"
  PART_SCIP="$OUT_DIR/${OUT_NAME}.partial.scip"

  # The store's provenance anchor: the commit whose sources it reflects, plus the
  # recorded index scope (subtree filter + tests state). An update must stay within
  # the graph's scope, so the recorded scope — not a re-typed argument — is the
  # source of truth. Emitted as 4 lines: commit, FILTER_SET/FILTER_UNSET, filter,
  # tests. FILTER_UNSET marks a legacy graph built before scope was recorded.
  # Read the 4 lines into vars without `mapfile` (absent in the bash 3.2 that
  # ships on macOS). Each field is a whole line, so `sed -n 'Np'` is exact.
  _META="$("$VENV_PY" - "$GRAPH_DB" <<'PYEOF'
import sys
from cppgraph.store import GraphStore
m = GraphStore(sys.argv[1]).meta()
print(m.get("source_commit", ""))
print("FILTER_SET" if "index_filter" in m else "FILTER_UNSET")
print(m.get("index_filter", ""))
print(m.get("index_tests", ""))
PYEOF
)"
  BASE_COMMIT="$(printf '%s\n' "$_META" | sed -n '1p')"
  REC_FILTER_SET="$(printf '%s\n' "$_META" | sed -n '2p')"
  REC_FILTER="$(printf '%s\n' "$_META" | sed -n '3p')"
  REC_TESTS="$(printf '%s\n' "$_META" | sed -n '4p')"
  if [[ -z "$BASE_COMMIT" ]]; then
    echo "error: $GRAPH_DB has no meta.source_commit — can't diff for an" >&2
    echo "       incremental update. Rebuild it with git provenance, or do a" >&2
    echo "       full build." >&2
    exit 1
  fi

  # Reuse the recorded scope. A non-empty positional SRC_FILTER that disagrees with
  # the recorded one would produce a graph that is neither the old scope nor a
  # clean new one — refuse it; changing scope means a full rebuild. An omitted
  # (empty) arg simply defers to the recorded scope.
  if [[ "$REC_FILTER_SET" == "FILTER_SET" ]]; then
    if [[ -n "$SRC_FILTER" && "$SRC_FILTER" != "$REC_FILTER" ]]; then
      echo "error: this graph was indexed with scope '${REC_FILTER:-<whole tree>}'," >&2
      echo "       but you passed filter '$SRC_FILTER'. An update must keep the graph's" >&2
      echo "       scope. To index a different scope, do a full rebuild instead." >&2
      exit 1
    fi
    SRC_FILTER="$REC_FILTER"
    _tests_state="$REC_TESTS"
    echo "  indexed scope: ${REC_FILTER:-<whole tree>}${_tests_state:+ (tests $_tests_state)}"
  else
    # Legacy graph without a recorded scope: fall back to the positional argument.
    _tests_state=""
    echo "  indexed scope: not recorded (legacy graph); using filter '${SRC_FILTER:-<none>}'"
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
  # Honour the recorded tests state: a graph built --no-tests must not re-absorb
  # test TUs on update. Drop them here (same is_test_file used by the full build).
  _no_tests_update=0; [[ "$_tests_state" == "excluded" ]] && _no_tests_update=1
  MATCHED="$("$VENV_PY" - "$COMPDB" "$PART_COMPDB" "$CHANGED_LIST" "$_no_tests_update" <<'PYEOF'
import json, sys

from cppgraph.export import is_test_file

compdb_path, out_path, changed_path, no_tests = sys.argv[1:5]
with open(changed_path) as f:
    changed = [line for line in f.read().splitlines() if line]
with open(compdb_path) as f:
    data = json.load(f)
filtered = [e for e in data if any(c in e["file"] for c in changed)]
if no_tests == "1":
    filtered = [e for e in filtered if not is_test_file(e.get("file", ""))]
with open(out_path, "w") as f:
    json.dump(filtered, f)
print(len(filtered))
PYEOF
)"
  _excl_note=""; [[ "$_no_tests_update" == 1 ]] && _excl_note=" (test TUs excluded per recorded scope)"
  echo "  $MATCHED changed TU(s) matched in the compdb${_excl_note}"

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
  [[ -n "$SCIP_VARIANT" ]] && UPDATE_ARGS+=(--scip-variant "$SCIP_VARIANT")
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
# Optional leading flags (any order):
#   --attributed-refs  upgrade the reference index to SYMBOL granularity (the
#                      functions that use a type, not just the files). Needs a
#                      #504 scip-clang (enclosing_range); larger store. Off by
#                      default — the plain reference index is already exact.
#   --no-tests         drop test translation units (is_test_file) from the index,
#                      for a lighter, production-only graph. (Queries drop tests
#                      by default anyway; this also keeps them out of the store.)
ATTRIBUTED_REFS=0
NO_TESTS=0
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --attributed-refs) ATTRIBUTED_REFS=1; shift ;;
    --no-tests)        NO_TESTS=1; shift ;;
    *) echo "error: unknown flag '$1' (expected --attributed-refs / --no-tests)" >&2; exit 2 ;;
  esac
done

if [[ $# -lt 1 ]]; then
  echo "usage: $0 [--attributed-refs] [--no-tests] COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]" >&2
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
# Default the project root to the git top-level of the compdb's directory, NOT the
# compdb dir. A compile_commands.json commonly lives in build/; using build/ as the
# root makes scip-clang cd there and drop every source under ../src/ (only vendored
# code physically under build/ survives) — a silently broken graph. git top-level
# is the real root. Falls back to the compdb dir when it isn't a git checkout.
PROJECT_ROOT="${4:-$(git -C "$COMPDB_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$COMPDB_DIR")}"
OUT_NAME="${3:-$(basename "$PROJECT_ROOT")}"
if [[ "$PROJECT_ROOT" != "$COMPDB_DIR" ]]; then
  echo "  note: project root = $PROJECT_ROOT (compile_commands.json is under $COMPDB_DIR)" >&2
fi

OUT_DIR="$(out_dir_for "$PROJECT_ROOT")"
OUT_COMPDB="$OUT_DIR/${OUT_NAME}.compdb.json"
OUT_SCIP="$OUT_DIR/${OUT_NAME}.scip"
OUT_GRAPH="$OUT_DIR/${OUT_NAME}.graph.db"

_tests_note=""; [[ "$NO_TESTS" == 1 ]] && _tests_note=", excluding tests"
echo "[1/3] Filtering compile_commands.json (substring: '${SRC_FILTER:-<none, indexing everything>}'${_tests_note}) ..."
# Uses the venv python so it can reuse cppgraph's is_test_file for --no-tests.
"$VENV_PY" - "$COMPDB" "$SRC_FILTER" "$OUT_COMPDB" "$NO_TESTS" <<'PYEOF'
import json
import sys

from cppgraph.export import is_test_file

compdb_path, src_filter, out_path, no_tests = sys.argv[1:5]
with open(compdb_path) as f:
    data = json.load(f)
filtered = [e for e in data if src_filter in e["file"]] if src_filter else list(data)
dropped = 0
if no_tests == "1":
    kept = [e for e in filtered if not is_test_file(e.get("file", ""))]
    dropped = len(filtered) - len(kept)
    filtered = kept
with open(out_path, "w") as f:
    json.dump(filtered, f)
msg = f"  {len(filtered)} of {len(data)} compdb entries matched"
if no_tests == "1":
    msg += f" ({dropped} test TU(s) excluded)"
print(msg)
if not filtered:
    sys.exit(
        f"  error: 0 entries left after filtering (substring {src_filter!r}"
        f"{', --no-tests' if no_tests == '1' else ''}). The filter is a plain "
        f'substring of each entry\'s "file" field (e.g. \'src/\', no leading '
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
[[ -n "$SCIP_VARIANT" ]] && BUILD_PROVENANCE+=(--scip-variant "$SCIP_VARIANT")

# Record the index scope (subtree filter + tests-included/excluded) in the graph,
# so `cppgraph status` shows it and an incremental --update reuses the same scope.
# --index-filter is always passed (empty = whole tree) to make the scope explicit.
BUILD_PROVENANCE+=(--index-filter "$SRC_FILTER")
[[ "$NO_TESTS" == 1 ]] && BUILD_PROVENANCE+=(--index-no-tests)

if [[ "$HAVE_SCIP_CLANG" == 1 ]]; then
  echo "[2/3] Running scip-clang ..."
  print_estimate "$OUT_COMPDB"
  run_scip_clang "$PROJECT_ROOT" "$OUT_COMPDB" "$OUT_SCIP"
elif [[ -f "$OUT_SCIP" ]]; then
  echo "[2/3] No native scip-clang here — reusing the existing index:"
  echo "      $OUT_SCIP"
  echo "      (delete it to force a fresh one; regenerate via scripts/index-in-container.sh)"
else
  cat >&2 <<EOF
[2/3] No native scip-clang on this platform, and no prebuilt index at:
        $OUT_SCIP
Two ways forward:
  1. Generate it here, then re-run this exact command (it will pick the index up):
       scripts/index-in-container.sh "$COMPDB" "$SRC_FILTER" "$OUT_NAME" "$PROJECT_ROOT"
  2. Already have a .scip (e.g. built on another machine for this same checkout)?
     Skip re-indexing and build the graph straight from it:
       "$CPPGRAPH" build --scip <that>.scip --out "$OUT_GRAPH"
EOF
  exit 1
fi

# Reference-attribution mode. --attributed-refs upgrades the usage view to symbol
# granularity, but only a #504 binary emits the enclosing_range it needs — warn
# instead of silently producing a file-granularity graph the user thinks is rich.
BUILD_ATTR=()
if [[ "$ATTRIBUTED_REFS" == 1 ]]; then
  if [[ "$SCIP_VARIANT" == "enclosing_range-504" ]]; then
    BUILD_ATTR+=(--attributed-refs)
    echo "  reference attribution: SYMBOL granularity (--attributed-refs; larger store)"
  else
    echo "  warning: --attributed-refs requested, but this scip-clang"
    echo "           (variant: ${SCIP_VARIANT:-unknown}) does not emit enclosing_range."
    echo "           Producing the file-granularity graph instead. Use a #504 build"
    echo "           for symbol granularity."
  fi
fi

echo "[3/3] Building the cppgraph graph ..."
"$CPPGRAPH" build --scip "$OUT_SCIP" --out "$OUT_GRAPH" \
  ${BUILD_ATTR[@]+"${BUILD_ATTR[@]}"} \
  ${BUILD_PROVENANCE[@]+"${BUILD_PROVENANCE[@]}"}

echo "Done. Graph: $OUT_GRAPH"
# The .scip is kept next to the graph, so a file-granularity graph built with a
# #504 binary can be upgraded to symbol granularity later without re-indexing.
if [[ "$ATTRIBUTED_REFS" != 1 && "$SCIP_VARIANT" == "enclosing_range-504" ]]; then
  echo
  echo "Tip: this graph is file-granularity. To upgrade to SYMBOL granularity"
  echo "     ('which functions use this type?') without re-indexing, run:"
  echo "       $CPPGRAPH enrich-refs --graph $OUT_GRAPH --scip $OUT_SCIP"
fi
echo
echo "Use it in Claude Code:"
echo "  1. register once per machine (part of setup; skip if already done):"
echo "       scripts/register-mcp.sh"
echo "  2. open Claude Code from this project, in a NEW session:"
echo "       $PROJECT_ROOT"
echo "     then ask your questions — it uses this project's graph automatically."
