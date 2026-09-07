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

---

## Worth patching upstream in scip-clang — PR shortlist from the field audit

Measured 2026-09-07 (`SCIP_AUDIT.md` at the repo root: every field of
`SymbolInformation`/`Occurrence`/`Relationship`/`Document`/`Metadata`,
counted exhaustively on the mongo corpus + a #504 fixture). This section
distills the gaps that would be worth a **real upstream PR**
(sourcegraph/scip-clang), ranked by effort vs. value to cppgraph. File:line
citations are from the v0.4.0 checkout at `~/.cache/cppgraph/scip-clang-src`
(the same one the other sections in this file cite), verified by reading the
cited code.

### 1. `SymbolRole::WriteAccess` / `ReadAccess` bits — moderate effort, high value

Upstream receptivity is on record: `saveDeclRefExpr` carries the literal
comment *"TODO: Add read-write access to the symbol role here"*
(`Indexer.cc:1018`), and `saveMemberExpr` (`Indexer.cc:1024`) is the sibling
site — both already hold the `Expr`, so classification is a parent-expression
check (assignment/compound-assignment LHS, `++`/`--` operand, etc., via
clang's `ParentMap`/`ASTContext::getParents`). The plumbing is ready too:
`saveReference` (`Indexer.cc:1164`) takes `extraRoles`, and
`saveOccurrenceImpl` sets roles verbatim (`Indexer.cc:1263` — the only
`set_symbol_roles` call for references). A `RefersToWrite`-style classifier
next to `RefersToForwardDecl` (`Indexer.h:254`, `check()` at
`Indexer.cc:339`, ~15 lines there) plus pass-through at the two visitor
sites: **~60–120 lines + tests**.

State the limits honestly in the PR: syntactic write detection only (no
dataflow — `f(x)` passing `x` by reference to an out-param is not detectable
at this site; ref-returning calls like `a.b() = x` need a decision).

Value to cppgraph: mutation analysis — "who *writes* this global/field?" —
a capability class we cannot offer today (measured: 0 of 15,176,411 mongo
occurrences carry either bit; even the fixture's unambiguous `g = 5;` write
comes back role 0), and a sharper `global_init_references` (write-at-init vs.
mere mention).

### 2. `SymbolRole::ForwardDefinition` bit on bodyless-declaration occurrences — small effort, high value for stock graphs

The discriminator already exists and runs: `RefersToForwardDecl::check`
(`Indexer.cc:339`) is `!canonicalDecl->isThisDeclarationADefinition()`, and
bodyless declarations (an in-class method declaration, a header prototype)
route through `saveFunctionDecl` → `saveForwardDeclaration`
(`Indexer.cc:565` → `:1151`) into the internal forward-decl pipeline
(`proto/fwd_decls.proto`, `ForwardDeclMap::emit` at `Indexer.cc:363`),
re-emerged into documents by `ForwardDeclOccurrence::addTo`
(`ScipExtras.cc:243-249`) — which sets symbol and range and **never touches
`symbol_roles`**, landing as plain role-0. That role-0 shape is exactly the
declaration-vs-call indistinguishability that forces cppgraph's stock-binary
over-capture (`DESIGN.md` §Building calls: "no separating signal exists;
measured 15–20% collateral"). One bit at the source fixes what no consumer
can recover — and it uses an existing SCIP role, no schema change.

Design caveat to resolve *in* the PR, not after: `saveReference`
(`Indexer.cc:1179`) also routes *references* that resolve to a
declaration-only decl into the same map, so the one-line version (tag every
`ForwardDeclOccurrence`) would also tag genuine calls to
declared-here/defined-elsewhere functions. The clean version carries an
origin marker (declaration-site vs. reference-site) in `fwd_decls.proto`'s
`ForwardDecl::Reference` and sets the bit only for declaration sites:
**~15–30 lines** across the internal proto, the two insert sites, and
`addTo`. cppgraph-side acceptance test before relying on it: the
`rwmutex.h:192` / `ProcessId::asLongLong` phantom-caller fixtures from
`DESIGN.md` must drop out while the real inline-body calls stay.

Value: this is the single biggest correctness win for **stock**-binary
graphs (the default install path), where #504's `enclosing_range` isn't
available to solve it by containment.

### 3. `SymbolInformation.kind` — small effort, moderate value

Mechanically trivial: `SymbolInformationBuilder` (`ScipExtras.h:89`) already
funnels every symbol through `finish()`, and each `save*Decl` site holds the
`clang::Decl` (`saveEnumDecl` 483, `saveEnumConstantDecl` 465,
`saveFieldDecl` 510, `saveFunctionDecl` 528, `saveRecordDecl` 685,
`saveVarDecl` 931). A Decl-kind → `scip::SymbolInformation::Kind` switch
(≈40 lines) + `set_kind` in the builder. Measured today: 100%
`UnspecifiedKind` (810,919/810,919 corpus, 16/16 fixture).

Value is moderate, not high, because cppgraph's descriptor-suffix derivation
(`builder.py`) is already exact for the mission's callable/type/term split;
`kind` would add the finer distinctions (enum vs. class, static vs. global,
parameter vs. field) and make `global_init_references` targeting firmer
than suffix parsing.

### 4. `Signature.signature_documentation` — moderate effort, moderate value

Nothing exists today (`set_signature_documentation` appears nowhere in the
indexer; 0/810,919 corpus). Would render declared signatures with clang's
printing machinery (`PrintingPolicy` / `DeclarationName::print`) at the same
`save*Decl` sites as (3): **~60–100 lines**.

Value: `explain_symbol` gains signatures without a source read — today they
are re-derived from source (CHANGELOG 2026-08-17), which needs `--root` and
the checkout present; this makes the store self-contained for signatures.
Moderate because the source-derived path already works.

### 5. `Document.position_encoding` — one line, compliance only

scip-clang emits `UnspecifiedPositionEncoding` on every document
(16,664/16,664 measured) while `scip.proto` says that value "should not be
used by new SCIP indexers" and names `UTF8CodeUnitOffsetFromLineStart` as
the correct choice for a C++ indexer. Zero cppgraph value (line numbers
only), but it is a one-line fix and a clean drive-by commit to bundle with
any PR above.

### Not worth filing (measured reasoning)

- **`Test` bit** — our file-path derivation (`exclude_tests`) works; an
  upstream emitter would itself need a path/gtest heuristic, so no exactness
  gain. 0 occurrences carry it today (0x20 never set in 15,176,411).
- **`Relationship.is_definition`** — proto semantics target mixin-style
  definition overrides; C++'s one-definition rule makes it moot. 0/260,223.
- **`override_documentation` / `enclosing_symbol` / identifier
  `syntax_kind`** — hover, local-symbol hierarchy, syntax highlighting: not
  cppgraph's mission; all measured 0% (identifier `syntax_kind` is
  macro-only by design, `Indexer.cc:98,101`).
- **`display_name`** — `filters.short_label` already derives a readable name
  from the symbol string (0/810,919 today).
- **`documentation`** — no PR needed: extraction already works (86,061 real
  doc comments on the corpus, `Indexer.cc:1324`); the follow-up is ours
  (surface it in `explain_symbol`).

Already written up elsewhere in this file / TODO.md and not repeated here:
landing #504 `enclosing_range` upstream (TODO.md's first upstream bullet),
lambda symbols/intervals (§ above), `is_type_definition` (§typed-by).
