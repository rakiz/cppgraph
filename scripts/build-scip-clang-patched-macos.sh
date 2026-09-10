#!/usr/bin/env bash
# Build scip-clang (v0.4.0 + this repo's full patch bundle from
# scip-clang-patches/ — see scip-clang-patches/README.md for the current
# list) NATIVELY on macOS, for
# THIS Mac's CPU architecture. Mirrors docker/build-scip-clang-patched-linux/build.sh's role
# but skips Docker entirely — a Linux container on a Mac can only ever produce
# a Linux binary, so getting a native macOS binary means building on the host.
#
# MANUAL maintainer tool: never run from CI, never called by cppgraph's own
# Python code. Compiles LLVM/Clang from source via Bazel — expect ~30-60 min
# and ~30-40 GB of disk.
#
#   scripts/build-scip-clang-patched-macos.sh [output_dir]   # default: CPPGRAPH_BIN_DIR
#
# Requires: macOS, Xcode Command Line Tools, git, python3, and Bazel or
# Bazelisk (auto-downloaded into a local dir if neither is found).
set -euo pipefail
cd "$(dirname "$0")/.."  # repo root

die() { echo "error: $*" >&2; exit 1; }

# --- host checks -----------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die \
  "this script is macOS-only. On Linux (or to cross-build a Linux binary via" \
  $'\n'"Docker, even from a Mac), use docker/build-scip-clang-patched-linux/build.sh instead."
HOST_ARCH="$(uname -m)"
echo "==> Host: Darwin $HOST_ARCH"
[ "$HOST_ARCH" = "arm64" ] || echo "  note: expected arm64 (Apple Silicon); continuing on $HOST_ARCH."

xcode-select -p >/dev/null 2>&1 || die \
  "Xcode Command Line Tools not found — install with: xcode-select --install"
command -v git >/dev/null || die "git not found"
command -v python3 >/dev/null || die "python3 not found"

# --- Bazel / Bazelisk (arch-aware; no sudo, no system install) -------------
BAZELISK_VERSION=v1.19.0
BUILD_ROOT="${CPPGRAPH_BUILD_SRC_DIR:-$HOME/.cache/cppgraph/scip-clang-src}"
LOCAL_BIN="$BUILD_ROOT/../tools-bin"

BAZEL_BIN=""
if command -v bazelisk >/dev/null; then
  BAZEL_BIN="$(command -v bazelisk)"
elif command -v bazel >/dev/null; then
  BAZEL_BIN="$(command -v bazel)"
else
  echo "==> No bazel/bazelisk on PATH — downloading Bazelisk $BAZELISK_VERSION locally"
  echo "    (alternative: brew install bazelisk)"
  mkdir -p "$LOCAL_BIN"
  case "$HOST_ARCH" in
    arm64) bl_asset=bazelisk-darwin-arm64 ;;
    x86_64) bl_asset=bazelisk-darwin-amd64 ;;
    *) die "unsupported arch for Bazelisk auto-download: $HOST_ARCH — install bazel/bazelisk yourself" ;;
  esac
  curl -fsSL -o "$LOCAL_BIN/bazelisk" \
    "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/${bl_asset}"
  chmod +x "$LOCAL_BIN/bazelisk"
  BAZEL_BIN="$LOCAL_BIN/bazelisk"
fi
echo "    using: $BAZEL_BIN"

# --- output dir -------------------------------------------------------------
OUT_DIR="${1:-${CPPGRAPH_BIN_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin}}"
mkdir -p "$OUT_DIR"

# --- disk preflight (advisory, same shape as build.sh) ----------------------
MIN_GB="${CPPGRAPH_MIN_BUILD_GB:-35}"
if [ "$MIN_GB" -gt 0 ] 2>/dev/null; then
  mkdir -p "$BUILD_ROOT"
  avail_gb="$(df -Pk "$BUILD_ROOT" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')"
  if [ -n "${avail_gb:-}" ] && [ "$avail_gb" -lt "$MIN_GB" ]; then
    echo "WARNING: only ${avail_gb} GB free on $BUILD_ROOT's filesystem." >&2
    echo "         This build needs ~${MIN_GB} GB and may fail late with no binary." >&2
    echo "         Fixes: free space; point CPPGRAPH_BUILD_SRC_DIR at a larger volume;" >&2
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

# --- pinned version (same read pattern as publish-scip-clang-patched.sh) -------
SCIP_CLANG_TAG="v$(python3 -c 'import json; print(json.load(open("versions.json"))["scip_clang"]["version"])')" \
  || die "could not read the scip-clang version pin from versions.json"
# Same pin, patch bundle version: a local build always bakes in the CURRENT
# patchset, so the sidecar must be stamped with it — without the stamp
# `cppgraph status` assumes p1 and nags "stale" forever (rebuilding repeats it).
PATCHSET_VERSION="$(python3 -c 'import json; print(json.load(open("versions.json"))["scip_clang"].get("patchset_version", 1))' 2>/dev/null)" \
  || die "could not read versions.json (missing/malformed file) — patchset_version itself defaults to 1 when absent"

# --- clone + patch, reused across runs --------------------------------------
echo "==> Source: $BUILD_ROOT (pinned $SCIP_CLANG_TAG)"
if [ -d "$BUILD_ROOT/.git" ]; then
  current_tag="$(git -C "$BUILD_ROOT" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$current_tag" = "$SCIP_CLANG_TAG" ]; then
    echo "  reusing existing clone (already at $SCIP_CLANG_TAG)"
  else
    echo "  existing clone is at '${current_tag:-unknown}', not $SCIP_CLANG_TAG — wiping and re-cloning"
    rm -rf "$BUILD_ROOT"
  fi
fi
if [ ! -d "$BUILD_ROOT/.git" ]; then
  # A leftover non-git dir (e.g. an aborted prior run) would make `git clone`
  # fail with "destination path already exists" — clear it first.
  [ -e "$BUILD_ROOT" ] && rm -rf "$BUILD_ROOT"
  mkdir -p "$(dirname "$BUILD_ROOT")"
  git clone --depth 1 --branch "$SCIP_CLANG_TAG" \
    https://github.com/sourcegraph/scip-clang.git "$BUILD_ROOT"
fi

PATCH="$(pwd)/scip-clang-patches/enclosing_range-on-v0.4.0.patch"
[ -f "$PATCH" ] || die "patch not found at $PATCH"
if grep -q 'enclosingRange' "$BUILD_ROOT/indexer/Indexer.cc" 2>/dev/null; then
  echo "  already patched (enclosingRange present) — skipping git apply"
else
  echo "==> Applying enclosing_range patch (PR #504)"
  git -C "$BUILD_ROOT" apply --verbose "$PATCH"
  grep -q 'enclosingRange' "$BUILD_ROOT/indexer/Indexer.cc" \
    || die "patch applied but grep for 'enclosingRange' still failed — patch may be a no-op"
fi

# Apply the ForwardDefinition bit fix (applied here after enclosing_range
# purely for build consistency — all six patches are actually order-independent,
# see scip-clang-patches/README.md).
FWD_PATCH="$(pwd)/scip-clang-patches/forward-definition-on-v0.4.0.patch"
[ -f "$FWD_PATCH" ] || die "patch not found at $FWD_PATCH"
if grep -q 'is_declaration_site' "$BUILD_ROOT/proto/fwd_decls.proto" 2>/dev/null; then
  echo "  already patched (is_declaration_site present) — skipping git apply"
else
  echo "==> Applying ForwardDefinition bit patch"
  git -C "$BUILD_ROOT" apply --verbose "$FWD_PATCH"
  grep -q 'is_declaration_site' "$BUILD_ROOT/proto/fwd_decls.proto" \
    || die "patch applied but grep for 'is_declaration_site' still failed — patch may be a no-op"
fi

# Apply the ReadAccess/WriteAccess syntactic classifier (applied here after
# the three patches above purely for build consistency — all six patches are
# actually order-independent, see scip-clang-patches/README.md). Tags
# symbol_roles with WriteAccess (ReadAccess alongside it on read-modify-write
# sites) based on the syntactic AST parent of the reference site.
RW_PATCH="$(pwd)/scip-clang-patches/read-write-access-on-v0.4.0.patch"
[ -f "$RW_PATCH" ] || die "patch not found at $RW_PATCH"
if grep -q 'classifyAccessRoles' "$BUILD_ROOT/indexer/Indexer.cc" 2>/dev/null; then
  echo "  already patched (classifyAccessRoles present) — skipping git apply"
else
  echo "==> Applying ReadAccess/WriteAccess classifier patch"
  git -C "$BUILD_ROOT" apply --verbose "$RW_PATCH"
  grep -q 'classifyAccessRoles' "$BUILD_ROOT/indexer/Indexer.cc" \
    || die "patch applied but grep for 'classifyAccessRoles' still failed — patch may be a no-op"
fi

# Apply the SymbolInformation.kind syntactic classifier (applied here after
# the three patches above purely for build consistency — all six patches are
# actually order-independent, see scip-clang-patches/README.md). Fills SCIP's
# SymbolInformation.kind (upstream leaves it at UnspecifiedKind on 100% of
# symbols) by mapping the clang::Decl at each SymbolInformation-creating site
# to its kind; the kind survives the TU-merge pipeline via
# SymbolInformationBuilder::kind.
KIND_PATCH="$(pwd)/scip-clang-patches/kind-on-v0.4.0.patch"
[ -f "$KIND_PATCH" ] || die "patch not found at $KIND_PATCH"
if grep -q 'classifySymbolKind' "$BUILD_ROOT/indexer/Indexer.cc" 2>/dev/null; then
  echo "  already patched (classifySymbolKind present) — skipping git apply"
else
  echo "==> Applying SymbolInformation.kind classifier patch"
  git -C "$BUILD_ROOT" apply --verbose "$KIND_PATCH"
  grep -q 'classifySymbolKind' "$BUILD_ROOT/indexer/Indexer.cc" \
    || die "patch applied but grep for 'classifySymbolKind' still failed — patch may be a no-op"
fi

# Apply the SymbolInformation.signature_documentation emitter (applied here
# after the five patches above purely for build consistency — all six
# patches are actually order-independent, see scip-clang-patches/README.md).
# Fills SCIP's SymbolInformation.signature_documentation (upstream leaves it
# unset on 100% of symbols) with the printed declaration (no body, default
# args kept) for every defined/pure-virtual function & method; the text
# survives the TU-merge pipeline via
# SymbolInformationBuilder::signatureDocumentation.
SIGDOC_PATCH="$(pwd)/scip-clang-patches/signature-documentation-on-v0.4.0.patch"
[ -f "$SIGDOC_PATCH" ] || die "patch not found at $SIGDOC_PATCH"
if grep -q 'declToSignatureText' "$BUILD_ROOT/indexer/Indexer.cc" 2>/dev/null; then
  echo "  already patched (declToSignatureText present) — skipping git apply"
else
  echo "==> Applying SymbolInformation.signature_documentation patch"
  git -C "$BUILD_ROOT" apply --verbose "$SIGDOC_PATCH"
  grep -q 'declToSignatureText' "$BUILD_ROOT/indexer/Indexer.cc" \
    || die "patch applied but grep for 'declToSignatureText' still failed — patch may be a no-op"
fi

# Apply the Relationship.is_type_definition emitter (applied here after the
# five patches above purely for build consistency — all six patches are
# actually order-independent, see scip-clang-patches/README.md). Fills SCIP's
# Relationship.is_type_definition ("go to type definition", upstream never
# sets it): a syntactic type resolver attaches a {symbol: <type>,
# is_type_definition: true} relationship to a field's / variable's own
# SymbolInformation, reusing trySaveTypeReference's type-resolution policy.
TYPEDBY_PATCH="$(pwd)/scip-clang-patches/typed-by-on-v0.4.0.patch"
[ -f "$TYPEDBY_PATCH" ] || die "patch not found at $TYPEDBY_PATCH"
if grep -q 'saveTypeDefinitionRelationship' "$BUILD_ROOT/indexer/Indexer.cc" 2>/dev/null; then
  echo "  already patched (saveTypeDefinitionRelationship present) — skipping git apply"
else
  echo "==> Applying Relationship.is_type_definition (typed-by) patch"
  git -C "$BUILD_ROOT" apply --verbose "$TYPEDBY_PATCH"
  grep -q 'saveTypeDefinitionRelationship' "$BUILD_ROOT/indexer/Indexer.cc" \
    || die "patch applied but grep for 'saveTypeDefinitionRelationship' still failed — patch may be a no-op"
fi

# scip-clang v0.4.0 hardcodes a full-Xcode.app SDK path in setup_llvm.bzl, which
# doesn't exist on a Command Line Tools-only machine (no /Applications/Xcode.app).
# Point it at whatever SDK `xcrun` actually resolves on this host instead.
ACTUAL_SDK="$(xcrun --show-sdk-path 2>/dev/null)" || die "xcrun --show-sdk-path failed — is Xcode CLT set up correctly?"
if [ -f "$BUILD_ROOT/setup_llvm.bzl" ]; then
  sed -i.bak "s|_MACOS_SDK = .*|_MACOS_SDK = \"${ACTUAL_SDK}\"|" "$BUILD_ROOT/setup_llvm.bzl"
  echo "  patched setup_llvm.bzl's _MACOS_SDK -> $ACTUAL_SDK"
fi

# --- build -------------------------------------------------------------------
# macOS note: --config=release-linux (used by the Docker build) enables a
# LLD-based thin-LTO link that scip-clang's own .bazelrc says the macOS
# toolchain doesn't support — so the native build uses plain --config=release.
BAZEL_JVM_HEAP="${BAZEL_JVM_HEAP:-8g}"
# macOS note: without an explicit deployment target, Clang falls back to
# MACOSX_DEPLOYMENT_TARGET if set, else an old baked-in default that this
# CLT/SDK combo resolves to 10.11. scip-clang's indexer/ uses std::filesystem
# unconditionally (introduced in macOS 10.15) with no availability guards, so
# under a 10.11 target every std::filesystem symbol is "unavailable" and the
# build fails with hard errors (not just the LLVM -Wunguarded-availability-new
# warnings seen earlier). Forcing a modern floor via -mmacosx-version-min
# fixes it; MACOSX_DEPLOYMENT_TARGET alone isn't reliably inherited by Bazel's
# sandboxed actions, so it goes in as explicit copt/linkopt flags instead.
MACOS_MIN="${CPPGRAPH_BUILD_MACOS_MIN:-11.0}"
# Named ci.bazelrc (not passed via --bazelrc=) so the project's own committed
# .bazelrc still auto-loads — it already does `try-import %workspace%/ci.bazelrc`
# for exactly this kind of local tweak (same file scip-clang's own Dockerfile
# writes). Passing --bazelrc= explicitly would make Bazel skip the workspace's
# real .bazelrc entirely (config:release, the C++20 flags, etc. all live there).
{
  echo "startup --host_jvm_args=-Xmx${BAZEL_JVM_HEAP}"
  # Both target and host flags are needed: parts of LLVM are also built
  # [for tool] (host config), same reasoning as the --host_features line
  # already in scip-clang's own .bazelrc for -layering_check.
  echo "build --copt=-mmacosx-version-min=${MACOS_MIN} --linkopt=-mmacosx-version-min=${MACOS_MIN}"
  echo "build --host_copt=-mmacosx-version-min=${MACOS_MIN} --host_linkopt=-mmacosx-version-min=${MACOS_MIN}"
} > "$BUILD_ROOT/ci.bazelrc"

echo "==> Building //indexer:scip-clang --config=release (expect ~30-60 min)"
(
  cd "$BUILD_ROOT"
  "$BAZEL_BIN" build //indexer:scip-clang --config=release
)

# --- extract + verify ---------------------------------------------------------
BIN="${OUT_DIR}/scip-clang"
cp -f "$BUILD_ROOT/bazel-bin/indexer/scip-clang" "$BIN"
chmod +x "$BIN"
ver_out="$("$BIN" --version 2>&1)" || die "built binary exited non-zero on --version"
echo "$ver_out" | grep -q "scip-clang" || die "unexpected --version output: $ver_out"

# --- provenance sidecar (identical shape to build.sh's / setup_cmd.py's) -----
ver="$(echo "$ver_out" | awk '/scip-clang/{print $2; exit}')"
cat > "${OUT_DIR}/scip-clang.json" <<EOF
{"version": "${ver:-0.4.0}", "variant": "patched", "patchset_version": ${PATCHSET_VERSION}, "source": "build", "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

# --- summary -------------------------------------------------------------------
echo
echo "==> Done. Binary at: ${BIN}"
file "$BIN" 2>/dev/null || true
echo "$ver_out"
case "$HOST_ARCH" in
  arm64)
    echo "==> Next: publish it with: scripts/publish-scip-clang-patched.sh ${OUT_DIR} arm64-darwin"
    ;;
  *)
    echo "==> Next: publish-scip-clang-patched.sh's platform allowlist has no 'x86_64-darwin' label"
    echo "    today — do not publish this as arm64-darwin. Add the label there first."
    ;;
esac

# --- optional Bazel cleanup ---------------------------------------------------
echo
output_base="$(cd "$BUILD_ROOT" && "$BAZEL_BIN" info output_base 2>/dev/null)" || output_base=""
cache_size="~20-40 GB"
if [ -n "$output_base" ]; then
  measured_size="$(du -sh "$output_base" 2>/dev/null | awk '{print $1}')" || measured_size=""
  [ -n "$measured_size" ] && cache_size="$measured_size"
fi
echo "Bazel's native build cache for this workspace is currently using ${cache_size}. The Docker build"
echo "reclaims this automatically when its container/image is discarded; native"
echo "builds do not, so this optional step mirrors that behavior."
if [ -t 0 ]; then
  read -r -p "Reclaim ${cache_size} of Bazel build cache now? [Y/n] " _ans < /dev/tty
  case "${_ans:-}" in
    n | N | no | NO)
      echo "Skipped cleanup."
      ;;
    *)
      if (cd "$BUILD_ROOT" && "$BAZEL_BIN" clean --expunge); then
        echo "==> Bazel build cache cleanup complete."
      else
        echo "warning: Bazel cache cleanup failed (the build itself already succeeded — safe to ignore, or clean up manually later)." >&2
      fi
      ;;
  esac
else
  echo "Skipped cleanup (non-interactive)."
fi
echo "Bazel cache size at measurement: ${cache_size}. To reclaim it later:"
echo "  cd $BUILD_ROOT && $BAZEL_BIN clean --expunge"
