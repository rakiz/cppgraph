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
  - **ARM-Linux (aarch64, e.g. Ubuntu arm64)** → no prebuilt binary. Two options:
    **build scip-clang natively** (the setup wizard's *build* option, ~25-60 min,
    Docker — recommended, and gets PR #504), or run the x86_64 binary via a
    container (emulated, slow — for a subsystem only). See
    [INSTALL.md](INSTALL.md) → "ARM-Linux / Windows: index via a container" and
    `docker/build-scip-clang/`.
  - **Windows** → run everything inside **WSL2 (Ubuntu)**; it behaves as Linux x86_64.
  - **Intel Mac** → not supported (no `scip-clang` binary). You can still *use* a
    graph someone else built — ask the maintainer for a prebuilt `graph.db` and
    jump to step 3.

Two phases: **set up the machine once** (§1, light) then **index each project**
(§2, the heavy one-time-per-project step). §1 is done once and reused for every
project on the machine.

## 1. Set up the machine (once)

Clone into the per-machine tool dir, then run `setup.sh` — it creates the venv +
deps and runs the interactive setup (obtain scip-clang, register the MCP server,
then index your first project):

```bash
git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
"${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo/scripts/setup.sh"
```

`setup.sh` (needs [`uv`](https://docs.astral.sh/uv/)) asks how to obtain scip-clang
from a menu — **download** the prebuilt binary (~1 min; macOS arm64 / Linux
x86_64), **build** it locally with PR #504 (~25–60 min, Docker, Linux only), or
**emulate** via an x86 container — with an "abort" choice throughout. It then
registers the MCP server (globally, auto-discovering each project's `.cppgraph/` at
launch) and hands off to the project index wizard. Every stage checks what already
exists and asks before (re)doing it.

## 2. Index a project (once per project)

`setup.sh` indexes your first project automatically. To index another (or refresh
one), run the wizard from the project directory:

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo/scripts/index.sh"
# or, if the venv is on PATH:  cppgraph index
```

It finds the `compile_commands.json`, shows what's indexable, and asks the scope
questions as selectable menus (subtree / tests / attribution) with the info to
choose well. When a `.scip` or `.graph.db` already exists it shows its details and
asks whether to reuse or recompute — nothing expensive is overwritten by surprise.

Prefer to see the breakdown first, or drive it non-interactively?

```bash
cppgraph compdb-summary /path/to/project/compile_commands.json   # TUs, subtrees, tests %
cppgraph index <compdb> -y --filter src/mongo --no-tests --run   # scope from flags, no prompts
```

`--no-tests` is a trade-off, not a free win: tests are often a big share of TUs
(the summary shows the %), so skipping them speeds indexing — but the graph then
can't answer "which tests exercise symbol X". Keep them if that matters. The
scope you pick (filter + tests) is recorded in the graph: `cppgraph status` shows
it, and an incremental update reuses it — no need to re-pass the filter.

**Usage-view granularity (only if your scip-clang is a #504 build).** By default
the reference index is *file* granularity ("used somewhere in these files"). Answer
yes to the attribution question (or pass `--attributed-refs`) for *symbol*
granularity ("used by *these functions*") — more useful, larger store. No rush: the
`.scip` is kept, so you can upgrade later without re-indexing —
`cppgraph enrich-refs --graph <…>.graph.db --scip <…>.scip`. With a stock
(non-#504) binary attribution does nothing.

## 3. Use it from Claude Code (the main way)

The MCP server is already registered (§1), so just open a **new** Claude Code
session **from your project directory** (that's how it finds this project's
graph) and ask, in plain language:

- *"What calls `SomeClass::someMethod`? Watch out for same-named overloads."*
- *"What's the blast radius if I change this function?"*
- *"Show me everything that uses the type `Foo` (without the tests)."*
- *"Show the dependency graph of `Bar`."* → opens a diagram in your browser.

Claude picks the right tool (`find`, `who_calls`, `impact_of`, `find_references`,
`path`, `visualize`, `status`, …).

The lookup is forgiving, so a rough name still lands: `find` matches multiple
words in any order and, if nothing hits exactly, falls back
case/separator-insensitively (`changestream` finds `change_stream`) and on the
bare method name when a `Class#method` guess is wrong. Same-named overloads are
grouped under one result (with their parameter signatures), and asking to hide
trivial helpers (`hide_trivial`) strips the operator/assert/`makeStatus` noise so
the real edges stand out.

## Or use the CLI directly

Run from **inside the indexed project** and it just works — the graph is
auto-discovered from the cwd's `.cppgraph/` (no `--graph` needed), and commands
accept a **plain name**, not only the exact SCIP symbol string:

```bash
cd /path/to/project
cppgraph callers someMethod          # graph discovered, name resolved
cppgraph callees someMethod
cppgraph view    someMethod --depth 1
```

If a name is ambiguous (e.g. same-named overloads), the CLI lists the candidates
so you can pass the exact SCIP symbol; `find` shows those strings too. Outside a
project, or to target a specific store, pass `--graph <path/to/.cppgraph/name.graph.db>`.

## Keeping it fresh

The graph is a snapshot. `cppgraph status --root /path/to/project` (run from the
project) tells you how far it has drifted; re-run `scripts/index.sh` and the wizard
offers an incremental update or a full rebuild.

## Feedback

This is early — tell the maintainer what worked, what was confusing, and whether
the answers were actually useful. That's the whole point of this round.
