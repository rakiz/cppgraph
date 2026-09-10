# cppgraph — design

## Problem

Syntactic (tree-sitter) code graphs key symbols by **name**. On real C++ this
fails two ways at once:

- **Over-capture**: distinct symbols sharing a name collapse into one node.
  Example: `makeResumeToken` names *two* different symbols — the method
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

scip-clang is a dependency pinned by **version** (the upstream release tag) and —
for the patched binary — by **patchset** (this repo's patch bundle, an independent
integer) in `versions.json` → `scip_clang`; `cppgraph status` flags a stale binary
/ a graph needing re-index on a version change, and advises (never nags) when an
installed patched binary predates the pinned patchset. Its **variant** (`stock`
upstream release vs `patched`, our bundle on top of PR #504) is *not* pinned as a
requirement: the two are valid
capability levels, not a right/wrong pair, and a graph's variant is independent
of the locally installed binary — a patched-indexed store can be copied to a machine
that only has the stock binary. So the installed binary's sidecar and each store's
`index_tool_variant` are reported by `status` for information, while what a graph
actually carries (file vs symbol-granularity usage) is surfaced by
`has_attributed_refs` / the usage view. See `updates.py` and
`docker/build-scip-clang-patched-linux/`.

### Why SCIP is the right foundation — and where its edge is

SCIP is a lossy serialization of clang's AST: a deliberately minimal,
cross-language format (Go, TS, Rust, Ruby, C++…). That loss is real but lands
almost entirely on data *peripheral* to cppgraph's mission (reliable
call/dependency facts for an LLM). Everything the mission needs is present and
exact — `calls`/`inherits`/`implements`/`typed-by`/`uses` edges, definition
locations, references, and (with #504) exact enclosing-range attribution. The
one gap the audit found that SCIP genuinely cannot express is **visibility**
(public/protected/private — no SCIP field at all; the format models privacy as
*local symbols*, a name-scope notion that doesn't map C++'s compile-time access
rule); see `TODO.md`. Every other field the audit flagged as unpopulated by the
official binary (`kind`, `signature_documentation`, `is_type_definition`,
`ReadAccess`/`WriteAccess`, `ForwardDefinition`) is now filled by one of our own
syntactic-classifier patches and consumed — see `CHANGELOG.md` for each, and
`SCIP_AUDIT.md` for the field-by-field measurement they were built against.
None of that touches the core graph.

The key point for build-vs-buy: **because we already patch scip-clang (#504),
the SCIP *format*'s limits are not hard limits.** scip-clang is itself a clang
LibTooling program — anything clang knows, a patched build can emit — and our
`.proto` is ours, so an extra field degrades gracefully on a stock binary
exactly as #504's enclosing ranges do. So the alternative to SCIP is never
"tree-sitter/ctags" (syntactic, not compiler-exact — breaks the founding
premise) nor "abandon scip-clang for libclang" (that *is* re-implementing
scip-clang); it is "patch scip-clang further". The visibility gap — the worst
case, a format gap rather than an emission gap — would move from *impossible* to
*a field to add in our fork*, at the cost of divergence from upstream SCIP.

That frames a standing decision, not a settled one: stay **schema-compatible
with upstream SCIP** (limited to their fields, advocate additions) versus treat
**our proto as our own** (unbounded up to what clang knows, at the cost of
maintaining a producer+schema fork). We already have one foot in the second camp
with #504. The line where SCIP would stop being enough is a mission pivot toward
rich semantic analysis (visibility-aware API surface, everything at type
granularity, CFG-based complexity) — and even then the move is "patch scip-clang
further", not "replace it".

## Graph model

Nodes = symbols, identified by SCIP symbol string (stable across TUs).
Node attrs: display name, kind, defining file+range, namespace/enclosing.

Edges (implemented unless marked planned):
- `calls`      caller-symbol → callee-symbol (attributed by `enclosing_range`
               containment with a #504 binary — the innermost callable
               definition whose body contains the call site — and via the nearest
               preceding callable definition as a stock-binary fallback; see below)
- `inherits`   derived type → base type (from SCIP `is_implementation`
               relationships where both endpoints are types; queried by
               `bases`/`subtypes` and `impact --kind inherits`)
- `implements` override-method → overridden-method (the method→method
               `is_implementation` relationships; the class→class ones are
               `inherits`)
- `typed-by`   field/variable → its declared type (from SCIP
               `is_type_definition` relationships — a patched-binary-only
               fact today, `#504`-free; queried by `impact --kind typed-by` /
               `reachable-from --kind typed-by`, opt-in on
               `boundary-violations` like `implements`)
- `defines` / `contains`  file/namespace/class → member, structural (planned)

References are **not** edges. They are stored as an exact **location index**
(the "C" approach), on by default (opt out with `cppgraph build
--no-references`): every non-local, non-definition occurrence recorded as
`symbol → file:line`, with no
attribution to an enclosing symbol by default. Rationale: the location is 100%
exact on its own, and attributing "who references" is only exact with a #504
binary. Without one, the sole option is the nearest-preceding proxy the stock
`calls` fallback uses — and references live disproportionately in *class bodies*
(field/param/return types), exactly where that proxy fails, so it is never
applied to references (unattributed beats mis-attributed). Exact attribution is
the opt-in `--attributed-refs`/`enrich-refs` path below, by containment — no
proxy. Either way the position is 100% exact, and the `references` query returns
coordinates or — with `--root` — the
snippet the tool reads itself (same dual mode as `explain`). This answers "where
is this type/symbol used?" — a dependency the call graph can't express (e.g.
`ResumeTokenData#`, a plain struct: 0 callers, 155 exact use sites across the
pipeline subsystem). Measured cost is modest — on a large index: 5.3M deduped
locations, store 323 MB → 468 MB (+45%), build ~40 s vs ~23 s — so it's on by
default; `--no-references` gives the leaner store when the index isn't wanted.

Each location can also carry the occurrence's `ReadAccess`/`WriteAccess` role
bits (`Reference.roles`, store column `refs.roles`, gated by the
`has_access_roles` meta flag) — set only by a scip-clang binary carrying the
`read-write-access-on-v0.4.0` patch; a stock binary sets neither bit, and the
query stays silent rather than guess. Where present, the `references` query
(CLI + MCP) tags each use site `(write)`/`(read+write)` and offers an
`--access {read,write}` filter ("who *writes* this?" vs plain reads).

When scip-clang emits `enclosing_range` (a #504-built binary), each reference can
additionally be *attributed* to the definition that contains it — exact via
containment, no proxy. This is opt-in (`cppgraph build --attributed-refs`, or
`cppgraph enrich-refs` to back-fill an existing store) because it costs a symbol
id per reference; the store records `has_attributed_refs`, and `status` (CLI +
MCP) advertises the granularity and the upgrade path. Measured on mongo: the
enrich attributed **3,032,620 / 7,237,345 references (42%)** and grew the store
**+146 MB (+23%)**, 626 → 773 MB, in **~3.5 min** (8.8 GB RSS, single-thread on
an 8-vCPU Graviton2). The `status` upgrade hint turns the cost into a
per-graph estimate — the graph's own `ref_count` × 0.39 B/ref — extrapolated
from a second, smaller A/B (a 224-TU project: 4,681,728 → 4,714,496 B for the
same 84,598 refs, attribution at build time), not from this number: the mongo
figure is the *in-place* `enrich-refs` path (row UPDATEs plus a temporary
composite index), a different write path that measured ≈20 B/ref — so the
hint is deliberately hedged as an extrapolation, never a measurement of the
graph it is shown on. The unattributed 58% is expected, not a
short-fall: they are the references that live *outside* any callable body —
file/namespace scope and, above all, class bodies (field/param/return types),
which aren't callable definitions. It powers the
symbol-granularity **usage view** (`export --mode usage`): `symbol → enclosing
definition` ("the *functions* that use this type", not just the files), falling
back to file granularity for any reference left unattributed — always exact
either way. The same containment sweep also takes **term** intervals (a
global's/field's `enclosing_range` spans its whole declaration *including the
initializer* — the #504 `saveVarDecl` patch, verified empirically), so a
global initializer's read of another global attributes to its global: the fact
behind **`global_init_references`** ("static initialization order fiasco" — a
reference, never a verdict, since a `constexpr`/`constinit` init is safe).
Terms are identified by SCIP descriptor (`.` but not a method's `).`), which is
also what excludes locals — measured, local variables carry `enclosing_range`
data too, but as `local <id>` symbols, so they never steal a reference from
their enclosing function. Known limit, measured: a lambda inside a global's
initializer gets no interval of its own, so a read inside its body attributes
to the global (it may run lazily — the tool states the region fact). The same
`enclosing_range`, on the `calls` side, gives exact caller
attribution (replacing the nearest-preceding heuristic when present), and each
definition's body extent is persisted on the node (`end_line`) — powering
`line_span` (rank by body size) and gating `no_incoming_calls` (a zero-caller
count is only trustworthy with exact attribution). Still
open: promoting attributed references to first-class traversable graph *edges*
for `impact`/`path`. See TODO.md.

### Templates (measured, not assumed)

scip-clang v0.4.0 represents each template as **one symbol — the primary
template** — not one per instantiation. Verified empirically: a function
template `maxv<T>` called as `maxv<int>` and `maxv<double>`, and a class
template `Box<T>` used as `Box<int>`/`Box<double>`, each produce a *single* node
(`maxv(…).`, `Box#`, `Box#get(…).`); every instantiation's call site attributes
to that one node. So the graph does **not** fragment across instantiations — no
collapse layer is needed, and `who_calls`/`impact` on a template already
aggregate all instantiations (the right default for "what does changing this
template affect?").

The flip side: per-instantiation granularity is unavailable — the graph knows
"calls to `maxv`", not "calls to `maxv<int>` specifically". *Explicit* and
*partial specializations* are genuinely different code and are expected to be
distinct symbols (correct); that edge case, plus cross-TU instantiation merging,
wasn't exhaustively measured — the core "one node vs N" question is what's
settled.

## Building calls from SCIP

SCIP gives occurrences (symbol, range, roles). The original plan was to
attribute each reference to the definition symbol whose `enclosing_range`
contains it — but `scip-clang` v0.4.0 never populates `enclosing_range` (nor
`SymbolInformation.kind`), verified against real index data. Implemented
fallback (`src/cppgraph/builder.py`):

1. Callability is read off the SCIP symbol string's own grammar: a
   method/constructor descriptor always ends in `).` — no reliance on
   `kind`.
2. A call edge's caller is the callable definition whose `enclosing_range`
   contains the call site, when the binary emits it (a #504 build) — exact,
   via containment. On a stock binary (no `enclosing_range`) it falls back to
   the nearest preceding callable-symbol *definition* in the same document, by
   start line. Verified to correctly recover both real callers of
   `ChangeStreamEventTransformation::makeResumeToken`. Point 3 below describes
   the fallback's known limit, which only applies when no enclosing range is
   present.
3. Known limitation (investigated 2026-07-15 — a *fundamental* limit of
   scip-clang v0.4.0, not a bug to refine): the nearest-preceding-definition
   proxy over-extends a definition's "territory" up to the next definition,
   so it misattributes references that occur in a *class body* rather than a
   function body — most visibly a member's own declaration sitting after a
   sibling definition (e.g. the base `makeResumeToken` declaration in
   `change_stream_event_transform.h`, or `ProcessId::asLongLong` /
   `WriteRarelyRWMutex::_lock` declarations). It shows up as spurious extra
   `calls` edges sourced at the preceding definition.

   This cannot be fixed correctly with scip-clang's current output. Measured
   on the pipeline index: a member's in-class *declaration* and a genuine
   inline-body *call* to that same member are both role-`0` occurrences with
   identical structure — proven by `WriteRarelyRWMutex::_lock`, declared at
   `rwmutex.h:192` and genuinely called at `rwmutex.h:150`, indistinguishable
   in SCIP. Every candidate suppression rule therefore also drops genuine
   edges (measured 15–20% collateral, mostly real inline-body calls). No
   separating signal exists: `SymbolInformation.kind`, `syntax_kind`, and
   `enclosing_range` are all unpopulated, and definition `range`s cover only
   the identifier, never the body. So we keep the over-capture — it is in the
   *safe* direction for over-capture (an extra caller); a separate drop path is
   documented below.

   The clean fix is `enclosing_range` (attribute a reference to a definition
   iff it is contained in that definition's body range). Stock scip-clang does
   not emit it (issue sourcegraph/scip-clang#323 was closed *not planned*), but
   PR sourcegraph/scip-clang#504 implements it. The builder consumes it: when a
   binary emits `enclosing_range`, caller attribution and (opt-in) reference
   attribution use range containment → exact, zero collateral; the over-capture
   above is the stock-binary fallback only. A #504 binary is built from source
   via `docker/build-scip-clang-patched-linux/`.

   On a #504 graph, `build_graph` detects the declaration case above by a
   different signal: `callable_intervals` is populated (the binary *does* emit
   body extents), yet a given role-`0` site still isn't contained by any of
   them — exactly what a bodyless in-class declaration looks like once real
   bodies are known. There it drops the edge instead of falling back to
   nearest-preceding, since that fallback is only a sound approximation when a
   document has no interval data at all.

   A patched (non-#504) graph has an equivalent signal, from a different
   patch: `scip-clang-patches/forward-definition-on-v0.4.0.patch` tags the
   bodyless declaration occurrence itself with `ForwardDefinition` — an
   existing SCIP role the official binary never sets — so a graph built from
   a patched binary (even without #504's `enclosing_range`) excludes it via
   the same `DEFINITION | FORWARD_DEFINITION` filter `build_graph` already
   applies for #504 graphs, before either the call-site or reference-site
   heuristic runs. Verified end-to-end: on an identical fixture, a stock
   official binary fabricates the phantom caller described above; our patched
   binary doesn't. Not yet upstreamed; see `TODO.md`'s scip-clang section.

   The "keep the over-capture" call above therefore stands only for a graph
   built from the OFFICIAL upstream binary, which has no signal to detect the
   case — the corpus this file's measurements were taken against. Both a
   #504 graph and a patched (forward-definition) graph have a signal and drop
   the edge instead.

    This asymmetry is why `no_incoming_calls` (definitions with zero incoming
    `calls` edges) refuses to answer on a stock graph: a phantom caller from a
    mis-attributed declaration site turns a real 0 into a false 1 — exactly the
    answer that tool exists to get right. It gates on the store's
    enclosing-range data (`has_enclosing_ranges`, the same signal as
    `line_span`) and reports unavailability with the reason instead.
    `explain_symbol`'s caller count applies the same signal as a caveat rather
    than a refusal — the count is always reported, but a `0` on a stock graph
    carries `zero_callers_reliable: false` + the reason (the stock fallback
    also *drops* a call site that precedes every callable definition in its
    document, so a reported 0 can be a false negative); a nonzero count needs
    no caveat, over-capture being the safe direction above.

This is where semantic identity still pays off even with the fallback: the
callee is the *exact* symbol, so the two `makeResumeToken` never mix,
independent of how the caller is attributed.

## Store

The graph is an **interned SQLite** store. The design principle: **keep the hot
topology (who-calls-who) raw, all-integer and indexed; keep the cold payload
(symbol strings, paths) separate, materialized only for results actually shown.**
The move that does the work is **symbol interning** — each distinct symbol gets an
integer id, edges reference ids, not the 127-char symbol strings. This is what
keeps size and memory bounded *and* makes queries fast (integer joins beat string
ops); `callers_of` runs off a B-tree index without loading the whole graph.

Scale on a large index (6004 TUs → 643,967 nodes / 2,735,021 edges): the store is
**~338 MB**, and `callers_of` answers in **~0.08 ms**. Symbol strings are the bulk
of the raw data (avg 127 chars, referenced ~8.5× across the edge set), which is
exactly why interning them out is the decisive win.

Payload is stored as **plain TEXT, no compression codec**: `find` needs a `LIKE`
substring match on `symbol`/`display_name`, which SQL can't do on a compressed
blob without a separate FTS index. (A symbol-column codec like zstd could shrink
further but adds a non-stdlib dependency and a per-row decode at display time —
not worth it at this size.)

Store schema (stdlib `sqlite3`):

```sql
-- COLD: payload, read only to materialize the results shown
files(id INTEGER PRIMARY KEY, path TEXT)
-- end_line: the definition's enclosing_range body extent (#504 binaries only,
-- NULL on stock); flagged has_enclosing_ranges in meta. Powers line_span.
-- documentation: the symbol's genuine doc-comment text (SymbolInformation.documentation,
-- placeholder/auto-generated text filtered at build time); NULL when none. Powers explain.
symbols(id INTEGER PRIMARY KEY, symbol TEXT, display_name TEXT, file_id INT, line INT, end_line INT, documentation TEXT)
CREATE INDEX ix_sym ON symbols(symbol);   -- keeps `find`'s LIKE substring search
-- HOT: topology walked by traversal — indexed, all-integer, never compressed
edges(kind TEXT, src_id INT, dst_id INT, file_id INT, line INT)
CREATE INDEX ix_src ON edges(src_id);     -- callees_of via B-tree, no full load
CREATE INDEX ix_dst ON edges(dst_id);     -- callers_of via B-tree, no full load
-- PROVENANCE: what was indexed (self-describing after the .scip is discarded)
meta(key TEXT PRIMARY KEY, value TEXT)    -- source_commit, project_root, ...
-- schema_version: on-disk format version, so a future migration can branch;
-- GraphStore refuses to open a store newer than the running binary.
```

### Provenance: recording *what* was indexed

The `meta` table (key/value) records the build's provenance so the store is
self-describing without the `.scip`: `project_root` and the indexing tool +
version (copied from SCIP `Metadata`), `built_at`, `node_count`/`edge_count`,
`cppgraph_version`, and — the one thing SCIP doesn't carry — the **source
commit** (`source_commit` + `source_dirty`). It's captured best-effort:
the index pipeline reads `git rev-parse HEAD` at *index* time (the accurate moment —
the state scip-clang actually reads) and passes it to `build --source-commit`;
absent that, `build` auto-detects via git on `project_root`, which is exact
when index→build run back-to-back. Non-git projects simply record no commit
(the tool stays general, `git`-optional). This commit is the **anchor for
incremental updates** — see below.

The `meta` table also carries a **`schema_version`** (currently 5): the on-disk
*format* version, distinct from `cppgraph_version` (the code that wrote it).
It's the enabler for format migrations — a future schema change bumps it, and
migration code can branch on the stored value. `GraphStore` refuses to open a
store whose `schema_version` is *newer* than the running binary supports (an old
binary silently misreading a new format would give wrong answers); an
unversioned store predates this and is read as legacy. `status` surfaces it
alongside `built_at` and the indexing tool/version, so "when/how was this
indexed?" is answerable without the `.scip`.

Trade-off: the artifact is a binary SQLite file, not a greppable/`jq`-able text
blob, and the write path + incremental rebuild (see below) are row management
(delete by `file_id`, keep the symbol table consistent).

The `.scip` input (797 MB) is read once at build, disposable, gitignored —
left uncompressed; gzipping it saves nothing measurable on the build path.

## Keeping the graph up to date

A large project's source changes continuously; re-running a full index + full
rebuild on every edit won't scale. Two update paths, kept in mind while
designing the builder so this isn't a later rewrite:

1. **Re-indexing** is already naturally incremental at the `compile_commands.json`
   level: only the changed TUs need to go through `scip-clang` again (a filtered
   compdb subset), producing a partial `.scip` — no need to touch
   `compile_commands.json` itself unless the *build structure* changed (new
   files/targets/includes; see `INSTALL.md`/`AGENTS.md`). *Which* files changed
   comes for free from the stored `source_commit`: `git diff --name-only
   <meta.source_commit>..HEAD` is the exact changed-file set, no mtime/hash
   guessing — this is why the commit is recorded in `meta` (see § Store). A
   dirty stored commit (`source_dirty`) means the diff base is approximate, so
   `update` should fall back to a full rebuild or warn.
2. **Merging + graph rebuild** is where the design choice matters. The graph
   builder is deliberately kept **document-local**: caller attribution only ever
   looks at occurrences within the *same* `Document` (nearest preceding
   callable-symbol definition by line), so every `calls`/`implements` edge's
   `Edge.file` is exactly the one document that produced it. This means a change
   to file A can only ever invalidate edges where `e.file == A` — an incremental
   rebuild doesn't need cross-file analysis.

   **Implemented** as `cppgraph update` (`store.update_store` /
   `GraphStore.apply_update`): rather than merging protobuf `Index`es and
   re-writing the whole store, it mutates the store in place. The set of changed
   files comes from the partial `.scip`'s Documents (authoritative — a file
   re-indexed to *zero* edges still gets its stale edges cleared, which deriving
   the set from the rebuilt graph would miss), plus any `--deleted` paths. In
   one transaction: delete each changed file's edges, clear the definition site
   of symbols defined there, re-insert the partial graph's nodes/edges in bulk
   (`_bulk_intern` resolves/assigns integer ids in a handful of `executemany`s,
   not a per-row probe), then GC symbols left orphaned — undefined *and*
   unreferenced — so `find` never surfaces a symbol that no longer exists.
   SQLite maintains `ix_sym`/`ix_src`/`ix_dst` incrementally, so no index
   rebuild. Measured: a 519-TU partial (3833 documents) applied to the full
   store in ~10 s, over-capture preserved.

   Caveat: the partial must be produced the *same way* as the base (same compdb
   scope/tree). A header's occurrences are source-line-local, so re-indexing
   only the changed TUs that include an unchanged header reproduces that
   header's edges identically; but feeding a differently-scoped index (e.g. a
   subsystem-only run against a full-repo store) will legitimately rewrite
   shared-header edges to that narrower index's view.

## Serving

- CLI: `build`, `update` (incremental), `find`, `callers`, `callees`, `path`,
  `bases` / `subtypes` (direct inheritance neighbours of a type), `impact`
  (reverse blast-radius; `--kind calls` = transitive callers, `--kind inherits`
  = all transitive subclasses), `reachable-from` (forward reachability from an
  entry point — a **lower bound** per the corollary below: "at least these are
  reachable", never a set to act on by exclusion; `--kind inherits` = the
  transitive base hierarchy above a derived type),   `references` (exact use sites, present unless the
  graph was built `--no-references`; `--root` for snippets, else coordinates), `explain`
  (definition + neighbors + the symbol's doc comment when the index carries
  one — no `--root` needed for that, unlike the source-read signature/snippet;
  pass `--root` to also get a source snippet, omit it
  for coordinates only), `line_span` (definitions ranked by body extent,
  #504-gated), `no_incoming_calls` (zero-caller definitions — a fact, never a
  "dead" verdict; #504-gated), `global_init_references <symbol>` (the globals a
  global's initializer references — the static-init-order fact, never a verdict;
  gated on `has_attributed_refs`), `boundary-violations` (declared-layering
  conformance — rules supplied per query as `--rule FROM:FORBIDDEN`; every
  hit is a real edge, zero false positives), `api-surface <prefix>` (the
  actually-used external surface of a module — definitions inside the prefix
  called/referenced from outside it; `external_calls`/`external_refs` as
  separate counters ranked by their sum, calls-only with a note on a
  `--no-references` store), `outline <file>` (every symbol
  defined in a file, sorted by line — the file's contents as a compact symbol
   list, replacing a `Read`), `class-members <symbol>` (members declared on a
   class/struct — methods, fields, nested types — by SCIP container nesting:
   a member's symbol string starts with its class's, so the boundary is exact
   and needs no #504), `strongly-connected-components` (the SCCs of the
   `calls` subgraph with >1 member — maximal sets of symbols that can all
   reach each other, the exact primitive behind "circular dependencies"; a
    graph fact, never a verdict, since mutual recursion is often legitimate),
    `dependency-cost` (call-site count against a target library — "if I
    replace/remove X, how many call sites change?", the `hotspots` fan-in
    ranking in its asymmetric-filter mode: callees pinned to `--target-path`
    prefixes, `--include-path`/`--exclude-path` filtering the *caller* side
    only, plus the summed total call sites over the per-symbol breakdown),
    `status` (source commit + drift check).
- MCP server (`cppgraph-mcp`, `src/cppgraph/mcp_server.py`): exposes the same
  queries to an LLM, token-budgeted. FastMCP over stdio; the graph store is
  fixed at launch (`--graph <db>`, optional `--root <checkout>`) so tools never
  take — and the LLM never has to guess or repeat — a filesystem path. Tools:
    `find`, `who_calls`, `what_it_calls`, `base_classes`, `subclasses`,
    `find_references`, `path`, `impact_of` (`kind` = calls|inherits|typed-by),
    `reachable_from` (the forward mirror — everything an entry point
    transitively reaches, worded as a lower bound per the corollary below),
    `hotspots`
   (global fan-in/fan-out/edge-count ranking), `dependency_cost`
   (call-site count against a target library — "if I replace X, how many
   call sites change?", the same ranking in its asymmetric `target_paths`
   mode: callees pinned to the prefix, path filters on the caller side only,
   `total_call_sites` + per-symbol breakdown), `stats` (per-file/per-directory
   aggregate counts: symbols, call edges, refs — a module "how big/dense" view),
    `line_span` (definitions ranked by body extent) and `no_incoming_calls`
    (zero-incoming-calls definitions — a fact, never a "dead" verdict), both
    gated on the store's enclosing-range data (#504) and reporting
    unavailability with the reason on a stock-binary graph,
    `global_init_references` (the globals a global's initializer references —
    the static-init-order fact, never a verdict; gated on `has_attributed_refs`
    and reporting unavailability with the rebuild pointer on anything less),
    `boundary_violations` (declared-layering conformance — rules supplied per
    query by the caller, the graph stores no intended architecture; every hit
    is a real edge, zero false positives, and 0 hits a lower bound, never a
     conformance verdict), `api_surface` (the actually-used external surface
    of a module — definitions under a prefix called/referenced from outside
    it, separate `external_calls`/`external_refs` counters ranked by their
    sum; calls-only with a note on a `--no-references` store), `outline`
    (every symbol defined in one file, sorted
    by line — a compact symbol list that replaces reading the file),
    `class_members` (every member declared on a class/struct, by the same
    SCIP container nesting — members that *exist*, a fact, since SCIP encodes
    no visibility) and `strongly_connected_components` (the SCCs of the
    `calls` subgraph with >1 member — maximal sets of symbols that can all
    reach each other, the exact primitive behind "circular dependencies"; a
    graph fact, never a verdict, since mutual recursion is often legitimate),
    `explain_symbol`, `status`, `visualize`. Each symbol-taking tool accepts a plain
  name as well as an exact SCIP string, through the shared `GraphStore.resolve`
  (also behind the CLI): a unique name resolves, `Class::method` maps to
  `Class#method`, an ambiguous name returns candidates, and no symbol is guessed.
  The same step also accepts a `file:line` (1-indexed, editor convention —
  the store keeps 0-indexed lines; the trailing `:<positive integer>` shape
  belongs to neither a SCIP symbol string nor a C++ name, so recognizing it
  first is unambiguous): the file is matched exactly against the recorded
  path (`outline`'s convention, backslashes normalized), the line first
  against definition start lines exactly, then — gated on
  `has_enclosing_ranges` (#504) — against the innermost (narrowest-span)
  `[line, end_line]` extent containing it, so a line inside a function body
  resolves to that function and not its enclosing class. Multiple symbols on
  the exact line (or tying on the narrowest span) are ambiguous candidates;
  a body line on a stock store is no-match, never a guess — the same
  three-outcome contract as names, so no caller had to change.
  On connect, the `initialize` `instructions` steer the model to these tools for
  in-scope code and to plain read/grep elsewhere, carrying the graph's scope and a
  `status` freshness pointer. The intended loop: `status` tells the LLM whether the graph is
  current (else an incremental update); then `impact_of`/`who_calls`/`path`/
  `explain_symbol` answer "what does this change affect?" with compiler-exact
  edges — no grep guessing, no loading files into context.
  - *Token budgeting*: every fan-out reply is capped (`DEFAULT_LIMIT=40`,
    `EXPLAIN_LIMIT=10`) and carries an explicit `total` + `truncated`, so a hub
    symbol with hundreds of callers never floods the model's context; the caller
    can raise the cap per query and always sees the true count.
  - *Cheap by default*: `explain_symbol` returns coordinates (`file:line`) only
    unless `include_source=True` **and** the server was launched with `--root`.
    The premise is that an LLM calling these tools already has file access, so
    coordinates suffice and cost far fewer tokens than pasted source.
  - *Compact identity*: the raw SCIP string is 150-250 chars of machine noise per
    hit. `scip-clang` leaves `SymbolInformation.display_name` empty (0% on the
    MongoDB index), so `cppgraph.filters.short_label` derives a readable name
    *from the SCIP string* (strips the scheme prefix, anonymous-namespace file
    path, overload hash, back-ticks) — still a substring `find` can re-resolve.
    Fan-out tools emit that label by default and the raw string only with
    `full_symbols=True`. Test callers/uses are dropped by default
    (`exclude_tests`, filtered on the far-end node's definition file, catching
    `~..._Test` teardown sites too). A `Node.file`-prefix predicate
    (`include_paths`/`exclude_paths`, simple prefix match) scopes a query to
      "my code, not vendored deps" the same way, on `find`/`who_calls`/
      `what_it_calls`/`find_references`/`impact_of`/`hotspots`/`stats`/
      `line_span`/`no_incoming_calls`/`strongly_connected_components`
      (where the filter drops whole components from the output rather than
      their members — see the CHANGELOG entry for the compiled-binary
      reasoning). `boundary_violations` matches its
     layering-rule prefixes against each endpoint's definition file with the
       same segment-boundary semantics. `dependency_cost` is the one
       **asymmetric** use of the primitive: its `target_paths` (CLI
       `--target-path`) pins the *callee* side to the target prefix while
       `include_paths`/`exclude_paths` apply to the *caller* side only —
       a mode of `hotspots`, whose symmetric both-endpoints semantics cannot
       express a cross-boundary count (see the CHANGELOG entry). These filter
    primitives live in `cppgraph.filters` and drive **both** surfaces — the MCP
    tools and the CLI query commands (`callers`/`callees`/`impact`, with
    `--limit`, `--exclude-tests`/`--no-exclude-tests`, `--hide-trivial`,
    `--full-symbols`, `--include-path`/`--exclude-path`) — so the same question
    gives the same answer whichever way it's asked.
    Measured effect on `who_calls` for a hub symbol: ~5.5× smaller payload
    (`scripts/measure_tokens.py`).
  - *Query quality*: `find` matches multiple words as an order-free AND (each
    must appear), groups overloads sharing a qualified name under one entry —
    each with a source-derived parameter `signature`, since scip-clang
    distinguishes them only by hash — and on a zero-hit query relaxes once
    (case/separator-insensitive, `changestream` ~ `change_stream`, then the bare
    leaf name), flagging the response `relaxed`. `what_it_calls`, `find`, and
    `explain_symbol` take an opt-in `hide_trivial` that drops ubiquitous helpers
    (operators, `*assert`, `makeStatus`, `source_location`, generated lambdas).
    And where a query would return a bare, misleading zero it returns a pointer
    instead: `impact_of` on a type (types have no call-graph callers) → a notice
    plus the reference-site count; an empty `base_classes`/`subclasses` → a
    `note`; a `path` with no static chain → a `hint` that the flow may cross
    runtime dispatch (virtual call / factory) rather than being unrelated.
  - *Update / rebuild advice*: `status` also reports a `tool` section —
    is a newer cppgraph published, and does adopting it (or the installed
    version) need a graph rebuild? The dangerous direction (a graph newer
    than the binary) is already a hard open-time error via `schema_version`; this
    covers the *benign-but-costly* direction, so an upgrade never silently blocks
    on minutes of re-indexing. The advice is level-aware: a per-release `rebuild`
    field (`none` / `store` / `reindex`) says which layer of the index stack a
    release invalidates — query-code only, a store rebuild from the existing
    `.scip`, or a full re-run of scip-clang — so a cheap store rebuild isn't
    mistaken for a full re-index. Source of truth is a hosted `versions.json`
    (deliberately tiny: `latest` + per-release `rebuild`; detail lives in the
    CHANGELOG, not the registry). Fetched best-effort with a 24h on-disk cache,
    fails soft offline; `cppgraph.updates.compute_advice` is the pure, tested core.
  - *Testable core*: the substance is a pure `(store, …) -> dict` layer
    (unit-tested without a transport); the FastMCP decorators are thin wiring
    over it. Errors (unknown symbol) come back as an `{"error": …}` dict
    pointing at `find`, not an exception.
- Export: optional graphify-compatible `graph.json` purely for visualization.

### Tools return facts, not judgments

Every tool returns a fact verifiable in the graph, never a verdict. `fan_in=42`
is a fact the LLM can trust and reason from directly; `is_dead=true` is a
judgment the LLM has to re-verify — vtable dispatch, exported API, template
instantiation elsewhere, entry points all break it — and that re-verification
costs the very tokens the tool exists to save. An unreliable answer is worse
than no tool: it turns one call into ten `who_calls` checks.

So the rule holds for names and outputs alike: state the *measured fact*, not
the *conclusion the reader might draw from it*. `line_span` (an exact body
extent), not `most_complex` (a proxy the reader may disagree with);
`no_incoming_calls` (a graph fact), not `dead_code` (a claim about the world).
The LLM draws the conclusion; the tool supplies the ground truth. This is what
makes a cppgraph answer cheaper than the manual investigation it replaces —
and it applies to every future tool, not just the ones shipped today.

**Corollary — an approximation must declare its direction, and never license an
omission.** "Fact, not judgment" is one axis; reachability adds a second. A
static call graph is an *under*-approximation of runtime reachability — it
misses virtual dispatch, function pointers, factories (which is why `path`
already hints when a flow may cross runtime dispatch). So a reachability result
is exact about what it *includes* and incomplete about the *total*, and that
incompleteness is safe in one direction and dangerous in the other. A tool whose
error over-reports (a dead-code *candidate* list — some listed symbols are
actually live) is safe: the reader verifies each addition. A tool whose error
under-reports is only safe when framed as a lower bound ("at least these are
reachable" — attack surface; `reachable_from` words every response this way),
never as a set the reader may act on by *removing*
the rest. Concretely: a static graph can say "these tests reach your change"
(a subset worth running), never "you may skip the others" (the skipped test may
reach the change through a virtual call the graph can't see). When a tool returns
an approximation it states its direction (lower/upper bound); it is never worded
so that an omission reads as a guarantee of absence.

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
This is what lets the same graph store be reused after moving the checkout, or
handed to a teammate with their own local clone, without rebuilding anything.

## Language choice

Python-first. The perf-critical work (C++ parsing) is already an external
compiled binary (scip-clang). Everything here is glue + graph traversal at modest
scale. Port hot paths to Rust only if a measurement demands it.

## Roadmap

See `CHANGELOG.md` for what's built and `TODO.md` for what's open.
