# cppgraph — design

## Problem

Syntactic (tree-sitter) code graphs key symbols by **name**. On real C++ this
fails two ways at once:

- **Over-capture**: distinct symbols sharing a name collapse into one node.
  Example (MongoDB): `makeResumeToken` names *two* different symbols — the method
  `ChangeStreamEventTransformation::makeResumeToken` (2 real callers) and the free
  function `change_stream_test_helper::makeResumeToken` (~57 test call-sites). A
  name-based tool reports one node with ~66 edges; the truth is two nodes.
- **Under-capture**: calls that can't be bound syntactically are dropped —
  overloads, `ptr->method()` without an explicit type at the call-site, free
  functions, templates, virtual dispatch.

The fix is to key symbols by a **compiler identity** (USR / mangled name) and to
take edges from a compiler front-end index, not an AST guess.

## Source of truth: SCIP via scip-clang

`scip-clang` reads `compile_commands.json` and emits a SCIP index (protobuf):
per-occurrence symbol roles (definition vs reference), enclosing ranges, and
relationships (implementation/override, type definition). It is batch, parallel,
and crash-isolated per translation unit — important because clangd (tried first)
crashes on some third_party TUs.

## Graph model

Nodes = symbols, identified by SCIP symbol string (stable across TUs).
Node attrs: display name, kind, defining file+range, namespace/enclosing.

Edges:
- `calls`      caller-symbol → callee-symbol (attributed via enclosing definition
               of a reference occurrence with a call role)
- `references` symbol → symbol (non-call use)
- `overrides` / `implements`  (from SCIP relationships)
- `inherits`   type → base type
- `defines` / `contains`  file/namespace/class → member (structural)

## Building calls from SCIP

SCIP gives occurrences (symbol, range, roles). The original plan was to
attribute each reference to the definition symbol whose `enclosing_range`
contains it — but `scip-clang` v0.4.0 never populates `enclosing_range` (nor
`SymbolInformation.kind`), verified against real MongoDB data. Implemented
fallback (`src/cppgraph/builder.py`):

1. Callability is read off the SCIP symbol string's own grammar: a
   method/constructor descriptor always ends in `).` — no reliance on
   `kind`.
2. A call edge's caller is the nearest preceding callable-symbol
   *definition* in the same document, by start line (no range containment
   available, so line order is the next best signal). Verified to correctly
   recover both real callers of `ChangeStreamEventTransformation::makeResumeToken`.
3. Known limitation: this misattributes references that occur in
   declaration-only contexts (e.g. a pure-virtual method's own header
   declaration, sitting after a sibling method's declaration) to that
   sibling. Function-body call sites are unaffected. See `HANDOFF.md`.

This is where semantic identity still pays off even with this fallback: the
callee is the *exact* symbol, so the two `makeResumeToken` never mix,
independent of how the caller is attributed.

## Store

Start simple: in-memory graph + a single-file store (SQLite via stdlib `sqlite3`,
or a compact JSON). Scale target is modest for the pipeline subsystem alone
(162k nodes / 395k edges after dedup) — full `src/mongo` will be much larger;
measure before picking JSON vs SQLite for Phase 2.

## Keeping the graph up to date

MongoDB's source changes continuously; re-running a full index + full rebuild
on every edit won't scale once Phase 2 covers all of `src/mongo`. Two update
paths, kept in mind while designing the builder so this isn't a later rewrite:

1. **Re-indexing** is already naturally incremental at the `compile_commands.json`
   level: only the changed TUs need to go through `scip-clang` again (a filtered
   compdb subset), producing a partial `.scip` — no need to touch
   `compile_commands.json` itself unless the *build structure* changed (new
   files/targets/includes; see `INSTALL.md`/`AGENTS.md`).
2. **Merging + graph rebuild** is where the design choice matters. A partial
   `.scip`'s documents replace the corresponding `relative_path` entries in the
   full `Index`. The graph builder is deliberately kept **document-local**:
   caller attribution only ever looks at occurrences within the *same*
   `Document` (nearest preceding callable-symbol definition by line), so every
   `calls`/`implements` edge's `Edge.file` is exactly the one document that
   produced it. This means a change to file A can only ever invalidate edges
   where `e.file == A` — an incremental rebuild doesn't need cross-file
   analysis, just: drop all edges/owned-node-definitions for the changed
   file(s), re-run the per-document builder pass on their new occurrences, and
   re-insert. Not implemented yet (Phase 1 always does a full rebuild from a
   full `.scip`), but the document-local attribution design is what makes it
   possible later without changing the model.

## Serving

- CLI: `build`, `callers`, `callees`, `path`, `impact` (reverse blast-radius),
  `explain`.
- MCP server (later): expose the same queries to an LLM, token-budgeted.
- Export: optional graphify-compatible `graph.json` purely for visualization.

### Project root is a query-time parameter, never stored

`Node.file`/`Edge.file` are relative paths (from `Document.relative_path`) —
`graph.json` never embeds an absolute checkout location, which is what makes
it portable (unlike `compile_commands.json`, which bakes in machine/user-
specific absolute paths and can't be shared as-is — see `INSTALL.md`).
The one absolute path in the pipeline is `Metadata.project_root` in the
`.scip` file itself (a `file://` URI, set by scip-clang at index time) — it's
only useful as a *default suggestion* for whoever builds the graph, never as
a fact baked into the stored graph.

Consequence for query commands and the future MCP server: any operation that
needs to actually read source (e.g. returning a code snippet for a
`file:line` result) must take the checkout root as a **runtime argument**
(CLI flag / MCP tool parameter), not something read from the graph store.
This is what lets the same `graph.json` be reused after moving the mongo
checkout, or handed to a teammate with their own local clone, without
rebuilding anything.

## Language choice

Python-first. The perf-critical work (C++ parsing) is already an external
compiled binary (scip-clang). Everything here is glue + graph traversal at modest
scale. Port hot paths to Rust only if a measurement demands it.

## Roadmap

1. **POC**: install scip-clang; index one MongoDB subsystem (change_stream /
   pipeline); parse SCIP; build calls graph; verify the `makeResumeToken`
   disambiguation and a virtual-dispatch case that tree-sitter drops.
2. Full `src/mongo` index; store; CLI queries.
3. MCP server + token-budgeted retrieval.
4. graph.json export for viz; generalize / document for any C++ project.
