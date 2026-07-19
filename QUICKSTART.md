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
    **build scip-clang natively** (`setup.sh --scip-source build`, ~30-60 min,
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

**One command** — clone into the per-machine tool dir, venv + deps, scip-clang,
MCP registration, each gated by a confirmation:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rakiz/cppgraph/main/scripts/bootstrap.sh)
```

Use `bash <(curl …)`, not `curl … | bash` (keeps your terminal for the prompts).
It confirms before installing and asks how to get scip-clang (download ~1 min /
build PR #504 ~30–60 min / emulate), with a "don't install" option throughout.

Prefer to do it by hand (or drive each step)?

```bash
git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
cd "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
scripts/setup.sh          # venv + deps, then asks the scip-clang source
scripts/register-mcp.sh   # register the MCP server, once per machine
```

`setup.sh` asks how to obtain scip-clang (download the prebuilt binary, build it
locally with PR #504, or route to an emulated container). On a terminal it prompts
per option (with a don't-install choice); non-interactively it stops and asks you
to pass `--scip-source download|build|emulate|auto` (or `CPPGRAPH_SCIP_SOURCE`)
rather than deciding for you.

`register-mcp.sh` is part of this one-time setup: it registers the server
globally and auto-discovers each project's `.cppgraph/` at launch, so you run it
now — before indexing anything — and never again. That's the whole machine setup;
next you point it at a project.

## 2. Index a project (once per project)

**Easiest: `cppgraph init`.** From the project directory, it finds the
`compile_commands.json`, shows what's indexable, and asks the scope questions in
order (subtree / tests / attribution) with the info to choose well, then runs the
pipeline. Deterministic and LLM-free:

```bash
cppgraph init                     # auto-finds the compdb; --print to only show
                                  # the command; re-run to resume/update
# non-interactive (e.g. an agent that already asked you the questions):
cppgraph init <compdb> -y --filter src/mongo --no-tests --print
```

Prefer to drive it yourself? The manual steps are below.

First, see what's indexable and pick a scope — don't index blind:

```bash
cppgraph compdb-summary /path/to/project/compile_commands.json
# total TUs, which subtrees they live in, how many are tests;
# add --filter src/ to preview how many a filter would keep.
```

Then index. The 2nd argument is a **path-substring filter** (scope to your source
subtree, skip third-party/vendored code); add `--no-tests` to leave test TUs out
for a lighter, production-only graph:

```bash
scripts/reindex.sh /path/to/project/compile_commands.json src/ myproject
scripts/reindex.sh --no-tests /path/to/project/compile_commands.json src/mongo mongo
# → writes /path/to/project/.cppgraph/<name>.graph.db (gitignored, next to your
#   code; a big codebase takes ~minutes, one time).
```

`--no-tests` is a trade-off, not a free win: tests are often a big share of TUs
(the summary shows the %), so skipping them speeds indexing — but the graph then
can't answer "which tests exercise symbol X". Keep them if that matters. The
scope you pick (filter + tests) is recorded in the graph: `cppgraph status` shows
it, and `reindex.sh --update <graph.db> <compdb>` reuses it — no need to re-pass
the filter (a divergent one errors; changing scope means a full rebuild).

**Usage-view granularity (only if your scip-clang is a #504 build).** By default
the reference index is *file* granularity ("used somewhere in these files"). Add
`--attributed-refs` for *symbol* granularity ("used by *these functions*") — more
useful, larger store:

```bash
scripts/reindex.sh --attributed-refs /path/to/project/compile_commands.json src/ myproject
```

No rush to decide: the default keeps the `.scip`, so you can upgrade later
without re-indexing — `cppgraph enrich-refs --graph <…>.graph.db --scip <…>.scip`.
With a stock (non-#504) binary the flag does nothing.

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
project) tells you how far it has drifted and whether to run an incremental
`scripts/reindex.sh --update` or a full rebuild.

## Feedback

This is early — tell the maintainer what worked, what was confusing, and whether
the answers were actually useful. That's the whole point of this round.
