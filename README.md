# cppgraph

Compiler-exact C++ code intelligence for AI agents. It answers the questions your
assistant otherwise guesses at with grep — **who calls this function, what it
calls, who references this type, the class hierarchy, the change impact / blast
radius, the call path between two symbols, and where the busiest fan-in/fan-out
symbols in the whole project are** — exposed as **MCP tools your agent calls
directly** (`who_calls`, `what_it_calls`, `find_references`, `impact_of`, `path`,
`base_classes` / `subclasses`, `hotspots`), backed by a pre-indexed graph, plus a CLI.

Built from a **compiler front-end index** (SCIP via `scip-clang`), not a syntactic
AST: every symbol has a stable, unique identity (USR / mangled name), so edges are
exact — overloads, `ptr->method()`, free functions, templates, and virtual dispatch
all resolved, with no name-collision merges.

> **Why it matters for C++.** By-name graph tools (tree-sitter) and LSP-based
> agents (e.g. Serena) degrade on large C++ — templates, macros, overloads — so the
> agent silently **falls back to grep**, which merges distinct same-named symbols,
> counts comments/decls as calls, and misses `ptr->method()` / virtual dispatch.
> cppgraph is the graph the compiler actually sees — see
> [COMPARISON.md](COMPARISON.md) for measured numbers (grep-and-read vs cppgraph,
> and vs graphify / Serena).

## Install with your AI agent

**To install, tell your agent:** *"Follow instructions to install
https://github.com/rakiz/cppgraph"* — that points it at this section.

Two phases. **Phase A (machine setup, once):** clone the tool into its
per-machine data dir and run `scripts/setup.sh`, which creates the venv, obtains
the `scip-clang` indexer binary from a source menu, and registers the MCP
server. **Phase B (per project, the heavy step):** run the index wizard with the
scope the user chose — it locates the project's `compile_commands.json`, filters
to that scope, indexes, and builds the queryable graph into the project's
`.cppgraph/`.

**The agent gathers every choice by asking the user (in its own question UI),
then runs the scripts non-interactively with those choices as flags** — an
interactive run inside an agent `!` command gets EOF. The full agent protocol
(the interview shapes, the exact flags, the guardrails) is in
[AGENTS.md](AGENTS.md) → "Indexing a project" / "Per-machine tool install".

- Human, step by step: [QUICKSTART.md](QUICKSTART.md).
- Machine-setup mechanics (source menu, platform rules, manual fallbacks):
  [INSTALL.md](INSTALL.md) — the canonical install reference.
- Uninstalling: run `! ~/.local/share/cppgraph/repo/scripts/uninstall.sh` (it
  asks per item; it keeps each project's index data by default). Details:
  [INSTALL.md](INSTALL.md) → "Uninstalling".
- Every CLI subcommand, grouped by purpose with one example each:
  [CLI_REFERENCE.md](CLI_REFERENCE.md).

## Status

Early, but functional end-to-end: build, query, incremental update, an MCP
server for LLMs, and visualization. The tool is general — point it at any C++
project's `compile_commands.json`.

## Pipeline

```
scip-clang        compile_commands.json  →  <name>.compdb.json  →  <name>.scip  →  <name>.graph.db
(indexer binary)  (target's build)          (filtered subset)      (SCIP protobuf)  (SQLite: query/MCP/viz)
```

Five stages get you from nothing to a queryable graph. Only the last three are
cppgraph's; the timings are rough and **dominated by your machine** (the indexer
runs the C++ front-end once per translation unit, so it scales with cores/CPU).

| # | Stage | Produces | Rough time |
|---|-------|----------|------------|
| 1 | **Get `scip-clang`** — copy the prebuilt binary, or compile from source | the per-machine indexer binary | **~1 min** (copy) … **~25–60 min** (compile, #504; ~26 min measured on m7g) |
| 2 | **Get `compile_commands.json`** — from the target's build system, if not already present | `compile_commands.json` | **~3 min** (warm) … **~15 min** (cold) — the *target's* build, not cppgraph |
| 3 | **Filter what to index** — scope to a subtree, optionally drop tests | `<name>.compdb.json` | **seconds** |
| 4 | **Index** — `scip-clang` runs the C++ front-end per TU → SCIP | `<name>.scip` | **the big one: ~20 min → several hours** |
| 5 | **Build the store** — parse SCIP, intern symbols + edges into SQLite | `<name>.graph.db` | **~30 s** (filtered `src/mongo`) … **~3.5 min** (full repo + refs) |

Then you're ready to query (see the gains below).

Notes:
- **Stage 1 is once per machine.** **macOS arm64** and **Linux aarch64** get a
  native prebuilt **patched** binary (`download-patched`, ~1 min); **Linux x86_64** gets
  the native stock prebuilt (`download`, ~1 min, unpatched — #504 there means a
  local build). All three index **natively — no Docker**. Docker is only for the
  `build` source (a #504 compile, ~25–60 min, Linux-only) or for hosts with no
  native binary at all (Intel Mac, Windows) via `emulate`. "#504 build is
  Linux-only" ≠ "scip-clang needs Docker on macOS".
- **Stage 4 is the variable one.** Measured extremes: ~20 min on a fast 14-core
  x86 for ~6 000 TUs; **~4 h for 6 482 TUs on an 8-core AWS Graviton2**
  (`m6g.2xlarge` — older Neoverse-N1 cores are slow at this); **~1 h 42 for
  4 301 TUs on an 8-core Graviton3** (`m7g.2xlarge` — ~1.4 s/TU vs ~2.2 on
  Graviton2). the index wizard prints
  a per-machine estimate right before it starts. Fewer/older cores → proportionally
  longer; scope to a subtree (stage 3) to cut it down.
- **`enclosing_range` (#504) is a choice at indexing**, with consequences: it
  enables *exact* caller attribution and the symbol-granularity usage view
  (`--attributed-refs`), but needs the compiled #504 binary and makes the `.scip`
  and store larger. Without it you still get an exact graph, just with
  file-granularity usage. You can add it later without re-indexing (`enrich-refs`)
  — but note `enrich-refs` re-parses the `.scip` and rebuilds the graph *with*
  references, so it costs a **full store build** (mongo: ~3.5 min, ~9 GB RAM) —
  the top of stage 5's range, not its filtered ~30 s floor.

**Two components:** the **builder** (`scip-clang`, external compiled binary) does
the expensive, perf-critical C++ parsing, crash-isolated per TU; **cppgraph**
(this repo, Python) is the glue + graph — parses SCIP, builds the store, serves
queries, exposes the MCP server, and exports a graph.json for visualization.

## Does it actually beat by-name tools?

Yes, measurably. On a real case (two distinct methods sharing the name
`makeResumeToken`), a tree-sitter tool drops the real call edges *and* collapses
431 unrelated `Value` sites onto one node; cppgraph returns the correct,
separated caller sets. Full write-up with numbers and reproduction steps:
**[COMPARISON.md](COMPARISON.md)** (cppgraph vs graphify vs Serena/LSP, on a
large C++ codebase).

## Why not just grep?

Your AI assistant already answers "what calls X?" with `grep` — but on a large
C++ codebase that's **wrong** (matches by name: merges distinct symbols, includes
comments/decls, misses `ptr->method()` / virtual dispatch / templates) and
**token-expensive** (noisy output, then whole-file reads to disambiguate, all
through the model's context).

grep's *raw* dump is only a floor: it can't tell a call from a decl, a comment,
or a different same-named symbol, so to answer correctly it must **read around
every hit** — and that's where the cost explodes. Measured on MongoDB, *"who
calls the method `makeResumeToken`?"*: grep dumps a raw dump that is ~99 % noise
(4 symbols merged) and needs a full read-and-disambiguate pass to trust the 2
real call sites; cppgraph ingests a few hundred tokens, exact — orders of
magnitude leaner. On a rare, uniquely
named symbol (grep's best case) cppgraph still wins several-fold once grep reads
to verify; on a hot type like `OperationContext` reading every hit would take
thousands of times more than cppgraph — a theoretical number nobody ingests, so
in practice grep truncates and answers from a partial view, silently missing
call sites, where cppgraph stays exact and complete.
The trade-off is a one-time index (minutes), amortized over every later query.
Measured figures (token counts, noise ratios, multipliers), the full spectrum,
and the token-lean defaults: **[COMPARISON.md](COMPARISON.md)** (reproduce with
`scripts/measure_tokens.py --suite`).

Same idea versus an LSP-driven agent (e.g. Serena): the questions are the same,
but cppgraph is a pre-indexed graph the agent queries in one call, not a live
language server it has to drive file-by-file — which is what keeps it token-lean
and complete on large C++. Numbers: [COMPARISON.md](COMPARISON.md).

## Documentation

| Doc | What's in it |
|---|---|
| [AGENTS.md](AGENTS.md) | Working instructions, principles, guardrails — read first |
| [DESIGN.md](DESIGN.md) | Architecture, edge model, the call-attribution heuristic + its known limitation |
| [COMPARISON.md](COMPARISON.md) | Measured comparison vs graphify and Serena on a real design question |
| [INSTALL.md](INSTALL.md) | Setting up a new machine (`scip-clang`, `protoc`, the venv) |
| [QUICKSTART.md](QUICKSTART.md) | Human, step-by-step: install, index your first project, ask questions |
| [CLI_REFERENCE.md](CLI_REFERENCE.md) | Every CLI subcommand, grouped by purpose, with an example each |
| [CREDITS.md](CREDITS.md) | Credits & third-party dependencies |
| [viz/README.md](viz/README.md) | The bundled graph viewer + `cppgraph export` graph.json format |
| [CHANGELOG.md](CHANGELOG.md) | What's been built so far |
| [TODO.md](TODO.md) | Open tasks |

## Non-goals

- Re-implementing a C++ parser. We consume a compiler index.
- Being a linter or a refactoring engine. This is about *understanding structure*
  (who calls what, impact/blast-radius, inheritance) for humans and LLMs.

## Visualize a neighbourhood, call path, or cycle

Secondary to the query tools: `cppgraph export <symbol> --depth 2 --out graph.json`
(run from the indexed project — graph auto-discovered, `<symbol>` a plain name or
exact SCIP string), or the `visualize` MCP tool, writes a bounded neighbourhood and
opens it in a self-contained, offline viewer (`viz/cppgraph-viz.html`). The
`graph.json` is [graphify](https://github.com/Graphify-Labs/graphify)-compatible.
`export`/`view` (and the MCP `visualize`) also take `--mode path --dst <target>` to
draw how two symbols connect — the shortest `calls` chain by default,
`--expand-paths` for the full corridor of every route between them — or
`--mode cycle` to draw the multi-member call cycle containing a symbol.
`boundary-violations --out` (or the MCP `visualize_boundary_violations`) draws
the edges that cross your declared layering rules.
Details: [viz/README.md](viz/README.md).

## Where is this type used?

The call graph can't answer it — a plain struct has no callers. cppgraph keeps an
exact **reference index** (on by default) so `cppgraph references <type>` (and the
`find_references` tool) lists every use site, and `export --mode usage` draws a
usage graph.

By default that graph is at **file** granularity ("used somewhere in these
files"). Built with a scip-clang that emits `enclosing_range` (a source build
carrying [PR #504](docker/build-scip-clang-patched-linux/)), you can upgrade it to **symbol**
granularity — "used by *these functions*" — with `cppgraph build
--attributed-refs`, or add it to an existing graph with `cppgraph enrich-refs`.

> **Worth it when you want symbol-level usage — at a cost.** Attribution stores
> one extra symbol id per reference, so the graph grows. Enable it when "which
> functions use this type?" matters; otherwise the default file granularity is
> already exact and leaner. `cppgraph status` tells you which granularity a graph
> has and how to upgrade.

## License

[MIT](LICENSE). The bundled viewer vendors [vis-network](https://github.com/visjs/vis-network)
(MIT / Apache-2.0); see [viz/README.md](viz/README.md#third-party-notices).
