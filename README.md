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
   x86_64**. On **Windows**, do everything inside **WSL2 (Ubuntu)**. On an
   **Intel Mac** indexing is not supported — stop and tell the user (they can
   still use a graph built elsewhere).
2. **Clone and set up** (needs [`uv`](https://docs.astral.sh/uv/) and `curl`):
   ```bash
   git clone https://github.com/rakiz/cppgraph && cd cppgraph
   scripts/setup.sh              # venv + deps + scip-clang
   ```
3. **Get a `compile_commands.json`.** Ask the user where theirs is. If they don't
   have one, it must be generated — and generating it may run a **full build**
   (long/heavy). Apply the RULE above: propose the right command, get the OK, or
   let them run it. Detect the build system:
   - `CMakeLists.txt` → re-configure with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
     (the compdb lands in the build dir; symlink/copy it to the project root);
   - `WORKSPACE`/`BUILD` (Bazel) → the `hedron_compile_commands` rule
     (`bazel run @hedron_compile_commands//:refresh_all`), or the project's own
     target if it ships one (e.g. MongoDB: `bazel build //:compiledb`);
   - `Makefile`/other → `bear -- <their build command>`.

   See [AGENTS.md](AGENTS.md) → "The compilation database" for details. Also ask
   for the project's **source root** and, optionally, a **subtree filter** to
   skip vendored code (e.g. `src/`).
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

Your AI assistant already answers "what calls X?" — with `grep`. On a large C++
codebase that's both **wrong** and **token-expensive**:

- **Wrong.** grep matches by name: it merges distinct symbols sharing a name,
  includes comments/strings/declarations, and misses calls it can't see
  textually (`ptr->method()`, virtual dispatch, templates).
- **Expensive.** The grep output is noisy, and to disambiguate, the assistant
  then reads whole files — all of it flows through the model's context.

Measured on MongoDB, question *"who calls the **method** `makeResumeToken`?"*
(four distinct symbols share that name). The cppgraph rows count the **MCP tool
JSON** — the payload the LLM actually ingests:

| Approach | Tokens ingested\* | Correct? |
|---|---|---|
| `grep -rn makeResumeToken src/mongo` (untargeted — realistic) | ~6,600 | ✗ 4 symbols merged, decls/comments; needs file reads to disambiguate |
| same grep on the known subtree (best case) | ~5,900 | ✗ same problems — targeting barely helps |
| cppgraph `find` + `who_calls` on the method | **~400** | ✓ exact: the method's 3 callers, nothing else |

→ **~16× fewer tokens, and exact** — and grep still needs follow-up file reads
that cppgraph doesn't. The trade-off: a one-time index (minutes), amortized over
every later query.

The fan-out tools are **token-lean by default**: each hit ships a readable label
(derived from the SCIP string) + `file:line`, not the 150-250-char raw SCIP
symbol, and test callers are dropped. On a hub symbol these compound — e.g.
`who_calls(ResumeToken::parse)` goes from ~2,780 tokens (raw strings, 100 callers
incl. tests) to ~500 (13 production callers, shortened labels): **~5.5× leaner**,
same exact answer. Pass `full_symbols=True` / `exclude_tests=False` to opt out.

\* ≈ chars÷4 — rough and deliberately **conservative** (code and SCIP symbol
strings tokenize *denser* than prose, so real counts are higher; the ratio
holds). grep is run over all of `src/mongo` because you don't know up front where
the symbol lives — and the LLM isn't told how to scope it. A symbol with many
*genuine* callers costs more in cppgraph too, but that's the complete, attributed
list grep can't produce at all. Reproduce with `scripts/measure_tokens.py`; method
in [COMPARISON.md](COMPARISON.md).

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
