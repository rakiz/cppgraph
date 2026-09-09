# scip-clang-patches — our patches on top of upstream scip-clang v0.4.0

Shared by both build paths — `docker/build-scip-clang-patched-linux/` (Docker,
Linux binary) and `scripts/build-scip-clang-patched-macos.sh` (native, macOS
binary) — hence living here at the repo root instead of inside either one.

## Files

- `enclosing_range-on-v0.4.0.patch` — [PR #504](https://github.com/sourcegraph/scip-clang/pull/504)
  (`enclosing_range`), rebased onto `v0.4.0`, plus a same-file hardening guard
  over the raw PR (see the patch's own header / `docker/build-scip-clang-patched-linux/README.md`
  for why). Upstream PR in progress. Generated with reduced diff context
  (`-U1`) — see "Independence" below.
- `forward-definition-on-v0.4.0.patch` — the `ForwardDefinition` bit fix (see
  `TODO.md`'s scip-clang section). Not yet upstreamed. Generated with reduced
  diff context (`-U1`).
- `read-write-access-on-v0.4.0.patch` — the `ReadAccess`/`WriteAccess` syntactic
  classifier (`classifyAccessRoles` in `indexer/Indexer.cc`, plus test fixtures).
  Tags `symbol_roles` with `WriteAccess` when the reference site's syntactic AST
  parent is an assignment, compound assignment, `++`/`--`, or their
  overloaded-operator forms (with `ReadAccess` set alongside `WriteAccess` on
  read-modify-write sites); constructor-initializer field references are
  unconditional writes; plain reads stay untagged. Not yet upstreamed. Generated
  with reduced diff context (`-U1`).

## Independence

Each patch applies **standalone** on a clean `v0.4.0` checkout, and all three
apply cleanly together in **any of the 6 possible orderings** — verified
empirically (every ordering tried against a fresh `v0.4.0` clone; the
resulting files are byte-identical regardless of order). None of the three
requires either of the others.

This wasn't true by default: at the standard 3-line diff context, two of
these patches touch overlapping lines near `TuIndexer::saveReference`
(`enclosing_range` and `forward-definition` both edit that function), so
whichever applied second would see stale context from the other and fail.
Reducing all three to `-U1` context removes that coupling entirely — none of
the actual *content* changes overlap, only the diff format's context window
did. This matters for upstreaming: a maintainer reviewing these independently
(in whatever order they pick, possibly rejecting one) won't hit an artificial
"depends on patch X" requirement that was never semantically real.

## Apply order

Our own build applies them in a fixed order — `enclosing_range` →
`forward-definition` → `read-write-access` — purely for consistency (it's the
one sequence that's actually end-to-end tested), not because any of them
requires it:

```sh
git apply enclosing_range-on-v0.4.0.patch      # order-independent
git apply forward-definition-on-v0.4.0.patch   # order-independent
git apply read-write-access-on-v0.4.0.patch    # order-independent
```

Each is also ready to become an independent upstream PR without regenerating
it, regardless of whether or in what order the others land.

## Versioning

The upstream scip-clang tag these patches are rebased on (`v0.4.0`) and *our
own* patch bundle version (`patchset_version`, bumped whenever a patch here is
added/changed) are two independent numbers — see `versions.json`'s
`scip_clang` object and its `$comment_patchset` for the current history.

## Consumers

- `docker/build-scip-clang-patched-linux/Dockerfile` — copies all three patches
  into the Docker build context and applies them in order.
- `scripts/build-scip-clang-patched-macos.sh` — applies them directly to a
  local clone, in the same order.
- `scripts/publish-scip-clang-patched.sh` — publishes the resulting binary as a
  GitHub Release asset; does not touch these files itself.
</content>
