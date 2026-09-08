# scip-clang-patches — our patches on top of upstream scip-clang v0.4.0

Shared by both build paths — `docker/build-scip-clang-patched-linux/` (Docker,
Linux binary) and `scripts/build-scip-clang-patched-macos.sh` (native, macOS
binary) — hence living here at the repo root instead of inside either one.

## Files

- `enclosing_range-on-v0.4.0.patch` — [PR #504](https://github.com/sourcegraph/scip-clang/pull/504)
  (`enclosing_range`), rebased onto `v0.4.0`, plus a same-file hardening guard
  over the raw PR (see the patch's own history / `docker/build-scip-clang-patched-linux/README.md`
  for why). Upstream PR in progress.
- `forward-definition-on-v0.4.0.patch` — the `ForwardDefinition` bit fix (see
  `TODO.md`'s scip-clang section). Not yet upstreamed. Generated with reduced
  diff context (`-U1`) so it applies both **standalone** on a clean `v0.4.0`
  checkout and **stacked on top of** `enclosing_range-on-v0.4.0.patch`.

## Apply order

For OUR build (both patches together), apply in this order — it does **not**
work reversed, since both patches touch `TuIndexer::saveReference`:

```sh
git apply enclosing_range-on-v0.4.0.patch      # first
git apply forward-definition-on-v0.4.0.patch   # second
```

`forward-definition-on-v0.4.0.patch` alone (no `#504`) also applies cleanly to
a clean `v0.4.0` checkout — verified — which is what would let it become an
independent upstream PR later without regenerating it, regardless of whether
`#504` ever lands.

## Versioning

The upstream scip-clang tag these patches are rebased on (`v0.4.0`) and *our
own* patch bundle version (`patchset_version`, bumped whenever a patch here is
added/changed) are two independent numbers — see `versions.json`'s
`scip_clang` object and its `$comment_patchset` for the current history.

## Consumers

- `docker/build-scip-clang-patched-linux/Dockerfile` — copies both patches into
  the Docker build context and applies them in order.
- `scripts/build-scip-clang-patched-macos.sh` — applies them directly to a
  local clone, in the same order.
- `scripts/publish-scip-clang-patched.sh` — publishes the resulting binary as a
  GitHub Release asset; does not touch these files itself.
</content>
