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

## Graph model

Nodes = symbols, identified by SCIP symbol string (stable across TUs).
Node attrs: display name, kind, defining file+range, namespace/enclosing.

Edges (implemented unless marked planned):
- `calls`      caller-symbol → callee-symbol (attributed via nearest preceding
               callable definition of a reference occurrence — see below)
- `inherits`   derived type → base type (from SCIP `is_implementation`
               relationships where both endpoints are types; queried by
               `bases`/`subtypes` and `impact --kind inherits`)
- `implements` override-method → overridden-method (the method→method
               `is_implementation` relationships; the class→class ones are
               `inherits`)
- `defines` / `contains`  file/namespace/class → member, structural (planned)

References are **not** edges. They are stored as an exact **location index**
(the "C" approach), on by default (opt out with `cppgraph build
--no-references`): every non-local, non-definition occurrence recorded as
`symbol → file:line`, with no
attribution to an enclosing symbol. Rationale: a reference edge's `src` ("who
references") would need the same nearest-preceding proxy as `calls`, but
references live disproportionately in *class bodies* (field/param/return types),
exactly where that proxy fails — so attributing them would concentrate the
known misattribution. Locations sidestep it entirely: the position is 100%
exact, and the `references` query returns coordinates or — with `--root` — the
snippet the tool reads itself (same dual mode as `explain`). This answers "where
is this type/symbol used?" — a dependency the call graph can't express (e.g.
`ResumeTokenData#`, a plain struct: 0 callers, 155 exact use sites across the
pipeline subsystem). Measured cost is modest — on a large index: 5.3M deduped
locations, store 323 MB → 468 MB (+45%), build ~40 s vs ~23 s — so it's on by
default; `--no-references` gives the leaner store when the index isn't wanted.

When scip-clang emits `enclosing_range` (PR #504), *attributed* reference
**edges** (symbol → symbol, traversable) become exact via a containment test —
worth adding then as an opt-in, type-only ("A") or all ("B"). Until then,
locations-only. See TODO.md.

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
2. A call edge's caller is the nearest preceding callable-symbol
   *definition* in the same document, by start line (no range containment
   available, so line order is the next best signal). Verified to correctly
   recover both real callers of `ChangeStreamEventTransformation::makeResumeToken`.
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
   *safe* direction (an extra caller, never a dropped real call).

   The clean fix is `enclosing_range` (attribute a reference to a definition
   iff it is contained in that definition's body range). scip-clang does not
   emit it (issue sourcegraph/scip-clang#323 was closed *not planned*), but
   PR sourcegraph/scip-clang#504 implements it. When #504 merges + releases,
   replace the nearest-preceding proxy with a range-containment test → exact
   attribution, zero collateral. Tracked, not scheduled — nothing depends on
   it today.

This is where semantic identity still pays off even with this fallback: the
callee is the *exact* symbol, so the two `makeResumeToken` never mix,
independent of how the caller is attributed.

## Store

**Phase 1 (shipped):** in-memory graph + a flat `graph.json` (`Graph.save_json`
in `model.py`). Every query does `load_json` → the entire file is parsed into
Python dicts in RAM before answering.

**Store decision — measured, not guessed.** Indexing a full large codebase
(6004 TUs) produced **643,967 nodes / 2,735,021 edges → a 1.19 GB `graph.json`**.
That settles JSON vs SQLite: flat JSON does not scale. The redundancy is the
**symbol strings** — they average **127 characters** and each is referenced
**~8.5×** in the edge set alone, because edges store `src`/`dst` as full symbol
strings (`model.py`). That is ~700 MB of repeated strings out of the 1.19 GB.
But the decisive cost isn't disk: `load_json` pulls the whole 1.19 GB into RAM
**per query** just to answer one `callers_of`.

### Storage architecture: hot topology raw, cold payload as-is

The guiding principle — and the maintainer's constraint: **shrink storage
meaningfully without hurting query performance**:

> **Keep the hot data raw and indexed; keep the cold payload separate (and
> compressible later if ever needed).**

- **Hot = the graph topology** (who-calls-who) that traversal walks millions of
  times. All-integer, indexed, never string-keyed on the hot path.
- **Cold = what the topology points to** (the 127-char symbol strings, file
  paths, display names) — materialized only for the handful of results actually
  shown.

The one move that does the work is **symbol interning (dictionary encoding)**:
assign each distinct symbol an integer id; edges reference ids, not 127-char
strings. This *both* shrinks storage *and speeds up queries* (integer
comparison/join beats string ops) — the opposite of blind whole-file
compression, which would kill perf by forcing a full decompress per query and is
explicitly rejected.

**Measured on the full graph** (prototype conversion of the real 1.19 GB
`graph.json`; throwaway script, not committed):

| Representation | Size | vs Phase 1 |
| --- | --- | --- |
| flat JSON, `indent=2` (Phase 1 today) | 1249 MB | — |
| compact JSON (drop `indent=2` alone) | 1085 MB | 1.2× |
| **SQLite, interned, plain TEXT** | **338 MB** | **3.7×** |

Query timing on that SQLite: `callers_of(makeResumeToken)` returns in
**0.08 ms** off the B-tree index — versus ~3.4 s just to `load_json` the JSON
into RAM today, per query, at 4–6 GB RSS.

**Decision: ship the store as interned SQLite, plain TEXT, no compression codec.**
3.7× smaller is enough, and the *major* wins are the bounded memory and the
per-query speed, not maximal shrinkage. Two things that made `indent=2` removal
and codec compression not worth it up front:

- Dropping `indent=2` alone is only 1.2× — the symbols dwarf the whitespace, so
  it's irrelevant next to interning.
- **zstd on the symbol column is deferred, not adopted.** It would push size
  toward ~8× but (a) adds a non-stdlib dependency, (b) costs a per-row decode at
  display time, and (c) **breaks `find`**: `Graph.find` (`model.py`) does a
  substring match on `symbol`/`display_name`, which SQL does with `LIKE` on
  plain TEXT but *cannot* do on a compressed blob without a separate full-text
  index or a plaintext copy. Only revisit if 338 MB ever becomes a real problem.

Store schema (stdlib `sqlite3`):

```sql
-- COLD: payload, read only to materialize the results shown
files(id INTEGER PRIMARY KEY, path TEXT)
symbols(id INTEGER PRIMARY KEY, symbol TEXT, display_name TEXT, file_id INT, line INT)
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
`reindex.sh` reads `git rev-parse HEAD` at *index* time (the accurate moment —
the state scip-clang actually reads) and passes it to `build --source-commit`;
absent that, `build` auto-detects via git on `project_root`, which is exact
when index→build run back-to-back. Non-git projects simply record no commit
(the tool stays general, `git`-optional). This commit is the **anchor for
incremental updates** — see below.

The `meta` table also carries a **`schema_version`** (currently 1): the on-disk
*format* version, distinct from `cppgraph_version` (the code that wrote it).
It's the enabler for format migrations — a future schema change bumps it, and
migration code can branch on the stored value. `GraphStore` refuses to open a
store whose `schema_version` is *newer* than the running binary supports (an old
binary silently misreading a new format would give wrong answers); an
unversioned store predates this and is read as legacy. `status` surfaces it
alongside `built_at` and the indexing tool/version, so "when/how was this
indexed?" is answerable without the `.scip`.

Cost accepted with this move (deliberately, over the flat-JSON simplicity):
the artifact is no longer greppable/`jq`-able or textually diffable, and the
write path + the document-local incremental rebuild (see below) become row
management (delete by `file_id`, keep the symbol table consistent) instead of a
one-line `json.dump`. Worth it for the memory and speed gains.

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
  = all transitive subclasses), `references` (exact use sites, present unless the
  graph was built `--no-references`; `--root` for snippets, else coordinates), `explain`
  (definition + neighbors; pass `--root` to also get a source snippet, omit it
  for coordinates only), `status` (source commit + drift check).
- MCP server (`cppgraph-mcp`, `src/cppgraph/mcp_server.py`): exposes the same
  queries to an LLM, token-budgeted. FastMCP over stdio; the graph store is
  fixed at launch (`--graph <db>`, optional `--root <checkout>`) so tools never
  take — and the LLM never has to guess or repeat — a filesystem path. Tools:
  `find`, `who_calls`, `what_it_calls`, `base_classes`, `subclasses`,
  `find_references`, `path`, `impact_of` (`kind` = calls|inherits),
  `explain_symbol`, `status`. The intended loop: `status` tells the LLM whether the graph is
  current (else `reindex.sh --update`); then `impact_of`/`who_calls`/`path`/
  `explain_symbol` answer "what does this change affect?" with compiler-exact
  edges — no grep guessing, no loading files into context.
  - *Token budgeting*: every fan-out reply is capped (`DEFAULT_LIMIT=25`,
    `EXPLAIN_LIMIT=10`) and carries an explicit `total` + `truncated`, so a hub
    symbol with hundreds of callers never floods the model's context; the caller
    can raise the cap per query and always sees the true count.
  - *Cheap by default*: `explain_symbol` returns coordinates (`file:line`) only
    unless `include_source=True` **and** the server was launched with `--root`.
    The premise is that an LLM calling these tools already has file access, so
    coordinates suffice and cost far fewer tokens than pasted source.
  - *Compact identity*: the raw SCIP string is 150-250 chars of machine noise per
    hit. `scip-clang` leaves `SymbolInformation.display_name` empty (0% on the
    MongoDB index), so `_short_label` derives a readable name *from the SCIP
    string* (strips the scheme prefix, anonymous-namespace file path, overload
    hash, back-ticks) — still a substring `find` can re-resolve. Fan-out tools
    emit that label by default and the raw string only with `full_symbols=True`.
    Test callers/uses are dropped by default (`exclude_tests`, filtered on the
    far-end node's definition file, catching `~..._Test` teardown sites too).
    Measured effect on `who_calls` for a hub symbol: ~5.5× smaller payload
    (`scripts/measure_tokens.py`).
  - *Testable core*: the substance is a pure `(store, …) -> dict` layer
    (unit-tested without a transport); the FastMCP decorators are thin wiring
    over it. Errors (unknown symbol) come back as an `{"error": …}` dict
    pointing at `find`, not an exception.
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
This is what lets the same graph store be reused after moving the checkout, or
handed to a teammate with their own local clone, without rebuilding anything.

## Language choice

Python-first. The perf-critical work (C++ parsing) is already an external
compiled binary (scip-clang). Everything here is glue + graph traversal at modest
scale. Port hot paths to Rust only if a measurement demands it.

## Roadmap

See `CHANGELOG.md` for what's built and `TODO.md` for what's open.
