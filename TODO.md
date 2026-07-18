# TODO

Only open items live here. Completed work is in `CHANGELOG.md`; design detail in
`DESIGN.md`.

## Align CLI query filters with the MCP tools

The MCP tools carry filtering/budget options the CLI equivalents lack: `who_calls`
/ `what_it_calls` take `limit`, `exclude_tests`, `full_symbols` (and
`what_it_calls` also `hide_trivial`), while `cppgraph callers` / `callees` print
every edge with none of these. Same gap on `impact` (`exclude_tests`) and the
type notices.

To close it: add the matching flags (`--limit`, `--exclude-tests`,
`--hide-trivial`, `--full-symbols`) to the CLI query commands, reusing the pure
functions the MCP layer already calls so behaviour stays identical across both
surfaces.

## Packaging / open-source

- Contributing notes, CI (lint + pytest), publish.
- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`rebuild` level `none`/`store`/`reindex` — what the release
  invalidates in the index stack — plus one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.
- **Version for non-git installs.** `current_version` falls back to the static
  `pyproject`/`__version__` when the source isn't a git checkout (tarball/PyPI).
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`) so those installs report the truth too.

## Verify + wire up the native scip-clang build container

`docker/build-scip-clang/` compiles scip-clang from source for the host's arch
(Bazel in-image) carrying the `enclosing_range` patch (#504), avoiding the
emulated x86 indexer for ARM-Linux. `setup.sh --scip-source build` drives it
(default output → the machine data dir `reindex.sh` reads). Not yet exercised
end-to-end. Remaining:

- **Build it on an ARM-Linux host** (CPU/RAM-heavy, ~30-60 min) and confirm the
  produced binary indexes a real project natively. The #504 patch is already
  rebased onto `v0.4.0` (`enclosing_range-on-v0.4.0.patch`), so `git apply`
  should be clean — the Dockerfile guards it anyway.
- **Decide the distribution stance.** Building #504 ourselves means owning the
  build for every arch that needs it — a native binary per host, built once
  locally. Fine as build-and-use-locally; revisit if it grows into a hosted
  matrix.
- **Wire the `download` path for ARM-Linux once upstream publishes it.** If
  scip-clang ships a `scip-clang-aarch64-linux` asset (see the arm64-linux issue
  draft in `docker/build-scip-clang/`), add the `Linux/aarch64` case in
  `setup.sh` (a commented one-liner is already there) so `--scip-source download`
  works on ARM-Linux — the stock binary (no #504), build stays the #504 route.

Once verified, this also unblocks the `enclosing_range` items below.

## Synthetic factory-registry edges (reconnect plan→exec across dispatch)

`path` today only *hints* that a missing static chain may cross a runtime
boundary; it can't rebuild the edge. In codebases like MongoDB the plan→exec
flow hops through a factory table keyed by a string
(`REGISTER_DOCUMENT_SOURCE("$match", DocumentSourceMatch::createFromBson)`) then
through virtual dispatch, so there is no static edge from `buildPipeline` to
`DocumentSourceMatch::doGetNext` even though they're linked at runtime.

To close it: parse the registration macros to learn `"$match" → createFromBson`
and inject a synthetic edge into the graph, so end-to-end paths resolve.

Blocked on / costs: the registration macros are codebase-specific (each project
has its own), so this needs per-codebase pattern support, and a synthetic edge
departs from the graph's otherwise exact, heuristic-free model — decide how to
mark such edges (e.g. a distinct `kind`) before adding them.

## Blocked on scip-clang `enclosing_range` (PR #504)

Both need exact reference→enclosing-symbol attribution — the nearest-preceding
proxy can't give it in class bodies, so until #504 we stay locations-only
(exact, zero heuristic). See `DESIGN.md` § Graph model.

- Attributed reference **edges** (opt-in at indexing, since they're large):
  approach "A" (type→type) and "B" (all references) — symbol→symbol and
  traversable, exact via containment.
- `usage` view at **symbol** granularity (type → the functions that use it)
  instead of file granularity, for `export --mode usage` / the `visualize` tool.

Robustness when consuming the field: read `Occurrence.enclosing_range` as
optional. An empty value — a stock downloaded binary, or one built without #504 —
must degrade to today's locations-only behaviour, never error (an absent
`repeated` field reads as an empty list, so the guard is "empty ⇒ feature
absent"). Keep the vendored `scip.proto` a superset of the schema any supported
scip-clang emits, so the committed binding can always read what a self-built
binary produces; regenerate the binding (`docker/gen-bindings/`) only to pick up
a *new* field a newer binary starts emitting, never to avoid a crash. The
authoritative schema is the `_SCIP_COMMIT` scip-clang pins in its Bazel deps
(the #504 patch bumps it to the commit that adds `enclosing_range = 7`).
