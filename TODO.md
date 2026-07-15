# TODO

Ordered. Check off as you go. Detail lives in `DESIGN.md`.

## Phase 1 — POC (prove the thesis on one subsystem)

- [x] Install `scip-clang` (GitHub release, darwin arm64). Recorded version
      + install path in `HANDOFF.md` / `INSTALL.md`.
- [x] Vendor `scip.proto`; generate `scip_pb2.py`/`.pyi` via `protoc` (committed,
      not gitignored — see `INSTALL.md`).
- [x] Index a single MongoDB subsystem into SCIP.
      `src/mongo/db/pipeline`, 519 TUs, `scratch/pipeline.scip` (23 MB), 0 errors.
- [x] `cppgraph build`: parse the `.scip`, build nodes (SCIP symbol id) + edges
      (`calls`, `implements`). `references`/`inherits`/`defines` deferred to
      Phase 2 (not needed for the acceptance tests; see DESIGN.md follow-up).
- [x] **Acceptance test A (over-capture):** the two `makeResumeToken` symbols are
      *distinct nodes*, each with correctly separated callers. Verified as a
      unit test and against the real pipeline index. See `HANDOFF.md`.
- [x] **Acceptance test B (under-capture):** calls are attributed from the
      SCIP-resolved callee symbol only, never call-site syntax — a
      `ptr->virtualMethod()` call is captured like any other. Unit test in
      `tests/test_builder.py`.
- [x] Write up the before/after in `HANDOFF.md` (numbers vs graphify).

## Phase 2 — scale + store + query

- [ ] Index all of `src/mongo` (measure time; scip-clang is parallel).
- [ ] Persistent store (sqlite via stdlib, or compact json). Measure size.
- [x] CLI queries: `find` (search by name), `callers`, `callees` — working
      against `graph.json`, verified on the real pipeline graph.
- [ ] CLI queries: `path A B`, `impact` (reverse blast radius), `explain`.
- [ ] Project root as a runtime parameter for query commands that need to
      read actual source (see `DESIGN.md` § "Project root is a query-time
      parameter, never stored") — not needed yet since `callers`/`callees`
      only print symbol/file/line, no source snippets.
- [ ] **Incremental update path** (design already sketched in `DESIGN.md` §
      "Keeping the graph up to date" — don't lose this while building the
      full-repo store): re-index only changed TUs → merge partial `.scip`
      documents into the full index by `relative_path` → rebuild only the
      edges/nodes whose `Edge.file`/definition-site is one of the changed
      files. The document-local caller-attribution design already makes this
      possible without cross-file analysis; what's missing is (a) a merge
      function for partial `Index` → full `Index`, (b) a `Graph.drop_file(path)`
      to invalidate before re-inserting, (c) wiring a `cppgraph update` CLI
      command around this instead of always re-running `build` from scratch.

## Phase 3 — serve to LLMs

- [ ] MCP server exposing the queries, token-budgeted retrieval.
- [ ] Compare usefulness against Serena on a real design question.

## Phase 4 — open-source / generalize

- [ ] Remove any MongoDB-specific assumptions from the tool core.
- [ ] Optional `graph.json` export (graphify-compatible) for visualization.
- [ ] LICENSE, contributing notes, CI, publish.

## Open questions (decide when reached)

- ~~SCIP call-edge attribution~~ **resolved**: scip-clang v0.4.0 emits neither
  `enclosing_range` nor `SymbolInformation.kind`. Fallback implemented:
  callability from the symbol's `).`  suffix, caller = nearest preceding
  callable-symbol definition by line. See `HANDOFF.md` for the verified
  details and known declaration-context false-positive limitation.
- Templates & header-only: how instantiations appear in SCIP; whether to collapse
  instantiations to the primary template node.
- Whether to keep graphify at all, even just for viz, or ship our own viz.
- Phase 2: the nearest-preceding-definition heuristic misattributes
  declaration-only references (see `HANDOFF.md`) — refine before scaling to
  all of `src/mongo`, e.g. skip attribution for occurrences in a class body
  with no matching out-of-line definition in the same TU.
