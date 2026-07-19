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
# scip-clang source (the indexer binary):
#   --scip-source download   fetch the prebuilt release binary (no PR #504) — ~1 min
#   --scip-source build      compile it locally with enclosing_range / PR #504
#                            (Linux host only; ~30-60 min; needs Docker)
#   --scip-source emulate    install no host binary; index via an x86 container
#                            (nothing installed now; indexing is slower later)
#   --scip-source auto       accept the recommended default (download where a
#                            prebuilt exists, else emulate) — for CI/non-interactive
#   Also via env CPPGRAPH_SCIP_SOURCE (the flag wins). Unset + a TTY: prompt.
#   Unset + non-interactive (no TTY): STOP and ask for an explicit --scip-source
#   (nothing is auto-installed), unless emulate is the only option on this host.
#
# Platforms: a prebuilt binary exists for macOS arm64 and Linux x86_64 (use WSL2
# on Windows). ARM Linux has none yet — build locally (PR #504, native) or
# emulate. Intel Mac / Windows: emulate, or use a graph.db built elsewhere
# (query/MCP/viz are pure Python and run anywhere).
#
# Prereqs: `uv` (https://docs.astral.sh/uv/) and `curl`.
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

# --- version / ref selection ------------------------------------------------
ref_mode="default"; ref_arg=""; scip_source_flag=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) ref_arg="${2:?--version needs a value (e.g. 0.2.0)}"; ref_mode="version"; shift 2 ;;
    --branch)  ref_arg="${2:?--branch needs a value}"; ref_mode="branch"; shift 2 ;;
    --nightly) ref_mode="nightly"; shift ;;
    --scip-source) scip_source_flag="${2:?--scip-source needs download|build|emulate}"; shift 2 ;;
    -h|--help) sed -n '2,/^# Prereqs/p' "$0"; exit 0 ;;
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
# Prebuilt release binary for this platform, if any (empty = none published).
case "$os/$arch" in
  Darwin/arm64)  native_asset="scip-clang-arm64-darwin" ;;
  Linux/x86_64)  native_asset="scip-clang-x86_64-linux" ;;
  # ARM Linux has no prebuilt binary yet. When upstream publishes one, wire it:
  #   Linux/aarch64|Linux/arm64) native_asset="scip-clang-aarch64-linux" ;;
  *)             native_asset="" ;;
esac
# A local build (docker/build-scip-clang) compiles a LINUX binary for the host
# arch, so it's only an option on a Linux host.
case "$os/$arch" in
  Linux/x86_64|Linux/aarch64|Linux/arm64) host_can_build=1 ;;
  *)                                       host_can_build=0 ;;
esac

# scip-clang source: flag > env > (TTY prompt | ask). download|build|emulate|auto.
SCIP_SOURCE="${scip_source_flag:-${CPPGRAPH_SCIP_SOURCE:-}}"
case "$SCIP_SOURCE" in
  ""|download|build|emulate|auto) ;;
  *) echo "error: --scip-source must be download|build|emulate|auto (got '$SCIP_SOURCE')" >&2; exit 2 ;;
esac

command -v uv   >/dev/null || { echo "uv not found — install: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl not found — please install it." >&2; exit 1; }

echo "==> Python venv + dependencies (.venv)"
# `uv pip install` picks its target venv in this order: --python, then an active
# $VIRTUAL_ENV, then ./.venv. So a venv from *another* project left active in the
# shell (e.g. the target repo's) would capture the install — cppgraph would land
# there, not in our .venv, and .venv/bin/cppgraph would be missing. Pin every uv
# command to our own venv by absolute path so an inherited VIRTUAL_ENV can't win.
VENV="$PWD/.venv"
if [ -n "${VIRTUAL_ENV:-}" ] && [ "$VIRTUAL_ENV" != "$VENV" ]; then
  echo "  note: a different VIRTUAL_ENV is active ($VIRTUAL_ENV);"
  echo "        ignoring it — installing into $VENV"
fi
if [ -d "$VENV" ]; then
  echo "  reusing existing .venv"
else
  uv venv "$VENV"
fi
uv pip install --python "$VENV/bin/python" -e ".[dev,mcp]"

# The scip-clang binary is a per-MACHINE artifact (one per arch, shared by the
# dev checkout and every indexed project). It lives in the persistent user data
# dir (XDG_DATA_HOME), NOT a cache: a self-built binary costs 30-60 min to
# rebuild, so it must survive cache cleaners. Not under scratch/ or a project's
# .cppgraph/. Override with CPPGRAPH_BIN_DIR.
BIN_DIR="${CPPGRAPH_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin}"
SCIP_CLANG="$BIN_DIR/scip-clang"
mkdir -p "$BIN_DIR"

# Pinned scip-clang version comes from versions.json `scip_clang.version` (single
# source of truth, shared with `cppgraph status`); fall back to a known-good tag.
scip_pin_version() {
  python3 - <<'PY' 2>/dev/null || true
import json
try:
    p = json.load(open("versions.json")).get("scip_clang") or {}
    print(str(p.get("version", "")).lstrip("v"))
except Exception:
    pass
PY
}
SCIP_PIN_VERSION="$(scip_pin_version)"; SCIP_PIN_VERSION="${SCIP_PIN_VERSION:-0.4.0}"
SCIP_VERSION="v${SCIP_PIN_VERSION}"

# Record what we installed, next to the binary — `scip-clang --version` reports
# the version but not the patch variant, so `cppgraph status` reads this sidecar
# to compare the installed binary against the pin.
write_provenance() {  # version variant source
  cat > "$BIN_DIR/scip-clang.json" <<EOF
{"version": "$1", "variant": "$2", "source": "$3", "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
}

download_scip() {
  if [ -z "$native_asset" ]; then
    echo "error: no prebuilt scip-clang for $os/$arch. Use --scip-source build" >&2
    echo "       (Linux host, compiles PR #504) or emulate (index via a container)." >&2
    exit 1
  fi
  echo "==> Downloading scip-clang $SCIP_VERSION ($native_asset) -> $BIN_DIR"
  url="https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_VERSION}/${native_asset}"
  if ! curl -fL --retry 3 -o "$SCIP_CLANG" "$url"; then
    rm -f "$SCIP_CLANG"
    echo "error: failed to download scip-clang from:" >&2
    echo "       $url" >&2
    echo "       Check your network/proxy, then re-run. Or download it manually" >&2
    echo "       to $SCIP_CLANG and 'chmod +x' it." >&2
    exit 1
  fi
  chmod +x "$SCIP_CLANG"
  write_provenance "$SCIP_PIN_VERSION" stock download
}

# List the scip-clang sources valid on this host, as copy-pasteable commands.
# Printed whenever the choice was made non-interactively, so a human (or an LLM
# driving the install) can see the alternatives and re-run with an explicit one.
print_source_options() {
  echo "    scip-clang sources on this host (pick one; each shows the rough cost):" >&2
  [ -n "$native_asset" ] && \
    echo "      scripts/setup.sh --scip-source download   # prebuilt binary, no PR #504 — ~1 min" >&2
  [ "$host_can_build" = 1 ] && \
    echo "      scripts/setup.sh --scip-source build      # compile PR #504 natively — ~30-60 min, needs Docker" >&2
  echo "      scripts/setup.sh --scip-source emulate    # no host binary; index via x86 container — nothing now, slower indexing later" >&2
}

build_scip() {
  if [ "$host_can_build" != 1 ]; then
    echo "error: a local build produces a Linux binary — not runnable on $os/$arch." >&2
    echo "       Use --scip-source download, or index via a container (emulate)." >&2
    exit 1
  fi
  command -v docker >/dev/null 2>&1 || {
    echo "error: building scip-clang needs Docker (BuildKit). Install Docker, or" >&2
    echo "       use --scip-source download|emulate." >&2
    exit 1
  }
  echo "==> Building scip-clang locally with enclosing_range / PR #504 (~30-60 min)"
  ./docker/build-scip-clang/build.sh "$BIN_DIR"
}

HAVE_HOST_BINARY=0
if [ -x "$SCIP_CLANG" ]; then
  echo "==> scip-clang already present ($SCIP_CLANG)"
  HAVE_HOST_BINARY=1
else
  # `auto`: accept the recommended default without a prompt (CI/non-interactive).
  if [ "$SCIP_SOURCE" = auto ]; then
    if [ -n "$native_asset" ]; then SCIP_SOURCE=download; else SCIP_SOURCE=emulate; fi
    echo "==> scip-clang source: $SCIP_SOURCE (--scip-source auto)." >&2
  fi
  # Resolve the source only if the user didn't force one (flag/env).
  if [ -z "$SCIP_SOURCE" ]; then
    if [ -t 0 ] && [ -t 1 ]; then
      # Every case offers an explicit "don't install (stop)" — the user always
      # confirms, even when only one source applies.
      if [ -n "$native_asset" ] && [ "$host_can_build" = 1 ]; then
        echo "scip-clang: [1] download prebuilt binary (no PR #504) — ~1 min" >&2
        echo "            [2] build locally with PR #504 — ~30-60 min, Docker" >&2
        echo "            [n] don't install (stop)" >&2
        printf "Choose [1]: " >&2; read -r reply || reply=""
        case "$reply" in
          2) SCIP_SOURCE=build ;;
          n | N | no) echo "Aborted — scip-clang not installed." >&2; exit 3 ;;
          *) SCIP_SOURCE=download ;;
        esac
      elif [ -n "$native_asset" ]; then
        # macOS arm64: the prebuilt binary is the only native option (no #504 build).
        echo "scip-clang: the prebuilt binary is available to download — ~1 min (no native PR #504 on $os/$arch)." >&2
        echo "            [1] download   [2] emulate (no binary; slower indexing)   [n] don't install (stop)" >&2
        printf "Choose [1]: " >&2; read -r reply || reply=""
        case "$reply" in
          2) SCIP_SOURCE=emulate ;;
          n | N | no) echo "Aborted — scip-clang not installed." >&2; exit 3 ;;
          *) SCIP_SOURCE=download ;;
        esac
      elif [ "$host_can_build" = 1 ]; then
        echo "No prebuilt scip-clang for $os/$arch." >&2
        echo "scip-clang: [1] build locally with PR #504 — ~30-60 min, Docker (recommended)" >&2
        echo "            [2] emulate via x86 container (works, but slow)   [n] don't install (stop)" >&2
        printf "Choose [1]: " >&2; read -r reply || reply=""
        case "$reply" in
          2) SCIP_SOURCE=emulate ;;
          n | N | no) echo "Aborted — scip-clang not installed." >&2; exit 3 ;;
          *) SCIP_SOURCE=build ;;
        esac
      else
        # Intel Mac / Windows: no binary, no native build — emulation only.
        echo "No prebuilt binary and no native build on $os/$arch; indexing would use an x86 container (emulate)." >&2
        printf "Set up for emulated indexing? [Y/n]: " >&2; read -r reply || reply=""
        case "$reply" in n | N | no) echo "Aborted — nothing installed." >&2; exit 3 ;; *) SCIP_SOURCE=emulate ;; esac
      fi
    else
      # Non-interactive: NEVER auto-install, even when only one source applies —
      # the user still confirms. Stop and have the caller re-run with an explicit
      # --scip-source. This is what stops an agent from silently downloading (or
      # silently falling back to emulation) without asking.
      echo "" >&2
      echo "ACTION NEEDED — confirm how to obtain scip-clang (nothing installed yet):" >&2
      print_source_options
      echo "      scripts/setup.sh --scip-source auto       # accept the recommended default (CI)" >&2
      echo "    Re-run setup.sh with your choice; everything else here is already done." >&2
      exit 3
    fi
  fi

  case "$SCIP_SOURCE" in
    download) download_scip; HAVE_HOST_BINARY=1 ;;
    build)    build_scip;    HAVE_HOST_BINARY=1 ;;
    emulate)  echo "==> No host scip-clang installed — indexing goes through an x86 container." ;;
  esac
fi

echo "==> Verifying"
.venv/bin/python -c "from cppgraph.proto import scip_pb2; scip_pb2.Index()" && echo "  python package OK"
.venv/bin/python -c "from cppgraph.updates import current_version as v; print('  cppgraph version:', v() or '(unknown)')"
[ "$HAVE_HOST_BINARY" = 1 ] && "$SCIP_CLANG" --version | head -1

if [ "$HAVE_HOST_BINARY" != 1 ]; then
  echo
  echo "Setup complete (venv only — scip-clang runs in an x86 container here)."
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
                    (or the project's own target, e.g. MongoDB: bazel build --config=compiledb //src/...)
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
