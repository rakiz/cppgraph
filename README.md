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

> **RULE — heavy steps need explicit sign-off (do not skip).** Generating a
> `compile_commands.json` (step 3) and building the graph (step 4) are the only
> expensive steps; they use significant CPU. **Give a realistic estimate — do
> not overstate.** Indexing is usually **minutes to tens of minutes** (reference
> on ~14 cores: ~2.5 min for ~500 translation units, ~20 min for ~6000; a few
> minutes per 1000 TUs, proportionally longer on fewer cores). Gauge it by
> counting entries in `compile_commands.json` (≈ one per TU). `reindex.sh` also
> prints an exact estimate for the machine right before it starts indexing.
>
> Before running either heavy step, in **one message** you **MUST**:
> 1. say what it does and a realistic time estimate (per above);
> 2. show the **exact command, verbatim** (so they can run it themselves);
> 3. ask the user to pick: **"I run it for you"** or **"you run it yourself"**.
>
> Then stop and wait — do not start until they choose, and never launch it in
> the background. Tip: for a first try, suggest scoping to one subtree (a filter
> like `src/foo/`) so it finishes in a couple of minutes. The light steps (1, 2,
> 5, 6) you may run directly.

1. **Check the platform.** Local indexing needs **macOS arm64** or **Linux
   x86_64**. On **ARM-Linux (aarch64)**, indexing runs via an x86_64 container
   (Docker/Podman + amd64 emulation) — see [INSTALL.md](INSTALL.md) "ARM-Linux /
   Windows: index via a container". On **Windows**, do everything inside **WSL2
   (Ubuntu)**. On an **Intel Mac** indexing is not supported — stop and tell the
   user (they can still use a graph built elsewhere).
2. **Clone and set up** (needs [`uv`](https://docs.astral.sh/uv/) and `curl`):
   ```bash
   git clone https://github.com/rakiz/cppgraph && cd cppgraph
   scripts/setup.sh              # venv + deps + scip-clang
   ```
3. **Get a `compile_commands.json`.** Ask the user where theirs is. If they don't
   have one, it must be generated — and generating it may run a **full build**
   (long/heavy). Apply the RULE above: propose the right command, get the OK, or
   let them run it. How to produce one per build system (CMake / Bazel / Make):
   [AGENTS.md](AGENTS.md) → "The compilation database". Also ask for the project's
   **source root** and, optionally, a **subtree filter** to skip vendored code
   (e.g. `src/`).
4. **Build the graph** — **heavy: apply the RULE above** (one-time; minutes to
   tens of minutes, *not hours*). Present this exact command, with a realistic
   estimate, and let the user choose to run it or have you run it. Prefer a
   `<filter>` (e.g. `src/foo/`) on a first run so it finishes fast. It writes
   into the target's gitignored `.cppgraph/` and prints the register command:
   ```bash
   scripts/reindex.sh <compile_commands.json> <filter> myproject
   ```
5. **Register the MCP server** — once per machine (idempotent; no project args,
   it auto-discovers each project's `.cppgraph/` from the working directory):
   ```bash
   scripts/register-mcp.sh
   ```
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
compile_commands.json  →  scip-clang  →  index.scip  →  cppgraph build  →  graph store
                          (semantic       (protobuf,      (parse + edges)    (query / MCP / viz)
                           indexer)         USR-keyed)
```

- **Builder** (`scip-clang`, external compiled binary): does the expensive,
  perf-critical C++ parsing. Crash-isolated per translation unit.
- **cppgraph** (this repo, Python): glue + graph. Parses SCIP, builds the graph,
  serves queries, exposes an MCP server, and can export a graph.json for
  visualization.

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

`cppgraph export '<symbol>' --graph <graph.db> --depth 2 --out graph.json` writes
a bounded neighbourhood in a [graphify](https://github.com/Graphify-Labs/graphify)-compatible
`graph.json`. Open it in the bundled viewer (`viz/cppgraph-viz.html`, our own
code + a vendored copy of vis-network, fully offline) — or, since the container
format is shared, in graphify itself. Details: [viz/README.md](viz/README.md).

## License

[MIT](LICENSE). The bundled viewer vendors [vis-network](https://github.com/visjs/vis-network)
(MIT / Apache-2.0); see [viz/README.md](viz/README.md#third-party-notices).
