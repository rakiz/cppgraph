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
