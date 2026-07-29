# TODO

The active list — open work we intend to do. Parked "someday / just in case"
ideas live in the **Attic** at the bottom: kept for reference, not part of the
active list. The project isn't versioned yet, so this is a snapshot, not a log;
design detail is in `DESIGN.md`, shipped features in `CHANGELOG.md`.

## Release (0.1.0)

- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`rebuild` level `none`/`store`/`reindex` — what the release
  invalidates in the index stack — plus one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.

## Other (not release-gating)

- **BUG: declaration sites counted as callers, then mis-attributed to an arbitrary
  neighbor.** Found in real use: `who_calls(extractShardKeyFromDoc)` returned
  `getKeyPatternFields` (`shard_key_pattern.h:195`) as a caller — but `.h:195` is
  `extractShardKeyFromDoc`'s own *declaration*, and `getKeyPatternFields` is an unrelated
  inline member 90+ lines up. Two compounding defects in `build_graph`: (a) a pure member
  **declaration** (no body) isn't marked `DEFINITION`/`FORWARD_DEFINITION`, so it slips the
  `call_sites` filter and is counted as a call *to that method*; (b) with no body interval
  containing it, the `bisect` fallback attributes it to the nearest-preceding callable
  definition — an arbitrary neighbor with a misleading name. Two costs, the second severe:
  a phantom caller destroys trust (sent the user to `grep`), and it **corrupts the dead-code
  signal** — `emplaceMissing…` showed "1 caller" with 0 real callers, the exact question
  being asked. Fix: (1) exclude declaration occurrences from call sites (classify them as
  `declaration`, not `calls`); (2) never attribute a site to a symbol whose interval doesn't
  contain it — on a #504 graph fall back to the enclosing type/file, not `bisect`-nearest
  (keep `bisect` only for genuinely interval-less stock graphs, as the documented
  approximation it is); (3) expose an explicit `caller_count` (incl. `0`). **Blocks
  `no_incoming_calls`** — that feature is untrustworthy until this is fixed.
- **Full signature with default arguments in `explain_symbol`.** From real use: the crux of
  an investigation was that `extractElementsBasedOnTemplate(…, bool useNullIfMissing = false)`
  has a defaulted param the caller omits — invisible today. `explain_symbol` gives
  callers/callees but not the signature, so the user had to read the `.h`. cppgraph already
  derives a source-derived `signature` for `find`'s overload disambiguation; extend it to
  carry default values and surface it in `explain_symbol` (needs `--root`, factual — read
  from source). Note: this shows the default *exists*; knowing whether a given call *relies*
  on it needs per-call arity — see the scip-clang section.
- **Align `pipeline.incremental_update` with the dirty fingerprints.** `status`
  (CLI + MCP) reads `meta.dirty_fingerprints` via `changed_files_since` so a graph
  built from a dirty tree isn't falsely reported stale (and a revert *is* caught).
  But `pipeline.incremental_update` computes its changed set with its own `git diff`
  (`_git_diff_names`), unaware of the fingerprints — so an explicit update
  re-indexes dirty-at-build files it needn't, and wouldn't notice a revert. Have it
  call `changed_files_since` so the report and the actual update agree.
- **Resolve a `file:line` to a symbol in the query tools.** The tools take a symbol
  or a plain name (unique resolves, ambiguous lists candidates) but not a `file:line`.
  Add it so "who calls the function at `foo.cpp:120`?" works without a name — needs a
  store lookup by definition location (`symbols.file_id`/`line`).
- **Attributed references as first-class `uses` edges.** `impact_of`/`path` traverse
  `calls`/`inherits` only, so a *type* has no reachable callers — "what breaks if I
  change this struct?" isn't answerable transitively; the answer lives in
  `find_references` (usage view), which isn't traversable. Promote the #504 attributed
  references (`refs.enclosing_id`, function → used symbol) into real traversable edges
  (a distinct `kind`) so `impact_of`/`path` cover type-change blast radius. Needs a
  #504 graph; the data already exists but this adds ~one edge per attributed reference
  (millions on mongo → larger store), so it's opt-in.
- **Show the storage cost of the symbol-granularity upgrade in `status`.** `status`
  already prints the file→symbol upgrade hint (index with a #504 binary / `enrich-refs`);
  add the extra `.graph.db` cost so the user can weigh it. Measure the real delta first
  (same graph with vs without `--attributed-refs`); don't hardcode a guess.
- **`hotspots` — global ranking by fan-in / fan-out / edge count.** Per-symbol
  tools (`who_calls`/`what_it_calls`) can't answer "top N most-called / most-calling
  symbols across the project" — today that needs manual SQL or N parallel calls.
  `hotspots(limit, kind=fan_in|fan_out|edges)` is a `Counter` over `graph.edges`:
  exact, data already in memory, works on any graph. The canonical "where's the
  weight?" question, one call replacing ~10 queries. Highest-value of the batch.
- **`stats` — aggregate counts by file / directory.** No tool gives a module-level
  map (symbols, edges, refs per file, rolled up per directory via `dirname`). Exact
  aggregation over `Node.file`/`Edge.file`; lets an LLM size up an unfamiliar module
  without reading a file. Works on any graph.
- **`line_span` — definitions ranked by body extent.** `enclosing_range` (#504)
  carries each definition's exact `(start, end)`; the builder already computes it for
  attribution (`_occurrence_enclosing_range`) but discards the end — `Node` keeps only
  `line`. Persist `end_line` on `Node`, then rank by `end - start`. Exact line span,
  not the fragile "def → next symbol" heuristic. #504-only, degrades cleanly on a
  stock graph like ref attribution does. States span, not complexity (facts-not-
  judgments rule in `DESIGN.md`) — so it's `line_span`, not `most_complex`.
- **`no_incoming_calls` — definitions with zero incoming `calls` edges.** The exact
  primitive behind the "dead code" question: a graph fact (`callers_of(sym) == []`),
  not a verdict. Unreliable as "dead" — vtable dispatch, exported API, templates,
  entry points all have no static caller yet are live — so the tool states the fact
  and the LLM judges. Cheap (`Counter` complement over `Edge.dst`). Pairs naturally
  with an `exclude` of known entry points (`setup`/`loop`/`main`) surfaced as a hint,
  never a filter that hides. **Gated on the declaration-attribution bug above** — a
  phantom caller from a mis-attributed declaration site turns a real 0 into a false 1,
  which is exactly the answer this tool must get right.
- **`strongly_connected_components` — call-graph cycles.** The exact primitive behind
  "circular deps": SCC membership on the `calls` subgraph (Tarjan), not a "bad
  architecture" verdict — mutual recursion is often legitimate. Returns the components
  of size > 1 as a fact; the LLM decides which matter. Bounded output (cap + `total`,
  like the fan-out tools).
- **`boundary_violations` — declared-layering conformance check.** The project
  declares a strict layering (e.g. `common/` → `platform/` → `projects/`); the graph
  can confirm it factually. Given rules ("`common/` must not call `platform/`"), list
  the `calls`/`inherits` edges that cross them — each reported violation *is* a real
  edge, so zero false positives (the strongest fact cppgraph can produce). The rules
  come from the user (the project's SPEC), not the tool: it confronts exact edges with
  a given constraint, no interpretation. Directory membership from `Edge.file` prefix;
  works on any graph. Highest-value of this batch — answers a question nobody can
  answer today without reading the whole tree.
- **`reachable_from(symbols)` — forward transitive reachability.** Complements
  `impact_of` (reverse): from an entry point, what does it reach? Forward closure over
  the `calls` adjacency (already built for `shortest_call_path`). Underpins attack-
  surface mapping ("what can an external handler trigger?") and dependency-migration
  scope. Per the `DESIGN.md` corollary it is a **lower bound** — static edges only,
  runtime dispatch not covered — and is worded as "at least these are reachable",
  never as a set the reader may act on by exclusion. Bounded output (cap + `total`).
- **`api_surface(module)` — symbols crossing a module boundary.** Which definitions
  inside a directory are called/referenced from outside it — the *actually-used*
  external surface, distinct from what is merely declared public. A boundary query:
  callee/def file inside the prefix, caller/use file outside. Exact; onboarding and
  module-overview use case, replacing N manual `what_it_calls` + `find_references`.
  Generalization of `stats` with a boundary predicate; shares that primitive with
  `boundary_violations`.
- **`dependency_cost(library)` — call-site count against a set of symbols.** "If I
  replace library X, how many call sites change?" is an exact fan-in count over edges
  to X's symbols (a special case of `find_references`/`hotspots` aggregated by target
  prefix). Factual, works on any graph. Likely **falls out of `hotspots` + path-prefix
  filtering for free** — fan-in restricted to a target prefix, callers filtered to "my
  code" — so it should land as a *mode* of `hotspots` once the path-prefix predicate
  exists, not a standalone tool. Depends on that primitive arriving first; build it
  after.
- **`outline` / `class_members` — list definitions by container (class or file).**
  Two facets of one primitive, both exact and available today (no #504), no judgment —
  structure, not interpretation: (a) by class, symbols whose SCIP container descriptor
  is `Class#…` — the members declared on a class; (b) by file, nodes with
  `Node.file == path` sorted by `Node.line` — the file outline. From real usage this is
  the tool that beats the `Read`/`grep` reflex, because it wins on the reflex's own turf:
  it replaces a `Read` of a 1400-line file with a ~50-token symbol list, or a header
  `grep` with the class's real member list. Distinct from `api_surface` (members *used*
  from outside) — this lists members that *exist*. Naming per facts-not-judgments: SCIP
  doesn't encode visibility, so it's `class_members` (a fact), not `public_api` (a claim
  we can't back).
- **Typed-by edges via `Relationship.is_type_definition`.** The builder reads only
  `is_implementation` (inheritance/override); `is_type_definition` gives the exact
  variable/field → its-type relationship, unused today. Promote it to a traversable edge
  kind ("typed-by") so `impact_of`/`path` cover "who has a field of this type?" — the
  type-change blast radius, complementing the attributed-`uses`-edges item above. Exact,
  no #504, no new source data (already in every `.scip`).
- **Audit which SCIP fields scip-clang actually populates (on a #504 binary).** Several
  schema fields go unused because they're suspected empty — the builder header already
  notes `SymbolInformation.kind` comes back `UnspecifiedKind`. Before relying on any,
  introspect a real #504 index (`scip_introspect.py`) and record which carry data:
  `kind`, `documentation`/`signature_documentation`, and the `symbol_roles`
  `ReadAccess`/`WriteAccess`/`Test` bits. (`enclosing_range` on *term* globals is already
  confirmed — the #504 `saveVarDecl` patch emits it — so it's not in this list.) Cheap;
  gates the `scip-clang (upstream)` section below. Same "verify before promising" lesson
  as `line_span`.
- **`global_init_references` — references between globals' initializer regions.**
  States a fact behind the "static init order fiasco": global A's initializer
  references global B. Not a verdict (a `constexpr`/`constinit` init is safe) — per
  the facts-not-judgments rule it reports the reference, the LLM judges the hazard.
  Needs a builder change: today a global is a SCIP *term* (suffix `.`), so its
  `enclosing_range` interval isn't collected (only callable/type are) and its
  initializer's read of another global lands as a bare `Reference` with
  `enclosing_symbol=None`. Collect term intervals too, then attribute reads by the
  existing containment sweep. The data is confirmed present on a #504 build: the patch's
  `saveVarDecl` passes `varDecl.getSourceRange()` (which spans the initializer) as the
  enclosing range for `isFileVarDecl()`/`isStaticDataMember()`, so a global's definition
  already carries an interval covering its initializer — the gap is purely that our
  builder doesn't collect term intervals. Two properties fall out: (a) containment
  attributes a read inside a lambda body to the *lambda* (its own inner `enclosing_range`),
  not to A — so only direct init-expression reads surface, correctly excluding lazily-run
  reads; (b) the tool gates on the data like ref attribution / `line_span` do — present on
  a #504 graph, unavailable on a stock one (report unavailable rather than a partial answer).
- **Path-prefix filtering on the query tools (`exclude_paths`/`include_paths`).**
  A generalization of `exclude_tests` (already a filter primitive driving both the MCP
  tools and the CLI): filter results by `Node.file` prefix so a query returns project
  code, not vendored deps. Real friction: filtering `.pio/libdeps/` noise (ArduinoJson,
  Adafruit, PubSubClient) by hand — "who calls `convertValue` in *my* code?", ambiguity
  candidates not polluted by library overloads next to the project's, and `hotspots`
  not dominated by vendored symbols (`operator==` at 80 fan-in measures the deps, not
  the project). Applies to everything tagged by file (`who_calls`, `what_it_calls`,
  `find_references`, `impact_of`, `hotspots`, `find`); a `Node.file`-prefix predicate
  in `cppgraph.filters`, shared with `boundary_violations`/`api_surface` (same "belongs
  to a path prefix" notion) — build it once, feed both surfaces. The project-scope
  default is not a convenience but an **adoption precondition**: real usage shows the
  noise actively discourages the tools — `find "Block"` → 188 hits (mostly `spirv_cross`),
  `build/vcpkg_installed/` swamping results — so an unscoped default pushes the agent
  back to `Read`/`grep`. Two levels, like `exclude_tests` (a param *and* a default):
  (1) per-query `exclude_paths`/`include_paths` — firm, do this; (2) a project-scope
  default that excludes vendored/external code everywhere so "my code, not libs" is free
  — open question on how cppgraph knows what's external: leaning toward deriving it
  factually from `compile_commands.json`, whose `arguments` we don't read today
  (`compdb.py` reads only `file`). The `-I` include paths there delineate vendored
  (`.pio/libdeps`, `vcpkg_installed`) from project sources factually — a stronger
  grounding than root-containment alone, no name heuristic (`.pio`/`third_party`/
  `vendor`). Decide the default when we build it.
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
- **Ship a `SKILL.md` (agent steering + distribution).** A short Claude Code skill
  that steers the agent to the cppgraph tools (`who_calls`, `impact_of`,
  `find_references`, …) over grep for in-scope C++, plus the install pointer. Two
  payoffs: it activates *before* an MCP connection (complementing the MCP
  `instructions` field, which only steers on connect) and it's a distribution
  artifact — third-party skill collections (e.g. MassGen bundles a Serena skill)
  are an inbound-link/adoption channel that feeds the discoverability item. Keep it
  short; the value is reach and pre-connect activation, not new capability. Real usage
  shows the `Read`/`grep` reflex beats the tools even on an indexed repo ("when I want
  to read code I reach for `Read`; a word, `grep`") — so the skill must target the
  *reflex* tasks, not only graph questions: use `outline` instead of `Read` for a
  file's/class's structure, a scoped `find` instead of `grep`, `explain_symbol` instead
  of opening the header. Correct one specific misconception seen in real use: agents
  think `find_references`/`who_calls` need a two-step "resolve the SCIP symbol, then
  query" and fall back to `grep` for a one-shot `file:line` list — but these tools
  **already take a unique human name in a single call** (shared `resolve`). Say so
  explicitly so the one-call path is used.

## scip-clang (upstream)

cppgraph is downstream of scip-clang: some features are blocked not by our code but by
what the indexer emits. Items here are gaps in scip-clang itself — candidates to advocate
upstream (sourcegraph/scip-clang) or, if it comes to it, to patch in our own clone (we
already carry the #504 `enclosing_range` patch that way). Each links the feature of ours
it unblocks. Until the audit ticket above runs, treat the *field-emptiness* items
(`kind`, read/write, `Test`, doc) as **suspected** absent; the `enclosing_range` and ARM
items are confirmed.

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
  local compile there. Once #504 lands and an `aarch64-linux` asset exists, wire the
  `Linux/aarch64` `download` case in `setup_cmd.py` `platform_sources()` (stock binary, no
  #504). → unblocks a no-toolchain install on Linux ARM.
- **`SymbolInformation.kind` — not emitted (comes back `UnspecifiedKind`).** The builder
  header notes it, which is why we derive node kind (callable/type/term) from the SCIP
  descriptor suffix. If scip-clang filled it, we could drop that derivation and make
  global/field/enum distinctions exact. → would unblock cleaner node typing; a firmer
  `global_init_references` (identify globals without suffix parsing).
- **`symbol_roles` `ReadAccess` / `WriteAccess` bits — not set today.** If set, they would
  tag a reference read vs write — "who *mutates* this global/field?" vs who reads it, a
  capability class we can't offer now. → would unblock mutation analysis; a sharper
  `global_init_references` (a write at init vs a mere mention).
- **`symbol_roles` `Test` bit — not set today.** We derive "is a test" from the file path
  (`exclude_tests`); if scip-clang set the Test role it would beat the path heuristic. →
  would unblock exact test filtering.
- **`documentation` / `signature_documentation` — empty today** (like `display_name`, 0%
  on the mongo index). If populated with doc comments / declared signatures, they would
  enrich `explain_symbol` (signature/doc without reading source).
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
  is a thin wrapper over `reachable_from` and could live there; the token-saving pitch
  (replace the full suite) is unsafe and parked.
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
