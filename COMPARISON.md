# cppgraph vs graphify vs Serena — a measured comparison

A case study on a large real-world C++ codebase. cppgraph is
project-agnostic; to measure at scale we use **MongoDB** as the example target —
specifically the `src/mongo` tree, because it contains a clean instance of the
over/under-capture problem (`ChangeStreamEventTransformation::makeResumeToken`,
a method that shares its name with free test helpers). Nothing here is
MongoDB-specific; any large C++ project with name collisions and virtual
dispatch shows the same effects.

The thesis of this project is that a **compiler index** (SCIP) gives *exact,
disambiguated* symbol identity, where a **by-name / tree-sitter** graph both
over-captures (merges distinct symbols) and under-captures (drops calls it can't
bind syntactically). This document tests that thesis on a real design question
against two other tools, with numbers you can reproduce.

> **This revision (2026-09-23) is a full re-measurement, not an update of the
> old numbers.** Three things changed. (1) Two claims in the previous document
> were wrong and are corrected here: it described its cppgraph graph as
> "`src/mongo`, 5416 TUs" while that graph actually covered the *whole* tree
> including `src/third_party` (~7600 TUs) — every count below is stated with its
> exact scope; and it concluded clangd's index "never finishes in interactive
> time" — measured properly, it reaches **99 % in ~75 minutes**. (2) The audit
> found a real cppgraph **under-capture** (calls in macro-introduced definitions
> were dropped); it is **fixed in this revision** by a new scip-clang patch
> (patchset 7), and § "Where cppgraph is wrong too" reports it with before/after
> numbers. (3) clangd is now measured both cold and warm.

## Versions compared (all measured 2026-09-23)

| Tool | Version | Released | Basis |
|---|---|---|---|
| **cppgraph** (this repo) | 0.4.3 | 2026-09-23 | SCIP compiler index (`scip-clang` 0.4.0, patched, patchset 7) |
| **graphify** | 0.9.66 | 2026-09-22 | tree-sitter AST, no compiler |
| **Serena** | 1.7.0 | 2026-08-09 | clangd 19.1.2 Language Server (live) |

Target: MongoDB `d2afb4f` (checkout dated 2026-06-29), `compile_commands.json`
regenerated from the current build graph. Nothing in `src/mongo` changed between
the previous measurement and this one, so the deltas below come from the tools,
not the code.

## The design question

> _"I want to change how `ChangeStreamEventTransformation::makeResumeToken`
> builds resume tokens. **What calls this method** — and only this method, not
> the identically-named test helper? And what's the transitive blast radius?"_

This is a canonical over/under-capture case: `makeResumeToken` is really
**four distinct symbols** across `src/mongo` — the class method, a free
test-helper function, a second helper (`makeResumeTokenWithEventId`), and an
anonymous-namespace test function — three of which share the name.

## Results

cppgraph graph for this measurement: **814,073 nodes, 2,677,348 `calls`/
`inherits` edges, 5,284,223 references** (scope: `src/mongo`, 5416 TUs, tests
included, plus the bazel-generated sources and the handful of third-party TUs
the tree reaches; `src/third_party` otherwise excluded). Built in **~27 minutes**
in one pass on 14 cores; store 592 MB (from an 864 MB `.scip`).

| Query | graphify | cppgraph | Serena / clangd |
|---|---|---|---|
| `find` splits the name | no (by-name) | **4 distinct symbols** | resolves one at a time, live |
| callers of the **method** | **0** call edges | **2** real callers | **1** (same-TU only; stays 1 even fully indexed) |
| callers of the **free helper** | **0** call edges | **122** (attributed to the generated test `TestBody`) | needs the whole-repo index; never the full set |
| the two `makeResumeToken` kept distinct? | yes (file+class id), but no call edges | **yes**, with correct separate caller sets | yes (compiler-grade) |
| `Value` (ubiquitous nested type) | **collapsed** — see below | distinct symbols, keyed by USR | distinct |
| type `ResumeTokenData` usage | 35 `references` edges on one node | 0 callers (it's a type) + **177 exact use-sites** | **182** references once fully indexed (12 before) |
| transitive blast-radius of the method | `affected` finds **nothing** | **2** symbols, one query | N sequential LSP round-trips |
| latency to first cross-TU answer | instant (precomputed) | instant (precomputed) | >70 min to index; instant **after** that (warm) |

## graphify — measured, not assumed

graphify 0.9.66 has grown a lot since the previous measurement (path, affected,
god-nodes, communities, exports, hooks, watch). Its basis has not changed: it
parses C++ with **tree-sitter** (`tree_sitter_cpp` 0.23.4), with **no compiler
integration** — a grep for `libclang`/`compile_commands`/`clangd` in the package
returns nothing, and its SCIP-JSON ingest module is explicitly "not wired to the
CLI".

On a copy of `src/mongo/db/pipeline` (the previous document's scope), graphify
builds **18,847 nodes / 43,461 edges** in ~20 s (2,778 of them `calls`):

1. **Under-capture — real calls dropped.** The method
   `ChangeStreamEventTransformation::makeResumeToken` has **zero** incoming
   `calls` edges; the free helper likewise. graphify sees the *definitions*
   (`contains`/`defines` edges) but never binds the actual call sites to them.
   For the design question, its answer is **"nothing"**. `graphify affected
   makeResumeToken` reports "No affected nodes found", and `graphify path
   applyTransformation makeResumeToken` finds no directed path.

2. **Over-capture — distinct sites merged into one node.** The single
   `Value` node (labelled from `document_source_tee_consumer.h`) carries
   **102 incoming `calls` edges**, 699 `references` and 237 `imports` — every
   `.getValue()`, `serialize() → Value`, `parse() → Value` in the subsystem
   attributed to one arbitrary node, because they all mention "Value" by name.
   `Value` is one of the most common types in MongoDB.

graphify does try to type member calls via a **per-file `var → ClassName`
table**, and it guards against god-nodes: a receiver type that doesn't resolve
to exactly one definition produces **no edge** ("a false call edge is worse
than a missing one"). Free calls fall back to a lowercased bare-name lookup
with tie-breakers, then no edge. That is a reasonable design for a syntax-only
tool — but it is still by-name, so `makeResumeToken` resolves to nothing
(ambiguous) and `Value` collapses.

On the **full `src/mongo` tree** (10,089 source files, `.inl`/`.ipp` skipped),
graphify builds **222,221 nodes / 550,268 edges** in **~112 s**, no LLM, no
network. The `Value` collapse is worse there, not better: **583 distinct nodes
labelled `Value`**, each carrying a handful of unrelated `references`. The
"most connected nodes" it reports are `unique_ptr` (4,391 edges),
`NamespaceString()` (3,735), `ExpressionContext()` (2,064) — generic names, as
expected for a by-name graph.

## Serena (clangd / LSP) — measured, not assumed, and measured both ways

Serena is **not** a by-name tool: it drives clangd (a Language Server), so
where it answers, it answers with compiler precision. graphify is the outlier,
not Serena. The previous document measured only the first six minutes of
clangd's background index and concluded it was unusable. We measured it
properly, in both states, because a tool's *first-run* behaviour matters as much
as its best case.

We drove **Serena's bundled clangd 19.1.2** directly against the MongoDB
checkout (the same engine Serena's `find_referencing_symbols` uses), with
mongo's `compile_commands.json`, `--background-index`, 14 workers, from a cold
index:

- **Cold, first use (t = 0).** The first query returns in **~2.5 s** — Serena
  sets `server_ready` immediately and waits a fixed 2 s, once, before the first
  cross-file request; it does **not** wait for the index and does **not** say
  the answer is incomplete. Measured: **1** caller of the method (the same-TU
  call site) and **12** references of `ResumeTokenData` — against cppgraph's
  **2** and **177**. This is the real first-run experience: confident,
  immediate, incomplete.
- **Indexing.** clangd indexes 7,587 files, reaching **99 % only at ~74
  minutes**. Throughout, `textDocument/references` on `ResumeTokenData` climbs
  (12 → 55 → 179 → **182**); `callHierarchy/incomingCalls` on the method stays
  at **1** the entire time (the cross-TU virtual-dispatch callers are never
  returned by clangd's call hierarchy here).
- **Essentially complete (99 %).** References converge to **182** for `ResumeTokenData` —
  essentially matching cppgraph's **177**. So on *references*, clangd is
  correct once warm; the old document, stopping at six minutes, understated it.
  Callers of the method: still **1**.
- **Warm restart.** clangd caches its index under `.cache/clangd`. On the next
  start, references are complete within **~0.5 s** (12 → 182 on the first poll).
  So the cold-index cost is paid **once per checkout**, and only when clangd's
  fingerprint still matches (it includes the md5 of `compile_commands.json`); a
  regenerated compdb or a `git pull` invalidates it.

Two structural facts follow, independent of timing:

- **Serena exposes no call hierarchy.** `serena tools list` has
  `find_referencing_symbols` (→ `textDocument/references`) but nothing that
  consumes `callHierarchy/incomingCalls`/`outgoingCalls`. Transitive questions
  ("everything that transitively calls X", shortest call path, blast radius)
  mean the caller drives the recursion with N sequential LSP round-trips.
- **`find_symbol` is a whole-project `documentSymbol` walk**, not a
  `workspace/symbol` query — slow on a large tree.

Serena's real strength is elsewhere, and cppgraph has no equivalent: it is an
**editing** toolkit bound to the live working tree — `rename_symbol`,
`replace_symbol_body`, `safe_delete_symbol`, `find_implementations`,
diagnostics — always in sync with your unsaved edits. cppgraph is a read-only
snapshot; Serena is a live editor.

## Where cppgraph is wrong too

This project's whole premise is "measure, don't guess", so the audit cuts both
ways. Re-indexing with the patched (#504) binary exposed a real
**under-capture**, and a scope/attribution change between the previous
measurement and this one:

**1. Calls inside macro-generated definitions *were* dropped — fixed this
revision.** scip-clang emits `enclosing_range` (each definition's body extent),
and cppgraph attributes a call to the innermost callable definition whose
extent contains it. The #504 patch records that extent from
`decl.getSourceRange()` — but for a definition introduced through a macro (a
gtest `TEST`/`TEST_F` body, or a function carrying a leading macro like
`MONGO_COMPILER_ALWAYS_INLINE`) that range's *begin* is the macro's spelling
location, in the macro's own file. The range is cross-file, so #504's same-file
guard skipped it, and every call inside the definition was dropped. Measured on
the pre-fix graph: callers of the free helper
`change_stream_test_helper::makeResumeToken` = **1** (its 122 *references* were
already exact), and `MONGO_COMPILER_ALWAYS_INLINE static void
Ordering::verifyCardinality` (`bson/ordering.h:137`) = **0** callees instead of
8. Net on `src/mongo`: test-side `calls` edges **763,800 → 217,039 (−72 %)**,
production-side **−7 %**.

The old (stock) behaviour masked this by attributing to the *nearest preceding*
callable — a caller was produced, but often the **wrong** one (for a gtest
`TEST`, an arbitrary sibling method of the generated class, e.g. its
destructor); cppgraph's `ForwardDefinition` patch had removed exactly those
phantoms, so the #504 graph chose to drop rather than re-fabricate.

**Fixed in patchset 7** (`scip-clang-patches/enclosing-range-macro-on-v0.4.0.patch`):
when a declaration's `getSourceRange()` is cross-file, fall back to the function
**body** extent — written where the definition is, so single-file. Same graph,
after: the helper again has **122** callers, now attributed to the **correct**
generated `TestBody` (`..._Test#TestBody`), and `verifyCardinality` again has
**8** callees. Test-side `calls` edges recover to **713,554**; the residual
**−6 %** versus stock (production 587,031 → 550,717) is the intended removal of
the declaration-site/sibling phantoms — a smaller, safer graph, not a loss.

**2. `impact` follows `calls` edges, not virtual dispatch.** The blast radius of
`makeResumeToken` (a virtual override) is reported as **2** callers; clangd's
call hierarchy likewise returns **1**. The base-class virtual call sites
(`ChangeStreamEventTransformer::applyTransformation` → the virtual override) are
reached over a vtable, which no static call graph — cppgraph's, graphify's, or
clangd's — resolves here. The graph is a *lower bound* on runtime reachability.

**3. Scope honesty.** The previous graph's node/edge counts (975,932 / 3,066,339)
were for the **whole tree**; this measurement's (814,073 / 2,677,348) are for
`src/mongo`. Every number in this document carries its scope; do not compare
counts across the two documents without checking it.

## Token cost: cppgraph vs a grep-and-read loop

The tool an LLM actually reaches for first is `grep`. So the practical question
is: how many **tokens** does it cost to answer *"who calls X?"* each way?

The honest grep cost is **not the raw match dump** — grep can't tell a call from
a declaration, a comment, a string, or a different symbol sharing the name, so
to answer it must **read around every hit** (`grep -C 10`, conservative).
cppgraph's cost is the **MCP tool JSON** the LLM ingests: `find` (which splits
the name into distinct compiler symbols) + `who_calls` on the one you mean.

One question across the whole spectrum, reproducible with
`scripts/measure_tokens.py --suite` (which now mirrors the tool's 40-result
`find` cap — the previous numbers were inflated by comparing an uncapped `find`
against the capped tool):

| Regime | Symbol (`who calls …?`) | grep raw | grep + read | cppgraph | grep noise | Verdict\*\* |
|---|---|---:|---:|---:|:---:|:---:|
| **Rare unique name** — grep's best case | `setBlockNewUserShardedDDL` | 94 | 1,836 | 254 | 0 % | grep wins raw; **7× loss** on read |
| | `_amIFreshEnoughForPriorityTakeover` | 105 | 1,976 | 184 | 67 % | **11×** |
| **Real method** | `ChangeStreamEventTransformation::makeResumeToken` | 6,635 | 110,857 | 432 | 98 % | **257×** |
| **Real class / method** | `ResumeToken::parse` | 68,651 | 598,711 | 2,724 | 99 % | **220×†** |
| | `PlanExecutor::getPostBatchResumeToken` | 43,145 | 419,162 | 3,025 | 100 % | **139×†** |
| | `BSONObjBuilder::obj` (4000+ callers) | 281,594 | 4,037,937 | 9,515 | 99 % | **424×†** |
| **Ubiquitous type name** | `NamespaceString::NamespaceString` | 717,673 | 8,015,288 | 8,203 | 98 % | **977×†** |
| | `OperationContext::getClient` (873 callers) | 973,323 | 11,952,684 | 6,245 | 100 % | **1,914×†** |

**†** = theoretical multiplier: grep + read exceeds one context (~200k tok), so
nobody ingests it; the number shows the **scale** of what grep would need. In
practice grep truncates the dump and answers from a partial, unverified view —
**neither correct nor complete**, with no signal that anything was dropped. The
rare-name rows (no †) are real, ingestible costs.

**Latency.** Per query (best of 3, warm cache): `grep -rn` scans `src/mongo` in
**~1.7 s**; cppgraph's `find` + `who_calls` returns in **~0.15 s** off the
prebuilt store (in-process, what the MCP path pays; the CLI adds ~0.8 s of
Python startup, still ~2× faster than grep end-to-end). Neither counts the time
the LLM spends *consuming* the returned tokens, which is proportional to the
token columns above.

**Worked example — `makeResumeToken`.** The method resolves to **4** distinct
symbols. grep dumps **156 lines / ~6,635 tokens**, of which **2** are real call
sites (**98.7 % noise**); reading around all 156 to trust those 2 costs
**~110,857 tokens**. cppgraph: `find` (304 tok) + `who_calls` on the method
(128 tok) = **~432 tokens**, exactly the 2 callers. **257× leaner, and exact
where grep is ambiguous.**

**Method.** tokens ≈ **characters ÷ 4** (`scripts/measure_tokens.py`, tunable) —
conservative for code (SCIP strings tokenize denser, so true counts are higher
on both sides; ratios are stable). grep is scoped to all of `src/mongo`.
cppgraph pays a one-time index (~27 min) amortized over every later query.

## Capability matrix — what each tool can actually do

cppgraph exposes its query surface identically as CLI subcommands and MCP tools
(`AGENTS.md`'s parity rule): 23 query commands + 7 operational ones on the CLI,
24 MCP tools. `graphify` and `Serena` are each strong in a different place.

| Capability | cppgraph | graphify 0.9.66 | Serena 1.7.0 |
|---|:---:|:---:|:---:|
| Compiler-exact caller/callee (`calls`) | **yes** | by-name, drops/mis-binds | via `references` only |
| Exact use-sites of a **type** (`references`) | **yes** | noisy (`references` edges) | yes (warm) |
| Callees / callers, one hop | yes | partial | one hop |
| Transitive impact / forward reachability | **yes, one query** | `affected` (CLI only) | N round-trips |
| Shortest call path A→B | **yes** | `path` (by-name) | no |
| Fan-in / fan-out ranking (`hotspots`) | **yes** | `god-nodes` | no |
| Cycle detection (`strongly-connected-components`) | **yes** | no | no |
| Architecture / layering conformance | **yes** | no | no |
| Module API surface (external uses) | **yes** | no | no |
| Dependency cost (call sites into a library) | **yes** | no | no |
| Global-init-order hazards | **yes** | no | no |
| Largest bodies (`line-span`), zero-caller defs | **yes** | no | no |
| Class members / base / subclasses | **yes** | partial | partial (`find_symbol`) |
| File outline / symbol search | `find`, `outline` | `query`, `explain` | `find_symbol` |
| Documentation in the graph | yes (docstrings) | no | no (reads source) |
| Editing (`rename`, body replace, safe delete) | **no** | no | **yes** |
| Live working-tree sync | no (snapshot) | via `watch`/hooks | **yes** |
| Visualisation / export | 1 HTML viewer | **html, svg, graphml, obsidian, wiki, neo4j…** | no |
| Offline, no server needed | **yes** | yes | no (runs clangd) |
| Token-budgeted LLM output | **yes (MCP)** | yes (MCP) | tool results |

### Detail on cppgraph's whole-graph queries (all on the 814k-node graph)

- **Layering (`boundary-violations`)**: 2,867 real edges from
  `src/mongo/client/` into `src/mongo/db/`, ~1.5 s — a rule engine over exact
  edges neither other tool has.
- **Cycles (`strongly-connected-components`)**: 130 groups in mongo's own
  code (excluding `third_party` and tests), ~1 s. A whole-graph query by construction;
  LSP answers one symbol at a time.
- **API surface (`api-surface`)**: 1,602 symbols actually used from outside
  `src/mongo/db/pipeline/`, ~5.6 s — the *observed* external surface.
- **Dependency cost (`dependency-cost`)**: 14,630 call sites into abseil-cpp
  from 1,643 target symbols, ~3.5 s.
- **Fan-in (`hotspots`)**: 45,529 symbols ranked in ~4 s (excluding tests and
  `third_party`).
- **Body size / dead-ish code**: `line-span` ranks 176,510 definitions (largest
  5,480 lines); `no-incoming-calls` reports 108,265 defined callables with zero
  *static* callers — a graph fact with a standing caveat, never a bare
  dead-code verdict.
- **Global-init hazards (`global-init-references`)**: correctly returned **0**
  for `Expression::parserMap` — a precise negative, not a failure to find one
  (and, per finding 1 above, it would miss reads inside macro-wrapped
  initializers).

## Verdict — when to use which

- **graphify**: fast, language-agnostic, zero build setup, and a rich
  visualisation/export surface. Good for a rough map and for non-C++ corpora.
  **Not** trustworthy for "exactly what calls this symbol" in a large C++
  codebase with name collisions — it both misses real edges and merges
  same-named ones.
- **Serena / clangd**: best for **live, interactive** navigation and
  refactoring **while editing**, always in sync with the working tree, and the
  only one of the three that edits code (`rename_symbol`, `replace_symbol_body`,
  `safe_delete_symbol`). On a large tree, its first-run answers are fast and
  **silently incomplete** until the background index finishes (~75 min raw,
  then cached); it exposes no call hierarchy, so transitive questions cost N
  round-trips.
- **cppgraph**: best for **compiler-exact, transitive, offline** structural
  questions — "what is the full blast radius of changing X?", "show every path
  from A to B", "every exact use-site of this type", plus the whole-graph
  questions neither other tool answers at all (layering conformance, cycles,
  API surface, dependency cost, fan-in ranking). Costs a one-time index
  (~27 min for `src/mongo`) and is a snapshot that goes stale until refreshed
  (`cppgraph status --root` detects drift; `update --rescope` widens scope
  without a full reindex). Its own limitations: calls in macro-generated bodies
  (gtest `TEST`, macro-decorated inlines) are dropped, and `impact` does not
  follow virtual dispatch.

## Reproduce

```sh
# cppgraph (from the indexed checkout's parent; graph auto-discovered)
cppgraph init <mongo>/compile_commands.json -y --filter src/mongo \
  --attributed-refs --run --name mongo_p7 --project-root <mongo>
cppgraph status                              # scope, schema, usage view
cppgraph find makeResumeToken
cppgraph callers 'ChangeStreamEventTransformation::makeResumeToken'
cppgraph references 'ResumeTokenData'        # 177 exact use-sites

# token cost — whole spectrum (grep raw / grep+read / cppgraph), one table
python scripts/measure_tokens.py --suite <mongo>/src/mongo \
  <mongo>/.cppgraph/mongo_p7.graph.db

# whole-graph queries (§ "Detail on cppgraph's whole-graph queries")
cppgraph boundary-violations --rule src/mongo/client/:src/mongo/db/
cppgraph strongly-connected-components --exclude-tests --exclude-path src/third_party
cppgraph api-surface src/mongo/db/pipeline/ --exclude-tests
cppgraph dependency-cost --target-path src/third_party/abseil-cpp/ --exclude-tests

# graphify (on a copy outside the mongo repo — it writes graphify-out/)
cp -R <mongo>/src/mongo/db/pipeline /tmp/gp && cd /tmp/gp
graphify update . --no-cluster               # → graphify-out/graph.json
graphify explain makeResumeToken             # inspect nodes/edges
graphify affected makeResumeToken            # reverse traversal
graphify path applyTransformation makeResumeToken
# full tree: cp -R <mongo>/src/mongo /tmp/gm && cd /tmp/gm && graphify update . --no-cluster

# Serena / clangd — drive the bundled clangd 19.1.2 over stdio LSP, or via Serena
clangd --compile-commands-dir=<mongo> --background-index   # then
#   textDocument/references on ResumeTokenData  (12 cold → 182 fully indexed)
#   textDocument/prepareCallHierarchy + callHierarchy/incomingCalls on the method
#   (Serena itself exposes only find_referencing_symbols → references; no call hierarchy)
```

The Serena/clangd numbers were produced by driving clangd 19.1.2 over stdio LSP
against mongo's `compile_commands.json` with `--background-index -j=14`, polling
`callHierarchy/incomingCalls` and `textDocument/references` once a minute until
the index reached 99 %, then again from a cold restart against the warm cache.
graphify's numbers are its own CLI on copies of `db/pipeline` and of the full
`src/mongo` tree. The throwaway probe scripts live under the job tmp dir, not
committed.
