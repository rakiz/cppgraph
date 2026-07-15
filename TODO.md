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

- [x] Index all of `src/mongo` (6004 TUs, ~1253s, 0 errored; see `INSTALL.md`).
- [x] Persistent store: interned SQLite (`src/cppgraph/store.py`, stdlib
      `sqlite3`). Measured on the full graph: 1.19 GB flat JSON → 323 MB store
      (3.7×); `callers_of` off the `ix_dst` B-tree in 0.08 ms vs ~3.4 s to
      load the JSON per query. Decision + measurements in `DESIGN.md` § Store.
- [x] CLI queries: `find` (search by name), `callers`, `callees` — now served
      by `GraphStore` over the SQLite store, verified on the full mongo graph.
- [x] CLI queries: `path A B`, `impact` (reverse blast radius) — also served by
      `GraphStore` (id-space BFS over the indexed edges).
- [x] CLI query: `explain <symbol>` — definition site, caller/callee summary,
      and (only with `--root`) a source snippet. `--root` is a query-time
      argument, never stored, and is the *single* snippet switch: omit it for
      coordinates only (callers with their own file access), realizing
      `DESIGN.md` § "Project root is a query-time parameter" without any
      implicit fallback to the recorded `project_root`. Verified on mongo.
- [x] CLI query: `status [--root R]` — reports the graph's `source_commit` and,
      with `--root`, whether the working tree has drifted (exit 1 if stale) and
      which C++ files changed (non-source drift filtered out). This is the
      LLM/MCP "am I up to date?" check → feeds `reindex.sh --update`.
- [x] **Incremental update path** (`cppgraph update`, `store.update_store` /
      `GraphStore.apply_update`): re-index only changed TUs → apply the partial
      `.scip` to the store in place. The set of files whose old contributions
      to invalidate comes from the partial index's Documents (not the rebuilt
      graph — so a file that changed to produce *no* edges still gets its stale
      edges cleared). For each changed file: delete its edges, clear the
      definition site of symbols defined there, re-insert the partial graph's
      nodes/edges in bulk (`_bulk_intern`), then GC symbols left orphaned
      (undefined *and* unreferenced) so `find` doesn't surface stale symbols.
      All in one transaction; SQLite maintains the indexes incrementally.
      Verified on the full mongo store: a 519-TU partial (3833 documents,
      ~400k edges replaced) applied in ~10 s with the `makeResumeToken`
      over-capture preserved (3 callers, unchanged). Rests on the document-local
      builder — a change to file A only ever invalidates edges with `file == A`.
      - [x] Provenance anchor in place: `meta.source_commit` (+`source_dirty`)
            is recorded at build (`store.build_provenance`, captured at index
            time by `reindex.sh`). `git diff --name-only <source_commit>..HEAD`
            gives the exact changed-file set — the input to the update.
      - [x] Shell glue: `scripts/reindex.sh --update GRAPH_DB COMPDB [FILTER]
            [ROOT]` diffs the project's working tree against the store's
            `meta.source_commit`, filters the compdb to the changed TUs,
            re-indexes just those into a partial `.scip`, and calls `cppgraph
            update` (passing `--deleted` for removed files, and the new HEAD as
            the refreshed anchor). Verified end-to-end on a throwaway git repo:
            edit `lib.cpp` (compute→other), update re-indexed 1 TU, replaced its
            edge, and left the unchanged `main.cpp` edge intact. Warns when the
            diff contains headers (only refreshed if a re-indexed TU includes
            them → prefer a full rebuild for widely-included header changes).

## Phase 3 — serve to LLMs

- [x] MCP server exposing the queries, token-budgeted retrieval
      (`src/cppgraph/mcp_server.py`, console script `cppgraph-mcp`, optional
      `[mcp]` extra). FastMCP over stdio; the graph is fixed at launch
      (`--graph`, optional `--root`) so tools never re-pass a path. Tools:
      `find`, `who_calls`, `what_it_calls`, `path`, `impact_of`,
      `explain_symbol`, `status`. Every fan-out reply is capped (`DEFAULT_LIMIT`
      / `EXPLAIN_LIMIT`) with an explicit `total` + `truncated` flag; `explain`
      returns coordinates only unless `include_source=True` *and* `--root` was
      given. Pure `(store) -> dict` layer is unit-tested (17 tests); verified
      in-process end-to-end on the full mongo graph (both `makeResumeToken`
      symbols come back distinct through MCP). Target loop realized:
      status → impact/who_calls/path → explain.
- [x] Enrich the graph with `inherits` edges (derived → base). scip-clang emits
      `is_implementation` for both class inheritance and method override; split
      by SCIP descriptor kind (type→type = `inherits`, method→method =
      `implements`). Definition sites now recorded for every defined symbol
      (types too), so classes are locatable. Queries: `bases`/`subtypes` (CLI +
      MCP `base_classes`/`subclasses`) and `impact --kind inherits` (transitive
      subclasses). Verified on the real pipeline index (30445 inherits / 11950
      implements; `ServerParameter#` → 3 subclasses with def sites).
- [x] `references` as an exact **location index** (approach "C"): every
      non-local reference occurrence recorded as `symbol -> [file:line]` (no
      enclosing attribution, so zero heuristic and 100% exact), on by default
      (opt out with `--no-references`). Query returns coordinates, or — with
      `--root` — the tool reads the sources and returns snippets itself (reusing
      `read_source_snippet`, same dual mode as `explain`). Answers "where is this
      type/symbol used?" — the dependency the call graph is blind to. Verified on
      the real pipeline graph: `ResumeTokenData#` (a struct, 0 callers) → 155
      exact use sites, `--root` serving the parameter-type usages. CLI
      `references`, MCP `find_references`; incremental `update` refreshes the
      index too. Cost (full mongo): 5.3M deduped locations, store 323→468 MB
      (+45%), build ~40 s — modest, so default-on.
  - [ ] When scip-clang emits `enclosing_range` (PR #504, tracked), we can
        additionally offer *attributed* reference **edges** — approach "A"
        (type→type only) and "B" (all references), symbol→symbol and
        traversable, exact because containment replaces the nearest-preceding
        proxy. Make these an opt-in at indexing (they're big, and only worth it
        once attribution is exact). Until #504, edges would be heuristic-noisy
        exactly where references live (class bodies), so we deliberately do
        locations-only. See DESIGN.md § Graph model.
- [x] Compare usefulness against Serena on a real design question. Done:
      `COMPARISON.md` — cppgraph vs graphify (by-name, drops all real
      `makeResumeToken` call edges + collapses 431 `Value` sites) vs
      Serena/clangd (compiler-grade but its whole-repo background index doesn't
      finish in interactive time on mongo: cross-TU references stuck at 1 after
      6 min). Verdict: cppgraph wins for exact + transitive + offline structure.
- [ ] Staleness *magnitude*, not just binary. `status --root` today is
      fresh/STALE on any single changed C++ file and always recommends the
      incremental `reindex.sh --update`. Add a "how far behind" measure (e.g.
      `git rev-list --count` of commits since the indexed one, or % of TUs
      touched) and, past a threshold, recommend a full rebuild instead of an
      incremental update (incremental stops paying off once most TUs changed).

## Phase 4 — open-source / generalize

- [ ] Remove any MongoDB-specific assumptions from the tool core.
- [x] `graph.json` export (graphify-compatible) for visualization. `cppgraph
      export <symbol> --depth N --direction in|out|both` exports the bounded
      neighbourhood around a symbol (full graph is too large to render) as the
      graphify `nodes`/`links` schema, so it opens in our own viewer *and* in
      graphify. `src/cppgraph/export.py` (pure mapper) + `GraphStore.subgraph`.
- [x] Bundled offline viewer: `viz/cppgraph-viz.html` (our own code, MIT) with
      vis-network vendored locally (`viz/vendor/`, MIT/Apache-2.0). Loads a
      graph.json via picker / drag-drop / `?graph=` (http). See `viz/README.md`.
- [x] LICENSE — project is MIT (`LICENSE`, `pyproject` license field).
- [ ] Contributing notes, CI (lint + pytest), publish.

## Open questions (decide when reached)

- ~~SCIP call-edge attribution~~ **resolved**: scip-clang v0.4.0 emits neither
  `enclosing_range` nor `SymbolInformation.kind`. Fallback implemented:
  callability from the symbol's `).`  suffix, caller = nearest preceding
  callable-symbol definition by line. See `HANDOFF.md` for the verified
  details and known declaration-context false-positive limitation.
- Templates & header-only: how instantiations appear in SCIP; whether to collapse
  instantiations to the primary template node.
- Whether to keep graphify at all, even just for viz, or ship our own viz.
- ~~Phase 2: refine the nearest-preceding-definition misattribution of
  declaration-only references~~ **resolved (won't-fix heuristically,
  2026-07-15)**: proven a fundamental scip-clang v0.4.0 limitation — an
  in-class declaration and a genuine inline-body call are structurally
  identical (no `kind`/`syntax_kind`/`enclosing_range`), so every suppression
  rule drops 15–20% genuine edges. Kept as safe-direction over-capture. Clean
  fix blocked on `enclosing_range` (scip-clang PR #504, not yet merged);
  revisit on merge+release. Details in `HANDOFF.md` / `DESIGN.md`.
