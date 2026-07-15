# HANDOFF — start here

_Last updated: 2026-07-15_

## Where we are

Incremental update landed: `cppgraph update --graph <graph.db> --scip
<partial.scip> [--deleted PATH ...] [--source-commit C]` applies a partial
re-index (only the changed TUs) to an existing store **in place**, instead of
rebuilding from scratch. `store.update_store` / `GraphStore.apply_update`: the
changed-file set is the partial index's Documents (+ `--deleted`), so a file
re-indexed to zero edges still has its stale edges cleared; for each changed
file it deletes edges, clears defs sited there, bulk-reinserts the partial
graph's nodes/edges (`_bulk_intern`), then GCs orphaned symbols (undefined +
unreferenced) so `find` stays clean — all in one transaction, indexes
maintained incrementally. Verified on the full mongo store: a 519-TU partial
(3833 documents, ~400k edges replaced) in ~10 s, `makeResumeToken` over-capture
preserved (3 callers). 57 tests green. The shell glue is also done:
`scripts/reindex.sh --update GRAPH_DB COMPDB [FILTER] [ROOT]` diffs the working
tree against the store's `meta.source_commit`, filters the compdb to the
changed TUs, re-indexes just those, and calls `cppgraph update` — verified
end-to-end on a throwaway git repo (edit a .cpp → only its edges change, the
unchanged file's edges stay). See `DESIGN.md` § "Keeping the graph up to date".

## Where we were (Phase 2 store)

Phase 2 store landed: the graph now persists as an **interned SQLite store**
(`src/cppgraph/store.py`), not flat JSON. `cppgraph build --scip <index.scip>
--out <graph.db>` writes it; all queries (`find`/`callers`/`callees`/`path`/
`impact`) are served by `GraphStore` off B-tree indexes without loading the
whole graph into RAM. Measured on the full `src/mongo` graph (643,967 nodes,
2,735,021 edges): 1.19 GB flat JSON → **323 MB** store (3.7× smaller), build
~23 s; `callers` query ~0.08 ms off `ix_dst` vs ~3.4 s to load the old JSON per
query. Over-capture still holds at full scale: the `ChangeStreamEventTransformation`
method `makeResumeToken` has 3 callers, the free-function helper 122 — two
distinct nodes. Decision + numbers in `DESIGN.md` § Store. 49 tests green.

The store also carries a `meta` provenance table: `project_root`, indexing
tool + version, build timestamp, node/edge counts, and the **source commit**
(`source_commit`/`source_dirty`) — captured at index time by `reindex.sh`
(`--source-commit`), else auto-detected via git on `project_root`. That commit
is the anchor for the still-TODO incremental `cppgraph update` (`git diff` the
stored commit vs HEAD → exact changed-file set). `cppgraph build` prints it.

Phase 1 (POC) remains complete. `cppgraph build` works end-to-end. Both
acceptance tests pass:

- **Test A (over-capture)**: `ChangeStreamEventTransformation::makeResumeToken`
  and `change_stream_test_helper::makeResumeToken` come out as two distinct
  graph nodes, each with its own correctly separated caller set. Verified
  both as a unit test (`tests/test_builder.py`) and against the real indexed
  MongoDB pipeline subsystem (`scratch/pipeline.scip`).
- **Test B (under-capture)**: calls are attributed purely from the
  SCIP-resolved callee symbol, never from call-site syntax, so a call through
  a pointer/virtual dispatch is captured like any other call — a case a
  name-based (tree-sitter) tool would drop. Covered by
  `test_call_attributed_to_nearest_preceding_function_definition`.

Tooling installed and verified (see `INSTALL.md`): `scip-clang` v0.4.0
(`scratch/bin/scip-clang`, gitignored), `scip.proto` + generated bindings
committed at `src/cppgraph/proto/`. Local `.venv` via `uv`.

Read order for a fresh session: `AGENTS.md` → this file → `DESIGN.md` →
`TODO.md`. For setting up a new machine, `INSTALL.md`.

## The decision, in one paragraph

Tree-sitter graph tools (graphify) key symbols *by name* → they merge distinct
symbols (over-capture) and drop hard-to-bind calls (under-capture). Verified
empirically on MongoDB: `makeResumeToken` is really **two** symbols — a method
and a test-helper free function — that a name-based tool would report as one
node. Fix: build the graph from a **compiler index** (SCIP via `scip-clang`)
where identity is a stable symbol string. Tool is Python (glue); the heavy
C++ parsing is scip-clang (external binary). Standalone open-source project,
C++-general, MongoDB-first.

## Key implementation facts (verified against real data, not assumed from the schema)

- `scip-clang` v0.4.0 **never populates** `SymbolInformation.kind` (100% of
  152,984 entries in the pipeline index are `UnspecifiedKind`) nor
  `Occurrence.enclosing_range` (0 of 177 definitions in one file had it).
  `src/cppgraph/builder.py` cannot and does not rely on either field.
- Instead: callability comes from the SCIP symbol grammar itself — a
  method/constructor descriptor always ends in `).` (`is_callable_symbol`).
  Caller attribution is the nearest preceding callable-symbol *definition* in
  the same document, by start line — verified to correctly find both real
  callers of `ChangeStreamEventTransformation::makeResumeToken` in
  `change_stream_event_transform.cpp`.
- A header included by N translation units surfaces the same occurrence
  (identical file/line/symbol/roles) once per TU after scip-clang merges
  partial indexes — verified on `change_stream_event_transform.h` (included
  by 3 TUs). Edges are deduped by `(kind, src, dst, file, line)`.
- **Known limitation**: the nearest-preceding-definition heuristic can
  misattribute a reference that appears in a *declaration-only* context
  (e.g. a pure-virtual method's header declaration sitting right after a
  sibling method's declaration) to that sibling as a false "caller". Real
  function-body call sites are unaffected — seen on the real pipeline data as
  one spurious 3rd caller edge for the base `makeResumeToken` declaration in
  the header, alongside its two genuine `.cpp` callers. Deferred to Phase 2
  (e.g. skip attribution for occurrences inside a class body that has no
  matching out-of-line definition in the same TU).

## Environment facts (verified)

- Target repo: `/Users/sebastien.mendez/code/mongo` (read-only for us).
- `compile_commands.json` EXISTS at mongo root: ~203 MB. Some entries use a
  bazel-out absolute path (`bazel-out/.../bin/src/mongo/...`), others use a
  bare relative path (`src/mongo/...`) for the *same* logical location —
  filter compdb subsets on the substring `src/mongo/...` (no leading `/`),
  not a prefix match, or you'll silently drop files like
  `change_stream_event_transform.cpp`.
- `scip-clang` indexed 519 TUs under `src/mongo/db/pipeline` in ~151s, 0
  errors (`scratch/pipeline.scip`, 23 MB). Full `src/mongo`: 6004 TUs, ~1253s,
  0 errored → `scratch/mongo_full.scip` (797 MB) → `scratch/mongo_full.graph.db`
  (323 MB interned SQLite). All gitignored.

## Exact next step

Phase 2 store + queries + incremental `update` are done (SQLite `GraphStore`,
all five CLI queries, `cppgraph update`, `reindex.sh --update`). Remaining
Phase 2 work in `TODO.md`: (a) the declaration-context false-positive
refinement in the builder heuristic (known limitation above), (b) `explain`
query + project-root runtime param when queries start returning source
snippets.

## Key reference symbols for the acceptance tests

- `ChangeStreamEventTransformation::makeResumeToken`
  — defined `src/mongo/db/pipeline/change_stream_event_transform.cpp:235`
  (declared in `.h:72`).
- `change_stream_test_helper::makeResumeToken` — separate free function.

## Guardrails (from AGENTS.md)

- No commits without explicit maintainer approval — **and always ask again
  each time**, even after a plan was agreed on (standing user rule).
- Never write into the mongo repo.
- `*.scip` / graph dumps stay in `scratch/` (gitignored).
