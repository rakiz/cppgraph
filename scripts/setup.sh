#!/usr/bin/env bash
# One-time cppgraph setup: Python venv + deps + the scip-clang indexer binary.
#
# Supported (for local indexing, because scip-clang only ships these binaries):
#   - macOS Apple Silicon (arm64)
#   - Linux x86_64
#   - Windows: run this inside WSL2 (Ubuntu) — it behaves as Linux x86_64.
# NOT supported: Intel Mac, ARM Linux (no scip-clang binary). Those can still
# *use* a graph.db someone else built (query/MCP/viz are pure Python) — ask the
# maintainer for a prebuilt graph.db and skip straight to registering the MCP.
#
# Prereqs: `uv` (https://docs.astral.sh/uv/) and `curl`.
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

os="$(uname -s)"; arch="$(uname -m)"
case "$os/$arch" in
  Darwin/arm64)  asset="scip-clang-arm64-darwin" ;;
  Linux/x86_64)  asset="scip-clang-x86_64-linux" ;;
  Darwin/x86_64)
    echo "Intel Mac isn't supported for local indexing (no scip-clang binary)." >&2
    echo "Use Apple Silicon / Linux x86_64, or ask for a prebuilt graph.db." >&2
    exit 1 ;;
  *)
    echo "Unsupported platform: $os/$arch." >&2
    echo "Supported: macOS arm64, Linux x86_64 (Windows: run inside WSL2 Ubuntu)." >&2
    exit 1 ;;
esac

command -v uv   >/dev/null || { echo "uv not found — install: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl not found — please install it." >&2; exit 1; }

echo "==> Python venv + dependencies (.venv)"
if [ -d .venv ]; then
  echo "  reusing existing .venv"
else
  uv venv
fi
uv pip install -e ".[dev,mcp]"

SCIP_VERSION="v0.4.0"
mkdir -p scratch/bin
if [ -x scratch/bin/scip-clang ]; then
  echo "==> scip-clang already present (scratch/bin/scip-clang)"
else
  echo "==> Downloading scip-clang $SCIP_VERSION ($asset)"
  url="https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_VERSION}/${asset}"
  if ! curl -fL --retry 3 -o scratch/bin/scip-clang "$url"; then
    rm -f scratch/bin/scip-clang
    echo "error: failed to download scip-clang from:" >&2
    echo "       $url" >&2
    echo "       Check your network/proxy, then re-run. Or download it manually" >&2
    echo "       to scratch/bin/scip-clang and 'chmod +x' it." >&2
    exit 1
  fi
  chmod +x scratch/bin/scip-clang
fi

echo "==> Verifying"
.venv/bin/python -c "from cppgraph.proto import scip_pb2; scip_pb2.Index()" && echo "  python package OK"
scratch/bin/scip-clang --version | head -1

cat <<'EOF'

Setup complete. Next (see QUICKSTART.md):
  1. Build a graph:  scripts/reindex.sh /path/to/compile_commands.json src/ myproject
     (writes into <project>/.cppgraph/ and prints the exact register command)
  2. Run the register command it printed, then open a new Claude Code session.
EOF
