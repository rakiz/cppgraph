#!/usr/bin/env bash
### USAGE START
# Publish a locally built patched scip-clang binary (carrying this repo's
# whole scip-clang-patches/ bundle — see that directory's README.md for the
# current patch list) as a GitHub Release asset on this repo's origin, so
# other machines can later download it instead of running the ~30-60 min
# Docker build. MANUAL maintainer tool: never run from CI, never called by
# cppgraph's own code. It only publishes — it does NOT change how `cppgraph
# setup` obtains scip-clang (setup's `download-patched` source consumes
# exactly these assets).
#
#   scripts/publish-scip-clang-patched.sh <binary> <platform> [version] [patchset]
#
#     <binary>    the scip-clang executable from docker/build-scip-clang-patched-linux — a
#                 path to the file, or to a dir containing `scip-clang` (e.g.
#                 the scp'd data dir ~/.local/share/cppgraph/bin)
#     <platform>  the platform the binary was BUILT for (not this host's —
#                 cross-machine scp'd binaries are expected): one of
#                 aarch64-linux | x86_64-linux | arm64-darwin
#     [version]   upstream scip-clang version the patches are based on; defaults
#                 to the pin in versions.json (scip_clang.version)
#     [patchset]  this repo's patch-bundle version; defaults to the pin in
#                 versions.json (scip_clang.patchset_version)
#
# The release tag is `scip-clang-patched-v<version>-p<patchset>` — deliberately
# distinct from upstream sourcegraph/scip-clang's own `v<version>` tags. The
# patchset comes from versions.json `scip_clang.patchset_version` (bumped whenever
# the patch bundle changes), so a rebuilt bundle on the same upstream version
# lands in a fresh release instead of silently replacing the old one. Re-running
# with a different platform uploads to the same release; re-running with the same
# platform asks before replacing the asset (--clobber, never silent).
#
# Prereq: gh (GitHub CLI), authenticated; python3; sha256sum or shasum.
### USAGE END
set -euo pipefail
cd "$(dirname "$0")/.."

die() { echo "error: $*" >&2; exit 1; }

# --- args ----------------------------------------------------------------------
case "${1:-}" in
  -h|--help) sed -n '/^### USAGE START/,/^### USAGE END/{/^### USAGE/!p;}' "$0"; exit 0 ;;
esac
if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  sed -n '/^### USAGE START/,/^### USAGE END/{/^### USAGE/!p;}' "$0" >&2
  die "usage: $0 <binary> <platform> [version] [patchset]"
fi
BIN_ARG="$1"; PLATFORM="$2"; VERSION_ARG="${3:-}"; PATCHSET_ARG="${4:-}"

case "$PLATFORM" in
  aarch64-linux|x86_64-linux|arm64-darwin) ;;
  *) die "unknown platform '$PLATFORM' — must be one of: aarch64-linux, x86_64-linux, arm64-darwin (the platform the binary was BUILT for, not this host's)" ;;
esac

# Accept the file itself, or a dir holding it (scp'd data dir / build output).
if [ -d "$BIN_ARG" ]; then
  BIN="$BIN_ARG/scip-clang"
  echo "  note: given a directory — using $BIN"
else
  BIN="$BIN_ARG"
fi
[ -f "$BIN" ] || die "no such file: $BIN"
[ -x "$BIN" ] || die "$BIN is not executable — chmod +x it first"

# Version + patchset: explicit args win; else the pins in versions.json. A
# silently-wrong version or patchset would mis-tag the release, so a bad parse
# is fatal, not defaulted.
if [ -n "$VERSION_ARG" ]; then
  VERSION="${VERSION_ARG#v}"
else
  VERSION="$(python3 -c 'import json; print(json.load(open("versions.json"))["scip_clang"]["version"])' 2>/dev/null)" \
    || die "could not read the scip-clang pin from versions.json (python3 ok? file intact?) — pass the version explicitly"
  VERSION="${VERSION#v}"
fi
[ -n "$VERSION" ] || die "empty version — pass it explicitly"
if [ -n "$PATCHSET_ARG" ]; then
  PATCHSET_VERSION="$PATCHSET_ARG"
else
  PATCHSET_VERSION="$(python3 -c 'import json; print(json.load(open("versions.json"))["scip_clang"]["patchset_version"])' 2>/dev/null)" \
    || die "could not read scip_clang.patchset_version from versions.json (python3 ok? file intact?) — pass the patchset explicitly"
fi
[ -n "$PATCHSET_VERSION" ] || die "empty patchset version — pass it explicitly"

# --- gh + repo (resolved by gh itself, never hardcoded or hand-parsed) ----------
command -v gh >/dev/null || die "gh (GitHub CLI) not found — https://cli.github.com/"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated — run: gh auth login"

REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  || die "could not determine the GitHub repo via 'gh repo view' — check you're in the right directory and gh is configured for this remote"

# --- sanity: can this host run it, and does it answer --version? ----------------
echo "==> Binary: $BIN"
file "$BIN" 2>/dev/null || true   # eyeball the reported arch against the label

host_pair="$(uname -s)/$(uname -m)"
case "$PLATFORM" in
  arm64-darwin)  want_pair="Darwin/arm64" ;;
  x86_64-linux)  want_pair="Linux/x86_64" ;;
  aarch64-linux) want_pair="Linux/aarch64" ;;
esac
if [ "$host_pair" = "$want_pair" ] || { [ "$want_pair" = "Linux/aarch64" ] && [ "$host_pair" = "Linux/arm64" ]; }; then
  echo "==> Sanity check: $BIN --version"
  ver_out="$("$BIN" --version 2>&1)" || die "binary exited non-zero on --version — truncated scp? wrong platform label?"
  echo "$ver_out" | grep -q "scip-clang" || die "unexpected --version output: $ver_out"
  echo "    $ver_out"
  bin_ver="$(echo "$ver_out" | awk '/scip-clang/{print $2; exit}')"
  if [ -n "$bin_ver" ] && [ "${bin_ver#v}" != "$VERSION" ]; then
    echo "WARNING: the binary reports version '${bin_ver}' but the release tag will use" >&2
    echo "         '${VERSION}' (from the arg or the versions.json pin) — continue only if intended." >&2
  fi
else
  echo "  note: '$PLATFORM' binary on a $host_pair host — skipping the execution sanity"
  echo "        check (a foreign-arch binary can't run here). Checksum is still computed."
fi

# --- stage the asset + checksum (temp dir; the source binary is never touched) --
ASSET="scip-clang-patched-$PLATFORM"
TAG="scip-clang-patched-v${VERSION}-p${PATCHSET_VERSION}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$BIN" "$STAGE/$ASSET"
chmod +x "$STAGE/$ASSET"

echo "==> sha256"
if command -v sha256sum >/dev/null; then
  SUM="$(sha256sum "$STAGE/$ASSET" | awk '{print $1}')"
elif command -v shasum >/dev/null; then
  SUM="$(shasum -a 256 "$STAGE/$ASSET" | awk '{print $1}')"
else
  die "neither sha256sum nor shasum found on this machine"
fi
[ "${#SUM}" -eq 64 ] || die "checksum didn't parse as a sha256 (got: '$SUM')"
echo "$SUM  $ASSET" > "$STAGE/$ASSET.sha256"   # sha256sum -c compatible

# --- release: reuse if it exists, else create (clobber is loud, never silent) ---
echo "==> Release $TAG on $REPO"
if gh release view -R "$REPO" "$TAG" >/dev/null 2>&1; then
  echo "  already exists — reusing it (platforms stack up as assets)"
  ASSET_LIST="$(gh release view -R "$REPO" "$TAG" --json assets --jq '.assets[].name')" \
    || die "could not list assets on existing release $TAG (gh error) — re-run to retry"
  if echo "$ASSET_LIST" | grep -Fx -e "$ASSET" -e "$ASSET.sha256" >/dev/null; then
    echo "WARNING: '$ASSET' (or its .sha256) is already an asset of $TAG —" >&2
    echo "         re-uploading REPLACES it." >&2
    if [ -t 0 ]; then
      read -r -p "         Replace it? [y/N] " ans < /dev/tty
      case "${ans:-}" in
        y|Y|yes|YES) ;;
        *) echo "Aborted — nothing was overwritten." >&2; exit 1 ;;
      esac
    else
      echo "         non-interactive run: aborting instead of overwriting." >&2
      exit 1
    fi
  fi
else
  echo "  creating it"
  # Patch list is generated from what's actually in scip-clang-patches/ at
  # publish time — never hardcoded prose here, so this can't go stale the way
  # a fixed patch list did before (a real bug: this text once still named
  # only 3 of the then-6 shipped patches). One line per *-on-v$VERSION.patch
  # file, alphabetical.
  PATCH_LIST="$(cd "$(dirname "$0")/.." && for p in scip-clang-patches/*-on-v"$VERSION".patch; do
    [ -e "$p" ] || continue
    echo "- \`$(basename "$p")\`"
  done)"
  [ -n "$PATCH_LIST" ] || die "no scip-clang-patches/*-on-v$VERSION.patch files found — check the version"
  # Built with printf, not a heredoc inside $(): bash 3.2 (macOS system bash)
  # parses a heredoc body in a command substitution with a raw quote scanner, so
  # an odd number of apostrophes in the body ("cppgraph's") breaks it (bash -n
  # fails far below the actual line). printf keeps the text byte-identical.
  NOTES="$(printf '%s\n' \
    "Locally built scip-clang binary carrying cppgraph's full patch bundle" \
    "(patchset p${PATCHSET_VERSION}) on top of upstream scip-clang v${VERSION} —" \
    'see `scip-clang-patches/README.md` in the cppgraph repo for what each patch' \
    'does and the current apply order. Patches in this bundle:' \
    '' \
    "$PATCH_LIST" \
    '' \
    "- Base: upstream scip-clang v${VERSION} tag (sourcegraph/scip-clang)" \
    '- Built with Bazel (--config=release-linux via' \
    '  docker/build-scip-clang-patched-linux on Linux, or --config=release' \
    '  natively via scripts/build-scip-clang-patched-macos.sh on macOS — see the' \
    '  release asset name for the platform actually used)' \
    '' \
    'NOT an official sourcegraph/scip-clang release — built and published by the' \
    "cppgraph maintainer. Verify downloads against the matching .sha256 asset.")"
  gh release create -R "$REPO" "$TAG" \
      --title "scip-clang patched (patchset p$PATCHSET_VERSION) v$VERSION" \
      --notes "$NOTES" >/dev/null
fi

echo "==> Uploading assets"
gh release upload -R "$REPO" "$TAG" "$STAGE/$ASSET" "$STAGE/$ASSET.sha256" --clobber

# --- summary --------------------------------------------------------------------
URL="$(gh release view -R "$REPO" "$TAG" --json url --jq '.url')"
echo
echo "==> Published:"
echo "    release: $URL"
echo "    assets:  $ASSET, $ASSET.sha256"
echo "    sha256:  $SUM"
echo '  note: this only publishes the binary. `cppgraph setup` downloads these'
echo "        assets via its download-patched source, keyed on the same"
echo '        versions.json pins (scip_clang.version + scip_clang.patchset_version).'
