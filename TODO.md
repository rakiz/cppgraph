# TODO

Only open items live here. Completed work is in `CHANGELOG.md`; design detail in
`DESIGN.md`.

## Packaging / open-source

- Contributing notes, CI (lint + pytest), publish.
- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`requires_rebuild`, one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.
- **Version for non-git installs.** `current_version` falls back to the static
  `pyproject`/`__version__` when the source isn't a git checkout (tarball/PyPI).
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`) so those installs report the truth too.

## A build container that compiles scip-clang native to its host arch

Today's ARM-Linux workaround (`scripts/index-in-container.sh`) runs the x86_64
scip-clang emulated, so indexing is slow.

To close it: a Docker image carrying the build toolchain (Bazel + LLVM deps)
that compiles a vanilla scip-clang for the arch the container runs on (native,
e.g. `linux/arm64` on an ARM host — no emulation), dropping the binary into
`scratch/bin/` for native indexing afterwards. The build toolchain stays in the
image, not on the host; no prebuilt binary needs hosting, since each machine
without one builds it once.

Blocked on / costs: needs scip-clang's Bazel build recipe (pin the version); the
build is CPU/RAM-heavy, tens of minutes.

Related: building scip-clang ourselves would also let us carry the
`enclosing_range` patch (#504), but that widens scope from "build locally" to
"maintain the whole distributed build matrix" — a separate item if taken.

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
