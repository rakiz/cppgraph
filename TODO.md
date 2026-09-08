# TODO

The active list — open work we intend to do. Parked "someday / just in case"
ideas live in the **Attic** at the bottom: kept for reference, not part of the
active list. Design detail is in `DESIGN.md`, shipped features in
`CHANGELOG.md`, releases in `versions.json`.

## Other


- **Follow-up to the declaration-site phantom-caller bug (fixed for #504 graphs):**
  `who_calls(extractShardKeyFromDoc)` used to return `getKeyPatternFields` as a caller
  because a bodyless member declaration (role 0, no `DEFINITION`/`FORWARD_DEFINITION`)
  fell back to `bisect`-nearest-preceding. `build_graph` now drops such a call site
  instead of guessing, but only when the document has `callable_intervals` (#504) — a
  stock graph still has no way to tell a declaration from a real call at the same shape
  (documented, accepted limitation, see `DESIGN.md`).
- **Attributed references as first-class `uses` edges.** `impact_of`/`path` traverse
  `calls`/`inherits` only, so a *type* has no reachable callers — "what breaks if I
  change this struct?" isn't answerable transitively; the answer lives in
  `find_references` (usage view), which isn't traversable. Promote the #504 attributed
  references (`refs.enclosing_id`, function → used symbol) into real traversable edges
  (a distinct `kind`) so `impact_of`/`path` cover type-change blast radius. Needs a
  #504 graph; the data already exists but this adds ~one edge per attributed reference
  (millions on mongo → larger store), so it's opt-in.
  **Effort/interest note (checked against reality, not the pitch above):** the
  "type-change blast radius" headline doesn't actually land — `impact_of`/`reachable_from`
  only walk ONE edge kind per call (same limit found for `typed-by`), so `uses` edges
  alone can't chain into a `calls` traversal any better than manually combining
  `find_references` + `impact_of` already does today. Real incremental value is narrower:
  (a) `boundary_violations` could check type-usage crossing declared layers (a genuinely
  new capability, not a `find_references` reformulation), (b) `visualize`/`subgraph`
  (already generic over edge kinds) would render `uses` relationships automatically. Cost
  (millions of extra edges) vs. that narrower payoff — moderate cost, modest-but-real
  value; lower priority than the headline made it sound.
- **Simplify installation.** Both macOS arm64 and Linux aarch64 #504 binaries are now
  published (`scip-clang-504-v0.4.0` on GitHub releases), and `setup_cmd.py`'s
  `platform_sources()`/`obtain_scip_clang()` now offer a `download-504` source
  (checksum-verified against the release's `.sha256` asset) ahead of `download`
  (stock) and `build` (local compile) whenever a prebuilt #504 binary exists for the
  host. Still open: reconsider whether the agent-interview ritual (README's two-phase
  setup flow) is still the right default now that the two heaviest costs (compile
  time, disk space) can often be skipped entirely — and publish an `x86_64-linux` #504
  asset too, the one platform still without one.
- **Contributing notes, CI (lint + pytest), publish.** Not a 0.1.0 blocker.
- **Make the repo discoverable to LLMs (distribution).** LLMs asked to compare
  code-intelligence tools describe cppgraph from the *name* only — the page isn't
  crawled/indexed, and the homonym `6502/cppgraph` outranks it for the bare term
  "cppgraph", so they hallucinate it as a generic graph data structure. Get inbound
  links so `rakiz/cppgraph` gets crawled and ranks on "cppgraph mcp" / "cppgraph
  claude code": submit to the MCP registry (best-targeted, most durable), optionally
  a short write-up / Show HN. Refer to it with a descriptor everywhere it's linked
  ("cppgraph — compiler-exact C++ code-intelligence MCP server"), never bare
  "cppgraph". Gated on making the repo publicly visible / cutting 0.1.0.
- **Add a Graft contrast to `COMPARISON.md`.** Graft (nanonets/graft) is the tool an
  LLM will most likely bring up unprompted when asked about "code graph for agents" —
  broad (21 languages), tree-sitter by default with optional per-language LSP
  precision (clangd for C/C++), and an LLM-written markdown "node" layer (summaries +
  cruxes) an agent reads as context. Two real axes of contrast, not a dismissal:
  (1) **exactness** — its C++ precision option is clangd, which `COMPARISON.md`
  already measured stalling on MongoDB (cross-TU references never warming up); scip-clang
  is a batch compiler index, not a live LSP session, so this is a real, evidenced
  difference for large real C++, not a assumed one; (2) **facts vs. judgments** — Graft's
  node summaries are model-written prose, which is explicitly what `DESIGN.md`'s
  facts-not-judgments principle refuses to ship (a summary can be wrong in a way a
  compiler-traced edge cannot). Write this measured, the way the existing Serena/graphify
  sections are — no unearned superiority claims on axes we haven't measured (their
  SWE-bench Verified numbers are real and we have no equivalent yet, see the benchmark
  items above).
- **Hook-based triggering: `SubagentStart`, `PreToolUse`, `SessionStart`.** The only
  steering channel today is the MCP `instructions` string (`mcp_server.py:_server_instructions`),
  delivered once at `initialize` — so it is weakly attended and, critically, **not inherited
  by subagents**. That is a coverage hole by construction: the recommended navigation path
  (an `Explore` agent) starts with no cppgraph steering and reaches for grep. Three hooks,
  by leverage: (1) `SubagentStart` — replay the steering + indexed scope, closing the hole;
  (2) `PreToolUse` on `Grep` / `Bash(rg|grep)` — when the pattern looks like a C++ identifier
  and the target path is inside the indexed scope, return advisory `additionalContext`
  naming the tool that answers exactly (`find`/`who_calls`). This fires at the moment of the
  wrong decision, which a session-header instruction cannot. Keep it **advisory**, never
  `deny`: grep over comments, string literals and non-indexed files stays correct;
  (3) `SessionStart` — emit the scope + freshness line instead of waiting for the agent to
  think of calling `status`. Complements the `SKILL.md` item above (skill descriptions are
  permanently in context; hooks are positional). External validation of the general
  pattern (not our measurement, so not a substitute for the benchmark items below, but
  a reason to prioritize this): Graft (nanonets/graft), a competing tool wiring similar
  hooks into Claude Code, reports 46% fewer tool calls and 60% less latency in its own
  controlled benchmark — the closest independent evidence that this exact mechanism
  (auto-injected steering + auto-resync, not just a static `instructions` string) moves
  the needle for agent-facing code tools generally.
- **Package as a Claude Code plugin (`.claude-plugin/`).** Install today is a two-phase
  README ritual where the agent interviews the user and `setup.sh` runs `claude mcp add`.
  A plugin manifest + `marketplace.json` carries the MCP server declaration, the skill,
  slash commands and the hooks above in one `/plugin marketplace add` — removing all the
  wiring from the install. The compute (obtain scip-clang, index a project) stays a script:
  a plugin cannot do it. Add a `/cppgraph-index` command so the no-graph path
  (`_NO_GRAPH`, which today only reports "not indexed here") has an exit.
- **Per-answer staleness instead of global drift.** Drift is reported only by `status`,
  which an agent rarely calls unprompted — so answers about files edited since the index
  look authoritative. Mark files touched in-session (a `PostToolUse` hook on `Edit`/`Write`,
  or the existing dirty fingerprints) and annotate any result citing one. Precision of the
  answer *as delivered*, not of the store. Pairs with single-TU incremental reindex on demand
  (`pipeline.incremental_update` already has the machinery — see the alignment item above).
- **Answer-accuracy benchmark, tier 1: oracle + static metrics.** Bumped priority: a
  competing tool, Graft (nanonets/graft), just published exactly this kind of evidence —
  an official SWE-bench Verified run (54%→66% resolved, +12 pts) plus a controlled
  162-run sweep — where we still only *argue* accuracy. Being the tool that measures
  vs. the tool that asserts is a credibility gap, not just a completeness one. `COMPARISON.md`
  §"Token
  cost" *measures* tokens but only *argues* accuracy and completeness (noise %, `†` = does
  not fit a context). Put all three axes on the same evidentiary footing. Needs ground truth,
  and cppgraph cannot be its own oracle. Primary oracle: a hand-curated set (20–30 symbols)
  covering the hard cases — overloads, `ptr->method()`, virtual dispatch, templates, macros,
  cross-TU homonyms. Automatable safety net: an independent LLVM callgraph
  (`-emit-llvm -O0` + `opt -passes=print-callgraph`), which is backend-derived and so shares
  no code with scip-clang — valid for **direct calls only** (virtual calls become indirect
  and vanish), hence a lower bound that catches recall *regressions* in CI without human
  work. Serena/clangd is not an oracle but its disagreements cheaply generate candidates for
  the curated set. **The experiment that unifies the three axes: give both arms the same
  token budget (one real context, ~200k) and measure what they return** — precision
  (grep's declarations/comments/homonyms become a number), recall under budget (grep's
  truncation becomes measured rather than inferred), and tokens ingested to reach it. Turns
  the current `†` into a result: at equal budget grep plateaus at X% recall, cppgraph at Y%,
  for Z× fewer tokens. No LLM in this tier — extend `scripts/measure_tokens.py`, keep it
  deterministic and CI-able. Assume the outcome will expose **our** gaps (unindexed TUs, the
  aarch64 gap, what `--attributed-refs` changes); the table must not read 100/100.
- **Answer-accuracy benchmark, tier 2: agentic, multi-model.** Depends on the oracle from
  tier 1. Run a real agent on the same questions in two configurations (with / without
  cppgraph) across models, and score the **final answer**, not the tool output — that is the
  claim we actually make. It doubles as the triggering metric: an agent that has cppgraph
  and greps anyway shows up in the results, replacing the current intuition that the tools
  are sometimes skipped. Report as dated files (`benchmarks/results/YYYY-MM-DD-*.md`) with a
  reproducible runner rather than growing `COMPARISON.md`. Consider the official `swebench`
  harness (SWE-bench Verified) as one arm here instead of inventing our own scoring —
  Graft used it directly (real merged-PR instances, official grader, no judge model needed)
  and it reads as more credible than a self-graded rubric; would need C++ instances, which
  SWE-bench Verified is thin on (it's mostly Python) — check coverage before committing to it.
- **Multi-host steering adapters.** Generate one steering ruleset into the per-host formats
  (`AGENTS.md`, `.cursor/rules/`, `.windsurf/rules/`, `.clinerules/`,
  `.github/copilot-instructions.md`) from a single source, with a drift check in CI so the
  copies cannot diverge. Only worth the maintenance if non-Claude hosts become a goal —
  the pitch is Claude Code-only today. Lowest priority of this group. Note this is
  table-stakes for at least one competing tool (Graft's `init` detects and wires Claude
  Code, Cursor, Codex, Gemini, Copilot, Kiro, Windsurf, AdaL from one source) — a reason to
  revisit the "Claude Code-only" premise if adoption data ever shows non-Claude usage
  demand, not a reason to build it speculatively now.

## scip-clang (upstream)

cppgraph is downstream of scip-clang: some features are blocked not by our code but by
what the indexer emits. Items here are gaps in scip-clang itself — candidates to advocate
upstream (sourcegraph/scip-clang) or, if it comes to it, to patch in our own clone (we
already carry the #504 `enclosing_range` patch that way). Each links the feature of ours
it unblocks. The field-emptiness audit has now run (`SCIP_AUDIT.md`, measured against
`scratch/mongo_src_tests.scip` + a #504 fixture, exhaustive counts, not sampling) — every
item below states a verified fact, not a suspicion, and carries the effort estimate and
`scip-clang` file:line the audit found for whoever files the PR.

- **`enclosing_range` — not emitted by official scip-clang at all (PR #504 in progress).**
  The single biggest gap: enclosing ranges are the definition-body extents that drive exact
  caller/reference attribution and the symbol-granularity usage view. The official binary
  emits none. We carry a patch (`docker/build-scip-clang/enclosing_range-on-v0.4.0.patch`,
  tracking [sourcegraph/scip-clang#504](https://github.com/sourcegraph/scip-clang/pull/504))
  and apply it when we build — including on *term* (global) definitions, where our patch's
  `saveVarDecl` emits the range over the initializer (this is what `global_init_references`
  relies on). So *we* have the feature, scip-clang doesn't — the ask upstream is to land
  #504 so a stock binary carries it and the local compile step goes away. → unblocks (stock,
  no patch) ref attribution, `line_span`, `global_init_references`.
- **No `aarch64-linux` release asset.** scip-clang publishes no Linux ARM binary, forcing a
  local compile there. Verified on v0.4.0 (Feb 2026): the release ships only
  `x86_64-linux`, `dev-x86_64-linux`, `arm64-darwin` — their only ARM runner is macOS. The
  `release.yml` `build-and-upload-artifacts` matrix has 3 jobs, all
  `ubuntu-24.04-…-amd64` or `macos-14`; adding Linux ARM is one matrix entry pointing at an
  arm64 runner (`ubuntu-24.04-arm` hosted, or a graph-team arm64 runner). The real cost is
  the runner, not the YAML — the LLVM/Clang-from-source + LTO build needs a large ARM box.
  The upload step already globs `./*-release-artifacts/*` (platform-agnostic), so a new
  artifact is picked up with no further change. Filed upstream:
  [sourcegraph/scip-clang#542](https://github.com/sourcegraph/scip-clang/issues/542). Once the
  asset exists, wire the `Linux/aarch64` `download` case in `setup_cmd.py`
  `platform_sources()` (stock binary, no #504). → unblocks a no-toolchain install on Linux ARM.
- **`SymbolInformation.kind` — confirmed unemitted (`UnspecifiedKind` on 810,919/810,919
  corpus symbols, 100%).** The builder derives node kind (callable/type/term) from the
  SCIP descriptor suffix instead. Small effort, moderate value: `SymbolInformationBuilder`
  (`ScipExtras.h:89`) already funnels every symbol through `finish()`, and each
  `save*Decl` site holds the `clang::Decl` (`saveEnumDecl:483`, `saveEnumConstantDecl:465`,
  `saveFieldDecl:510`, `saveFunctionDecl:528`, `saveRecordDecl:685`, `saveVarDecl:931`) —
  a Decl-kind → `scip::SymbolInformation::Kind` switch (≈40 lines) + `set_kind` in the
  builder. Moderate not high value because cppgraph's descriptor-suffix derivation is
  already exact for the callable/type/term split; `kind` would add finer distinctions
  (enum vs. class, static vs. global, parameter vs. field). If scip-clang filled it, we
  could drop that derivation and make global/field/enum distinctions exact. → would
  unblock cleaner node typing; a firmer `global_init_references` (identify globals
  without suffix parsing).
- **`symbol_roles` `ReadAccess` / `WriteAccess` bits — confirmed never set (0 of
  15,176,411 corpus occurrences carry either bit).** If set, they would tag a reference
  read vs write — "who *mutates* this global/field?" vs who reads it, a capability class
  we can't offer now. Moderate effort, high value — upstream receptivity is on record:
  `saveDeclRefExpr` carries the literal comment *"TODO: Add read-write access to the
  symbol role here"* (`Indexer.cc:1018`), and `saveMemberExpr` (`:1024`) is the sibling
  site — both already hold the `Expr`, so classification is a parent-expression check
  (assignment/compound-assignment LHS, `++`/`--` operand, via clang's
  `ParentMap`/`ASTContext::getParents`). Plumbing is ready: `saveReference` (`:1164`)
  takes `extraRoles`, `saveOccurrenceImpl` sets roles verbatim (`:1263`, the only
  `set_symbol_roles` call for references). A `RefersToWrite`-style classifier next to
  `RefersToForwardDecl` (`Indexer.h:254`, `check()` at `:339`, ~15 lines) plus
  pass-through at the two visitor sites: **~60–120 lines + tests**. State the limits
  honestly in the PR: syntactic write detection only (no dataflow — `f(x)` passing `x`
  by reference to an out-param isn't detectable here; ref-returning calls like
  `a.b() = x` need a decision). → would unblock mutation analysis; a sharper
  `global_init_references` (a write at init vs a mere mention).
- **`symbol_roles` `ForwardDefinition` bit on bodyless-declaration occurrences —
  small effort, the single biggest correctness win for STOCK-binary graphs (where
  #504's `enclosing_range` isn't available to solve it by containment).** The
  discriminator already exists and runs: `RefersToForwardDecl::check` (`Indexer.cc:339`)
  is `!canonicalDecl->isThisDeclarationADefinition()`, and bodyless declarations (an
  in-class method declaration, a header prototype) route through `saveFunctionDecl` →
  `saveForwardDeclaration` (`:565` → `:1151`) into the internal forward-decl pipeline
  (`proto/fwd_decls.proto`, `ForwardDeclMap::emit` at `:363`), re-emerging into
  documents via `ForwardDeclOccurrence::addTo` (`ScipExtras.cc:243-249`) — which sets
  symbol and range and **never touches `symbol_roles`**, landing as plain role-0. That
  role-0 shape is exactly the declaration-vs-call indistinguishability that forces
  cppgraph's stock-binary over-capture (`DESIGN.md` § Building calls: "no separating
  signal exists"; the same declaration-site phantom-caller bug at the top of this
  file, fixed for #504 graphs only). One bit at the source fixes what no consumer can
  recover — an existing SCIP role, no schema change. Design caveat to resolve *in* the
  PR: `saveReference` (`:1179`) also routes *references* that resolve to a
  declaration-only decl into the same map, so the naive one-line version (tag every
  `ForwardDeclOccurrence`) would also tag genuine calls to declared-here/
  defined-elsewhere functions — the clean version needs an origin marker
  (declaration-site vs. reference-site) in `fwd_decls.proto`'s `ForwardDecl::Reference`,
  setting the bit only for declaration sites: **~15–30 lines** across the internal
  proto, the two insert sites, and `addTo`. Acceptance test before relying on it: the
  `rwmutex.h:192` / `ProcessId::asLongLong` phantom-caller fixtures from `DESIGN.md`
  must drop out while the real inline-body calls stay. → unblocks a stock-graph fix
  for the declaration-site phantom-caller bug at the top of this file, without needing
  #504 at all.
- **`symbol_roles` `Test` bit — confirmed never set (0 of 15,176,411).** We derive "is a
  test" from the file path (`exclude_tests`); if scip-clang set the Test role it would
  beat the path heuristic, though an upstream emitter would itself need a path/gtest
  heuristic, so no exactness gain — not worth filing (see `FOLLOWUP.md`). → would unblock
  exact test filtering, in principle.
- **`documentation` — confirmed WORKING, contradicts this bullet's own prior claim.**
  Measured: 99.99% non-empty, but 84.45% is a literal `"No documentation available."`
  placeholder — the genuine doc-comment rate is 10.09% (81,844 of 810,919 symbols). No
  upstream ask here: `explain_symbol` can consume this today, cppgraph-side.
  `signature_documentation` remains genuinely empty (0/810,919) — moderate effort,
  moderate value: nothing exists today (`set_signature_documentation` appears nowhere
  in the indexer), would render declared
  signatures without a source read.
- **Effective call arity per call site — not modeled.** A call occurrence carries the
  callee symbol and location but not how many arguments the call expression actually passes.
  clang's AST knows it; SCIP drops it. Without it the graph can't tell "called with 2 args"
  from "with 3" — so it can't detect a call that omits a defaulted parameter (the real
  question behind the `explain_symbol` default-args item). Would need a scip-clang patch to
  emit per-call arity (or a source re-parse of each call site, expensive). → would unblock
  "which calls rely on a default?" analysis.

- **Member visibility (public/protected/private) — a *format* gap, not an emission
  one.** clang knows it (AST `AccessSpecifier`), but SCIP has no field for it and models
  privacy as *local symbols* (a name-scope notion that doesn't map C++'s compile-time
  access rule), and `kind` has no Public/Private either. So unlike the items above —
  which scip-clang could fill into existing fields — this needs a **schema extension**
  (our proto + a scip-clang patch), less likely upstream. Would unblock a real
  `public_api` distinct from `class_members`. Lower odds; see `DESIGN.md § Why SCIP is
  the right foundation` for the build-vs-buy framing.

## Attic

Kept for reference; most may never happen. Promote one back up if it becomes real.

- **`cppgraph index` wizard — step-back.** The wizard (`src/cppgraph/init.py`) can
  restart from the top (`--from-scratch`) but only moves forward within a run. Open:
  let the user step back to redo an earlier stage (re-filter, re-index) from a later
  prompt.
- **`test_impact` "which tests to skip" — wrong side of the reachability axis.**
  The idea: from changed files, run only the tests that reach them, skipping the rest.
  Rejected per the `DESIGN.md` corollary: static reachability under-reports (a test
  may reach the change via virtual dispatch the graph can't see), so it can never
  license *skipping* a test — only *adding* the ones known to reach it. The honest,
  additive form ("tests statically known to touch this change, run at least these")
  is a thin wrapper over the now-shipped `reachable_from` and could live there;
  the token-saving pitch (replace the full suite) is unsafe and parked.
- **Version for non-git installs.** `current_version` derives from `git describe`;
  a non-git install (tarball/PyPI) falls back to the static `pyproject`/`__version__`.
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`). Parked until we support a non-git install.
- **Auto-enrich after a #504 re-index.** Attribution (`--attributed-refs`) is an
  explicit opt-in; decide whether a re-index with a #504 binary should enrich by
  default.
- **Build speed (pure-Python wins, then maybe native).** The graph build is ~3.5 min
  wall, single-thread, ~8.8 GB RSS on mongo — pure-Python object churn (`build_graph`
  ~51%, `enrich_references` loop ~39%), not protobuf (already the upb C backend).
  Tier 1 (no toolchain): `__slots__` + `gc.disable()` done; open — columnar typed
  arrays instead of object-per-element, and multiprocessing by document. Tier 2:
  native `build_graph` (Rust/PyO3 or Cython) on the hot loops, at the cost of the
  pip-install/no-toolchain property. Lower priority than the scip-clang download/
  compile path; a graph built once and queried many times may make ~3.5 min acceptable.
- **Synthetic factory-registry edges (reconnect plan→exec across dispatch).** In mongo,
  plan→exec hops through a string-keyed factory table
  (`REGISTER_DOCUMENT_SOURCE("$match", …)`) then virtual dispatch, so there's no static
  edge — `path` can only hint. Parsing the registration macros to inject synthetic
  edges would close it, but the macros are codebase-specific and a synthetic edge
  departs from the exact, heuristic-free model (against the tool's exactness goal);
  it would need a distinct `kind` and an explicit decision before adding.
