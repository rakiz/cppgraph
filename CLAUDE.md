@AGENTS.md

## Session start

Read `HANDOFF.md` (current state + exact next command) before doing anything.
Then `DESIGN.md` (architecture) and `TODO.md` (ordered tasks). For setting up a
new machine (installing `scip-clang`, `protoc`, the venv), see `INSTALL.md`.

## Large artifacts

Never version `compile_commands.json` (the target's input, can be hundreds of MB)
nor any derived index/graph (`*.scip`, graph dumps). They live in `scratch/` and
are gitignored. See AGENTS.md → "The compilation database" for how to obtain or
refresh one.
