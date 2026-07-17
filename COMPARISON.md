# cppgraph vs graphify vs Serena — a measured comparison

A case study on a large real-world C++ codebase. cppgraph is
project-agnostic; to measure at scale we use **MongoDB** as the example target —
specifically the `src/mongo/db/pipeline` subsystem (~770 C++ files), because it
contains a clean instance of the over/under-capture problem. Nothing here is
MongoDB-specific; any large C++ project with name collisions and virtual
dispatch shows the same effects.

The thesis of this project is that a **compiler index** (SCIP) gives *exact,
disambiguated* symbol identity, where a **by-name / tree-sitter** graph both
over-captures (merges distinct symbols) and under-captures (drops calls it can't
bind syntactically). This document tests that thesis on a real design question
against two other tools, with numbers you can reproduce.

## The design question

> _"I want to change how `ChangeStreamEventTransformation::makeResumeToken`
> builds resume tokens. **What calls this method** — and only this method, not
> the identically-named test helper? And what's the transitive blast radius?"_

This is a canonical over/under-capture case: `makeResumeToken` is really **two
distinct symbols** — a class method and a free test-helper function — that share
a name.

## The three tools

| Tool | Basis | How it answers "what calls X?" |
|---|---|---|
| **graphify** 0.9.16 | tree-sitter AST, symbols keyed **by name** | precomputed `graph.json`, by-name edges |
| **cppgraph** (this repo) | SCIP compiler index (`scip-clang` 0.4.0), symbols keyed by **USR / mangled id** | precomputed SQLite graph, compiler-exact edges + transitive queries |
| **Serena** (LSP) | clangd Language Server (v19.1.2), live | live `find_referencing_symbols`, one hop — index-permitting |

## Results

| Query | graphify | cppgraph | Serena / clangd |
|---|---|---|---|
| callers of the **method** `ChangeStreamEventTransformation::makeResumeToken` | **0** call edges | **3** (2 real overrides + 1 known decl false-positive) | **1** same-TU call site (see below) |
| callers of the **free helper** `change_stream_test_helper::makeResumeToken` | **0** call edges | **122** (test code) | needs whole-repo index (never completed in 6 min) |
| the two `makeResumeToken` kept distinct? | yes (file+class id) but with no call edges | **yes**, with correct separate caller sets | yes (compiler-grade) |
| `Value` (common nested type) | **431** unrelated calls collapsed onto **one** node | hundreds of distinct `Value` symbols kept separate | distinct |
| type `ResumeTokenData` usage | name-collisioned | 0 callers (it's a type) + **155 exact use-sites** via the reference index | refs, one hop, index-permitting |
| transitive blast-radius of the method | not supported | **14 symbols** in one query | N sequential LSP round-trips |
| latency to first cross-TU answer | instant (precomputed) | instant (precomputed) | **>6 min and counting** (background index) |

## What graphify got wrong (measured, not assumed)

graphify's graph on this subsystem: **17,789 nodes / 43,405 edges** (3,201 of
them `calls`). Two concrete failures:

1. **Under-capture — real calls dropped.** The method
   `ChangeStreamEventTransformation::makeResumeToken` has **zero** incoming
   `calls` edges; the free helper likewise. graphify sees the *definitions*
   (`contains`/`defines` edges) but never binds the actual call sites to them.
   For the design question, graphify's answer to "what calls this method?" is
   **"nothing"** — the two genuine `applyTransformation` overrides and the 122
   test call sites are simply absent.

2. **Over-capture — hundreds of distinct sites merged.** The single node
   `Value` (labelled from `document_source_tee_consumer.h:59`) has **431
   incoming `calls` edges** — every `.getValue()`, `serialize() → Value`,
   `parse() → Value` across the whole subsystem got attributed to one arbitrary
   `Value` node, because they all mention "Value" by name. `Value` is one of the
   most common types in MongoDB; a by-name graph collapses them all.

These are not bugs in graphify — they are the **inherent limit of keying a graph
by name from a syntactic AST**, which is exactly what this project set out to
avoid.

## What cppgraph gets right

Because edges come from the compiler's resolved symbol (USR), the two
`makeResumeToken` symbols carry **separate, correct caller sets** (3 vs 122),
`Value` stays hundreds of distinct symbols, and the transitive blast-radius
(`impact`) is one query returning 14 symbols. cppgraph also records **155 exact
use-sites** for the *type* `ResumeTokenData` — a symbol with **zero** call
edges, invisible to any call-graph, that a by-name tool collapses with every
other `ResumeTokenData` mention.

The one caveat we own: cppgraph reports **3** method callers where **2** are
genuine — the third is a known false-positive from the nearest-preceding
attribution heuristic against a member's in-class declaration. It's in the
*safe* direction (over-, never under-report on real function-body calls), and is
fixable once `scip-clang` emits `enclosing_range` (upstream PR #504). See
`DESIGN.md` § "Building calls".

## Serena (clangd / LSP) — measured, not assumed

Serena is **not** a by-name tool: it drives clangd (a Language Server), so on
*precision* it is compiler-grade like cppgraph — where it answers, it answers
correctly. graphify is the outlier, not Serena. But the LSP **query model** costs
you on a codebase this size, and we measured it.

We drove **Serena's own bundled clangd (v19.1.2)** directly against the MongoDB
checkout (same engine Serena's `find_referencing_symbols` uses), with mongo's
`compile_commands.json` and `--background-index`:

- **`callHierarchy/incomingCalls` on the method** returned in **~2.7 s** — but
  with **1** caller: the direct, same-translation-unit call site. The full
  override / virtual-dispatch caller set that cppgraph gives (3, of which 2 are
  genuine) lives in *other* TUs and needs the whole-project index.
- **`textDocument/references`**, polled over a **6-minute** background-index
  warmup, stayed at **1 reference, 0 cross-TU** the entire time. clangd indexes
  MongoDB's ~6000 TUs lazily in the background; that index simply does not finish
  in interactive time, so cross-file / whole-program answers never arrive within
  a usable budget.

> This matches the maintainer's lived experience ("Serena on mongo — I use it,
> but it's not very useful"): great for the file you're in, weak for
> whole-program structure on a large C++ tree, because the LSP index is the
> bottleneck.

So the real cppgraph-vs-Serena axis is the **query model**:

- **Serena = live, one-hop navigation** over a running clangd + the full source
  checkout. Excellent for "what's around the symbol I'm editing, right now, in
  sync with my edits". But *transitive* questions ("everything that transitively
  calls / derives from X", shortest call path, full blast radius) mean the caller
  drives the recursion with N sequential LSP round-trips — each waiting on an
  index that, on mongo, isn't there.
- **cppgraph = a precomputed, portable graph artifact.** The whole-project index
  is built **once** (full mongo: ~40 s incl. references), then transitive
  traversal, shortest-path and blast-radius are **first-class single queries**
  served instantly off B-tree indexes; responses are **token-budgeted** for an
  LLM loop; the graph is a self-contained file you can ship, diff, and query
  **offline** without clangd or the source tree; and it carries **reference
  locations for types** that have no call edges at all.

The trade-off cppgraph pays for that: the graph is a snapshot and goes stale
until refreshed (`cppgraph status --root` detects drift against the indexed
commit and points at `reindex.sh --update`). Serena is always in sync with the
working tree.

## Token cost: cppgraph vs a grep-and-read loop

The tool an LLM actually reaches for first isn't graphify or Serena — it's
`grep`. So the most practical comparison is: how many **tokens** does it cost to
answer a dependency question each way? (Fewer tokens ingested = cheaper, faster,
and more room left in the context window.)

Same question, *"who calls the **method** `makeResumeToken`?"*. `makeResumeToken`
resolves to **four** distinct symbols across `src/mongo` (a method, two
test-helper free functions, an anonymous-namespace test symbol). Measured with
`scripts/measure_tokens.py` — the cppgraph rows count the **MCP tool JSON**, i.e.
what the LLM ingests:

| Approach | Chars | ≈ Tokens\* | Correct? |
|---|---|---|---|
| `grep -rn makeResumeToken src/mongo` (untargeted) | 26,540 | ~6,600 | ✗ 4 symbols merged; decls/comments/strings; needs file reads to disambiguate |
| grep on the known subtree `.../db/pipeline` (best case) | 23,569 | ~5,900 | ✗ same problems; targeting barely helps |
| cppgraph `find` (1,009) + `who_calls` on the method (613) | 1,622 | **~400** | ✓ exact — the method's 3 callers, nothing else |

**~16× fewer tokens** (untargeted) / ~15× (even against the best-case targeted
grep) — and exact, where grep is ambiguous *and* still needs follow-up file
reads. `find` alone is the step grep can't do: it splits the name into its four
real symbols with their definition sites.

**Where those tokens go — and the token-lean defaults.** Each fan-out hit could
carry the raw 150-250-char SCIP symbol string; instead the tools ship a readable
label derived from that string (`full_symbols=True` to opt out) and drop test
callers (`exclude_tests=False` to keep them). On a hub symbol the two compound.
`who_calls(ResumeToken::parse)` (`scripts/measure_tokens.py ResumeToken … 'ResumeToken#parse'`):

| who_calls payload | Chars | ≈ Tokens | |
|---|---|---|---|
| raw SCIP strings + test callers kept (pre-optimisation) | 11,102 | ~2,780 | 100 callers |
| + drop test callers | 3,472 | ~870 | 13 production callers (−69%) |
| + derive labels from SCIP (**default**) | 1,998 | ~500 | −42% again → **~5.5× leaner** overall |

The flip side, kept honest: a symbol with many *genuine* production callers still
costs more in cppgraph — but that *is* the complete, attributed answer; grep's
6,600 tokens don't contain an attributed caller list at all. The token win is in
disambiguation + targeting a specific symbol, which is the actual question.

\* **Method & honesty.** Tokens ≈ **characters ÷ 4** (`scripts/measure_tokens.py`,
tunable). That's the rough rule for prose; code and SCIP symbol strings (heavy
punctuation, hex hashes, paths) tokenize *denser* (~3–3.5 chars/token), so true
counts are **higher on both sides** — deliberately conservative, ratio stable. No
exact tokenizer is used: Claude's isn't available offline, and a proxy (e.g.
tiktoken's `o200k_base`) would shift both sides similarly. grep is scoped to all
of `src/mongo` (the realistic case — the LLM isn't told how to scope). cppgraph
pays a one-time index (~minutes) amortized over every later query.

## Verdict — when to use which

- **graphify**: fast, language-agnostic, zero build setup, nice clustering/viz.
  Good for a rough map. **Not** trustworthy for "exactly what calls this symbol"
  in a large C++ codebase with name collisions — it will both miss real edges
  and invent merged ones.
- **Serena / LSP**: best for **live, interactive** navigation and refactoring
  while editing, always in sync with the working tree. One hop at a time.
- **cppgraph**: best for **compiler-exact, transitive, offline** structural
  questions — "what is the full blast radius of changing X?", "show every path
  from A to B", "every exact use-site of this type" — and for feeding those
  answers to an LLM within a token budget (MCP). Costs an index + build step and
  goes stale until refreshed (`cppgraph status --root` detects drift).

## Reproduce

```sh
# cppgraph (pipeline graph already built at scratch/pipeline_refs.graph.db)
.venv/bin/cppgraph find makeResumeToken --graph scratch/pipeline_refs.graph.db
.venv/bin/cppgraph callers '<method symbol>' --graph scratch/pipeline_refs.graph.db
.venv/bin/cppgraph impact  '<method symbol>' --graph scratch/pipeline_refs.graph.db
.venv/bin/cppgraph references '<ResumeTokenData# symbol>' --graph scratch/pipeline_refs.graph.db

# graphify (on a copy of the sources, outside the mongo repo — it writes graphify-out/)
cp -R <mongo>/src/mongo/db/pipeline /tmp/gp && cd /tmp/gp
graphify update . --no-cluster            # → graphify-out/graph.json
graphify explain "makeResumeToken"        # inspect nodes/edges

# token cost: grep (whole tree + a known subtree) vs cppgraph find+who_calls
.venv/bin/python scripts/measure_tokens.py makeResumeToken \
  <mongo>/src/mongo <mongo>/.cppgraph/mongo.graph.db \
  <mongo>/src/mongo/db/pipeline 'ChangeStreamEventTransformation#makeResumeToken'
```

The Serena/clangd numbers were produced by driving Serena's bundled clangd
(`~/.serena/language_servers/.../clangd_19.1.2`) over stdio LSP against mongo's
`compile_commands.json` — `callHierarchy/incomingCalls` and
`textDocument/references` on the method, polled during background indexing. The
throwaway probe scripts live under the job tmp dir, not committed.
