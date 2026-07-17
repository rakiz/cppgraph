# QUICKSTART — try cppgraph on your project

Goal: from zero to asking your AI assistant real questions about a C++ codebase
("what calls X?", "what breaks if I change Y?", "show the dependency graph of
Z"), in a handful of commands.

## Before you start

- **Prereqs:** [`uv`](https://docs.astral.sh/uv/), `curl`, and a C++ project with
  a `compile_commands.json` (see [AGENTS.md](AGENTS.md) → "The compilation
  database" for how to produce one).
- **Supported platforms for indexing** (limited by the `scip-clang` binary):
  - macOS Apple Silicon (arm64) ✅
  - Linux x86_64 ✅
  - **ARM-Linux (aarch64, e.g. Ubuntu arm64)** → indexing runs via an x86_64
    container (needs Docker/Podman + amd64 emulation); the graph then builds
    natively. See [INSTALL.md](INSTALL.md) → "ARM-Linux / Windows: index via a
    container".
  - **Windows** → run everything inside **WSL2 (Ubuntu)**; it behaves as Linux x86_64.
  - **Intel Mac** → not supported (no `scip-clang` binary). You can still *use* a
    graph someone else built — ask the maintainer for a prebuilt `graph.db` and
    jump to step 3.

## 1. Clone + set up

```bash
git clone https://github.com/rakiz/cppgraph && cd cppgraph
scripts/setup.sh          # venv + deps + downloads scip-clang
```

## 2. Build a graph of your project

Point it at your project's `compile_commands.json`; the second argument filters
to your source subtree (skip third-party/vendored code):

```bash
scripts/reindex.sh /path/to/project/compile_commands.json src/ myproject
# → writes /path/to/project/.cppgraph/myproject.graph.db (gitignored, next to
#   your code; a big codebase takes ~minutes, one time) and prints the exact
#   register command for the next step.
```

## 3. Use it from Claude Code (the main way)

Register the server once per machine (it auto-discovers each project's
`.cppgraph/`, so you only do this the first time):

```bash
scripts/register-mcp.sh
```

Then open a **new** Claude Code session **from your project directory** (that's
how it finds this project's graph) and just ask, in plain language:

- *"What calls `SomeClass::someMethod`? Watch out for same-named overloads."*
- *"What's the blast radius if I change this function?"*
- *"Show me everything that uses the type `Foo` (without the tests)."*
- *"Show the dependency graph of `Bar`."* → opens a diagram in your browser.

Claude picks the right tool (`find`, `who_calls`, `impact_of`, `find_references`,
`path`, `visualize`, `status`, …).

## Or use the CLI directly

(`$G` = the graph path reindex printed, e.g. `/path/to/project/.cppgraph/myproject.graph.db`.)

```bash
.venv/bin/cppgraph find someMethod --graph "$G"
.venv/bin/cppgraph callers '<exact symbol from find>' --graph "$G"
.venv/bin/cppgraph view    '<exact symbol>' --graph "$G" --depth 1
```

## Keeping it fresh

The graph is a snapshot. `.venv/bin/cppgraph status --graph "$G" --root
/path/to/project` tells you how far it has drifted and whether to run an
incremental `scripts/reindex.sh --update` or a full rebuild.

## Feedback

This is early — tell the maintainer what worked, what was confusing, and whether
the answers were actually useful. That's the whole point of this round.
