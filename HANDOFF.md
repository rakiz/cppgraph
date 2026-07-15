# HANDOFF — start here

_Last updated: 2026-07-15_

## Where we are

Project scaffolded. Direction decided. **No builder code yet.** Next concrete
action is installing `scip-clang` and indexing one MongoDB subsystem.

Read order for a fresh session: `AGENTS.md` → this file → `DESIGN.md` → `TODO.md`.

## The decision, in one paragraph

Tree-sitter graph tools (graphify) key symbols *by name* → they merge distinct
symbols (over-capture) and drop hard-to-bind calls (under-capture). Verified
empirically on MongoDB: `makeResumeToken` is really **two** symbols — a method
(~2 callers) and a test-helper free function (~57 test callers) — that a
name-based tool reports as one ~66-edge node. Fix: build the graph from a
**compiler index** (SCIP via `scip-clang`) where identity is a stable USR. Tool
is Python (glue); the heavy C++ parsing is scip-clang (external binary). Standalone
open-source project, C++-general, MongoDB-first. Graphify not used as a store
(name-based model is incompatible); maybe kept later just for viz.

## Environment facts (verified)

- Target repo: `/Users/sebastien.mendez/code/mongo` (read-only for us).
- `compile_commands.json` EXISTS at mongo root: ~203 MB, dated 2026-06-25.
  It's ~3 weeks old — fine for structure; regenerate if fresh is needed.
- `/usr/bin/clangd` present but **crashes on some third_party TUs** — that's why
  we chose scip-clang (crash-isolated per TU) over driving clangd.
- `scip-clang`: NOT installed yet.
- `uv` available; Python 3.13.

## Exact next step

1. Install scip-clang for darwin arm64 from
   https://github.com/sourcegraph/scip-clang/releases (latest). Put the binary on
   PATH or in the repo; record version + path here.
2. Index just the change_stream subsystem to keep the POC fast. Start from
   `src/mongo/db/pipeline`. Output the `.scip` into `scratch/`.
3. Then implement the SCIP parser + graph builder (TODO Phase 1) and run
   acceptance tests A (makeResumeToken split) and B (a dropped virtual call).

## Key reference symbols for the acceptance tests

- `ChangeStreamEventTransformation::makeResumeToken`
  — defined `src/mongo/db/pipeline/change_stream_event_transform.cpp:235`
  (declared in `.h:72`). Real callers: ~2, both in that .cpp.
- `change_stream_test_helper::makeResumeToken` — separate free function, ~57
  test call-sites. Must be a DIFFERENT node from the method.

## Guardrails (from AGENTS.md)

- No commits without explicit maintainer approval.
- Never write into the mongo repo.
- `*.scip` / graph dumps stay in `scratch/` (gitignored).
