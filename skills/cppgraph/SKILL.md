---
name: cppgraph
description: >
  Navigate and understand C++ codebases with compiler-exact code-graph queries
  (cppgraph MCP tools / CLI). Use when working in a C++ codebase and the user
  asks things like "who calls X", "what does X call", "impact of changing X /
  blast radius", "find references to X", "class hierarchy / base classes /
  subclasses", "call path between X and Y", "dead code / never called",
  "circular dependencies", "hotspots / most-called symbols", "API surface of
  this module", "what depends on this library", layering violations, static
  initialization order, or generally needs to navigate or understand a large
  C++ codebase — instead of guessing with grep.
---

## What this is

cppgraph answers code-navigation questions from a **compiler index** (SCIP via
scip-clang), not grep or a by-name AST: every symbol has a unique compiler
identity (USR), so edges are exact — overloads, `ptr->method()`, free
functions, and templates are resolved with no name-collision merges. Prefer
these tools over grep for any "who/what/where/impact" question in C++.

## Prerequisite

An indexed graph must already exist for the project. Check with the `status`
MCP tool (or `cppgraph status` in the CLI). If no graph is found, tell the
user to follow the install/index steps in cppgraph's README.md (and its
agent-facing AGENTS.md) — do not attempt to install or index from a skill.

## Tool selection

| Question | MCP tool |
|---|---|
| Who calls X? | `who_calls` |
| What does X call? | `what_it_calls` (`hide_trivial=True` drops operator/assert noise) |
| Find a symbol by name / disambiguate overloads | `find` (shows exact SCIP strings) |
| Where is X used / find references? | `find_references` (`include_source=True` for code inline) |
| Impact of changing X / blast radius | `impact_of` (`kind="calls"`; `kind="inherits"` for subclass blast) |
| What can X reach / attack surface, migration scope | `reachable_from` |
| Call path between X and Y | `path` |
| Class hierarchy | `base_classes` / `subclasses` (or `impact_of kind="inherits"` for the whole subtree) |
| Tell me about X: definition, docs, callers, snippet | `explain_symbol` (`include_source=True` for the snippet inline) |
| Members of a class / symbols in a file | `class_members` / `outline` |
| Dead code / zero callers | `no_incoming_calls` |
| Circular dependencies | `strongly_connected_components` |
| Hotspots / most-called symbols | `hotspots` (`kind="fan_in"|"fan_out"|"edges"`) |
| API surface of a module | `api_surface` |
| Dependency cost of a library ("replace/remove it") | `dependency_cost` |
| Layering violations | `boundary_violations` (you supply the rules) |
| Largest function bodies | `line_span` |
| Static initialization order (what a global's init reads) | `global_init_references` |
| Codebase size/density per file or dir | `stats` |
| Show the graph around X | `visualize` (keep depth 1–2) |

### Reflex correction

The `Read`/`grep` reflex wins by default even on indexed repos — override it
for everyday structural tasks, not only graph questions: use `outline` instead
of `Read` for a file's/class's structure, a scoped `find` instead of `grep` to
locate a symbol by name, `explain_symbol` instead of opening the header
(definition + callers in one shot).

## Usage patterns

- **Plain names work in a single call**: a unique name auto-resolves
  (`who_calls("parseConfig")`). Specifically, `find_references` and `who_calls`
  do **not** need a two-step "resolve the SCIP symbol, then query" — they take
  a unique human name directly, so never fall back to `grep` for a one-shot
  `file:line` list. Ambiguous names return candidates — get the exact SCIP
  string via `find`.
- **Check `truncated`** before assuming a list is complete; raise `limit`.
  Totals are always full counts.
- **Scope to production code** with `exclude_tests=True` (off by default:
  `who_calls` drops test callers, `what_it_calls` drops test callees,
  `impact_of`/`reachable_from` drop test symbols along the traversal).
- **"0 results" is never proof of dead code.** `no_incoming_calls` and
  similar zero-caller answers carry a `note`: virtual dispatch, templates,
  function pointers, and entry points have no static caller. Report the fact
  with its caveat — never assert "X is dead" as fact.
- **Reference-dependent tools** (`find_references`, `api_surface`) report
  `available` / `refs_available: false` on a graph built `--no-references`.
  `line_span` and `no_incoming_calls` additionally need a #504-built
  scip-clang (`has_enclosing_ranges`); `global_init_references` needs the
  #504 binary **plus** `--attributed-refs`/`enrich-refs`
  (`has_attributed_refs`). When a tool reports `available: false` it names
  why in `reason`; `note` is a separate caveat field on successful or
  provisional results (zero callers, reachability bounds, empty hierarchy).
  Heed both instead of guessing.
- **Facts, not verdicts**: cycles, zero callers, and layering hits are graph
  facts — judge legitimacy yourself before reporting conclusions.

## MCP tools unavailable?

Fall back to the `cppgraph` CLI — same behavior, same parity by project
invariant; some names differ (`who_calls`→`callers`, `what_it_calls`→`callees`,
`find_references`→`references`, `impact_of`→`impact`, `base_classes`→`bases`,
`subclasses`→`subtypes`, `explain_symbol`→`explain`, `api_surface`→`api-surface`,
`dependency_cost`→`dependency-cost`, `class_members`→`class-members`,
`strongly_connected_components`→`strongly-connected-components`,
`reachable_from`→`reachable-from`, `boundary_violations`→`boundary-violations`,
`visualize`→`view`). Everything else is spelled the same on both surfaces
(`find`, `path`, `hotspots`, `stats`, `status`, `outline`, `line_span`,
`no_incoming_calls`, `global_init_references` — underscores included).
Commands auto-discover the graph from the cwd and accept plain names.
