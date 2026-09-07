#!/usr/bin/env bash
### USAGE START
# Publish a locally built #504 (enclosing_range) scip-clang binary as a GitHub
# Release asset on this repo's origin, so other machines can later download it
# instead of running the ~30-60 min Docker build. MANUAL maintainer tool: never
# run from CI, never called by cppgraph's own code. It only publishes — it does
# NOT change how `cppgraph setup` obtains scip-clang today (pointing setup's
# download path at these assets is separate future work).
#
#   scripts/publish-scip-clang-504.sh <binary> <platform> [version]
#
#     <binary>    the scip-clang executable from docker/build-scip-clang — a
#                 path to the file, or to a dir containing `scip-clang` (e.g.
#                 the scp'd data dir ~/.local/share/cppgraph/bin)
#     <platform>  the platform the binary was BUILT for (not this host's —
#                 cross-machine scp'd binaries are expected): one of
#                 aarch64-linux | x86_64-linux | arm64-darwin
#     [version]   upstream scip-clang version the patch is based on; defaults
#                 to the pin in versions.json (scip_clang.version)
#
# The release tag is `scip-clang-504-v<version>` — deliberately distinct from
# upstream sourcegraph/scip-clang's own `v<version>` tags. Re-running with a
# different platform uploads to the same release; re-running with the same
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
if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  sed -n '/^### USAGE START/,/^### USAGE END/{/^### USAGE/!p;}' "$0" >&2
  die "usage: $0 <binary> <platform> [version]"
fi
BIN_ARG="$1"; PLATFORM="$2"; VERSION_ARG="${3:-}"

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

# Version: explicit arg wins; else the pin in versions.json. A silently-wrong
# version would mis-tag the release, so a bad parse is fatal, not defaulted.
if [ -n "$VERSION_ARG" ]; then
  VERSION="${VERSION_ARG#v}"
else
  VERSION="$(python3 -c 'import json; print(json.load(open("versions.json"))["scip_clang"]["version"])' 2>/dev/null)" \
    || die "could not read the scip-clang pin from versions.json (python3 ok? file intact?) — pass the version explicitly"
  VERSION="${VERSION#v}"
fi
[ -n "$VERSION" ] || die "empty version — pass it explicitly"

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
ASSET="scip-clang-504-$PLATFORM"
TAG="scip-clang-504-v$VERSION"
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
  NOTES="$(cat <<EOF
Locally built scip-clang binaries carrying the enclosing_range feature
(sourcegraph/scip-clang) from PR #504, which cppgraph uses for exact
reference-to-symbol attribution.

- Base: upstream scip-clang v$VERSION tag (sourcegraph/scip-clang)
- Patch: PR #504 rebased onto that tag, plus a same-file-range guard —
  docker/build-scip-clang/enclosing_range-on-v0.4.0.patch in the cppgraph repo
- Built with Bazel (--config=release-linux via docker/build-scip-clang on Linux,
  or --config=release natively via scripts/build-scip-clang-macos.sh on macOS —
  see the release asset / build script for the platform actually used)

NOT an official sourcegraph/scip-clang release — built and published by the
cppgraph maintainer. Verify downloads against the matching .sha256 asset.
EOF
)"
  gh release create -R "$REPO" "$TAG" \
      --title "scip-clang #504 (enclosing_range) v$VERSION" \
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
echo '  note: this only publishes the binary. `cppgraph setup` still downloads'
echo '        upstream stock binaries today; pointing its download path at these'
echo '        #504 assets is a separate follow-up task.'
