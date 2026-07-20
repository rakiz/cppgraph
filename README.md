# cppgraph

A **semantically accurate** code-graph tool for C++ (and any project with a
`compile_commands.json`). It builds a call/reference/inheritance graph from a
compiler-grade index — not from a syntactic AST — so symbol identity is exact
and edges are disambiguated (no name collisions, resolves overloads, virtual
dispatch, and pointer/reference method calls).

> **Why this exists.** Tree-sitter-based graph tools identify symbols *by name*.
> That both **over-captures** (merges distinct symbols that share a name into one
> node) and **under-captures** (drops calls it cannot bind syntactically:
> overloads, `ptr->method()`, free functions, templates, virtual dispatch).
> `cppgraph` uses a compiler front-end index (SCIP via `scip-clang`) where every
> symbol has a stable, unique identity (USR / mangled name), so the graph is the
> one the compiler actually sees.

## Install with your AI agent

**To install, tell your agent:** *"Follow instructions to install
https://github.com/rakiz/cppgraph"* — that points it at this section.

If you are that agent, follow these steps exactly. They are deterministic; the
scripts fail loudly with actionable messages.

Two phases. **Phase A (machine setup):** clone, then run setup with the scip-clang
source the user chose. **Phase B (index a project):** run the index with the scope
the user chose. **You gather each choice by asking the user (in your own question
UI), then run the script non-interactively with those choices as flags.**

> **RULE — `!` commands can't be answered, so you ask, then pass flags.** A Claude
> Code `! …` run has no interactive stdin: a prompt inside it gets EOF. So the
> interactive menus are for a human in a real terminal, **not** for you. Your loop
> is: (1) **ask the user** every choice via your question UI — the scip-clang source
> (Phase A) and the index scope (Phase B) — surfacing the real options; (2) run the
> script **non-interactively** with those choices as flags. Decide nothing yourself;
> the answers are the user's. Give a realistic **time estimate** with each heavy
> step: indexing is the long one (**minutes to hours**, CPU-bound — ~20 min for
> ~6 000 TUs on a fast 14-core x86, **~4 h for 6 482 TUs on an 8-core Graviton2**);
> downloading scip-clang is seconds, compiling it (#504) is ~30–60 min.

### Phase A — set up the machine (once)

1. **Clone** into the per-machine tool dir (**always this path** — not the user's
   project, not a dev checkout; even "install from a local copy" means clone that
   copy *into* this path):
   ```bash
   git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
   ```
2. **Ask the user how to obtain scip-clang** — present the sources valid on their
   platform, each with its cost: **download** (prebuilt, ~1 min — macOS arm64 /
   Linux x86_64 only), **build** (#504 natively, ~30–60 min, Docker, Linux only —
   unlocks symbol-granularity usage), **emulate** (no host binary; indexing later
   runs in an x86 container, much slower). Windows → WSL2; Intel Mac → only emulate.
3. **Run it with their choice** (`!` runs it non-interactively; `--scip-source` is
   what makes that work — without it a piped run stops with `ACTION NEEDED`):
   ```
   ! ~/.local/share/cppgraph/repo/scripts/setup.sh --scip-source <download|build|emulate>
   ```
   `setup.sh` is the **sole** entry point — it creates the venv, obtains scip-clang,
   registers the MCP server, and (in a real terminal) offers to index the current
   project. Never tell the user to run `cppgraph setup` / `.venv/bin/cppgraph …` /
   anything under a dev checkout: none of that exists on a fresh machine until
   `setup.sh` has run.

### Phase B — index a project (per-project; the heavy step)

1. **Get the scope options:** `cppgraph index --plan-json` (from the project dir; it
   auto-locates the `compile_commands.json` — if it reports none, generate one, see
   [AGENTS.md](AGENTS.md) → "Fallback", after the user's OK). It returns the compdb
   breakdown, the questions (subtree / tests / attribution), and `artifacts` (whether
   a `.scip`/`.graph.db` already exists).
2. **Ask the user** each question, surfacing the real options. If `artifacts` shows
   the project is already indexed, ask whether to **keep** it (default) or rebuild.
3. **Run it with their answers** (non-interactive, so `!` works):
   ```
   ! ~/.local/share/cppgraph/repo/scripts/index.sh <compdb> -y --filter <sub> [--no-tests] [--attributed-refs] --run
   ```
   **Non-destructive by default:** an existing `.scip`/`.graph.db` is kept, not
   overwritten — only what's missing is built. Add `--from-scratch` only if the user
   asked to rebuild. The chosen scope is recorded and reused by later updates. (A human in a
   real terminal can instead run `scripts/index.sh` with no flags for the interactive
   wizard.) Then tell the user to **open a new Claude Code session _from their project
   directory_** (that's how the server finds this project's graph) and ask *"what
   calls X?"*, *"impact of changing Y?"*, *"show the dependency graph of Z"*.

Humans: the same flow, step by step, is in [QUICKSTART.md](QUICKSTART.md).

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
| 1 | **Get `scip-clang`** — copy the prebuilt binary, or compile from source | the per-machine indexer binary | **~1 min** (copy) … **~30–45 min** (compile) |
| 2 | **Get `compile_commands.json`** — from the target's build system, if not already present | `compile_commands.json` | **~3 min** (warm) … **~15 min** (cold) — the *target's* build, not cppgraph |
| 3 | **Filter what to index** — scope to a subtree, optionally drop tests | `<name>.compdb.json` | **seconds** |
| 4 | **Index** — `scip-clang` runs the C++ front-end per TU → SCIP | `<name>.scip` | **the big one: ~20 min → several hours** |
| 5 | **Build the store** — parse SCIP, intern symbols + edges into SQLite | `<name>.graph.db` | **~1 min** |

Then you're ready to query (see the gains below).

Notes:
- **Stage 1 is once per machine**; copy is only available on macOS arm64 /
  Linux x86_64 (a stock binary, no PR #504). Everywhere else — and to get PR #504
  (`enclosing_range`) — you compile (`docker/build-scip-clang/`).
- **Stage 4 is the variable one.** Measured extremes: ~20 min on a fast 14-core
  x86 for ~6 000 TUs; **~4 h for 6 482 TUs on an 8-core AWS Graviton2**
  (`m6g.2xlarge` — older Neoverse-N1 cores are slow at this). the index wizard prints
  a per-machine estimate right before it starts. Fewer/older cores → proportionally
  longer; scope to a subtree (stage 3) to cut it down.
- **`enclosing_range` (#504) is a choice at indexing**, with consequences: it
  enables *exact* caller attribution and the symbol-granularity usage view
  (`--attributed-refs`), but needs the compiled #504 binary and makes the `.scip`
  and store larger. Without it you still get an exact graph, just with
  file-granularity usage. You can add it later without re-indexing (`enrich-refs`)
  — but note `enrich-refs` re-parses the `.scip` and rebuilds the graph, so it
  costs about a **store build** (mongo: ~3.5 min, ~9 GB RAM), not stage 5's ~1 min.

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

Measured on MongoDB, *"who calls the method `makeResumeToken`?"*: grep ingests
~6,600 tokens and is wrong (4 symbols merged); cppgraph `find` + `who_calls`
ingests **~400** and is exact — **~16× fewer, and correct**. On a hub symbol the
gap widens to **~41×** (`ResumeToken::parse`). The trade-off is a one-time index
(minutes), amortized over every later query. Full numbers, noise ratios, and the
token-lean output defaults: **[COMPARISON.md](COMPARISON.md)** (reproduce with
`scripts/measure_tokens.py`).

## Documentation

| Doc | What's in it |
|---|---|
| [AGENTS.md](AGENTS.md) | Working instructions, principles, guardrails — read first |
| [DESIGN.md](DESIGN.md) | Architecture, edge model, the call-attribution heuristic + its known limitation |
| [COMPARISON.md](COMPARISON.md) | Measured comparison vs graphify and Serena on a real design question |
| [INSTALL.md](INSTALL.md) | Setting up a new machine (`scip-clang`, `protoc`, the venv) |
| [viz/README.md](viz/README.md) | The bundled graph viewer + `cppgraph export` graph.json format |
| [CHANGELOG.md](CHANGELOG.md) | What's been built so far |
| [TODO.md](TODO.md) | Open tasks |

## Non-goals

- Re-implementing a C++ parser. We consume a compiler index.
- Being a linter or a refactoring engine. This is about *understanding structure*
  (who calls what, impact/blast-radius, inheritance) for humans and LLMs.

## Visualize

`cppgraph export <symbol> --depth 2 --out graph.json` (run from the indexed
project — graph auto-discovered, `<symbol>` a plain name or exact SCIP string)
writes a bounded neighbourhood in a [graphify](https://github.com/Graphify-Labs/graphify)-compatible
`graph.json`. Open it in the bundled viewer (`viz/cppgraph-viz.html`, our own
code + a vendored copy of vis-network, fully offline) — or, since the container
format is shared, in graphify itself. Details: [viz/README.md](viz/README.md).

## Where is this type used?

The call graph can't answer it — a plain struct has no callers. cppgraph keeps an
exact **reference index** (on by default) so `cppgraph references <type>` (and the
`find_references` tool) lists every use site, and `export --mode usage` draws a
usage graph.

By default that graph is at **file** granularity ("used somewhere in these
files"). Built with a scip-clang that emits `enclosing_range` (a source build
carrying [PR #504](docker/build-scip-clang/)), you can upgrade it to **symbol**
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
