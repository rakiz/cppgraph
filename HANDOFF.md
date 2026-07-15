# HANDOFF — start here

_Last updated: 2026-07-15_

## Where we are

**Tool comparison done** (`COMPARISON.md`, linked from README's new Documentation
map). Ran the real design question — "what calls
`ChangeStreamEventTransformation::makeResumeToken`?" — through three tools on the
`src/mongo/db/pipeline` subsystem:
- **graphify** (by-name, tree-sitter, v0.9.16): **0** call edges into *either*
  `makeResumeToken` (under-capture — dropped all real calls), and **431**
  unrelated `.getValue()`/`serialize()` sites collapsed onto one `Value` node
  (over-capture). Confirms the thesis.
- **cppgraph**: method **3** callers (2 real overrides + 1 known decl FP), free
  helper **122**, kept distinct; `Value` stays hundreds of distinct symbols;
  type `ResumeTokenData` = 0 callers but **155** exact use-sites; transitive
  `impact` = 14 in one query.
- **Serena/clangd** (drove its bundled clangd 19.1.2 directly on mongo's compdb):
  compiler-grade precision *where it answers*, but `incomingCalls` gave only the
  1 same-TU caller (~2.7 s) and `textDocument/references` stayed at **1 ref, 0
  cross-TU after 6 min** of background indexing — mongo's ~6000-TU index doesn't
  finish in interactive time. Matches the maintainer's "not very useful on mongo"
  experience. Verdict: cppgraph wins for exact + transitive + offline structure;
  Serena wins for live, in-sync, in-file navigation.

Also noted a follow-up in TODO: `status` staleness is binary today; add a
magnitude measure so it can recommend a *full rebuild* (not just incremental
`update`) once drift is large.

Store now stamps a **`schema_version`** (meta, currently 1) — the on-disk format
version, distinct from `cppgraph_version` (the writing code). It's the migration
enabler: a future incompatible schema change bumps it, and migration code
branches on the stored value. `GraphStore` refuses to open a store *newer* than
it understands (`IncompatibleStoreError`); an unversioned store is read as
legacy. `status` (CLI + MCP) now surfaces `built_at`, indexing tool+version,
schema/cppgraph version, and counts — "when/how was this indexed?" without the
`.scip`. 113 tests green.

## Where we were (references location index)

`references` landed as an exact **location index** (approach "C", on by default;
opt out with `cppgraph build --no-references`). Every non-local, non-definition
occurrence is recorded as `symbol → file:line` with *no* enclosing attribution —
so 100% exact, zero heuristic. Deliberately not edges: a reference edge's "who
references" would need the same nearest-preceding proxy as `calls`, but
references live disproportionately in class bodies (field/param/return types),
exactly where that proxy fails. Locations sidestep it. The query returns
coordinates, or — with `--root` — the snippet the tool reads itself (same dual
mode as `explain`, reusing `read_source_snippet`). CLI `references`, MCP
`find_references` (returns `available: false` if the graph lacks the index).
Incremental `update` refreshes references for changed files too (store carries a
`has_references` meta flag; GC now considers refs, not just edges). Verified on
the real pipeline graph: `ResumeTokenData#` — a plain struct with 0 callers and
0 subclasses — has **155 exact use sites**, `--root` serving the parameter-type
usages the call graph is blind to. Cost is modest (full mongo: 5.3M deduped
locations, store 323 MB → 468 MB / +45%, build ~40 s vs ~23 s), so it's on by
default; `--no-references` gives the leaner store. 110 tests green.

When scip-clang emits `enclosing_range` (PR #504), attributed reference *edges*
(traversable, exact via containment) become worth adding as an opt-in — approach
"A" (type-only) / "B" (all). Tracked in TODO.md; locations-only until then.

## Where we were (inherits edges)

`inherits` edges landed (derived → base). scip-clang emits `is_implementation`
for *both* class inheritance and method override; the builder splits them by
SCIP descriptor kind — type→type (`#`→`#`) becomes `inherits`, method→method
stays `implements` (verified on the pipeline index: 30445 vs 11950). Definition
sites are now recorded for *every* defined symbol (types/fields, not just
callables), so a class is locatable by `find`/`explain`. New query surface:
`bases` / `subtypes` (CLI) and `base_classes` / `subclasses` (MCP) for direct
inheritance neighbours, plus `impact --kind inherits` / `impact_of(kind=
"inherits")` for the whole transitive subtree ("everything that derives from
this base"). `bases`/`subtypes` return the related *type* with its own
definition site (an inheritance edge has no meaningful line). Verified on the
real pipeline graph: `ServerParameter#` → `IDLServerParameterWithStorage#`,
`IDLServerParameterDeprecatedAlias#`, `FeatureFlagServerParameter#`, each with a
def site. 96 tests green.

Next: `references` edges are DEFERRED pending a scope decision — measured ~778k
new edges on the pipeline subsystem alone (~3× the store at full scale), and
attribution reuses the same nearest-preceding proxy as `calls` (with its
class-body limitation). Options in TODO.md / DESIGN.md § Graph model. Then the
Serena comparison, then Phase 4 (generalize / open-source).

## Where we were (Phase 3 MCP server)

Phase 3 MCP server landed (`src/cppgraph/mcp_server.py`, console script
`cppgraph-mcp`, optional `[mcp]` extra). FastMCP over stdio, launched with
`--graph <db> [--root <checkout>]` so the graph path is fixed once and tools
never re-pass it. Seven tools wrap the `GraphStore` query surface — `find`,
`who_calls`, `what_it_calls`, `path`, `impact_of`, `explain_symbol`, `status` —
each returning a *token-budgeted* JSON dict: fan-out lists capped
(`DEFAULT_LIMIT=25`, `EXPLAIN_LIMIT=10`) with an explicit `total` + `truncated`,
and `explain` giving coordinates only unless `include_source=True` *and* a
`--root` was given (an LLM caller usually has file access, so coordinates are
the cheap default). The substance is a pure `(store, …) -> dict` layer
(`find_symbols`/`callers`/`callees`/`call_path`/`impact`/`explain`/
`status_report`), unit-tested independently of the transport (17 tests, 83
total green); verified in-process end-to-end on the full mongo graph — the two
`makeResumeToken` symbols come back distinct through MCP, `status` reports
up-to-date, `explain --include_source` yields the snippet at line 235. Realizes
the target loop: `status` (trust the graph?) → `impact_of`/`who_calls`/`path`
→ `explain_symbol`.

Next: enrich the graph with `references`/`inherits` edges (builder work, agreed
useful for dependency reasoning beyond calls). Then compare against Serena on a
real design question (TODO Phase 3 item 2).

## Where we were (query surface)

Query surface completed for the LLM/MCP workflow: `cppgraph explain <symbol>`
prints the definition site + the caller/callee summary, and — only if given a
`--root` checkout (a query-time arg that never lives in the store) — a source
snippet; omit `--root` for coordinates only, for callers that read files
themselves. `--root` is the single snippet switch (no implicit project_root).
`cppgraph status [--root R]` reports the graph's `source_commit` and, with a
checkout, whether it has drifted (exit 1 if stale, C++-only file filter) — the
"am I up to date?" check an LLM runs before trusting the graph, feeding
`reindex.sh --update`. 65 tests green; all verified on the full mongo store.
Decision on the declaration-context false-positive: closed as a fundamental
scip-clang limitation (see "Known limitation"), tracked on upstream PR #504.

Next: Phase 3 (MCP server wrapping these queries, token-budgeted), then enrich
the graph with `references`/`inherits` edges (agreed useful, builder work).

## Where we were (incremental update)

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
- **Known limitation (fundamental — investigated & closed 2026-07-15, will
  NOT be fixed heuristically)**: the nearest-preceding-definition proxy
  over-extends a definition's line span to the next definition, so a
  reference sitting in a *class body* (not a function body) is misattributed
  to the preceding definition — most visibly a member's own declaration
  (e.g. the base `makeResumeToken` declaration in the header → one spurious
  3rd caller alongside its two genuine `.cpp` callers). Real function-body
  call sites are unaffected. **This is not refinable with scip-clang v0.4.0**:
  a member's in-class *declaration* and a genuine inline-body *call* to that
  member are structurally identical (both role-`0`, no `kind`/`syntax_kind`/
  `enclosing_range`, definition `range`s are identifier-only). Proven by
  `WriteRarelyRWMutex::_lock` — declared `rwmutex.h:192`, genuinely called
  `rwmutex.h:150`, indistinguishable. Every suppression rule tried also drops
  genuine edges (15–20% collateral measured, mostly real inline-body calls),
  so we keep the over-capture — it is in the *safe* direction. The clean fix
  needs `enclosing_range`, which scip-clang doesn't emit (issue
  sourcegraph/scip-clang#323 closed *not planned*) but PR #504 implements;
  revisit when #504 merges + releases. See `DESIGN.md` § "Building calls".

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

Phases 2 and 3 are complete, plus `inherits` edges and the `references` location
index: SQLite `GraphStore`; the CLI queries `find`, `callers`, `callees`,
`bases`, `subtypes`, `references`, `path`, `impact` (`--kind calls|inherits`),
`explain`, `status`; incremental `cppgraph update` + `reindex.sh --update`; the
`cppgraph-mcp` server (10 tools) exposing all of it to an LLM. The
declaration-context false-positive is closed as a fundamental scip-clang
limitation (see "Known limitation"), tracked on PR #504.

Next is the **Serena comparison** on a real design question (TODO Phase 3
item 2): does cppgraph's compiler-exact impact/references beat Serena's
LSP-driven navigation for a concrete "what does changing X affect?" question?
After that, Phase 4 (generalize / open-source). Attributed reference *edges*
(approach A/B) stay parked until `enclosing_range` (PR #504) lands.

To run the MCP server:
`.venv/bin/cppgraph-mcp --graph scratch/mongo_full.graph.db --root /Users/sebastien.mendez/code/mongo`

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
