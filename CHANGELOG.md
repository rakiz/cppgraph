# Changelog

All notable changes to cppgraph. The format follows
[Keep a Changelog](https://keepachangelog.com/); this project is pre-1.0 and the
on-disk store carries its own `schema_version` (see below).

## [Unreleased]

Everything so far — the project has not cut a numbered release yet.

### Graph model & builder
- Build a call graph from a **compiler index** (SCIP via `scip-clang`), not a
  syntactic AST, so symbol identity is exact and edges are disambiguated
  (overloads, `ptr->method()`, virtual dispatch, free functions, templates).
- `calls` edges attributed from the SCIP-resolved callee, never from call-site
  syntax. Caller attribution is the nearest preceding callable definition
  (documented limitation for in-class-declaration contexts — a fundamental
  `scip-clang` gap pending upstream `enclosing_range`, PR #504).
- `inherits` edges (class hierarchy) and `implements` edges (method override),
  split by SCIP descriptor kind; definition sites recorded for every symbol
  (types included), so a class is locatable.
- Exact **reference-location index** (`symbol → file:line`, default-on,
  `--no-references` to skip): every non-local use recorded as a location with no
  enclosing attribution — 100% exact, zero heuristic.

### Store
- Interned **SQLite** store: queries served off B-tree indexes without loading
  the whole graph into RAM.
- Provenance `meta` table: project root, indexing tool + version, build
  timestamp, counts, and the source commit.
- **`schema_version`** stamped on disk (migration enabler + forward-compat
  guard: refuses to open a store newer than the code understands).
- **Incremental `update`**: apply a partial re-index (only changed TUs) to an
  existing store in place, GC-ing orphaned symbols.

### Queries (CLI)
- `find`, `callers`, `callees`, `bases`, `subtypes`, `references`, `path`,
  `impact` (`--kind calls|inherits`), `explain`.
- `status [--root]`: provenance + drift. Reports the changed **fraction** of
  indexed files and **commits behind**, and recommends a full **rebuild** once
  drift is large (≥25%) instead of always an incremental update.

### Serve to LLMs (MCP)
- `cppgraph-mcp` FastMCP server (optional `[mcp]` extra) exposing the query
  surface as token-budgeted tools: `find`, `who_calls`, `what_it_calls`,
  `base_classes`, `subclasses`, `find_references`, `path`, `impact_of`,
  `explain_symbol`, `status`, `visualize`.
- Project auto-discovery (Serena-style): registered once, globally, the server
  finds the current project's graph from the working directory's `.cppgraph/`
  at launch — one registration serves every indexed project, no collision. In a
  project with no graph yet, tools return a clear "not indexed here" notice.

### Export & visualization
- `cppgraph export`: bounded neighbourhood around a symbol as a
  graphify-compatible `graph.json`. Two views: `--mode deps` (call/inherit
  subgraph) and `--mode usage` (symbol→file usage graph from exact references —
  the right view for a type). `--no-tests` filters test / test-support files.
- `cppgraph view` and the MCP `visualize` tool: one-shot render to a
  **self-contained** HTML (data + vis-network inlined) in a temp dir, opened in
  the browser — works under `file://` and offline.
- Bundled viewer `viz/cppgraph-viz.html` (MIT), vis-network vendored locally.

### Docs
- Measured comparison vs graphify (by-name) and Serena/clangd (LSP) on a real
  design question — see `COMPARISON.md`.
- Token-cost comparison vs an LLM's own `grep`-and-read loop (`README.md` §"Why
  not just grep?"), reproducible with `scripts/measure_tokens.py`: ~20× fewer
  tokens to answer "who calls this method?", and exact.

### Project
- Licensed **MIT**.
