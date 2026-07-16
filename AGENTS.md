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
  (protoc self-marks them `DO NOT EDIT!`) — this avoids requiring every
  contributor to install `protoc` just to run the tool. `protoc` is only needed
  to *regenerate* the bindings after `scip.proto` changes. See
  `src/cppgraph/proto/README.md` / `INSTALL.md` for exact steps.
- Tests with `pytest` under `tests/`. Prefer small fixtures (a tiny checked-in
  or synthetic `.scip`) over depending on a full external index.
- CLI entry: `cppgraph` (see `src/cppgraph/cli.py`); MCP server `cppgraph-mcp`.

## Working habits

- **Red/green TDD.** Write a failing test first, especially when fixing a bug or
  changing behavior; then make it pass. New code and changes get tests. The test
  for `src/cppgraph/foo.py` lives at `tests/test_foo.py`.
- **Format before you commit, only what you touched.** Never a full-repo
  reformat in an unrelated commit.
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
  @hedron_compile_commands//:refresh_all`).
- Make / other: `bear -- <build command>` wraps the build and records it.
- Multiple fragmented DBs: merge with `compdb`.

Regenerate when it's stale (it reflects the build graph at generation time). The
tool takes the path as an argument — never hard-code it.

## Guardrails

- **Do not commit without the maintainer saying so explicitly.**
- Treat the target's **source** as read-only — never modify code or build files.
  The one thing the tool writes into the target is a **gitignored `.cppgraph/`**
  directory (its own outputs: `graph.db`, `.scip`, filtered compdb), dropped in
  with a `.gitignore` of `*` so it never dirties the repo — like `.vscode/`.
  Everything else is read (`compile_commands.json`, and sources with `--root`).
- The per-machine tool install (the `scip-clang` binary, the `.venv`) lives in
  this cppgraph checkout under `scratch/` — gitignored, set up by
  `scripts/setup.sh`.

## Layout

```
src/cppgraph/     package (cli, builder, scip parser, store, queries, mcp, export)
viz/              bundled offline graph viewer (HTML + vendored vis-network)
scripts/          setup.sh, reindex.sh, register-mcp.sh
tests/            pytest
scratch/          per-machine tool install (scip-clang binary) + throwaway (gitignored)
DESIGN.md         architecture + edge model
CHANGELOG.md      what's been done
TODO.md           open tasks
```

Per-project outputs (`graph.db`, `.scip`) live in the **target** project's
`.cppgraph/`, not here.
