#!/usr/bin/env bash
# Set up cppgraph on this machine, then index the current project.
#
# This launcher does the one thing that must happen before any Python can run —
# create the tool's virtualenv and install it — then hands off to the interactive
# `cppgraph setup`, which obtains the scip-clang indexer, registers the MCP server,
# and runs the project index wizard. Every step checks what already exists and asks
# before (re)doing it; nothing expensive is overwritten without your say-so.
#
#   scripts/setup.sh --list-sources           print the scip-clang sources valid on
#                                             THIS machine (no venv needed), then exit
#   scripts/setup.sh                          set up the tool interactively, then index
#   scripts/setup.sh --scip-source build      obtain scip-clang without prompting
#                                             (download|build|emulate — needed when run
#                                             non-interactively, e.g. via Claude Code `!`)
#   scripts/setup.sh --version 0.2.0          pin the tool to a released tag before setup
#   scripts/setup.sh --nightly | --branch foo track main / a branch
#   scripts/setup.sh --from-scratch           re-walk every setup stage
#   scripts/setup.sh --no-index               stop after tool setup (skip the index wizard)
#   -y/--yes                                  keep an existing binary / MCP registration
#
# Prereq: `uv` (https://docs.astral.sh/uv/).
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

# The valid scip-clang sources for THIS machine, from uname — so a caller (agent or
# human) never has to guess the platform. Prints, then exits; needs no venv.
list_sources() {
  local os arch; os="$(uname -s)"; arch="$(uname -m)"
  local native="" build=0
  case "$os/$arch" in
    Darwin/arm64|Linux/x86_64) native=1 ;;
  esac
  case "$os" in Linux) build=1 ;; esac
  echo "platform: $os $arch"
  echo "valid scip-clang sources (pass one as --scip-source):"
  [ -n "$native" ] && echo "  download   prebuilt binary (stock, no #504) — ~1 min"
  [ "$build" = 1 ] && echo "  build      compile #504 locally — ~30-60 min, needs Docker (Linux only)"
  echo "  emulate    no host binary; index via an x86 container — slower at index time"
  [ -z "$native" ] && echo "note: 'download' is NOT available on $os $arch."
}

# --- optional version/ref selection (cppgraph is pure Python: a version is a tag) ---
ref_mode="default"; ref_arg=""; passthrough=()
while [ $# -gt 0 ]; do
  case "$1" in
    --list-sources) list_sources; exit 0 ;;
    --version) ref_arg="${2:?--version needs a value (e.g. 0.2.0)}"; ref_mode="version"; shift 2 ;;
    --branch)  ref_arg="${2:?--branch needs a value}"; ref_mode="branch"; shift 2 ;;
    --nightly) ref_mode="nightly"; shift ;;
    --scip-source) passthrough+=("$1" "${2:?--scip-source needs download|build|emulate}"); shift 2 ;;
    -y|--yes|--from-scratch|--no-index) passthrough+=("$1"); shift ;;
    -h|--help) sed -n '2,/^# Prereq/p' "$0"; exit 0 ;;
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
  version) target_ref="v${ref_arg#v}" ;;
  branch)  target_ref="$ref_arg" ;;
  nightly) target_ref="main" ;;
  default) target_ref="$(latest_stable)" ;;
esac
if [ -n "$target_ref" ]; then
  if [ "$ref_mode" = "default" ] && ! (git diff --quiet && git diff --cached --quiet); then
    echo "==> Working tree has changes — installing it as-is (skipping checkout of $target_ref)."
  else
    echo "==> Checking out $target_ref"
    git checkout "$target_ref"
  fi
fi

command -v uv >/dev/null || { echo "uv not found — install: https://docs.astral.sh/uv/" >&2; exit 1; }

echo "==> Python venv + dependencies (.venv)"
# Pin every uv command to our own venv by absolute path, so an inherited
# VIRTUAL_ENV from another project can't capture the install.
VENV="$PWD/.venv"
if [ -n "${VIRTUAL_ENV:-}" ] && [ "$VIRTUAL_ENV" != "$VENV" ]; then
  echo "  note: ignoring active VIRTUAL_ENV ($VIRTUAL_ENV); installing into $VENV"
fi
[ -d "$VENV" ] && echo "  reusing existing .venv" || uv venv "$VENV"
uv pip install --python "$VENV/bin/python" -e ".[dev,mcp,tui]"

# Hand off to the interactive setup (scip-clang -> MCP -> project index wizard).
exec "$VENV/bin/cppgraph" setup ${passthrough[@]+"${passthrough[@]}"}
