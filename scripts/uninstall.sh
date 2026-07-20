#!/usr/bin/env bash
set -euo pipefail
#
# Uninstall cppgraph — the mirror of setup.sh.
#
# Asks, per item, what to remove (nothing is deleted without a yes):
#   1. the MCP registration ('cppgraph', user scope) — `claude mcp remove`;
#   2. the scip-clang binary (per-machine, in the bin dir);
#   3. the tool itself (the cppgraph checkout + its venv);
#   4. this project's graph data (./.cppgraph), if run from a project.
#
# Project graphs live in each project's own <project>/.cppgraph/ — this script
# only offers the one in the current directory (it can't know the others); delete
# the rest per-project.
#
# Usage:
#   scripts/uninstall.sh            interactive (recommended)
#   scripts/uninstall.sh --yes      non-interactive: remove MCP + binary + tool,
#                                   KEEP project data (the safe defaults)
#   scripts/uninstall.sh --purge    non-interactive: remove EVERYTHING, including
#                                   this project's .cppgraph data (--all is a synonym)
#   scripts/uninstall.sh --dry-run  print what would happen, change nothing
#
# Paths mirror setup.sh: the tool lives under ${XDG_DATA_HOME:-~/.local/share}/
# cppgraph (repo + bin); the binary dir can be overridden with CPPGRAPH_BIN_DIR.

DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph"
REPO="$DATA_ROOT/repo"
BIN_DIR="${CPPGRAPH_BIN_DIR:-$DATA_ROOT/bin}"
PROJECT_CPG="$PWD/.cppgraph"

ASSUME_YES=0
DRY_RUN=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    -y | --yes) ASSUME_YES=1 ;;
    --purge | --all) PURGE=1; ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *)
      echo "error: unknown argument '$arg' (see --help)" >&2
      exit 2
      ;;
  esac
done

# ask PROMPT DEFAULT  -> 0 (yes) / 1 (no). DEFAULT is "y" or "n".
ask() {
  local prompt="$1" default="$2" reply
  if [[ "$ASSUME_YES" == 1 ]]; then
    [[ "$default" == "y" ]]
    return
  fi
  local hint="y/N"
  [[ "$default" == "y" ]] && hint="Y/n"
  read -r -p "$prompt ($hint) " reply || reply=""
  reply="${reply:-$default}"
  case "$reply" in
    y | Y | yes | oui) return 0 ;;
    *) return 1 ;;
  esac
}

# rm_path DESCRIPTION PATH  — delete a path, honouring --dry-run.
rm_path() {
  local desc="$1" path="$2"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "  [dry-run] would remove $desc: $path"
    return
  fi
  rm -rf "$path"
  echo "  removed $desc: $path"
}

echo "cppgraph uninstall — found:"
echo "  tool (repo + venv): $REPO $([[ -d $REPO ]] && echo '(present)' || echo '(absent)')"
echo "  scip-clang binary:  $BIN_DIR $([[ -d $BIN_DIR ]] && echo '(present)' || echo '(absent)')"
if command -v claude >/dev/null 2>&1; then
  echo "  MCP server 'cppgraph': $(claude mcp get cppgraph >/dev/null 2>&1 && echo registered || echo 'not registered')"
else
  echo "  MCP server 'cppgraph': (claude CLI not found — cannot check/unregister)"
fi
echo "  this project's graph:  $PROJECT_CPG $([[ -d $PROJECT_CPG ]] && echo '(present)' || echo '(absent)')"
echo

# 1. MCP registration.
if command -v claude >/dev/null 2>&1; then
  if ask "Unregister the MCP server 'cppgraph' (user scope)?" y; then
    if [[ "$DRY_RUN" == 1 ]]; then
      echo "  [dry-run] would run: claude mcp remove cppgraph --scope user"
    else
      claude mcp remove cppgraph --scope user >/dev/null 2>&1 || true
      echo "  unregistered MCP server 'cppgraph'."
    fi
  fi
fi

# 2. scip-clang binary. Warn when it looks self-built (#504) — costly to rebuild.
if [[ -d "$BIN_DIR" ]]; then
  variant=""
  [[ -f "$BIN_DIR/scip-clang.json" ]] && variant="$(
    "${REPO}/.venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("variant",""))' \
      "$BIN_DIR/scip-clang.json" 2>/dev/null || true
  )"
  if [[ "$variant" == "enclosing_range-504" ]]; then
    echo "  note: this scip-clang is a self-built #504 binary (30-60 min to rebuild)."
  fi
  if ask "Delete the scip-clang binary?" y; then
    rm_path "scip-clang binary" "$BIN_DIR"
  fi
fi

# 3. The tool (checkout + venv).
if [[ -d "$REPO" ]]; then
  if ask "Delete the cppgraph tool (checkout + venv) at $REPO?" y; then
    rm_path "cppgraph tool" "$REPO"
    # If the data root is now empty (bin already gone), remove it too.
    if [[ "$DRY_RUN" != 1 && -d "$DATA_ROOT" ]]; then
      rmdir "$DATA_ROOT" 2>/dev/null && echo "  removed empty $DATA_ROOT" || true
    fi
  fi
fi

# 4. This project's graph data — default NO (data is precious; other projects
#    have their own .cppgraph to remove separately).
if [[ -d "$PROJECT_CPG" ]]; then
  # Default no (data is precious) — unless --purge/--all was asked, which means
  # "remove everything, project data included".
  proj_default=n; [[ "$PURGE" == 1 ]] && proj_default=y
  if ls "$PROJECT_CPG"/*.scip >/dev/null 2>&1; then
    echo "  WARNING: this includes a .scip index, which can take HOURS to rebuild." >&2
  fi
  if ask "Delete THIS project's graph data at $PROJECT_CPG?" "$proj_default"; then
    rm_path "project graph data" "$PROJECT_CPG"
  else
    echo "  kept project graph data (other projects keep their own <project>/.cppgraph)."
  fi
fi

echo
if [[ "$DRY_RUN" == 1 ]]; then
  echo "Done (dry-run — nothing changed)."
else
  echo "Done."
fi
