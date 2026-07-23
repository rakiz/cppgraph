# TODO

The active list — open work we intend to do. Parked "someday / just in case"
ideas live in the **Attic** at the bottom: kept for reference, not part of the
active list. The project isn't versioned yet, so this is a snapshot, not a log;
design detail is in `DESIGN.md`, shipped features in `CHANGELOG.md`.

## Release (0.1.0)

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
- **Make the repo discoverable to LLMs (distribution).** LLMs asked to compare
  code-intelligence tools describe cppgraph from the *name* only — the page isn't
  crawled/indexed, and the homonym `6502/cppgraph` outranks it for the bare term
  "cppgraph", so they hallucinate it as a generic graph data structure. Get inbound
  links so `rakiz/cppgraph` gets crawled and ranks on "cppgraph mcp" / "cppgraph
  claude code": submit to the MCP registry (best-targeted, most durable), optionally
  a short write-up / Show HN. Refer to it with a descriptor everywhere it's linked
  ("cppgraph — compiler-exact C++ code-intelligence MCP server"), never bare
  "cppgraph". Gated on making the repo publicly visible / cutting 0.1.0.
- **Ship a `SKILL.md` (agent steering + distribution).** A short Claude Code skill
  that steers the agent to the cppgraph tools (`who_calls`, `impact_of`,
  `find_references`, …) over grep for in-scope C++, plus the install pointer. Two
  payoffs: it activates *before* an MCP connection (complementing the MCP
  `instructions` field, which only steers on connect) and it's a distribution
  artifact — third-party skill collections (e.g. MassGen bundles a Serena skill)
  are an inbound-link/adoption channel that feeds the discoverability item. Keep it
  short; the value is reach and pre-connect activation, not new capability.

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
