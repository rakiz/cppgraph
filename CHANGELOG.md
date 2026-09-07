# Changelog

All notable changes to cppgraph. The format follows
[Keep a Changelog](https://keepachangelog.com/). This project is pre-1.0; the
on-disk store also carries its own `schema_version` for forward-compatibility.

## [Unreleased]

### Added

- **`reachable_from`**: forward transitive reachability — the exact mirror of
  `impact_of` (reverse): from an entry point, everything it transitively
  reaches along `calls` edges ("what can this external handler trigger?" —
  attack-surface mapping; also dependency-migration scope). Per the
  `DESIGN.md` corollary the result is a **lower bound** — static
  compiler-traced edges only; virtual dispatch, function pointers and runtime
  registration have no static edge, so the true reachable set may be larger —
  and it is *worded* that way on both surfaces, never as a set the reader may
  act on by exclusion: a standing `note` on every MCP response and a printed
  caveat line on the CLI ("at least these are reachable"). Algorithmically a
  near-direct port of `GraphStore.impact`: the same id-space BFS over the
  `edges` index, walking `src_id -> dst_id` instead of `dst_id -> src_id`
  (`ix_src` instead of `ix_dst`), symbol strings materialized only for the
  final result set. `kind="inherits"` is kept for parity with `impact` and is
  coherent forward: `inherits` edges run derived -> base, so the forward walk
  from a derived type is its transitive *base hierarchy* — the mirror of
  `impact --kind inherits`'s transitive subclasses. One forward-only special
  case: `kind="calls"` on a *type* returns a notice, not a bare `total: 0` —
  a type makes no calls itself, so its reachability lives in its methods
  (pointed at `class_members`), the same "never a misleading zero" rule as
  `impact_of`'s type redirect to `find_references`. Single-symbol signature
  (`reachable_from(symbol)`), matching `impact_of`'s shape: attack-surface
  mapping is naturally one entry point at a time. CLI: `reachable-from
  <symbol>` with `--depth`/`--kind` and the shared query filters; MCP tool
  `reachable_from`; both thin wrappers over the same
  `GraphStore.reachable_from` — bounded output (`limit` + `total` +
  `truncated`), `exclude_tests` on by default, `include_paths`/
  `exclude_paths` on the result set.

- **`dependency_cost`**: call-site count against a target library — "if I
  replace/remove this library, how many call sites change?", answered as an
  exact count of `calls` edges whose *callee* is defined under one of the
  given path prefixes (e.g. `spirv_cross/`). A fact, never a risk assessment:
  it says nothing about API compatibility or migration difficulty, and — like
  every static call graph — it counts compiler-bound call sites only. Landed
  as a *mode* of `hotspots`, per the TODO's intent, but **not** "for free" as
  the TODO optimistically assumed: `hotspots`' path filtering was symmetric
  (an edge counts only if *both* endpoints' definition files pass the same
  prefix), which cannot express "callee inside the prefix, caller anywhere" —
  symmetric `include_paths=["spirv_cross/"]` counts only *library-internal*
  fan-in (and there is none: no edge has both endpoints there), a different
  question. So `GraphStore.hotspots` gained `target_paths`: the callee side
  is pinned to symbols defined under the prefixes (`cpg_target_ok`, the same
  registered-`matches_path_prefix` SQL pattern as `cpg_path_ok`), while
  `include_paths`/`exclude_paths` switch to the **caller side only** —
  "call sites into `spirv_cross/` from my code, not from other vendored users
  of it" (`exclude_paths=["vendor/"]`; vendored callers count by default).
  `kind` must be `fan_in` there (the mode *is* an incoming-call-site count;
  anything else raises `ValueError`, matching `hotspots`' existing defensive
  validation), `exclude_tests` keeps its symmetric either-endpoint shape, and
  `target_paths=None` (the default) preserves today's symmetric semantics
  exactly — a fully backward-compatible additive change (pinned by a
  dedicated regression test). `hotspots` also accepts `limit=None` now (the
  uncapped ranking, for callers that aggregate over it). The headline number
  is added only at the presentation layer: the pure
  `dependency_cost_report` / the `dependency-cost` CLI subcommand report
  `total_call_sites` (the summed "N call sites" answer) and
  `distinct_target_symbols` alongside `top_targets` (the per-symbol
  breakdown, heaviest first, bounded by `limit`; the totals are always the
  full counts, so a capped list never under-states the headline) — both
  surfaces backed by the same `GraphStore.hotspots(kind="fan_in",
  target_paths=…)`, no duplicated SQL. CLI: `dependency-cost --target-path
  PREFIX` (repeatable, required) with `--limit`/`--exclude-tests`/
  `--full-symbols` and the caller-side `--include-path`/`--exclude-path`;
  MCP tool `dependency_cost` takes `target_paths` as a list.

- **`strongly_connected_components`**: the cycles of the call graph —
  strongly-connected components of the `calls` subgraph with more than one
  member, i.e. maximal sets of symbols that can all reach each other, the
  exact primitive behind "circular dependencies". A graph fact, never a
  verdict: mutual recursion is often completely legitimate (a visitor
  pattern, a recursive-descent parser's mutually-recursive rules), and the
  standing `note` says so — the LLM decides which cycles, if any, matter.
  Algorithmically the first whole-graph traversal among the ranking tools:
  iterative Tarjan over the `calls` edges in id-space (all `(src_id, dst_id)`
  pairs fetched in one scan, adjacency walked as integers, symbol strings
  resolved only for the components actually returned — the same
  "hot topology all-integer, cold payload materialized late" discipline as
  `hotspots`; iterative because a call graph has thousands of nodes and the
  recursive form would overflow Python's ~1000-frame limit on a deep chain).
  `exclude_tests`/path filters apply to the *output*, not the edges Tarjan
  sees — a cycle genuinely involving test-only or vendored symbols is still a
  real cycle in the compiled binary, and pre-filtering edges could split or
  hide one — so a component is dropped only when *every* member is filtered
  out, and a reported component always lists all its members (redacting
  members would misrepresent the compiled dependency). Direct self-recursion
  (a degenerate 1-node cycle) is out of scope by spec: components of
  size > 1 only. On the CLI (`strongly-connected-components`) and as an MCP
  tool, both backed by the same `GraphStore.strongly_connected_components`;
  components sorted biggest first, members by definition `file:line`; bounded
  output (`limit` caps components, `total` the full count).

- **`outline` / `class_members`**: list definitions by container — two facets
  of one primitive, both exact and available on any graph (no #504 needed),
  no judgment, structure not interpretation. `outline(file)` is the outline
  of a single file: every symbol *defined* there (`Node.file == path`),
  sorted by line — a compact symbol list that replaces a `Read` of a
  1400-line file, the tool that beats the Read/grep reflex on its own turf.
  `class_members(symbol)` is every member declared on a class/struct —
  methods, fields, nested types — found by SCIP container nesting: a member's
  symbol string starts with its class's own symbol string, which ends in `#`,
  so the boundary is exact (`mongo/Foo#` prefixes `mongo/Foo#parse(a1).`,
  never `mongo/FooBar#x.`) and no new symbol parsing is involved. Named
  `class_members`, not `public_api`, because SCIP doesn't encode C++
  visibility — members that *exist* (a fact), not a public/private claim.
  On the CLI (`outline <file>`, `class-members <symbol>`, both with
  `--limit`/`--full-symbols`) and as MCP tools, both backed by the same
  `GraphStore.outline`/`GraphStore.class_members`; bounded output (`limit` +
  `total`); `outline`'s path match is exact (an empty result carries a note
  pointing at `stats`, not a bare zero); `class_members` on an unknown symbol
  follows the shared resolution convention (error dict / candidates, never a
  guess) and on a known non-type symbol returns an error dict — bad input,
  never an empty list that would read as "no members".

- **`boundary_violations`**: declared-layering conformance check — given rules
  supplied per-query by the caller (`("common/", "platform/")` = "no symbol
  defined under `common/` may call one defined under `platform/`"; the graph
  stores no intended architecture of its own), lists the `calls`/`inherits`
  edges that cross them. Each reported violation *is* a real compiler-traced
  edge — zero false positives by construction — and the standing note states
  the converse as a lower bound (0 violations means no *statically indexed*
  edge crosses the rules; runtime dispatch — virtual calls, function pointers
  — can cross a boundary with no static edge), never a proof of conformance.
  Directory membership is the endpoint's own definition file, matched on a
  path-segment boundary via the shared `cppgraph.filters.matches_path_prefix`
  (registered as a SQL function, the same pattern as `hotspots`'
  `cpg_path_ok`); a symbol with no definition site belongs to no layer. On
  the CLI (`boundary-violations --rule FROM:FORBIDDEN`, repeatable, plus
  `--kind`/`--limit`/`--full-symbols`) and as an MCP tool (`rules` as
  `[from_prefix, forbidden_prefix]` pairs), both backed by the same
  `GraphStore.boundary_violations`; an edge matching several rules is
  reported once per rule, each record naming the rule it broke; bounded
  output (`limit` + `total`); malformed rules raise/`parser.error` on the CLI
  and come back as an error dict on MCP (a typo must never silently read as
  "layering holds").

## [0.2.0] - 2026-09-04

### Added

- **`no_incoming_calls`**: callable definitions with zero incoming `calls`
  edges — the exact primitive behind the "dead code" question, stated as a
  graph fact, never a verdict (vtable dispatch, exported API, templates, entry
  points all have no static caller yet are live; the MCP response carries that
  caveat as a standing `note`). Gated on the same `has_enclosing_ranges` signal
  as `line_span`: on a stock-binary graph it refuses with the reason — the
  nearest-preceding attribution fallback can fabricate a phantom caller from a
  bodyless declaration site, turning a real 0 into a false 1 — rather than
  answer unreliably. On the CLI and as an MCP tool, both backed by the same
  `GraphStore.no_incoming_calls` (a SQL anti-join over `edges`, callability
  from the shared `is_callable_symbol`); bounded output (`limit` + `total`),
  same `exclude_tests`/path-prefix filters, and only symbols with a recorded
  definition site (a test-only caller still counts as a caller).
- **`line_span`**: definitions ranked by body extent (`end_line - start line`,
  largest first) — where the biggest bodies live, from the exact
  `enclosing_range` extents, not a def→next-symbol heuristic. #504-only: on a
  stock-binary graph the tool reports `available: false` with the rebuild
  pointer (the same degrade-cleanly contract as the reference-attribution
  features) instead of a silently empty list. On the CLI and as an MCP tool,
  both backed by the same `GraphStore.line_span`; bounded output (`limit` +
  `total`) and the same `exclude_tests`/path-prefix filters as `hotspots`.
  Persists `Node.end_line` in the store — **schema v3**: older stores keep
  working (a store rebuild, or an incremental `update` that actually touches
  affected files, adds the column on demand), and an older
  cppgraph refuses a v3 store with the usual upgrade/rebuild error.
- **`stats`**: module-level aggregate counts per file (`--group-by file`) or
  rolled up per directory via `dirname` (`--group-by dir`) — symbols defined,
  `calls` edges whose call site, and reference use sites, sorted by the three
  summed descending. A "how big / how dense is this part of the codebase" view
  to size up an unfamiliar module without reading files, next to `hotspots`'
  "what's most-called". On the CLI and as an MCP tool, both backed by the same
  `GraphStore.stats` (three SQL `GROUP BY file_id` aggregations merged, dir
  rollup in Python); bounded output (`limit` + `total`), and the same
  `include_paths`/`exclude_paths` prefix filters as `hotspots`, applied to the
  counted file before aggregation.
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
- **`transport` in `status`**: `"cli"` or `"mcp"`, so a copied status block (e.g.
  from a Task subagent without direct MCP access) says which surface produced
  it instead of resting on an agent's say-so.
- **`signature` in `explain`/`explain_symbol`**: a source-derived parameter list
  for the definition, including any default argument value verbatim (e.g.
  `(bool useNullIfMissing = false)`) — invisible from the graph alone, which
  never carries a parsed signature. Needs `--root`/a configured checkout, like
  the existing `include_source`. Shares `extract_signature` with `find`'s
  overload-signature grouping (moved to `cppgraph.cli` alongside
  `read_source_snippet`, its only dependency).

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
