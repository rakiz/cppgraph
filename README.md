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

If someone pointed you (an AI coding agent) here with *"install this tool:
https://github.com/rakiz/cppgraph"*, follow these steps exactly. They are
deterministic; the scripts fail loudly with actionable messages.

There are two phases. **Phase A (machine setup)** is one-time and light — run it
end to end without stopping. **Phase B (index a project)** is per-project and has
the heavy steps — those need the user's sign-off.

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
>   fewer). So estimate by TU count **and** CPU, not TU count alone; `reindex.sh`
>   prints a per-machine range right before it starts.
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
> like `src/foo/`) so it finishes in a couple of minutes. **All of Phase A is
> light** (the one exception is compiling scip-clang on ARM-Linux, flagged
> below) — run it directly, don't pause between its steps.

### Phase A — set up the machine (once; light, run end to end)

1. **Check the platform.** A prebuilt `scip-clang` downloads on **macOS arm64**
   and **Linux x86_64** (light). On **ARM-Linux (aarch64)** there is no prebuilt
   binary — two routes, pick deliberately (this is the *one* Phase-A step that can
   be heavy, so apply the RULE for the build route):
   - **Compile scip-clang natively once** (`setup.sh --scip-source build`, PR
     #504, ~30 min on 8 cores, Docker) → then index **natively**, at normal speed.
     Recommended real workflow (also unlocks symbol-granularity usage).
   - **Emulated x86_64 container** (Docker/Podman + amd64 emulation): no build,
     but indexing runs *emulated* and is **much slower** (can be hours on a large
     codebase) — fine only for a quick try or a small subtree. See
     [INSTALL.md](INSTALL.md) "ARM-Linux / Windows: index via a container".

   On **Windows**, do everything inside **WSL2 (Ubuntu)**. On an **Intel Mac**
   indexing is not supported — stop and tell the user (they can still use a graph
   built elsewhere).
2. **Clone and set up** (needs [`uv`](https://docs.astral.sh/uv/) and `curl`).
   Clone into the per-machine tool dir — the same `~/.local/share/cppgraph/`
   where `setup.sh` installs the `scip-clang` binary — so the whole tool lives in
   one stable, persistent place. Use this exact path (do **not** clone into the
   user's project, and don't leave it wherever you happen to be):
   ```bash
   git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
   cd "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
   scripts/setup.sh              # venv + deps + scip-clang
   ```
3. **Register the MCP server** — once per machine, part of setup (idempotent; no
   project args, it auto-discovers each project's `.cppgraph/` at launch, so run
   it now, before any project is indexed):
   ```bash
   scripts/register-mcp.sh
   ```

**Then don't stop — carry on to Phase B.** After Phase A the tool is *installed*
but can't answer anything yet: it has no graph. Do not leave the user here. In
one message: (a) confirm setup is done, (b) **remind them what cppgraph is for
and why they installed it** (they may have waited through a long build and lost
the thread), and (c) propose Phase B for a specific project — then apply the RULE
for its heavy steps.

### Phase B — index a project (per-project; heavy, needs sign-off)

4. **Get a `compile_commands.json`.** Ask the user where theirs is. If they don't
   have one, it must be generated — and generating it may run a **full build**
   (long/heavy). Apply the RULE above: propose the right command, get the OK, or
   let them run it. How to produce one per build system (CMake / Bazel / Make):
   [AGENTS.md](AGENTS.md) → "The compilation database".
   **Then summarize what's indexable and let the user choose the scope — don't
   pick for them.** Run:
   ```bash
   cppgraph compdb-summary <compile_commands.json>
   ```
   and show the user the breakdown it prints (total TUs, the subtrees they live
   in, how many are tests). Then ask them to decide, before you run anything:
   - the **source root**;
   - the **subtree filter** (`reindex.sh`'s 2nd arg, a path substring) — the whole
     thing, or a subtree like `src/`, excluding vendored/third-party trees;
   - whether to **exclude tests** (`reindex.sh --no-tests`) for a lighter,
     production-only graph.

   State what your suggested filter would keep and leave out (use
   `compdb-summary --filter <substr>` to preview the count), and get their OK.
5. **Build the graph** — **heavy: apply the RULE above** (one-time; minutes to
   tens of minutes, *not hours*). Present this exact command, with a realistic
   estimate, and let the user choose to run it or have you run it. Prefer a
   `<filter>` (e.g. `src/foo/`) on a first run so it finishes fast. It writes
   into the target's gitignored `.cppgraph/`:
   ```bash
   scripts/reindex.sh <compile_commands.json> <filter> myproject
   ```
   **If (and only if) the installed scip-clang is a #504 build** (check `cppgraph
   status` → `usage_view`, or the binary's provenance sidecar), also ask the user
   which usage granularity they want, and pass the flag accordingly:
   - **light (default)** — file granularity ("used somewhere in these files"),
     smaller store;
   - **extended** — `scripts/reindex.sh --attributed-refs …`, symbol granularity
     ("used by *these functions*"), larger store.

   Not sure, or they want to decide later? Run the default light build — it keeps
   the `.scip`, so you can upgrade **without re-indexing**:
   `cppgraph enrich-refs --graph <…>.graph.db --scip <…>.scip`. With a stock
   binary the flag is a no-op, so don't offer the choice there.

   > **Warn before enabling attribution.** Both `--attributed-refs` and
   > `enrich-refs` re-parse the whole `.scip` and rebuild the graph in memory —
   > this costs roughly **a store build**, not the ~1 min SQLite write (mongo:
   > ~3.5 min and ~9 GB RAM). Tell the user the expected cost before kicking it
   > off, rather than starting a long job silently.
6. **Tell the user to open a new Claude Code session _from their project
   directory_** (that's how the server finds this project's graph), then ask
   *"what calls X?"*, *"impact of changing Y?"*, *"show the dependency graph of Z"*.

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
  (`m6g.2xlarge` — older Neoverse-N1 cores are slow at this). `reindex.sh` prints
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
