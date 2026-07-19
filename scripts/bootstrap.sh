#!/usr/bin/env bash
set -euo pipefail
#
# cppgraph one-command installer (the "magic link").
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/rakiz/cppgraph/main/scripts/bootstrap.sh)
#
# Use `bash <(curl …)`, NOT `curl … | bash`: process substitution keeps your
# terminal as stdin, so the confirmations below actually work. It:
#   1. confirms you want to install (install / don't install — stop);
#   2. checks prereqs (git, curl, uv);
#   3. clones the tool into ~/.local/share/cppgraph/repo (the stable per-machine
#      home; updates in place if already there);
#   4. runs setup.sh — which asks how to obtain scip-clang (download / build /
#      emulate, each with its cost, or don't install);
#   5. registers the MCP server (once per machine).
#
# Flags:
#   -y, --yes              non-interactive: confirm the install (for agents/CI).
#                          Required when there's no TTY (piped) — otherwise the
#                          script stops and asks you to re-run with it.
#   --scip-source SRC      pass through to setup.sh (download|build|emulate|auto);
#                          without it, setup.sh asks (or stops, when non-interactive).
#   --repo URL_OR_PATH     install from here instead of the public repo — a git URL
#                          or a local path (for testing "as if from GitHub"). Also
#                          via CPPGRAPH_REPO.
#
# After this, index a project from its directory with `cppgraph init`.

REPO_DEFAULT="https://github.com/rakiz/cppgraph"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"

ASSUME_YES=0
SCIP_SOURCE=""
REPO="${CPPGRAPH_REPO:-$REPO_DEFAULT}"

while [ $# -gt 0 ]; do
  case "$1" in
    -y | --yes) ASSUME_YES=1; shift ;;
    --scip-source) SCIP_SOURCE="${2:?--scip-source needs a value}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a URL or path}"; shift 2 ;;
    -h | --help) sed -n '4,42p' "$0"; exit 0 ;;
    *) echo "error: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
done

# 1. Confirm — install / don't install.
echo "cppgraph installer"
echo "  tool location: $DEST"
echo "  source:        $REPO"
if [ "$ASSUME_YES" != 1 ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    printf "Install cppgraph here? [Y/n]: "
    read -r reply || reply=""
    case "$reply" in n | N | no) echo "Aborted — nothing installed."; exit 0 ;; esac
  else
    echo "" >&2
    echo "ACTION NEEDED — this is a non-interactive run. Re-run confirming the install:" >&2
    echo "  bash <(curl -fsSL <url>) --yes [--scip-source download|build|emulate]" >&2
    echo "(or don't, to install nothing)." >&2
    exit 3
  fi
fi

# 2. Prereqs.
for cmd in git curl uv; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "error: '$cmd' not found — install it first (uv: https://docs.astral.sh/uv/)." >&2
    exit 1
  }
done

# 3. Obtain the tool at DEST (clone fresh, or update in place).
mkdir -p "$(dirname "$DEST")"
if [ -d "$DEST/.git" ]; then
  echo "==> Updating existing checkout at $DEST"
  git -C "$DEST" pull --ff-only || echo "  (could not fast-forward; leaving the checkout as-is)"
elif [ -e "$DEST" ]; then
  echo "error: $DEST exists but is not a git checkout — move it aside and re-run." >&2
  exit 1
else
  echo "==> Cloning $REPO -> $DEST"
  git clone "$REPO" "$DEST"
fi

# 4. Set up venv + deps + scip-clang (setup.sh asks the scip-clang source, or
#    stops with ACTION NEEDED when non-interactive and none was passed).
setup_args=()
[ -n "$SCIP_SOURCE" ] && setup_args+=(--scip-source "$SCIP_SOURCE")
if ! "$DEST/scripts/setup.sh" ${setup_args[@]+"${setup_args[@]}"}; then
  rc=$?
  if [ "$rc" = 3 ]; then
    echo "" >&2
    echo "Re-run adding a scip-clang source, e.g.:" >&2
    echo "  bash <(curl -fsSL <url>) --yes --scip-source download" >&2
    exit 3
  fi
  exit "$rc"
fi

# 5. Register the MCP server (idempotent, once per machine).
"$DEST/scripts/register-mcp.sh"

echo
echo "cppgraph installed. Next: from a C++ project's directory, index it with"
echo "  cppgraph init        # guided; auto-finds compile_commands.json"
