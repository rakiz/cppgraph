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

SCIP gives occurrences (symbol, range, roles). A call edge is derived by:
1. finding reference occurrences of a callee symbol,
2. attributing each to the **definition symbol whose range encloses** the
   occurrence's position → that enclosing symbol is the caller.

This is where semantic identity pays off: the callee is the *exact* symbol, so
the two `makeResumeToken` never mix.

## Store

Start simple: in-memory graph + a single-file store (SQLite via stdlib `sqlite3`,
or a compact JSON). Scale target is modest (MongoDB pipeline: ~17k nodes / ~43k
edges) — trivial for Python. Optimize only if measured.

## Serving

- CLI: `build`, `callers`, `callees`, `path`, `impact` (reverse blast-radius),
  `explain`.
- MCP server (later): expose the same queries to an LLM, token-budgeted.
- Export: optional graphify-compatible `graph.json` purely for visualization.

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
