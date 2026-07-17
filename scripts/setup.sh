#!/usr/bin/env bash
# One-time cppgraph setup: Python venv + deps + the scip-clang indexer binary.
#
# Version selection (cppgraph is pure Python, so a version is just a git tag —
# no build, checkout + editable install is the whole story):
#   scripts/setup.sh                 install the current checkout as-is (dev
#                                    default); if the tree is clean and a stable
#                                    release exists, check out that tag first
#   scripts/setup.sh --version 0.2.0 pin to a released version (tag v0.2.0)
#   scripts/setup.sh --nightly       track the main branch (bleeding edge)
#   scripts/setup.sh --branch foo    check out an arbitrary branch
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

# --- version / ref selection ------------------------------------------------
ref_mode="default"; ref_arg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) ref_arg="${2:?--version needs a value (e.g. 0.2.0)}"; ref_mode="version"; shift 2 ;;
    --branch)  ref_arg="${2:?--branch needs a value}"; ref_mode="branch"; shift 2 ;;
    --nightly) ref_mode="nightly"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

latest_stable() {  # newest stable tag from versions.json, or empty
  python3 - <<'PY' 2>/dev/null || true
import json
try:
    v = json.load(open("versions.json")).get("latest")
    print(f"v{str(v).lstrip('v')}" if v else "")
except Exception:
    pass
PY
}

case "$ref_mode" in
  version) target_ref="v${ref_arg#v}" ;;   # tags are v-prefixed
  branch)  target_ref="$ref_arg" ;;
  nightly) target_ref="main" ;;
  default) target_ref="$(latest_stable)" ;; # empty when no release cut yet
esac

if [ -n "$target_ref" ]; then
  if [ "$ref_mode" = "default" ] && ! (git diff --quiet && git diff --cached --quiet); then
    echo "==> Working tree has changes — installing it as-is (skipping checkout of $target_ref)."
  else
    echo "==> Checking out $target_ref"
    git checkout "$target_ref"
  fi
fi

os="$(uname -s)"; arch="$(uname -m)"
case "$os/$arch" in
  Darwin/arm64)  asset="scip-clang-arm64-darwin" ;;
  Linux/x86_64)  asset="scip-clang-x86_64-linux" ;;
  *)
    # No native scip-clang for this platform (arm64-linux, Intel Mac, Windows).
    # The tool itself is pure Python and installs fine everywhere — only the
    # indexer is platform-bound, so set up the venv and route indexing to a container.
    echo "No native scip-clang binary for $os/$arch — installing the tool (venv)" >&2
    echo "only; produce the SCIP index via a container (docker or podman):" >&2
    echo "  scripts/index-in-container.sh <compile_commands.json> [src/] [name]" >&2
    echo "then re-run reindex.sh (it reuses the index) or build with 'cppgraph build'." >&2
    echo "See INSTALL.md § 'ARM-Linux / Windows: index via a container'." >&2
    asset="" ;;
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
if [ -z "$asset" ]; then
  echo "==> Skipping scip-clang (no native binary here — index via a container)"
else
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
fi

echo "==> Verifying"
.venv/bin/python -c "from cppgraph.proto import scip_pb2; scip_pb2.Index()" && echo "  python package OK"
.venv/bin/python -c "from cppgraph.updates import current_version as v; print('  cppgraph version:', v() or '(unknown)')"
[ -n "$asset" ] && scratch/bin/scip-clang --version | head -1

if [ -z "$asset" ]; then
  echo
  echo "Setup complete (venv only — no native scip-clang for this platform)."
  if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
    echo "A container engine is present — you're ready to index."
  else
    echo "To index you need a container engine. Install one (podman is daemonless,"
    echo "rootless and fully FOSS):"
    echo "    sudo apt install podman qemu-user-static      # Debian/Ubuntu"
    echo "    # (or Docker; the script auto-detects either)"
  fi
  cat <<'EOF'
Next:
  1. Need a compile_commands.json? Generate one for your build:
       CMake:       cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...   (lands in the build dir)
       Bazel:       bazel run @hedron_compile_commands//:refresh_all
                    (or the project's own target, e.g. MongoDB: bazel build //:compiledb)
       Make/other:  bear -- <your build command>
  2. Produce the SCIP index in a container (writes it to your HOST disk):
       scripts/index-in-container.sh /path/to/compile_commands.json src/ myproject
     -> writes <project>/.cppgraph/<name>.scip and prints the exact step-3 command.
  3. Copy/paste the printed `cppgraph build ...` to build the graph natively,
     then register the MCP (QUICKSTART.md).
See INSTALL.md § "ARM-Linux / Windows: index via a container".
EOF
else
  cat <<'EOF'

Setup complete. Next (see QUICKSTART.md):
  1. Build a graph:  scripts/reindex.sh /path/to/compile_commands.json src/ myproject
     (writes into <project>/.cppgraph/ and prints the exact register command)
  2. Run the register command it printed, then open a new Claude Code session.
EOF
fi
