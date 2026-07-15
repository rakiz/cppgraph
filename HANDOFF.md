# HANDOFF — start here

_Last updated: 2026-07-15_

## Where we are

Phase 1 (POC) is functionally complete. `cppgraph build --scip <index.scip>
--out <graph.json>` works end-to-end. Both acceptance tests pass:

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
  errors. Output: `scratch/pipeline.scip` (23 MB), `scratch/pipeline.graph.json`
  (~183 MB — CLI output, gitignored; SQLite store is a Phase 2 TODO if JSON
  gets unwieldy at full-repo scale).

## Exact next step

Phase 1 acceptance is done. Next up is Phase 2 from `TODO.md`: index all of
`src/mongo`, move the store off flat JSON (SQLite), and add the `callers` /
`callees` / `path` / `impact` CLI queries.

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
