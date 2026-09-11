# CLI reference

`cppgraph` has one subcommand per question. This page groups them by what
you're trying to do, so you can pick the right one without reading every
`--help`. The table rows (command, purpose, arguments) are generated from the
CLI's own argument parsers — `scripts/gen-cli-reference.py` rewrites them, so
they always match `cppgraph <command> --help`. Only the section prose and the
examples are hand-written here.

Three facts apply to nearly everything below:

- **Graph auto-discovery.** Run from inside an indexed project and the graph is
  found in the cwd's `.cppgraph/` — no `--graph` needed. From anywhere else,
  pass `--graph <path/to/.cppgraph/name.graph.db>`.
- **Plain names, not SCIP strings.** Every command whose argument is a
  `<symbol>` accepts a human name: a unique name resolves directly, an
  ambiguous one (e.g. same-named overloads) lists the candidates so you can
  pass the exact SCIP string — `cppgraph find` shows those strings. Arguments
  that are paths (`outline`'s file, `api-surface`'s module prefix,
  `dependency-cost`'s `--target-path`) are taken verbatim as recorded in the
  index.
- **`*` marks a required argument.** Everything else is optional; each
  Arguments column lists them in `--help` order. Test-defined symbols are
  excluded by default on the call-graph commands (`--no-exclude-tests` keeps
  them).

The query commands have MCP twins (same store, same filters, same answers) —
see [QUICKSTART.md](QUICKSTART.md).

## Getting and keeping a graph

From nothing to a queryable store, then keeping it current. The normal path is
`scripts/setup.sh` followed by `scripts/index.sh` (interactive, for a human at
a real terminal); the commands below are what they drive, plus the non-interactive
forms an agent or a script can call directly.

<!-- cppgraph-gen:setup init compdb-summary build update enrich-refs status -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `setup` | obtain the scip-clang indexer, register the MCP server, then index this project (the interactive per-machine setup; run via scripts/setup.sh) | --scip-source, -y/--yes, --from-scratch, --no-index |
| `init` (alias `index`) | guided onboarding: find the compdb, show what's indexable, ask the scope questions (subtree / tests / attribution) in order, then index | [<compdb>], --project-root, --name, --run, --print, -y/--non-interactive, --filter, --no-tests, --attributed-refs, --from-scratch, --plan-json |
| `compdb-summary` (alias `compdb_summary`) | summarize a compile_commands.json before indexing: how many TUs, where they live, how many are tests — so the index scope is an informed choice | <compdb>, --filter |
| `build` | build the graph from a SCIP index | --scip*, --out*, --source-commit, --source-dirty, --scip-variant, --index-filter, --index-no-tests, --references/--no-references, --attributed-refs |
| `update` | refresh a graph for changed source files: with no args, auto-discovers the graph + compdb and re-indexes incrementally; with --scip, applies an already-produced partial re-index instead | --graph, --scip, --deleted PATH, --source-commit, --source-dirty, --scip-variant |
| `enrich-refs` (alias `enrich_refs`) | add symbol-granularity reference attribution to an existing store from a #504 .scip, without a full rebuild | --graph*, --scip* |
| `status` | show the graph's source commit and, with --root, whether the checkout has drifted | --graph, --root |
<!-- /cppgraph-gen -->

```bash
# once per machine — the script wraps `cppgraph setup` (never run it bare)
~/.local/share/cppgraph/repo/scripts/setup.sh --scip-source download-patched

# index this project (interactive wizard; `scripts/index.sh` wraps it)
cppgraph init                          # auto-finds compile_commands.json
cppgraph init --plan-json              # the scope choices as JSON, for an agent to render

# peek at a compdb before choosing a scope
cppgraph compdb-summary compile_commands.json --filter src/

# the manual step `init` automates (.scip -> .graph.db)
cppgraph build --scip .cppgraph/myproj.scip --out .cppgraph/myproj.graph.db

# after editing sources: incremental refresh (auto-discovers graph + compdb)
cppgraph update

# upgrade an existing store to symbol-granularity usage (needs a #504 .scip)
cppgraph enrich-refs --graph .cppgraph/myproj.graph.db --scip .cppgraph/myproj-504.scip

# how stale is the graph? (exit 1 when the checkout has drifted)
cppgraph status --root .
```

## Finding a symbol

The entry point when you only have a name. `find` matches substrings and
order-free word sets against symbol and display names, groups same-named
overloads with their signatures, and falls back case/separator-insensitively —
and it shows the exact SCIP strings the other `<symbol>` commands accept.

<!-- cppgraph-gen:find -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `find` | find symbols by name (SCIP symbol strings aren't memorable) | --graph, <query>, --limit, --hide-trivial, --root, --include-path PREFIX, --exclude-path PREFIX |
<!-- /cppgraph-gen -->

```bash
cppgraph find change_stream             # substring, any position
cppgraph find Connection pool           # every word must appear, any order
cppgraph find --include-path src/ plan  # scope the matches to your code
cppgraph find --hide-trivial pipeline   # skip operators/*assert/lambda noise
```

## Traversing the call graph

Who calls whom: one hop (`callers` / `callees`), a route between two symbols
(`path`), or the full reachability (`impact` backwards — the blast radius,
`reachable-from` forwards). `hotspots` and `dependency-cost` rank the same
edges globally instead of from one symbol. Every edge traces to a compiler
occurrence — and is static, so dynamic dispatch and runtime-registered
callbacks have no edge; that is why `reachable-from` is documented as a lower
bound.

<!-- cppgraph-gen:callers callees path impact reachable-from hotspots dependency-cost -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `callers` | list callers of a symbol | --graph, <symbol>, --limit, --exclude-tests/--no-exclude-tests, --include-path PREFIX, --exclude-path PREFIX, --full-symbols |
| `callees` | list callees of a symbol | --graph, <symbol>, --limit, --exclude-tests/--no-exclude-tests, --include-path PREFIX, --exclude-path PREFIX, --full-symbols, --hide-trivial |
| `path` | shortest call chain from one symbol to another (see it as a graph: `view <src> --dst <dst> --mode path`; `--expand-paths` widens it to the corridor) | --graph, <src>, <dst> |
| `impact` | reverse blast-radius: everything that transitively calls a symbol | --graph, <symbol>, --depth, --kind, --limit, --exclude-tests/--no-exclude-tests, --include-path PREFIX, --exclude-path PREFIX, --full-symbols |
| `reachable-from` (alias `reachable_from`) | forward reachability: everything a symbol transitively calls (a lower bound) | --graph, <symbol>, --depth, --kind, --limit, --exclude-tests/--no-exclude-tests, --include-path PREFIX, --exclude-path PREFIX, --full-symbols |
| `hotspots` | global ranking of symbols by call-edge volume | --graph, --kind, --limit, --exclude-tests/--no-exclude-tests, --full-symbols, --include-path PREFIX, --exclude-path PREFIX |
| `dependency-cost` (alias `dependency_cost`) | call-site count against a target library — 'if I replace/remove this library, how many call sites change?' (exact count of calls edges into it); the module-exposure complement: `api-surface` | --graph, --target-path PREFIX*, --limit, --exclude-tests/--no-exclude-tests, --full-symbols, --include-path PREFIX, --exclude-path PREFIX |
<!-- /cppgraph-gen -->

```bash
cppgraph callers MyClass::method                  # who calls it (tests excluded by default)
cppgraph callees MyClass::method --hide-trivial   # what it calls, minus operator/assert noise
cppgraph path main handleRequest                  # is there a static route from a to b?
cppgraph impact parseConfig --depth 3             # everything that transitively calls it
cppgraph reachable-from handleRequest             # everything it transitively calls (lower bound)
cppgraph hotspots --kind fan_in --limit 10        # the most-called symbols, globally
cppgraph dependency-cost --target-path src/third_party/absl/   # call sites into that library
```

## Types, files, members

Structure facts: inheritance one hop (`bases` / `subtypes`), what a class
declares (`class-members`), what a file defines (`outline`).

<!-- cppgraph-gen:bases subtypes class-members outline -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `bases` | direct base classes a type inherits from (full transitive hierarchy: `reachable-from --kind inherits`) | --graph, <symbol> |
| `subtypes` | direct subclasses of a type | --graph, <symbol> |
| `class-members` (alias `class_members`) | members declared on a class/struct (methods, fields, nested types), by line | --graph, <symbol>, --limit, --full-symbols |
| `outline` | the outline of one file: every symbol defined in it, sorted by line | --graph, <file>, --limit, --full-symbols |
<!-- /cppgraph-gen -->

```bash
cppgraph bases QueryStage
cppgraph subtypes QueryStage
cppgraph class-members ConnectionPool
cppgraph outline src/app.cpp
```

## Where a symbol is used

The reference side — exact use sites the call graph is blind to (a type has no
call edges). `references` lists one symbol's use sites; `api-surface` reports
the actually-used external surface of a module; `global_init_references`
shows what one global's initializer touches.

<!-- cppgraph-gen:references api-surface global_init_references -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `references` | exact use sites of a symbol (unless the graph was built --no-references); see them as a graph with `view <symbol> --mode usage` | --graph, <symbol>, --root, --context N, --limit, --access, --include-path PREFIX, --exclude-path PREFIX |
| `api-surface` (alias `api_surface`) | the actually-used external surface of a module: definitions inside a prefix called/referenced from outside it (the consumption complement: `dependency-cost`) | --graph, <module_prefix>, --limit, --exclude-tests/--no-exclude-tests, --full-symbols |
| `global_init_references` (alias `global-init-references`) | globals referenced by one global's initializer region (the fact behind the static-init-order question, not a verdict); needs a #504-built graph with --attributed-refs | --graph, <symbol>, --limit, --full-symbols |
<!-- /cppgraph-gen -->

```bash
cppgraph references RequestContext --root . --context 3   # use sites, with source lines
cppgraph api-surface src/pipeline    # what of src/pipeline the rest of the code actually uses
cppgraph global_init_references kLogVerbosity   # needs #504 + --attributed-refs
```

## Whole-graph facts

Questions that don't start from one symbol: counts per file or directory,
biggest bodies, never-called callables, call cycles, declared-layering
conformance. All of them report facts, not verdicts — a `no_incoming_calls`
row is not a dead-code order, a cycle is not a bad-architecture finding.

<!-- cppgraph-gen:stats line_span no_incoming_calls strongly-connected-components boundary-violations -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `stats` | aggregate counts (symbols, call edges, refs) per file or directory | --graph, --group-by, --limit, --include-path PREFIX, --exclude-path PREFIX |
| `line_span` (alias `line-span`) | rank definitions by body extent (end_line - start, largest first); needs a graph indexed with a #504-built scip-clang | --graph, --limit, --exclude-tests/--no-exclude-tests, --full-symbols, --include-path PREFIX, --exclude-path PREFIX |
| `no_incoming_calls` (alias `no-incoming-calls`) | defined callables with zero incoming calls edges (a fact, not a dead-code verdict); needs a graph indexed with a #504-built scip-clang | --graph, --limit, --exclude-tests/--no-exclude-tests, --full-symbols, --include-path PREFIX, --exclude-path PREFIX |
| `strongly-connected-components` (alias `strongly_connected_components`) | cycles in the calls graph: groups of 2+ symbols that can all reach each other (a fact, not a bad-architecture verdict); to see the shape of one as a graph, export/view a symbol with --mode cycle | --graph, --limit, --exclude-tests/--no-exclude-tests, --full-symbols, --include-path PREFIX, --exclude-path PREFIX |
| `boundary-violations` (alias `boundary_violations`) | declared-layering conformance: list calls/inherits edges that cross a rule you supply (zero false positives — each hit is a real edge); pass --out PATH to also render this as a graph | --graph, --rule FROM:FORBIDDEN*, --kind, --limit, --full-symbols, --out PATH |
<!-- /cppgraph-gen -->

```bash
cppgraph stats --group-by dir --limit 10
cppgraph line_span --limit 10                       # biggest bodies; needs #504
cppgraph no_incoming_calls --exclude-path src/third_party/
cppgraph strongly-connected-components --limit 10
cppgraph boundary-violations --rule src/common/:src/platform/   # common/ must not call platform/
```

## Reading and viewing

`explain` is the one-shot "tell me about this symbol" summary — definition
site, doc comment, snippet, callers/callees. `export` and `view` render a
bounded subgraph around one symbol: `view` writes a self-contained HTML and
opens it in your browser, `export` writes the graph.json for `viz/` or
graphify. `--mode path --dst OTHER` renders the call graph between two symbols
instead: the shortest chain by default, or with `--expand-paths` the corridor
of *every* route between them (two capped BFS passes intersected). `--depth N`
there means context around the chain/corridor (default 0 — the pure answer),
and `--limit` caps the node count (default 40), reporting truncation.
`--mode cycle` renders the multi-member call cycle containing a symbol
instead: nodes are the cycle's members, edges every `calls` edge induced on
them; `--dst` is not used for this mode.

<!-- cppgraph-gen:explain export view -->
| Command | Purpose (from `--help`) | Arguments |
|---|---|---|
| `explain` | summarize a symbol: definition site, doc comment, source snippet, callers/callees | --graph, <symbol>, --root, --context N |
| `export` | export a viewable subgraph around a symbol as graphify-compatible graph.json (open it in viz/ or in graphify) | --graph, <symbol>, --depth N, --direction, --mode, --dst SYMBOL, --expand-paths, --limit N, --no-tests, --out PATH |
| `view` | one-shot visualize: build the subgraph, write a self-contained HTML to a temp dir, and open it in your browser | --graph, <symbol>, --mode, --dst SYMBOL, --expand-paths, --limit N, --depth N, --direction, --no-tests, --no-open |
<!-- /cppgraph-gen -->

```bash
cppgraph explain MyClass::method --root .    # definition, docs, snippet, callers/callees
cppgraph export Shape --mode usage --out shape-usage.json
cppgraph view Shape --mode usage --no-open   # writes the HTML, prints the path
cppgraph export parse --mode path --dst finalize --expand-paths --depth 1
```

---

Regenerating the tables after a CLI change: `.venv/bin/python
scripts/gen-cli-reference.py` (it fails with the names of commands not yet
placed in a section — curate their place, don't let it pass silently).
`tests/test_gen_cli_reference.py` checks the checked-in page stays fresh.
