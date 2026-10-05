---
description: Query, index or update the current project's C++ cppgraph call graph
---

You drive cppgraph (C++ call graph indexed via scip-clang) for the current
project. The binary is on PATH as a bare `cppgraph` for installs where setup
linked it into `~/.local/bin`; on older installs, fall back to the full path
`~/.local/share/cppgraph/repo/.venv/bin/cppgraph`. Based on the first word of "$ARGUMENTS":

## status

Run `<binary> status` and summarize: graph present or not, drift (indexed
commit vs working tree, changed files), indexed scope, transport. If stale,
suggest `/cppgraph update`. Do nothing else.

## help

Explain what the graph can answer, with one example per case: who calls X /
what does X call (`callers`/`callees`), blast radius of a change (`impact`),
call path between two symbols (`path`), class hierarchies
(`bases`/`subtypes`), usages of a symbol (`references`), most-called symbols
(`hotspots`), a symbol's dossier (`explain`), JSON export (`export`). Note that
plain names suffice (`build_graph`, not the full SCIP symbol) and an ambiguous
name lists candidates instead of guessing.

## index

No graph exists yet (or a rebuild was requested). From the project root:

1. Run `<binary> index --plan-json` (it locates the `compile_commands.json` by
   itself). Only inspect the build system / offer to generate a compdb if
   `--plan-json` reports none.
2. Ask yes/no: "Index this project now? (~estimate)". If no, stop.
3. Offer the scope as selectable choices (your interactive question tool):
   whole tree + each subtree from `filter.options` with their TU counts, plus
   a yes/no "exclude tests?" and `attributed_refs` when
   `scip_clang.supports_attribution` is true. Never ask the user to type a
   path or a filter.
4. Run with their answers, non-interactively via the shell tool:
   `~/.local/share/cppgraph/repo/scripts/index.sh <compdb> -y --filter <sub> [--no-tests] [--attributed-refs] --run`
   Never launch the bare interactive wizard (no stdin → EOF). If an artifact
   already exists it is kept; `--from-scratch` only if a rebuild was
   explicitly chosen.
5. At the end, tell the user to reopen a session from the project directory if
   the scope changed (MCP instructions are fixed at connect time).

## update

The graph exists: re-run with the recorded scope (same command as `index`
step 4, without `--from-scratch`) — non-destructive, only missing or stale
artifacts are rebuilt. After a re-index inside an open session, MCP queries
are already current; only a changed scope requires reloading the MCP server
in your client.

## anything else (free-form query)

Treat "$ARGUMENTS" as a question about the graph. Use the `cppgraph_*` MCP
tools when available; otherwise the CLI has full parity:
`<binary> <status|find|callers|callees|bases|subtypes|references|path|impact|hotspots|explain|export|view>`.
If a tool reports no indexed graph, suggest `/cppgraph index`.

Absolute rules: never open the `.graph.db` with a sqlite client (the schema is
not a public contract); never pip-install cppgraph into a target project's
venv; treat the target project's sources as read-only (its gitignored
`.cppgraph/` is the only thing the tool writes).
