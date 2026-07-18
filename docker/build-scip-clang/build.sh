#!/usr/bin/env bash
# Build scip-clang (v0.4.0 + enclosing_range) natively for THIS machine's CPU
# architecture. By default the binary lands in the per-machine data dir where
# reindex.sh looks for it (${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin) — a
# persistent location, not a cache, so this 30-60 min build isn't wiped by a
# cache cleaner. Pass a dir to output elsewhere. The build image is discarded.
#
#   ./build.sh [output_dir]         # default: the data dir (CPPGRAPH_BIN_DIR)
#
# Requires: docker (with BuildKit, default on modern Docker).
set -euo pipefail
cd "$(dirname "$0")"

OUT_DIR="${1:-${CPPGRAPH_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin}}"
mkdir -p "$OUT_DIR"

# Disk preflight. The build compiles LLVM/Clang from source, so Docker's storage
# filesystem needs ~30-40 GB free; a small root partition is the common failure
# (dies late with no binary). Advisory only — Docker layer reuse/pruning changes
# the real need — so warn early rather than hard-fail. Override the threshold or
# skip entirely with CPPGRAPH_MIN_BUILD_GB=0.
MIN_GB="${CPPGRAPH_MIN_BUILD_GB:-35}"
if [ "$MIN_GB" -gt 0 ] 2>/dev/null; then
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  check_path="${docker_root:-/var/lib/docker}"
  [ -d "$check_path" ] || check_path="/"
  avail_gb="$(df -Pk "$check_path" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')"
  if [ -n "${avail_gb:-}" ] && [ "$avail_gb" -lt "$MIN_GB" ]; then
    echo "WARNING: only ${avail_gb} GB free on Docker's storage ($check_path)." >&2
    echo "         This build needs ~${MIN_GB} GB and may fail late with no binary." >&2
    echo "         Fixes: free space; point Docker's data-root at a larger partition;" >&2
    echo "         or use the emulated indexer (setup.sh --scip-source emulate)." >&2
    if [ -t 0 ]; then
      read -r -p "         Continue anyway? [y/N] " _ans
      case "${_ans:-}" in
        y | Y | yes | YES) ;;
        *) echo "Aborted — free up disk or use --scip-source emulate." >&2; exit 1 ;;
      esac
    else
      echo "         (non-interactive: continuing anyway)" >&2
    fi
  fi
fi

echo "==> Building scip-clang natively for host arch: $(uname -m)"
echo "    (compiles LLVM-based code from source; expect ~30-60 min)"

# --output writes the 'export' stage's filesystem (just the binary) to OUT_DIR.
DOCKER_BUILDKIT=1 docker build \
    --target export \
    --output "type=local,dest=${OUT_DIR}" \
    -t scip-clang-builder:local \
    .

BIN="${OUT_DIR}/scip-clang"
chmod +x "$BIN"

# Provenance sidecar next to the binary (same format setup.sh writes): this build
# carries PR #504, so the variant is enclosing_range-504. `cppgraph status` reads
# it to compare against the pin. Version parsed from the binary ("scip-clang X").
ver="$("$BIN" --version 2>/dev/null | awk '/scip-clang/{print $2; exit}')"
cat > "${OUT_DIR}/scip-clang.json" <<EOF
{"version": "${ver:-0.4.0}", "variant": "enclosing_range-504", "source": "build", "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

echo
echo "==> Done. Binary at: ${BIN}"
file "$BIN" 2>/dev/null || true
"$BIN" --version || true
