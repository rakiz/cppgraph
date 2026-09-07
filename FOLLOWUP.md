# FOLLOWUP — possible extension, not yet scoped

Not `TODO.md`: that's for decided, active work. This is a parking lot for a
direction not yet scoped. Nothing here is committed to.

## Problem

An agent repeats a mistake it was already corrected on in a past session — the
correction isn't recorded anywhere the agent re-reads before acting.

## Idea

Anchor a correction to the exact SCIP symbol it's about (not a line number, not
an embedding) — cppgraph already resolves symbols exactly, so staleness is free:
a symbol either still resolves in the graph or it doesn't.

Sketch:

- One new table in the existing SQLite store: `notes(symbol_id, text, author,
  created_at, status)`.
- A small MCP tool to record one (`record_note(symbol, text)`), surfaced
  inline in `explain_symbol`'s existing response.
- No new database, no vector/semantic search, no review UI, no build/test
  runner for a first cut — record on the spot, surface on tools already used,
  git history as the audit trail.

## Open questions

1. Part of cppgraph, or a separate tool that depends on it for anchoring?
   cppgraph's ethos is facts-not-judgments; a "correction" is a judgment, not
   a compiler fact — different trust model.
2. What triggers recording one — manual only, or some automatic capture?
3. Does "stale" (symbol no longer resolves) mean delete the note or just flag it?

## If it proves useful

1. Table + `record_note` + surfaced via `explain_symbol`.
2. A way to list/browse notes (`cppgraph notes`, plain listing).
3. Only then, if genuinely recurring friction: search, review workflow,
   automatic capture — each justified by an observed need.

---

## `typed-by` edges (`Relationship.is_type_definition`)

### Problem

TODO.md asked for a `typed-by` edge kind (field/variable -> its type) via SCIP's
`Relationship.is_type_definition`, to answer "who has a field of this type?"
traversably. Implemented it (builder + `impact_of`/`reachable_from`/
`boundary_violations` wiring) and verified it end-to-end — but `scip-clang`
v0.4.0 (including our own #504 build) **never sets this field**. Confirmed two
ways: 0 of 810,919 relationships on a real MongoDB index
(`scratch/mongo_src_tests.scip`, checked via
`python -c "from cppgraph.proto import scip_pb2; ..."` counting
`rel.is_type_definition` across every document/symbol — same one-liner as
`scratch/`'s other introspection scripts), and 0 on a fresh hand-built test TU
indexed with `~/.local/share/cppgraph/bin/scip-clang` (our own #504 binary,
confirmed via its `scip-clang.json` sidecar `variant: enclosing_range-504` —
so this isn't a stock-vs-#504 artifact, the #504 patch never touched this).
`set_is_type_definition` does not appear anywhere in `indexer/Indexer.cc`
(cloned locally at `~/.cache/cppgraph/scip-clang-src`, same checkout
`scripts/build-scip-clang-macos.sh` uses) — it's not a rare case, the indexer
simply never computes it. So the feature is correct but produces zero edges
on every graph anyone can build today — dead code. Reverted rather than
committed; not worth carrying a `typed-by` choice in every `--kind` flag/
validation list for a kind that can never appear.

### What it would take to fix upstream (or patch ourselves)

Small. `is_implementation` (which DOES work, powers `inherits`/`implements`)
is set in exactly 2 places in `Indexer.cc` (~line 547, ~line 748), same
pattern each time: resolve the related type's `Decl` to a SCIP symbol via the
already-existing `symbolFormatter.getNamedDeclSymbol(...)` helper, attach a
`scip::Relationship`. `saveFieldDecl` (~line 510, ~10 lines today) is the
natural place to add the field-type case; `saveVarDecl` (~line 931) similarly
for variables. Estimate: ~20-40 lines across those two functions, no new
infrastructure — same shape/size as the `enclosing_range` patch
(`docker/build-scip-clang/enclosing_range-on-v0.4.0.patch`) we already
maintain. A clean small upstream PR, or an equally small local patch in the
meantime (same recipe: clone at the pinned tag, patch, rebuild via
`scripts/build-scip-clang-macos.sh` / `docker/build-scip-clang/`).

### Is it actually worth doing?

Modest value, not the "full type-change blast radius" the TODO implied.
`find_references(TypeX)` **already** surfaces every field/variable/parameter
typed as `TypeX` today — SCIP records a reference occurrence at every mention
of a type, `typed-by` or not. The real gain is a clean, dedicated, traversable
edge instead of `find_references`' undifferentiated occurrence list. But
`impact_of`/`reachable_from` only walk **one edge kind per call** today — there's
no multi-kind/combined traversal — so `typed-by` alone doesn't chain into a
call-graph blast radius either; that would need a separate "walk kind X then
kind Y" feature that doesn't exist. Net: "who has a field of type X, as a
clean single-hop fact" — real, but narrow.

### If it proves useful

1. Patch `scip-clang` (upstream PR first choice; a maintained local patch,
   mirroring `enclosing_range-on-v0.4.0.patch`, if upstream stalls) to actually
   set `is_type_definition` in `saveFieldDecl`/`saveVarDecl`.
2. Re-apply the `cppgraph` side — it was correct and fully tested before being
   reverted; this is the exact checklist, not a redesign:
   - `builder.py`: in the `sym_info.relationships` loop, alongside the existing
     `if rel.is_implementation:` block, add `if rel.is_type_definition:
     graph.add_edge("typed-by", sym_info.symbol, rel.symbol, doc.relative_path)`
     — a separate `if`, not `elif` (the proto doesn't make the flags exclusive).
   - `store.py`: add `"typed-by"` to `boundary_violations`' edge-kind validation
     set (the `unknown = sorted(set(edge_kinds) - {...})` check).
   - `cli.py`: add `"typed-by"` to the `--kind` `choices=` tuples on `impact`,
     `reachable-from`, and `boundary-violations`'s subparsers; extend `impact`'s
     output-verb mapping (was a `calls`/`inherits` ternary -> a 3-entry dict,
     `"typed-by": "are typed as"`).
   - `model.py`: extend the `Edge.kind` comment.
   - `impact`/`reachable_from`'s type-on-`kind="calls"` redirect special-cases
     are unaffected (they only fire for `kind == "calls"` specifically) — no
     change needed there, confirmed by test at the time.
   - Do NOT touch `strongly_connected_components` or `shortest_call_path` —
     both hardcode `kind = 'calls'` with no kind parameter on any surface;
     out of scope then and still out of scope now.
   - Tests: mirror `is_implementation`'s existing builder/store test fixtures
     for the `inherits`/`implements` edges — same shape, `typed-by` direction
     confirmed (src=field/var, dst=type) via `GraphStore.impact(type,
     kind="typed-by")` returning the fields typed as it.
3. Only then consider whether a combined-kind traversal (calls + typed-by in
   one blast-radius query) is worth the added complexity — a separate,
   bigger design question, gated on this landing first.

---

## Patch `scip-clang` to emit symbols/`enclosing_range` for lambda bodies

### Problem

Confirmed while building `global_init_references`: `scip-clang` never visits a
lambda's closure declaration. `IndexerAstVisitor` (`indexer/AstConsumer.cc`)
doesn't override `TraverseLambdaExpr`, so Clang's default traversal walks
straight into the lambda's *body statements* but never calls `TraverseDecl` on
the closure class (`clang::LambdaExpr::getLambdaClass()`, a `CXXRecordDecl`)
or its implicit `operator()`. `saveFunctionDecl`/`saveRecordDecl` — the
functions that would emit a symbol and its `enclosing_range` — are never
invoked for it. Net effect: a lambda has no symbol of its own and no interval,
so anything referenced *inside* one attributes to whatever OUTER interval
happens to contain it (a global's initializer, or the enclosing function),
never to the lambda itself.

Verified empirically this session with `int g = compute([]() { return
other_global; });` on the #504 binary: no symbol, no interval for the lambda;
`other_global`'s reference attributes to `g` (see the `global_init_references`
CHANGELOG entry / its standing caveat note for the shipped, honestly-labeled
consequence).

### Why this is worth doing (not just a curiosity)

Lambdas are everywhere in modern C++ (STL algorithms, callbacks, async
code, `std::function` handlers) — this isn't a rare edge case, it's a
routine, load-bearing C++ idiom. The gap is broader than the one caveat
`global_init_references` currently carries:

- **Inside a global's initializer** (the case that surfaced this): a read
  inside a lambda is misattributed to the global — the specific caveat
  `global_init_references` ships today. Fixing scip-clang removes that
  caveat entirely, not just documents around it.
- **Inside a function body** (the far more common case): a call or reference
  inside a lambda passed to e.g. `std::sort`/`std::for_each` today attributes
  to the *enclosing function*, not the lambda — so `who_calls`/`impact_of`
  chains blur a lambda's own logic into its host function's, `no_incoming_calls`
  can never say "this lambda has no callers" as its own fact (it isn't a
  symbol at all), and `line_span`/`outline` can't size or list a lambda as
  its own definition. Every #504-gated tool that depends on precise
  definition boundaries is quietly less precise wherever a codebase uses
  lambdas — which in current C++ is most of it.

Fixing this at the source (scip-clang) benefits every one of those tools at
once, for free, the moment a graph is rebuilt — no per-tool workaround needed
on the cppgraph side, consistent with "consume SCIP, don't reimplement a C++
parser" (`AGENTS.md`).

### What the fix looks like, and the real risk

Override `TraverseLambdaExpr` in `IndexerAstVisitor` to additionally call
`TraverseDecl` on `lambdaExpr->getLambdaClass()`, so the closure type and its
`operator()` flow through the same `saveRecordDecl`/`saveFunctionDecl` path
every other class/method already does (the anonymous-type naming scheme
already in `SymbolFormatter.cc`, used for other compiler-synthesized anonymous
types, is very likely reusable as-is for a lambda's anonymous closure).

The real risk, and why this isn't a copy of the `enclosing_range`/`is_type_definition`-sized
patches: Clang's default `TraverseLambdaExpr` **already** traverses the
lambda's body statements directly (that's how a reference inside one gets
recorded at all today). Naively adding a `TraverseDecl` call on top risks
**double-visiting** the same expressions (once via the default body traversal,
once via the newly-added Decl traversal into the method body) — silently
duplicating occurrences/edges, a correctness bug worse than the current gap.
Getting this right needs either suppressing the default body traversal once
the Decl-level one takes over, or scoping the added traversal to just the
Decl/symbol-emission step without re-walking the statements. Also needs
checking against generic lambdas, nested lambdas, and no-capture lambdas that
decay to a function pointer. Estimate: a focused day, not an afternoon —
moderate effort and risk, not "trivial," unlike the other patches in this
file.

### If it proves useful

1. Prototype the `TraverseLambdaExpr` override locally (in the same cloned
   checkout, `~/.cache/cppgraph/scip-clang-src`, used for the other patches),
   verify no duplicate occurrences on a small fixture (the exact `global
   g = compute([]() {...})` case that surfaced this, plus a lambda inside a
   normal function body, plus a nested lambda) before trusting it on a real
   codebase.
2. If clean: propose upstream (small, well-motivated PR — cite this exact
   gap and its effect on lambda-heavy code); maintain as a local patch
   alongside `enclosing_range-on-v0.4.0.patch` in the meantime if upstream
   stalls.
3. Once lambdas are real symbols with real intervals: drop
   `global_init_references`'s lambda caveat (it becomes moot), and revisit
   whether `no_incoming_calls`/`line_span`/`outline` should surface lambdas
   as first-class listable definitions (currently a non-question since they
   don't exist as symbols at all).
