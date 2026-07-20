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

There are two phases. **Phase A (machine setup)** is one-time: you clone the repo,
the user runs `setup.sh` in their terminal. **Phase B (index a project)** is
per-project and has the heavy steps — the user runs `index.sh` and answers the
prompts. Your role is to clone, hand off the exact command, and give a realistic
time estimate before any heavy step.

> **RULE — heavy steps need explicit sign-off (do not skip).** The expensive
> steps that use significant CPU are: generating a `compile_commands.json`,
> **building the graph / indexing**, and — only on the ARM-Linux build path —
> **compiling scip-clang from source** (`--scip-source build`, PR #504,
> ~30 min on 8 cores, longer on fewer). **Give a realistic estimate — do not
> overstate, and do not mislabel which step is the long one.**
>
> - **Indexing** is the routine heavy step and usually the **longest** for a real
>   codebase — **minutes to hours**, CPU-bound. Reference points: ~20 min for
>   ~6 000 TUs on a fast 14-core x86; but **~4 h for 6 482 TUs on an 8-core AWS
>   Graviton2** (older ARM cores are several times slower *per core*, not just
>   fewer). So estimate by TU count **and** CPU, not TU count alone; the index
>   wizard prints a per-machine range right before it starts.
> - **The scip-clang binary** is **light when downloaded** (a prebuilt binary,
>   seconds) — the default on macOS arm64 / Linux x86_64. It is heavy **only**
>   when compiled from source (ARM-Linux, or anyone wanting #504). Don't call the
>   binary "the heavy step" on the download path.
>
> Before running any heavy step, in **one message** you **MUST**:
> 1. say what it does and a realistic time estimate (per above);
> 2. show the **exact command, verbatim** (so they can run it themselves);
> 3. ask the user to pick: **"I run it for you"** or **"you run it yourself"**.
>
> Then stop and wait — do not start until they choose, and never launch it in
> the background. Tip: for a first try, suggest scoping to one subtree (a filter
> like `src/foo/`) so it finishes in a couple of minutes. The wizard runs in the
> user's terminal, so they answer the prompts and see the progress themselves.

### Phase A — set up the machine (once)

**Your job is the strict minimum: clone the repo, then hand off.** Clone into the
per-machine tool dir (**always this path** — not the user's project, and not a dev
checkout; even "install from a local copy" means clone that copy *into* this path):

```bash
git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
```
Then tell the user to run **exactly this, verbatim** (the `!` prefix runs it in
their session so they answer the prompts):

```
! ~/.local/share/cppgraph/repo/scripts/setup.sh
```

`scripts/setup.sh` is the **sole** entry point — it creates the venv first. Never
tell the user to run `cppgraph setup` directly, or `.venv/bin/cppgraph …`, or
anything under a dev checkout: on a fresh machine none of that exists until
`setup.sh` has run. Don't run it yourself either (it's interactive).

`setup.sh` needs [`uv`](https://docs.astral.sh/uv/). It creates the venv + deps,
then runs the interactive `cppgraph setup`:

- **Obtain scip-clang** — the user picks the source from a menu, each with its cost:
  **download** (prebuilt, no #504, ~1 min — macOS arm64 / Linux x86_64), **build**
  (#504 natively, ~30–60 min, Docker, Linux only — unlocks symbol-granularity
  usage), or **emulate** (no host binary; indexing later runs in an x86 container,
  much slower). An "abort" choice stops without installing. On **Windows**, work
  inside **WSL2**; on an **Intel Mac**, only *emulate* applies.
- **Register the MCP server** (once per machine; idempotent, project-aware).
- **Hand off to the project index wizard** for the current project (Phase B).

Every stage checks what already exists and asks before (re)doing it — a self-built
#504 binary or a multi-hour `.scip` is never clobbered silently.

### Phase B — index a project (per-project; the heavy step)

`setup.sh` runs this automatically the first time; afterwards, to index another
project (or refresh a stale one), the user runs it directly:

```
! ~/.local/share/cppgraph/repo/scripts/index.sh
```

The wizard auto-locates the project's `compile_commands.json` (root / `build/` / up
the tree; if it reports none, generate one — see [AGENTS.md](AGENTS.md) →
"Fallback" — after the user's OK, since it may run a full build), shows the
breakdown, asks the scope questions as selectable menus (subtree / tests /
attribution), and — when a `.scip` or `.graph.db` already exists — shows its
details and asks reuse-vs-recompute. It records the chosen scope in the graph and
reuses it on incremental updates. **You hand the user the command; they answer the
prompts.** Then tell them to **open a new Claude Code session _from their project
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
