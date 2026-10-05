#!/usr/bin/env bash
set -euo pipefail
#
# Uninstall cppgraph — the mirror of setup.sh.
#
# Asks, per item, what to remove (nothing is deleted without a yes):
#   1. the MCP registration ('cppgraph', user scope) — `claude mcp remove`;
#   2. the installed agent extras (the bundled skill + /cppgraph slash command
#      copied into the detected agent tools' per-user dirs);
#   3. the scip-clang binary (per-machine, in the bin dir);
#   4. the tool itself (the cppgraph checkout + its venv);
#   5. the CLI command link (~/.local/bin/cppgraph), only when it is a symlink
#      pointing into $REPO's venv (never a foreign or real file, and never the
#      ~/.local/bin dir itself — a standard user dir that predates cppgraph);
#   6. this project's graph data (./.cppgraph), if run from a project.
#
# Project graphs live in each project's own <project>/.cppgraph/ — this script
# only offers the one in the current directory (it can't know the others); delete
# the rest per-project.
#
# Usage:
#   scripts/uninstall.sh            interactive (recommended)
#   scripts/uninstall.sh --yes      non-interactive: remove MCP + agent extras + the CLI
#                                   command link + binary + tool, KEEP project data (safe defaults)
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
# Installed agent extras (item 2): the bundled skill + /cppgraph slash command
# that setup.sh copied into the detected agent tools' per-user dirs (plain
# $HOME; opencode under ${XDG_CONFIG_HOME:-~/.config}, as setup_cmd resolves it).
CLAUDE_SKILL="$HOME/.claude/skills/cppgraph/SKILL.md"
CLAUDE_CMD="$HOME/.claude/commands/cppgraph.md"
OPENCODE_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
OPENCODE_SKILL="$OPENCODE_ROOT/skills/cppgraph/SKILL.md"
OPENCODE_CMD="$OPENCODE_ROOT/command/cppgraph.md"
# The CLI command link setup_cmd.ensure_cli_on_path creates. Only a symlink
# pointing into $REPO's venv is ever touched — a foreign file/link at this path
# (masora and other tools live in ~/.local/bin) is left alone, and the dir
# itself is never removed (it predates cppgraph; it is not ours to delete).
CLI_LINK="$HOME/.local/bin/cppgraph"

ASSUME_YES=0
DRY_RUN=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    -y | --yes) ASSUME_YES=1 ;;
    --purge | --all) PURGE=1; ASSUME_YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,28p' "$0"
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

# rm_dir_if_empty DESCRIPTION DIR IGNORED  — remove DIR only if it exists and
# holds nothing but IGNORED (a file removed — or about to be, under --dry-run —
# by the rm_path calls above), so a dir setup.sh created disappears without ever
# deleting unrelated content.
rm_dir_if_empty() {
  local desc="$1" dir="$2" ignored="$3" others
  [[ -d "$dir" ]] || return 0
  others="$(ls -A "$dir" 2>/dev/null | grep -vx "$ignored" || true)"
  [[ -z "$others" ]] || return 0
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "  [dry-run] would remove empty $desc: $dir"
  else
    rmdir "$dir" 2>/dev/null && echo "  removed empty $desc: $dir" || true
  fi
}

# rm_extra DESCRIPTION PATH — rm_path, but only when PATH exists (a missing
# agent extra is skipped, not "removed").
rm_extra() {
  local desc="$1" path="$2"
  [[ -e "$path" ]] && rm_path "$desc" "$path" || true
}

echo "cppgraph uninstall — found:"
echo "  tool (repo + venv): $REPO $([[ -d $REPO ]] && echo '(present)' || echo '(absent)')"
echo "  scip-clang binary:  $BIN_DIR $([[ -d $BIN_DIR ]] && echo '(present)' || echo '(absent)')"
if command -v claude >/dev/null 2>&1; then
  echo "  MCP server 'cppgraph': $(claude mcp get cppgraph >/dev/null 2>&1 && echo registered || echo 'not registered')"
else
  echo "  MCP server 'cppgraph': (claude CLI not found — cannot check/unregister)"
fi
extras_found=0
for extra in "$CLAUDE_SKILL" "$CLAUDE_CMD" "$OPENCODE_SKILL" "$OPENCODE_CMD"; do
  [[ -e "$extra" ]] && extras_found=$((extras_found + 1))
done
if [[ "$extras_found" -gt 0 ]]; then
  echo "  installed agent extras: $extras_found of 4 (claude skill+command, opencode skill+command)"
else
  echo "  installed agent extras: (absent)"
fi
echo "  this project's graph:  $PROJECT_CPG $([[ -d $PROJECT_CPG ]] && echo '(present)' || echo '(absent)')"
# Ours = a symlink into $REPO's venv (dangling counts: -L + readlink, not -e,
# which is false for a dangling link).
cli_link_state="(absent)"
if [[ -L "$CLI_LINK" ]]; then
  if [[ "$(readlink "$CLI_LINK" 2>/dev/null)" == "$REPO/.venv/bin/cppgraph" ]]; then
    cli_link_state="(present, ours)"
  else
    cli_link_state="(not-ours)"
  fi
elif [[ -e "$CLI_LINK" ]]; then
  cli_link_state="(not-ours)"
fi
echo "  CLI command link:      $CLI_LINK $cli_link_state"
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

# 2. Installed agent extras — trivially re-created by setup.sh, so the default
#    is yes.
extras_present=0
for extra in "$CLAUDE_SKILL" "$CLAUDE_CMD" "$OPENCODE_SKILL" "$OPENCODE_CMD"; do
  [[ -e "$extra" ]] && extras_present=1
done
if [[ "$extras_present" == 1 ]]; then
  if ask "Remove the installed agent extras (cppgraph skill + /cppgraph command)?" y; then
    rm_extra "Claude Code skill" "$CLAUDE_SKILL"
    rm_extra "Claude Code command" "$CLAUDE_CMD"
    rm_extra "opencode skill" "$OPENCODE_SKILL"
    rm_extra "opencode command" "$OPENCODE_CMD"
    rm_dir_if_empty "Claude Code skill dir" "${CLAUDE_SKILL%/*}" "$(basename "$CLAUDE_SKILL")"
    rm_dir_if_empty "opencode skill dir" "${OPENCODE_SKILL%/*}" "$(basename "$OPENCODE_SKILL")"
  fi
fi

# 3. scip-clang binary. Warn when it looks self-built (patched) — costly to rebuild.
if [[ -d "$BIN_DIR" ]]; then
  variant=""
  [[ -f "$BIN_DIR/scip-clang.json" ]] && variant="$(
    "${REPO}/.venv/bin/python" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("variant",""))' \
      "$BIN_DIR/scip-clang.json" 2>/dev/null || true
  )"
  # Any patched-build spelling (current "patched" + pre-rename sidecar values).
  if [[ "$variant" == "patched" || "$variant" == "enclosing_range-504" || "$variant" == "504" ]]; then
    echo "  note: this scip-clang is a self-built patched binary (30-60 min to rebuild)."
  fi
  if ask "Delete the scip-clang binary?" y; then
    rm_path "scip-clang binary" "$BIN_DIR"
  fi
fi

# 4. The tool (checkout + venv).
if [[ -d "$REPO" ]]; then
  if ask "Delete the cppgraph tool (checkout + venv) at $REPO?" y; then
    rm_path "cppgraph tool" "$REPO"
    # If the data root is now empty (bin already gone), remove it too.
    if [[ "$DRY_RUN" != 1 && -d "$DATA_ROOT" ]]; then
      rmdir "$DATA_ROOT" 2>/dev/null && echo "  removed empty $DATA_ROOT" || true
    fi
  fi
fi

# 5. The CLI command link — checked OUTSIDE the `[[ -d $REPO ]]` guard above:
#    the link can dangle when the repo was removed another way. Only a symlink
#    into $REPO's venv is offered (a foreign file/link stays untouched);
#    ~/.local/bin itself is never removed — a standard user dir, not ours.
if [[ -L "$CLI_LINK" && "$(readlink "$CLI_LINK" 2>/dev/null)" == "$REPO/.venv/bin/cppgraph" ]]; then
  if ask "Remove the CLI command link at $CLI_LINK?" y; then
    rm_path "CLI command link" "$CLI_LINK"
  fi
fi

# 6. This project's graph data — default NO (data is precious; other projects
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
