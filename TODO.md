# TODO

Only open items live here. Completed work is in `CHANGELOG.md`; design detail in
`DESIGN.md`.

## Packaging / open-source

- **Release blocker (0.1.0): re-measure the token numbers.** `README.md` and
  `COMPARISON.md` quote token counts that predate `DEFAULT_LIMIT = 40`
  (`mcp_server.py:56`). Re-run the measurement on the mongo graph (workstation)
  and update both docs before tagging, so the published figures match what the
  tool actually emits.
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

- **Build on an ARM-Linux host: done & measured.** Native aarch64 `scip-clang`
  0.4.0 + #504 built on an AWS Graviton `m6g.2xlarge` (8 vCPU), **~32 min cold**
  (~99 % Bazel compiling LLVM/Clang; CPU-bound, scales with cores). Provenance
  sidecar records `enclosing_range-504` / `build`. Timings in
  `docker/build-scip-clang/README.md`. **Remaining: confirm the binary indexes a
  real project natively** (native, not emulated) end-to-end.
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

## scip-clang `enclosing_range` (PR #504) — consume the field

Done (works when a #504-built binary emits `enclosing_range`, no-op otherwise):

- Exact **caller attribution** for `calls` edges via containment, replacing the
  nearest-preceding heuristic when the field is present (`builder.py`).
- Attributed **references** + symbol-granularity `usage` view (type → the
  functions that use it): opt-in `cppgraph build --attributed-refs`, back-fill an
  existing store with `cppgraph enrich-refs`, surfaced by `export --mode usage`.
  The store records `has_attributed_refs`; CLI/MCP `status` advertise the
  granularity and the upgrade path.

Remaining:

- `scripts/reindex.sh` now takes a leading `--attributed-refs` (warns + no-ops on
  a non-#504 binary, tips `enrich-refs` on the kept `.scip` otherwise). Still
  open: decide whether to enrich automatically after a #504 re-index, or keep it
  an explicit choice (current behaviour).
- Attributed reference **edges** as first-class graph edges (traversable
  symbol→symbol, distinct `kind`) for `impact`/`path`, beyond the usage view.
- Measure the real store-size cost of `--attributed-refs` on the mongo graph and
  quote it in the flag help / DESIGN (currently "extra space", unquantified).
- Verify end-to-end against the #504 binary (now built on the ARM workstation):
  index a real project natively, then confirm `enclosing_range` actually flows
  through — exact caller attribution on `calls`, and `--attributed-refs` /
  `enrich-refs` producing the symbol-granularity usage view on real data (tests
  so far use synthetic `.scip` only).

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
