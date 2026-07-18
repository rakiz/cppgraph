#!/usr/bin/env bash
# Produce a SCIP index on a host that can't run scip-clang natively (ARM-Linux,
# Windows) by running it in an x86_64 container. ONLY the .scip is built here —
# the graph is then built natively (this script prints the exact command).
#
# Same interface as reindex.sh's full build:
#   scripts/index-in-container.sh COMPDB [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
#
#   COMPDB        path to compile_commands.json
#   SRC_FILTER    keep only TUs whose file path contains this (e.g. 'src/foo/');
#                 "" or omit to index everything (filtered host-side, native)
#   OUT_NAME      output basename (default: PROJECT_ROOT's dir name)
#   PROJECT_ROOT  dir to run scip-clang from (default: the compdb's dir)
#
# Writes <PROJECT_ROOT>/.cppgraph/<OUT_NAME>.scip. Container engine is auto-
# detected (docker or podman — podman is daemonless/rootless/FOSS); force one
# with CPPGRAPH_CONTAINER=podman. Env: CPPGRAPH_INDEX_IMAGE (image tag, default
# cppgraph-index:latest), SCIP_CLANG_VERSION (build arg).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${CPPGRAPH_INDEX_IMAGE:-cppgraph-index:latest}"

[ $# -ge 1 ] || { awk 'NR>1{ if (/^#/) print; else exit }' "$0"; exit 2; }

# Pick a container engine: explicit override, else first of docker/podman found.
ENGINE="${CPPGRAPH_CONTAINER:-}"
if [ -z "$ENGINE" ]; then
  for e in docker podman; do
    if command -v "$e" >/dev/null 2>&1; then ENGINE="$e"; break; fi
  done
fi
if [ -z "$ENGINE" ] || ! command -v "$ENGINE" >/dev/null 2>&1; then
  echo "error: no container engine found. Install docker or podman (podman is" >&2
  echo "daemonless/rootless/FOSS — e.g. 'apt install podman qemu-user-static')," >&2
  echo "or set CPPGRAPH_CONTAINER to the engine to use." >&2
  exit 1
fi
echo "==> Using container engine: $ENGINE" >&2

# Preflight: we build/run an x86_64 image. On a non-x86_64 Linux host that needs
# QEMU binfmt emulation; without it the daemon pulls the amd64 image fine but the
# first RUN dies with a cryptic "exec /bin/sh: exec format error". Detect the
# missing registration up front and print the one-line fix instead.
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
case "$(uname -s 2>/dev/null)":"$HOST_ARCH" in
  Linux:x86_64|Linux:amd64) ;;                        # native, no emulation needed
  Linux:*)
    if ! ls /proc/sys/fs/binfmt_misc/qemu-x86_64 >/dev/null 2>&1; then
      cat >&2 <<EOF
error: this host is $HOST_ARCH but the indexer image is x86_64, and QEMU binfmt
emulation for amd64 is not registered — the build would fail with
"exec /bin/sh: exec format error". Register it once (persists until reboot):

  $ENGINE run --privileged --rm tonistiigi/binfmt --install amd64

or install it permanently on Ubuntu/Debian:

  sudo apt-get install -y qemu-user-static binfmt-support

Verify with:  $ENGINE run --rm --platform linux/amd64 alpine uname -m   # -> x86_64
Then re-run this script.
EOF
      exit 1
    fi ;;
esac

COMPDB="$1"; SRC_FILTER="${2:-}"
if [ ! -f "$COMPDB" ]; then
  echo "error: compile_commands.json not found: $COMPDB" >&2
  echo "  Generate one for your build first:" >&2
  echo "    CMake:       cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...  (in the build dir)" >&2
  echo "    Bazel:       bazel run @hedron_compile_commands//:refresh_all" >&2
  echo "                 (or the project's own target, e.g. MongoDB: bazel build --config=compiledb //src/...)" >&2
  echo "    Make/other:  bear -- <your build command>" >&2
  exit 1
fi
COMPDB="$(cd "$(dirname "$COMPDB")" && pwd)/$(basename "$COMPDB")"   # absolutize
COMPDB_DIR="$(dirname "$COMPDB")"
PROJECT_ROOT="$(cd "${4:-$COMPDB_DIR}" && pwd)"
OUT_NAME="${3:-$(basename "$PROJECT_ROOT")}"
OUT_DIR="$PROJECT_ROOT/.cppgraph"
OUT_SCIP="$OUT_DIR/$OUT_NAME.scip"
mkdir -p "$OUT_DIR"

echo "==> Building the x86_64 indexer image ($IMAGE) — cached after first run" >&2
"$ENGINE" build --platform=linux/amd64 \
  --build-arg "SCIP_CLANG_VERSION=${SCIP_CLANG_VERSION:-v0.4.0}" \
  -t "$IMAGE" "$REPO_ROOT/docker/index" >&2

# Optional source-subtree filter, done host-side with native python (keeps the
# container to just scip-clang). Mirrors reindex.sh's filter.
USE_COMPDB="$COMPDB"
if [ -n "$SRC_FILTER" ]; then
  USE_COMPDB="$OUT_DIR/$OUT_NAME.compdb.json"
  python3 - "$COMPDB" "$SRC_FILTER" "$USE_COMPDB" <<'PY'
import json, sys
src, flt, out = sys.argv[1:4]
db = json.load(open(src))
kept = [e for e in db if flt in e.get("file", "")]
if not kept:
    sys.exit(f"no TU in {src} matches filter {flt!r}")
json.dump(kept, open(out, "w"))
print(f"  filtered compdb: {len(kept)}/{len(db)} TUs match {flt!r}", file=sys.stderr)
PY
fi

# Mount the project (and the compdb dir, if it lives outside) at their SAME
# absolute paths so the absolute paths inside compile_commands.json resolve.
mounts=(-v "$PROJECT_ROOT:$PROJECT_ROOT")
case "$COMPDB_DIR/" in "$PROJECT_ROOT"/*) ;; *) mounts+=(-v "$COMPDB_DIR:$COMPDB_DIR") ;; esac

echo "==> Running scip-clang in-container (x86_64; emulated on ARM hosts) ..." >&2
"$ENGINE" run --rm --platform=linux/amd64 \
  "${mounts[@]}" -w "$PROJECT_ROOT" "$IMAGE" \
  --compdb-path "$USE_COMPDB" \
  --index-output-path "$OUT_SCIP" \
  -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

OUT_GRAPH="$OUT_DIR/$OUT_NAME.graph.db"
CPPGRAPH="$REPO_ROOT/.venv/bin/cppgraph"
echo >&2
echo "==> SCIP index written: $OUT_SCIP" >&2

# Resume the (native, non-container) build automatically — that's the actual
# indexing step, and it runs anywhere. Skip with CPPGRAPH_INDEX_NO_BUILD=1 to
# stop at the .scip (e.g. to hand it to another machine).
if [ -n "${CPPGRAPH_INDEX_NO_BUILD:-}" ]; then
  cat >&2 <<EOF
Build the graph natively (native step, no container):
  "$CPPGRAPH" build --scip "$OUT_SCIP" --out "$OUT_GRAPH"
EOF
elif [ -x "$CPPGRAPH" ]; then
  echo "==> Resuming natively: building the graph from the index ..." >&2
  # The container runs the stock x86 release binary, so stamp that variant.
  "$CPPGRAPH" build --scip "$OUT_SCIP" --out "$OUT_GRAPH" --scip-variant stock
  echo >&2
  echo "==> Graph built: $OUT_GRAPH" >&2
  echo "    Register the MCP as usual (see QUICKSTART.md)." >&2
else
  cat >&2 <<EOF
No native cppgraph found at $CPPGRAPH — run scripts/setup.sh first, then:
  <venv>/cppgraph build --scip "$OUT_SCIP" --out "$OUT_GRAPH"
EOF
fi
