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
- **`--graph` is optional**: query commands auto-discover the newest
  `.cppgraph/*.graph.db` from the cwd (same walk as the MCP server,
  `store.discover_graph`), so running from inside an indexed project needs no
  `--graph`. Pass it explicitly to target a specific store or work from outside.
- **Plain names accepted, not just exact SCIP strings**: `callers foo` resolves
  `foo` via `find` (one match used directly, several listed to disambiguate, none
  errors) — the exact SCIP symbol still works as before.
- `status [--root]`: provenance + drift. Reports the changed **fraction** of
  indexed files and **commits behind**, and recommends a full **rebuild** once
  drift is large (≥25%) instead of always an incremental update.

### Serve to LLMs (MCP)
- `cppgraph-mcp` FastMCP server (optional `[mcp]` extra) exposing the query
  surface as token-budgeted tools: `find`, `who_calls`, `what_it_calls`,
  `base_classes`, `subclasses`, `find_references`, `path`, `impact_of`,
  `explain_symbol`, `status`, `visualize`.
- **Token-lean output by default** (all fan-out tools): results carry a readable
  `name` + `file:line`, not the 150-250-char SCIP symbol string. The name is the
  indexed display name when present, else a label **derived from the SCIP string**
  (scheme prefix, anonymous-namespace file path, overload hash and back-ticks
  stripped) — `scip-clang` leaves display_name empty, so the derivation is what
  actually makes the output lean. Pass `full_symbols=True` for the raw SCIP
  strings (`find` always returns them, since it's the name→SCIP resolver).
  Measured on `who_calls`: ~5.5x smaller payload (test filtering + label
  shortening combined); see `scripts/measure_tokens.py`.
- **Test noise filtered by default** (`who_calls`, `what_it_calls`,
  `find_references`, `impact_of`, `explain_symbol`): callers/callees/uses in test
  files — including destructor teardown sites — are dropped; `exclude_tests=False`
  brings them back. Each response echoes `excluded_tests`.
- **`find_references` snippets deduplicated**: with `include_source`, sites are
  grouped by file and overlapping `± context` windows are merged into one snippet
  (shared lines sent once, hit lines flagged `is_use`) instead of re-sent per hit.
- **Update / rebuild advice in `status`**: a `tool` section reports whether a
  newer cppgraph is published and — the part that stings — whether adopting it
  (or the version already installed) needs a full graph rebuild, so an upgrade
  never silently blocks on minutes of re-indexing. Source of truth is a hosted
  `versions.json` (repo root) with a per-release `requires_rebuild` flag; fetched
  best-effort with an on-disk cache (24h TTL, `force_update_check=True` refetches),
  fails soft when offline. Opt out with `CPPGRAPH_NO_UPDATE_CHECK=1`; override the
  URL with `CPPGRAPH_VERSIONS_URL`. Logic in `cppgraph.updates`.
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

### Setup & platforms
- `scripts/setup.sh`: one-shot venv + deps + scip-clang, with version selection
  (`--version`/`--nightly`/`--branch`). Installs the (pure-Python) tool on
  **every** platform; where no native scip-clang exists (arm64-linux, Intel Mac,
  Windows) it skips only the indexer and points at the container flow.
- **ARM-Linux / Windows indexing via a container.** scip-clang ships no
  arm64-linux binary; `scripts/index-in-container.sh` runs it in an `linux/amd64`
  container (emulated on ARM) to emit the `.scip`, then you build the graph
  natively (pure Python, any platform). Uses **docker or podman** (auto-detected;
  `CPPGRAPH_CONTAINER` to force one). Same CLI as `reindex.sh`; after writing the
  `.scip` it **resumes automatically** with the native `cppgraph build`
  (`CPPGRAPH_INDEX_NO_BUILD=1` to stop at the index). Helps with prerequisites it
  can't assume: missing container engine (suggests `podman`) and missing
  `compile_commands.json` (CMake / Bazel incl. MongoDB's `//:compiledb` / `bear`).
  Dockerfile in `docker/`. Alternatively index on any x86_64 box and copy the
  `.graph.db` over.
- **Emulation preflight for `index-in-container.sh`.** On a non-x86_64 host,
  checks that QEMU `binfmt_misc` amd64 emulation is registered *before* building;
  if missing it stops with the one-line fix (`… tonistiigi/binfmt --install amd64`)
  instead of letting the amd64 build die on the opaque `exec /bin/sh: exec format
  error`. The fix command echoes the detected engine (docker/podman).
- **`reindex.sh` reuses a prebuilt `.scip`.** On a host without a native
  scip-clang, a full re-index skips indexing and builds straight from an existing
  `<project>/.cppgraph/<name>.scip` (from the container step or copied in), so
  "generate the index once, then `reindex.sh` as usual" works. Incremental
  `--update` still requires the native binary.

### Docs
- Measured comparison vs graphify (by-name) and Serena/clangd (LSP) on a real
  design question — see `COMPARISON.md`.
- Token-cost comparison vs an LLM's own `grep`-and-read loop (`README.md` §"Why
  not just grep?"), reproducible with `scripts/measure_tokens.py`: ~16× fewer
  tokens to answer "who calls this method?" (~41× on a hub symbol), and exact —
  the script also reports grep's **signal/noise** (e.g. 3 of 156 lines are real
  call sites → 98% noise; cppgraph is 100% signal, compiler-resolved).

### Project
- Licensed **MIT**.
