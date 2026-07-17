# Changelog

All notable changes to cppgraph. The format follows
[Keep a Changelog](https://keepachangelog.com/). This project is pre-1.0; the
on-disk store also carries its own `schema_version` for forward-compatibility.

## [Unreleased]

_Nothing yet._

## [0.1.0] - 2026-07-17

First release. cppgraph builds an exact, compiler-grade C++ call/type graph and
serves it to humans (CLI) and to LLMs (MCP), with a focus on precise answers and
token-lean output.

### Graph
- Built from a **compiler index** (SCIP via `scip-clang`), not a syntactic AST:
  exact symbol identity, edges disambiguated across overloads, virtual dispatch,
  templates, and free functions.
- Edge kinds `calls`, `inherits`, `implements`; a definition site is recorded for
  every symbol, types included.
- Exact **reference-location index** (`symbol → file:line`), on by default —
  answers "where is this used?" for symbols the call graph can't (e.g. a struct).

### Store
- Interned **SQLite** store, queried off B-tree indexes without loading the whole
  graph into RAM.
- Incremental **`update`**: re-index only the changed translation units, in place.
- Self-describing: build provenance plus an on-disk `schema_version` that refuses
  a store newer than the code understands.

### CLI
- Queries: `find`, `callers`, `callees`, `bases`, `subtypes`, `references`,
  `path`, `impact`, `explain`.
- Auto-discovers the project graph from the working directory (`--graph`
  optional) and accepts plain names, not just raw SCIP symbol strings.
- **`status`**: provenance and drift (changed fraction, commits behind) with a
  rebuild-vs-incremental recommendation, plus level-aware tool-update advice
  (`none` / `store` / `reindex`).

### MCP server
- `cppgraph-mcp` exposes the full query surface as token-budgeted tools; one
  global registration serves every indexed project via auto-discovery.
- **Token-lean by default**: readable `name` + `file:line` instead of raw SCIP
  strings, test noise dropped by default, and source snippets returned inline on
  request (no separate file read).
- **Query quality**: multi-term AND `find` with case/separator-insensitive and
  leaf-name fallbacks; overloads grouped with source-derived signatures; opt-in
  `hide_trivial`; and explicit notices instead of misleading empty results (type
  blast-radius, empty hierarchy, no static path).

### Export & visualization
- `export` a bounded neighbourhood as graphify-compatible JSON (dependency or
  usage view); `view` / the MCP `visualize` tool render a **self-contained**,
  offline HTML.

### Setup & platforms
- One-shot `setup.sh` (venv + deps + scip-clang, version-selectable); the
  pure-Python tool installs on every platform.
- **ARM-Linux / Windows indexing via a container** (docker or podman), resuming
  automatically into a native graph build; reuses a prebuilt `.scip` where no
  native indexer exists.

### Docs & license
- Measured comparisons vs graphify and Serena/clangd, and vs an LLM's own
  grep-and-read loop.
- Licensed **MIT**.
