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

**Phase 1 (shipped):** in-memory graph + a flat `graph.json` (`Graph.save_json`
in `model.py`). Every query does `load_json` → the entire file is parsed into
Python dicts in RAM before answering.

**Phase 2 decision — measured, not guessed.** Indexing all of `src/mongo`
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

**Decision: ship Phase 2 as interned SQLite, plain TEXT, no compression codec.**
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

Phase 2 target schema (stdlib `sqlite3`):

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

Cost accepted with this move (deliberately, over the flat-JSON simplicity):
the artifact is no longer greppable/`jq`-able or textually diffable, and the
write path + the document-local incremental rebuild (see below) become row
management (delete by `file_id`, keep the symbol table consistent) instead of a
one-line `json.dump`. Worth it for the memory and speed gains.

The `.scip` input (797 MB) is read once at build, disposable, gitignored —
left uncompressed; gzipping it saves nothing measurable on the build path.

## Keeping the graph up to date

MongoDB's source changes continuously; re-running a full index + full rebuild
on every edit won't scale once Phase 2 covers all of `src/mongo`. Two update
paths, kept in mind while designing the builder so this isn't a later rewrite:

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
