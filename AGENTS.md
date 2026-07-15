# AGENTS.md — working instructions for cppgraph

Read this first. Then `HANDOFF.md` (current state) and `DESIGN.md` (architecture).

## What this project is

A semantically accurate code-graph tool for C++, built from a **compiler index**
(SCIP via `scip-clang`), not a syntactic AST. The whole point is *correct,
disambiguated* symbol identity and edges. If a change makes the graph "by name"
again, it defeats the project. See `README.md` for the over/under-capture rationale.

MongoDB is the first target (a large real C++ codebase at
`/Users/sebastien.mendez/code/mongo`, with an existing
`compile_commands.json` at its root). The tool must stay **general**: it takes a
`compile_commands.json` path and works on any C++ project. Never hard-code
MongoDB paths into the tool — only into scratch/tests.

## Direction & principles

- **Correctness over coverage.** A smaller graph of exact edges beats a big graph
  of guessed ones. Every `calls` edge must trace to a compiler occurrence.
- **Don't re-implement a C++ parser.** Consume SCIP. The expensive work is
  scip-clang's job.
- **Python-first, glue not compute.** Perf-critical parsing is an external
  binary. Port to Rust only if a *measurement* demands it — not preemptively.
- **Ship the POC before generalizing.** Validate the `makeResumeToken`
  disambiguation and one virtual-dispatch case on a single subsystem before
  scaling to all of `src/mongo`.
- **Open-source hygiene.** Clear README, no secrets, no vendored huge blobs,
  reproducible build steps.

## Stack / conventions

- Python 3.13, type hints everywhere, `from __future__ import annotations`.
- Deps via `pyproject.toml`; a local `.venv` in the repo. Any Python you run
  MUST use that venv.
- SCIP protobuf: vendor `scip.proto`, generate `scip_pb2.py` (gitignored) with
  `protoc`. Do not commit generated bindings.
- Tests with `pytest` under `tests/`. Prefer small fixtures (a tiny checked-in
  `.scip` or a synthetic one) over depending on a full MongoDB index.
- CLI entry: `cppgraph` (see `src/cppgraph/cli.py`).

## Working habits (adapted from the MongoDB repo's conventions)

- **Red/green TDD.** Write a failing test first, especially when fixing a bug or
  changing behavior; then make it pass. New code and changes get tests. The test
  for `src/cppgraph/foo.py` lives at `tests/test_foo.py`.
- **Format before you commit, only what you touched.** Run the formatter on
  changed files, never a full-repo reformat in an unrelated commit.
- **Token economy.** For multi-file exploration or noisy searches, spawn a
  subagent (Explore/Haiku) and keep only the findings in the main context — don't
  fill it with raw greps or file dumps. Compress large outputs before reasoning
  on them.
- **Navigate with code intelligence, not blind reads.** Prefer symbol-level
  lookup (Serena / LSP: find_symbol, find_referencing_symbols, overview) over
  reading whole files; read a full body only when you actually need it. (Note:
  this project is itself about building that kind of intelligence for C++.)
- **Compact at phase boundaries.** Suggest `/compact` (never auto-run) when a
  phase from `TODO.md` is done and a new one starts, or when context is saturated
  with logs. Save key decisions to `HANDOFF.md` first so nothing is lost.
- **Verify before asserting.** Don't claim an edge is missing, a symbol is
  unused, or a build passes without checking. This project's whole thesis is
  "measure, don't guess" — hold the tooling to the same bar.
- **Small, reversible steps.** Prove the POC on one subsystem before scaling.
  Don't gold-plate (e.g. don't rewrite in Rust) without a measurement demanding it.

## The compilation database (compile_commands.json)

cppgraph needs a `compile_commands.json` for the target project — it's the input
to `scip-clang`. It is the target's artifact, never stored in this repo.

**For MongoDB** (our first target, at `/Users/sebastien.mendez/code/mongo`):
one already exists at the repo root (~203 MB). To refresh it (Bazel +
hedron_compile_commands):

```
# from the mongo repo root
bazel run @hedron_compile_commands//:refresh_all
```

Regenerate when it's stale (it reflects the build graph at generation time) or
after large structural changes. It's fine to be somewhat stale for *structure*.

**For any other C++ project**, produce one however that project supports:

- CMake: configure with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` → written to the
  build dir; symlink/copy it to the project root for tools that expect it there.
- Bazel (non-mongo): the `hedron_compile_commands` rule, same `refresh_all` idea.
- Make / other: `bear -- <build command>` wraps the build and records it.
- Multiple fragmented DBs: merge with `compdb`.

The tool takes the path as an argument — never hard-code it.

## Guardrails

- **Do not commit without the maintainer saying so explicitly** in the message.
- Keep MongoDB read-only. This tool never writes into the mongo repo; it only
  reads `compile_commands.json` by absolute path.
- Large artifacts (`*.scip`, graph dumps) stay gitignored / in `scratch/`.

## Layout

```
src/cppgraph/     package (cli, builder, scip parser, graph store, queries)
tests/            pytest
scratch/          local indexes, throwaway outputs (gitignored)
DESIGN.md         architecture + edge model + roadmap
TODO.md           actionable task list
HANDOFF.md        current state + exact next command
```
