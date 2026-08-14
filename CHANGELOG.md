# Changelog

All notable changes to cppgraph. The format follows
[Keep a Changelog](https://keepachangelog.com/). This project is pre-1.0; the
on-disk store also carries its own `schema_version` for forward-compatibility.

## [Unreleased]

### Added

- **`hotspots`**: ranks symbols by fan-in, fan-out, or total edge count across the
  whole graph — one call instead of N manual `who_calls`/`what_it_calls` queries.
  On the CLI and as an MCP tool, both backed by the same `GraphStore.hotspots`.
  Bounded output (`limit` + a `total` count), `exclude_tests` supported like other
  query tools.
- **`include_paths`/`exclude_paths`**: per-query `Node.file`-prefix filtering
  (simple prefix match, no glob/regex) on `find`, `who_calls`, `what_it_calls`,
  `find_references`, `impact_of`, and `hotspots` — on both the CLI
  (`--include-path`/`--exclude-path`, repeatable) and the MCP tools, driven by
  the shared `cppgraph.filters.matches_path_prefix`/`filter_by_path`. Scopes a
  query to "my code, not vendored deps" (e.g. `--exclude-path vendor/`).
  `hotspots` applies the filter in SQL (a `cpg_path_ok` function registered
  alongside the existing `cpg_is_test_file`), keeping its whole-graph
  aggregation off the Python side.

### Fixed

- **`cppgraph update`**: works with no arguments — auto-discovers the graph and
  compilation database from the working directory, re-indexes the changed
  translation units, and applies the update in place, matching every other query
  command's auto-discovery convention. `init.py`, `cli.py`, `mcp_server.py`, and
  `QUICKSTART.md` all point at this one command now, instead of four inconsistent
  (and in one case wrong) instructions. `pipeline.incremental_update` now reads
  "changed" the same way `status` does (via the dirty-fingerprint-aware
  `changed_files_since`), so the two agree on scope.

## [0.1.0] - 2026-08-14

First release. cppgraph builds an exact, compiler-grade C++ call/type graph and
serves it to humans (CLI) and to LLMs (MCP), with a focus on precise answers and
token-lean output.

### Graph
- Built from a **compiler index** (SCIP via `scip-clang`), not a syntactic AST:
  exact symbol identity, edges disambiguated across overloads, virtual dispatch,
  templates, and free functions.
- Edge kinds `calls`, `inherits`, `implements`; a definition site is recorded for
  every symbol, types included. Caller attribution is exact via `enclosing_range`
  containment when the binary emits it (a #504 build): a call site outside every
  known callable body yields no caller edge, never a guess. Without
  `enclosing_range` (stock binary), attribution falls back to the nearest-preceding
  callable definition in the same file.
- Exact **reference-location index** (`symbol → file:line`), on by default —
  answers "where is this used?" for symbols the call graph can't (e.g. a struct).
  With a #504 binary, references can be attributed to the enclosing definition
  (opt-in `--attributed-refs`, or `enrich-refs` for an existing store) for a
  **symbol-granularity usage view** — the functions that use a type, not just the
  files; `status` reports which granularity a graph carries.

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
- **Agent guidance on connect**: the `initialize` response carries an
  `instructions` block that points a connected model at the graph tools for code
  in the indexed scope — with the scope and a `status` freshness pointer baked in
  — and at its normal read/grep everywhere else (out-of-scope files, comments,
  string literals, non-indexed languages, and paging a file it has already
  located). `find` and `explain_symbol` lead their descriptions with the same
  text-search contrast.
- **Names, not just SCIP strings**: every symbol-taking tool (`who_calls`,
  `what_it_calls`, `impact_of`, `explain_symbol`, `path`, `base_classes`,
  `subclasses`, `find_references`) accepts a plain name through the shared
  `GraphStore.resolve` (also behind the CLI) — a unique name resolves, an
  ambiguous one returns candidates, `Class::method` maps to `Class#method`, and
  no symbol is ever guessed.
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
