# TODO

Ordered. Check off as you go. Detail lives in `DESIGN.md`.

## Phase 1 — POC (prove the thesis on one subsystem)

- [x] Install `scip-clang` (GitHub release, darwin arm64). Recorded version
      + install path in `HANDOFF.md` / `INSTALL.md`.
- [x] Vendor `scip.proto`; generate `scip_pb2.py`/`.pyi` via `protoc` (committed,
      not gitignored — see `INSTALL.md`).
- [ ] Index a single MongoDB subsystem into SCIP.
      Candidate: `src/mongo/db/pipeline` (change_stream lives here).
      Command shape: `scip-clang --compdb-path <mongo>/compile_commands.json ...`
      scoped to the subsystem's TUs. Output to `scratch/`.
- [ ] `cppgraph build`: parse the `.scip`, build nodes (SCIP symbol id) + edges
      (calls / references / overrides / inherits / defines).
- [ ] **Acceptance test A (over-capture):** the two `makeResumeToken` symbols are
      *distinct nodes*. `ChangeStreamEventTransformation::makeResumeToken` has its
      real callers only (~2); the test-helper free function is a separate node.
- [ ] **Acceptance test B (under-capture):** pick one call that tree-sitter drops
      (a `ptr->virtualMethod()` / overload) and confirm cppgraph has the edge.
- [ ] Write up the before/after in `HANDOFF.md` (numbers vs graphify).

## Phase 2 — scale + store + query

- [ ] Index all of `src/mongo` (measure time; scip-clang is parallel).
- [ ] Persistent store (sqlite via stdlib, or compact json). Measure size.
- [ ] CLI queries: `callers`, `callees`, `path A B`, `impact` (reverse blast
      radius), `explain`.

## Phase 3 — serve to LLMs

- [ ] MCP server exposing the queries, token-budgeted retrieval.
- [ ] Compare usefulness against Serena on a real design question.

## Phase 4 — open-source / generalize

- [ ] Remove any MongoDB-specific assumptions from the tool core.
- [ ] Optional `graph.json` export (graphify-compatible) for visualization.
- [ ] LICENSE, contributing notes, CI, publish.

## Open questions (decide when reached)

- SCIP call-edge attribution: confirm scip-clang emits enough (enclosing ranges /
  call roles) to attribute a reference to its enclosing caller. If not, fall back
  to range-containment over definition symbols.
- Templates & header-only: how instantiations appear in SCIP; whether to collapse
  instantiations to the primary template node.
- Whether to keep graphify at all, even just for viz, or ship our own viz.
