# TODO

Only open items live here. This is the current state of open work (the project
isn't versioned yet, so it's a snapshot, not a log). Design detail is in
`DESIGN.md`; shipped-feature summary in `CHANGELOG.md`.

## Packaging / open-source

- **`cppgraph init` wizard — extend the resume support.** The wizard ships
  (`src/cppgraph/init.py`): it finds the compdb, shows the breakdown, asks the
  scope questions in order (subtree / tests / attribution, the last gated on a
  #504 binary), and runs `reindex.sh`. It already detects an existing
  `<name>.graph.db` and offers update-vs-rebuild. Still open:
  - **Mid-pipeline resume.** When `<name>.scip` exists but `<name>.graph.db`
    doesn't (indexing done, build interrupted), offer to resume at the build step
    via `cppgraph build --scip … --out …` directly, instead of re-running
    scip-clang. Today it only notes the partial index; reindex.sh reuses a `.scip`
    only when no native binary is present.
  - **Step-back.** Let the user redo the previous stage (re-filter, re-index) from
    the resume prompt, not just accept the inferred next step.
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

## Native scip-clang build container

`docker/build-scip-clang/` compiles scip-clang from source (with the #504
`enclosing_range` patch) for the host arch — built and timed on ARM64 already.
Open:

- **Decide the distribution stance.** Building #504 ourselves means owning the
  build for every arch that needs it — a native binary per host, built once
  locally. Fine as build-and-use-locally; revisit if it grows into a hosted
  matrix.
- **Wire the `download` path for ARM-Linux once upstream publishes it.** If
  scip-clang ships a `scip-clang-aarch64-linux` asset (see the arm64-linux issue
  draft in `docker/build-scip-clang/`), add the `Linux/aarch64` case in
  `setup.sh` (a commented one-liner is already there) so `--scip-source download`
  works on ARM-Linux — the stock binary (no #504); build stays the #504 route.

## scip-clang `enclosing_range` (PR #504)

The field is consumed (exact caller attribution + the opt-in symbol-granularity
usage view). Open:

- **Report the crash upstream** (sourcegraph/scip-clang PR #504): the missing
  same-file guard is a bug in the PR itself. Draft ready in
  `docker/build-scip-clang/PR504-COMMENT.draft.md`; post it on the PR.
- **Auto-enrich after a #504 re-index?** `reindex.sh --attributed-refs` is an
  explicit opt-in today; decide whether a #504 re-index should enrich by default.
- **Attributed reference edges** as first-class graph edges (traversable
  symbol→symbol, distinct `kind`) for `impact`/`path`, beyond the usage view.

## Build speed (pure-Python wins, then maybe native)

Measured on mongo (`/usr/bin/time` + `py-spy`, Graviton2 8 vCPU): `enrich-refs`
runs in **~3.5 min wall, 99% CPU single-thread, 8.8 GB RSS**. The protobuf parse
is **not** the cost — the `protobuf` runtime already uses the **upb (C) backend**,
so parsing is compiled and near-invisible in the profile. The time is entirely
**pure-Python object churn**, in two places:
- `build_graph` construction (~51%): `add_reference` / `add_node` / `add_edge` /
  `_occurrence_start_line` / `_attribute_containment` — millions of objects.
- the `enrich_references` update loop (~39%): iterating 7.2M refs, two `dict.get`
  + `list.append` per ref.
- `executemany` (~0%): already solved by the composite index (28d3222).

The algorithm is already right (the containment sweep is O((n+m)·log)); the cost
is intrinsic to per-object allocation + refcounting + hashing in the interpreter.

**Tier 1 — pure Python, no toolchain (partly done).**
- *Done:* `__slots__` on `Node`/`Edge`/`Reference` (no per-instance `__dict__`)
  and `gc.disable()` during the bulk build (no pointless cycle scans). Keeps the
  pip-installable, no-compiler property. Re-measure the gain on the next build.
- *Open:* columnar storage — replace object-per-element with **parallel typed
  arrays** (`array`/`numpy`: `symbol_ids`, `file_ids`, `lines`) for refs/edges, so
  there is no per-element object at all. Bigger win, moderate refactor (adapt the
  consumers that iterate `Reference`/`Edge`). Optionally stream occurrence → row
  straight to SQLite without materializing `graph.references` in full.
- *Open:* parallelize by document (`multiprocessing`) — it is single-thread at
  99% on 8 cores; documents are independent until the merge.

**Tier 2 — native, only if Tier 1 is not enough.** A **native `build_graph`**
(Rust via PyO3, or C++/Cython on the hot loops) attacking the construction +
update loops — *not* a protobuf backend change (already C via upb). Explicit
tension: it moves cppgraph from **pure Python (`pip install`, no toolchain)** to a
compiled extension (per-platform build or distributed wheels) — the build
dependency we deliberately avoid elsewhere — plus a second language and a binding
to maintain. For a graph built **once** and queried many times, ~3.5 min
amortized over its lifetime may simply be acceptable. Decide, don't drift into it.

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
