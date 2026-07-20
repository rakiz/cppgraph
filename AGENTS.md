# AGENTS.md — working instructions for cppgraph

Read this first, then `DESIGN.md` (architecture). Completed work is in
`CHANGELOG.md`; open tasks in `TODO.md`.

## What this project is

A semantically accurate code-graph tool for C++, built from a **compiler index**
(SCIP via `scip-clang`), not a syntactic AST. The whole point is *correct,
disambiguated* symbol identity and edges. If a change makes the graph "by name"
again, it defeats the project. See `README.md` for the over/under-capture
rationale and `COMPARISON.md` for the measured evidence.

The tool is **general**: it takes a `compile_commands.json` path and works on
any C++ project. Never hard-code a specific project's paths into the tool — only
into `scratch/` and tests. (A large real codebase is handy as an example target
when you want to measure at scale; keep such paths out of the shipped code.)

## Direction & principles

- **Correctness over coverage.** A smaller graph of exact edges beats a big graph
  of guessed ones. Every `calls` edge must trace to a compiler occurrence.
- **Don't re-implement a C++ parser.** Consume SCIP. The expensive work is
  scip-clang's job.
- **Python-first, glue not compute.** Perf-critical parsing is an external
  binary. Port to Rust only if a *measurement* demands it — not preemptively.
- **Open-source hygiene.** Clear README, no secrets, no vendored huge blobs,
  reproducible build steps.

## Stack / conventions

- Python 3.13+, type hints everywhere, `from __future__ import annotations`.
- Deps via `pyproject.toml`; a local `.venv` in the repo. Any Python you run
  MUST use that venv.
- SCIP protobuf: `src/cppgraph/proto/scip.proto` is vendored, alongside the
  **generated and committed** bindings `src/cppgraph/proto/scip_pb2.py` / `.pyi`
  (protoc self-marks them `DO NOT EDIT!`) — so nobody needs `protoc` to run the
  tool. Regenerating after a `scip.proto` change runs a pinned `protoc` in a
  container (`docker/gen-bindings/`), never on the host. See
  `src/cppgraph/proto/README.md` / `INSTALL.md` for exact steps.
  **Invariant:** keep the vendored `scip.proto` a *superset* of what any
  supported scip-clang emits, and read every optional field as optional — an
  absent `repeated` field is an empty list, i.e. "feature not present", never an
  error (a stock binary and a #504 build must both index without crashing).
  Regenerate the binding only to start reading a *new* field, never to avoid a
  crash.
- Tests with `pytest` under `tests/`. Prefer small fixtures (a tiny checked-in
  or synthetic `.scip`) over depending on a full external index.
- CLI entry: `cppgraph` (see `src/cppgraph/cli.py`); MCP server `cppgraph-mcp`.
  Query commands (`find`, `callers`, `callees`, `path`, `impact`, `status`, …)
  **auto-discover the graph** from the cwd's `.cppgraph/` — run from inside the
  indexed project and `--graph` is optional — and **accept a plain name** (not
  only the exact SCIP string), resolving it via `find`; an ambiguous name lists
  candidates. Same discovery walk as the MCP server (`store.discover_graph`).
- **Keep the CLI and MCP surfaces equivalent.** A query behaviour goes on *both*,
  driven by the same pure functions (`cppgraph.filters`,
  `cppgraph.cli.build_export_json`) — never fork the logic into one surface only.
  When adding a flag or view, wire it through the shared function and expose it on
  each side.
- **Attributed references (`enclosing_range` / #504).** References carry an
  optional enclosing-definition symbol, enabling the symbol-granularity usage view
  (`export --mode usage`). It's opt-in at build (`--attributed-refs`) or added
  after the fact (`enrich-refs`), because it costs a symbol id per reference; the
  store records it in `has_attributed_refs`, and `build_graph(attribute_references=…)`
  only populates it when the binary emits `enclosing_range` (stock ⇒ no-op). The
  user-facing recommendation + size caveat live in the tool output (`status`
  `usage_view`, `build`/`enrich-refs` help), not here — keep them there so both
  surfaces say it.

## Working habits

- **Red/green TDD.** Write a failing test first, especially when fixing a bug or
  changing behavior; then make it pass. New code and changes get tests. The test
  for `src/cppgraph/foo.py` lives at `tests/test_foo.py`.
- **Format and lint before you commit, only what you touched.** `ruff format`
  and `ruff check --fix` (config in `pyproject.toml`, line length 100; generated
  `proto/` is excluded). Never a full-repo reformat in an unrelated commit — if a
  repo-wide format is needed, make it its own dedicated commit.
- **Token economy.** For multi-file exploration or noisy searches, spawn a
  subagent and keep only the findings in the main context — don't fill it with
  raw greps or file dumps.
- **Verify before asserting.** Don't claim an edge is missing, a symbol is
  unused, or a build passes without checking. This project's whole thesis is
  "measure, don't guess" — hold the tooling to the same bar.
- **Small, reversible steps.** Don't gold-plate (e.g. don't rewrite in Rust)
  without a measurement demanding it.

## Indexing a project — ask the scope, then run with flags

A Claude Code `! …` run has **no interactive stdin** — a prompt inside it gets EOF.
So you cannot hand the user a bare interactive wizard and expect them to answer it
through you. Instead: **ask the scope in your own question UI, then run the index
non-interactively with those answers as flags.**

**You drive Phase B; the user only picks from choices you offer — they never type a
scope, a path, or a flag, and never see `--plan-json` / `index.sh …`.** The shape of
the conversation:

- **First, a yes/no:** "Index this project now? (~<estimate>)" — proceed or skip,
  nothing else. If they skip, stop.
- **Then offer the scope as selectable choices** built from the plan (below): a
  single-select of **whole tree** + **each subtree** from `filter.options` (with
  their TU counts), and a yes/no **exclude tests?**. They pick from the list — do
  **not** say "tell me a subdirectory" or "say 'whole repo'", and never ask them to
  type a filter or a flag.
- **Then you run it**, report progress, and when done tell them to open a new Claude
  Code session from the project directory.

Keep the commands and JSON out of the conversation entirely.

1. **Get the options:** run `cppgraph index --plan-json` from the project directory
   (use the installed binary: `~/.local/share/cppgraph/repo/.venv/bin/cppgraph`, or
   `scripts/index.sh --plan-json` — a bare `cppgraph` is not on PATH).
   It auto-locates the `compile_commands.json` (root / `build/` / up the tree) and
   returns the compdb breakdown plus a `questions[]` array (`filter`, `no_tests`,
   `attributed_refs`), each with its `info`, `default`, and — for `filter` —
   concrete `options`. **Do not inspect the build system or offer to generate a
   compdb** unless `--plan-json` reports none (only then, see the fallback below,
   after the user's OK — it may run a full build).
2. **Ask the user each question as a selectable choice** (not free text). `filter`:
   a single-select of whole tree + each subtree from `options` (an "other — type a
   substring" entry is fine as a last item, but the pick-list is the default).
   `no_tests`: a yes/no with the count/% trade-off. `attributed_refs`: only when
   `scip_clang.supports_attribution` is true. Decide nothing yourself.
   **Also check `artifacts`:** if `graph` or `scip` is already `true`, this project
   is already indexed — tell the user, and ask whether to **keep** it (default) or
   **rebuild**. Only pass `--from-scratch` if they choose rebuild.
3. **Run it with their answers** (non-interactive, so `!` works):
   ```
   ! ~/.local/share/cppgraph/repo/scripts/index.sh <compdb> -y --filter <sub> [--no-tests] [--attributed-refs] --run
   ```
   **Non-destructive by default:** an existing `.scip`/`.graph.db` is **kept**, not
   overwritten — the run only builds what's missing. Add `--from-scratch` only when
   the user asked to rebuild. The chosen scope is recorded and reused by later
   updates. Give a time estimate first — indexing is the long step.

Never run the bare interactive `cppgraph index` (or `scripts/index.sh` with no
flags) through `!` — it gets EOF and stops. (A human at a real terminal *can* run
`scripts/index.sh` with no flags for the interactive wizard; that's for them, not
you.) When the graph is stale enough to matter — read `cppgraph status` (or the MCP
`status` tool: commits behind, changed files) — tell the user, and re-run step 3.

### Fallback: no compile_commands.json (only when `--plan-json` reports none)

cppgraph needs a `compile_commands.json` (the input to `scip-clang`) — the
target's artifact, never stored in this repo. Produce one however the target
supports, then re-run the index:

- CMake: configure with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` → written to the
  build dir; symlink/copy it to the project root.
- Bazel: the `hedron_compile_commands` rule (`bazel run
  @hedron_compile_commands//:refresh_all`), or the project's own target if it
  ships one (e.g. MongoDB has a bespoke `compiledb` aspect in `.bazelrc` →
  `bazel build --config=compiledb //src/...`; authoritative invocation in
  `buildscripts/clang_tidy_vscode.py`).
- Make / other: `bear -- <build command>` wraps the build and records it.
- Multiple fragmented DBs: merge with `compdb`.

Generating one may run a full build (heavy) — get the user's OK first. Regenerate
when it's stale (it reflects the build graph at generation time).

## Guardrails

- **Do not commit without the maintainer saying so explicitly.**
- Treat the target's **source** as read-only — never modify code or build files.
  The one thing the tool writes into the target is a **gitignored `.cppgraph/`**
  directory (its own outputs: `graph.db`, `.scip`, filtered compdb), dropped in
  with a `.gitignore` of `*` so it never dirties the repo — like `.vscode/`.
  Everything else is read (`compile_commands.json`, and sources with `--root`).
- Per-machine tool install — **ask the source, then run with a flag** (a `! …` run
  can't answer prompts):

  1. Clone the repo to its data dir (**always this path**, even when asked to
     install "from a local copy" — clone the local checkout *into* the data dir,
     don't point at the source checkout):
     ```
     git clone <repo> "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
     ```
     (`<repo>` = `https://github.com/rakiz/cppgraph`, or a local path when testing.)
  2. **Get the valid sources from the tool — do NOT guess the platform.** Run
     `! ~/.local/share/cppgraph/repo/scripts/setup.sh --list-sources` (pure bash, no
     venv needed): it prints this machine's OS/arch and the sources that actually
     apply (e.g. no `download` on ARM-Linux). **Ask the user to pick from exactly
     those** — each has a cost: download ~1 min, build (#504) ~30–60 min Docker,
     emulate (slower indexing). Offering a source the tool didn't list will fail.
  3. Run `setup.sh` with their choice as a flag (this is what lets `!` work):
     ```
     ! ~/.local/share/cppgraph/repo/scripts/setup.sh --scip-source <download|build|emulate>
     ```
     Without `--scip-source`, a piped run stops with `ACTION NEEDED` rather than
     picking a costly default — that's deliberate.

  **Never** run `cppgraph setup` directly, `.venv/bin/cppgraph …`, or anything under
  a dev checkout like `~/code_projects/...` — on a fresh machine that path and venv
  don't exist until `setup.sh` has run. `scripts/setup.sh` is the sole entry point:
  it creates the venv, obtains scip-clang (per `--scip-source`), registers the MCP
  server, then — in a real terminal — offers to index the current project. The whole
  tool lives under one persistent data dir, `${XDG_DATA_HOME:-~/.local/share}/cppgraph/`
  — the git checkout + its `.venv` in `repo/`, and the `scip-clang` binary (a
  per-machine artifact, one per arch, shared across projects) in `bin/` (override
  `CPPGRAPH_BIN_DIR`). A data dir, not a cache, so a self-built binary and the
  checkout the global MCP registration points at aren't wiped by cache cleaners.
  Not under `scratch/`, which is dev-only throwaway (example graphs, etc.).
- Uninstall — **always use the script; never improvise `rm -rf`.** There is no
  uninstall *flag* on `setup.sh`; the tool is a separate script that lives with the
  installed tool (not a dev checkout). Hand the user this exact path:
  ```
  ! ~/.local/share/cppgraph/repo/scripts/uninstall.sh
  ```
  It asks per item what to remove and, crucially, **keeps each project's
  `.cppgraph/` index data by default** — a `.scip` there can take *hours* to rebuild.
  **Never** propose or run `rm -rf` on `~/.local/share/cppgraph` or a project's
  `.cppgraph/`, and never print a delete command for the index data: let
  `uninstall.sh` handle it safely (it warns before touching anything precious).
  `--dry-run` previews; `--purge` removes everything including project data (only if
  the user explicitly asks for that). Same rule as install: never point at
  `~/code_projects/...` or any dev checkout.

## Layout

```
src/cppgraph/     package (cli, builder, scip parser, store, queries, mcp, export)
viz/              bundled offline graph viewer (HTML + vendored vis-network)
scripts/          setup.sh, index.sh, index-in-container.sh, uninstall.sh
tests/            pytest
scratch/          dev-only throwaway: example graphs, ad-hoc outputs (gitignored)
                  (the scip-clang binary lives in ~/.local/share/cppgraph/bin, not here)
DESIGN.md         architecture + edge model
CHANGELOG.md      what's been done
TODO.md           open tasks
```

Per-project outputs (`graph.db`, `.scip`) live in the **target** project's
`.cppgraph/`, not here.
