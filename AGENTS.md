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

## The compilation database (compile_commands.json)

cppgraph needs a `compile_commands.json` for the target project — it's the input
to `scip-clang`. It is the target's artifact, never stored in this repo. Produce
one however the target supports:

- CMake: configure with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` → written to the
  build dir; symlink/copy it to the project root for tools that expect it there.
- Bazel: the `hedron_compile_commands` rule (`bazel run
  @hedron_compile_commands//:refresh_all`), or the project's own target if it
  ships one (e.g. MongoDB has a bespoke `compiledb` aspect defined in `.bazelrc`
  → `bazel build --config=compiledb //src/...`; the authoritative invocation is
  in `buildscripts/clang_tidy_vscode.py`).
- Make / other: `bear -- <build command>` wraps the build and records it.
- Multiple fragmented DBs: merge with `compdb`.

Regenerate when it's stale (it reflects the build graph at generation time). The
tool takes the path as an argument — never hard-code it.

The turnkey path is `cppgraph init` — it finds the compdb, prints the breakdown,
and walks the scope questions (subtree / tests / attribution) in order; run it, or
use the manual steps below when you want to steer each stage.

Before indexing, inspect it with `cppgraph compdb-summary <compile_commands.json>`
(total TUs, subtrees, test count + %; `--filter <substr>` previews a scope). The
indexing scope — subtree filter, `reindex.sh --no-tests` — is the user's choice;
present the breakdown and let them pick, don't decide for them. Skipping tests is
a trade-off (faster index, but loses "which tests exercise symbol X") — offer it,
don't default it. The chosen scope is recorded in the graph (`cppgraph status`
shows it) and reused by `reindex.sh --update`.

## Guardrails

- **Do not commit without the maintainer saying so explicitly.**
- Treat the target's **source** as read-only — never modify code or build files.
  The one thing the tool writes into the target is a **gitignored `.cppgraph/`**
  directory (its own outputs: `graph.db`, `.scip`, filtered compdb), dropped in
  with a `.gitignore` of `*` so it never dirties the repo — like `.vscode/`.
  Everything else is read (`compile_commands.json`, and sources with `--root`).
- Per-machine tool install, set up by `scripts/setup.sh`: the whole tool lives
  under one persistent data dir, `${XDG_DATA_HOME:-~/.local/share}/cppgraph/` —
  the git checkout + its `.venv` in `repo/`, and the `scip-clang` binary (a
  per-machine artifact, one per arch, shared across projects) in `bin/` (override
  `CPPGRAPH_BIN_DIR`). A data dir, not a cache, so a self-built binary and the
  checkout the global MCP registration points at aren't wiped by cache cleaners.
  Not under `scratch/`, which is dev-only throwaway (example graphs, etc.).

## Layout

```
src/cppgraph/     package (cli, builder, scip parser, store, queries, mcp, export)
viz/              bundled offline graph viewer (HTML + vendored vis-network)
scripts/          setup.sh, reindex.sh, register-mcp.sh
tests/            pytest
scratch/          dev-only throwaway: example graphs, ad-hoc outputs (gitignored)
                  (the scip-clang binary lives in ~/.local/share/cppgraph/bin, not here)
DESIGN.md         architecture + edge model
CHANGELOG.md      what's been done
TODO.md           open tasks
```

Per-project outputs (`graph.db`, `.scip`) live in the **target** project's
`.cppgraph/`, not here.
