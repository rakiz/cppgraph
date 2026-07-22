# TODO

The active list — open work we intend to do. Parked "someday / just in case"
ideas live in the **Attic** at the bottom: kept for reference, not part of the
active list. The project isn't versioned yet, so this is a snapshot, not a log;
design detail is in `DESIGN.md`, shipped features in `CHANGELOG.md`.

## Release (0.1.0)

- **Re-measure the token numbers.** `README.md` and `COMPARISON.md` quote token
  counts; re-run `scripts/measure_tokens.py --suite` on the mongo graph and update
  both docs before tagging, so the published figures match what the tool emits.
- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`rebuild` level `none`/`store`/`reindex` — what the release
  invalidates in the index stack — plus one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.

## Other (not release-gating)

- **Align `pipeline.incremental_update` with the dirty fingerprints.** `status`
  (CLI + MCP) reads `meta.dirty_fingerprints` via `changed_files_since` so a graph
  built from a dirty tree isn't falsely reported stale (and a revert *is* caught).
  But `pipeline.incremental_update` computes its changed set with its own `git diff`
  (`_git_diff_names`), unaware of the fingerprints — so an explicit update
  re-indexes dirty-at-build files it needn't, and wouldn't notice a revert. Have it
  call `changed_files_since` so the report and the actual update agree.
- **Resolve a `file:line` to a symbol in the query tools.** The tools take a symbol
  or a plain name (unique resolves, ambiguous lists candidates) but not a `file:line`.
  Add it so "who calls the function at `foo.cpp:120`?" works without a name — needs a
  store lookup by definition location (`symbols.file_id`/`line`).
- **Attributed references as first-class `uses` edges.** `impact_of`/`path` traverse
  `calls`/`inherits` only, so a *type* has no reachable callers — "what breaks if I
  change this struct?" isn't answerable transitively; the answer lives in
  `find_references` (usage view), which isn't traversable. Promote the #504 attributed
  references (`refs.enclosing_id`, function → used symbol) into real traversable edges
  (a distinct `kind`) so `impact_of`/`path` cover type-change blast radius. Needs a
  #504 graph; the data already exists but this adds ~one edge per attributed reference
  (millions on mongo → larger store), so it's opt-in.
- **Show the storage cost of the symbol-granularity upgrade in `status`.** `status`
  already prints the file→symbol upgrade hint (index with a #504 binary / `enrich-refs`);
  add the extra `.graph.db` cost so the user can weigh it. Measure the real delta first
  (same graph with vs without `--attributed-refs`); don't hardcode a guess.
- **Contributing notes, CI (lint + pytest), publish.** Not a 0.1.0 blocker.

## Attic

Kept for reference; most may never happen. Promote one back up if it becomes real.

- **`cppgraph index` wizard — step-back.** The wizard (`src/cppgraph/init.py`) can
  restart from the top (`--from-scratch`) but only moves forward within a run. Open:
  let the user step back to redo an earlier stage (re-filter, re-index) from a later
  prompt.
- **Version for non-git installs.** `current_version` derives from `git describe`;
  a non-git install (tarball/PyPI) falls back to the static `pyproject`/`__version__`.
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`). Parked until we support a non-git install.
- **Upstream: get #504 merged and a Linux ARM binary published.** Advocate
  sourcegraph/scip-clang to include the `enclosing_range` patch (PR #504) and ship an
  `aarch64-linux` asset. That removes the local compile step (the real install-cost
  lever) and, once the asset exists, wire the `Linux/aarch64` `download` case in
  `setup_cmd.py` `platform_sources()` (stock binary, no #504). Don't forget the
  `download` wiring if they publish.
- **Auto-enrich after a #504 re-index.** Attribution (`--attributed-refs`) is an
  explicit opt-in; decide whether a re-index with a #504 binary should enrich by
  default.
- **Build speed (pure-Python wins, then maybe native).** The graph build is ~3.5 min
  wall, single-thread, ~8.8 GB RSS on mongo — pure-Python object churn (`build_graph`
  ~51%, `enrich_references` loop ~39%), not protobuf (already the upb C backend).
  Tier 1 (no toolchain): `__slots__` + `gc.disable()` done; open — columnar typed
  arrays instead of object-per-element, and multiprocessing by document. Tier 2:
  native `build_graph` (Rust/PyO3 or Cython) on the hot loops, at the cost of the
  pip-install/no-toolchain property. Lower priority than the scip-clang download/
  compile path; a graph built once and queried many times may make ~3.5 min acceptable.
- **Synthetic factory-registry edges (reconnect plan→exec across dispatch).** In mongo,
  plan→exec hops through a string-keyed factory table
  (`REGISTER_DOCUMENT_SOURCE("$match", …)`) then virtual dispatch, so there's no static
  edge — `path` can only hint. Parsing the registration macros to inject synthetic
  edges would close it, but the macros are codebase-specific and a synthetic edge
  departs from the exact, heuristic-free model (against the tool's exactness goal);
  it would need a distinct `kind` and an explicit decision before adding.
