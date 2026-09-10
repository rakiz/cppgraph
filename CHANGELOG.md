# Changelog

All notable changes to cppgraph. The format follows
[Keep a Changelog](https://keepachangelog.com/). This project is pre-1.0; the
on-disk store also carries its own `schema_version` for forward-compatibility.

## [Unreleased]

### Fixed

- **MCP `visualize` didn't resolve plain names** — the one MCP tool that bypassed
  `_resolve()` entirely, unlike every other tool and unlike the CLI's equivalent
  `export`/`view` commands: a plain "human" name that wasn't already an exact
  SCIP string returned an unhelpful unknown-symbol error instead of resolving
  (or listing ambiguous candidates). Now calls `_resolve()` like every other
  MCP tool. Found via a real-world stress test on a large C++ codebase.
- **`find` (MCP and CLI) missed the `::`→`#` relaxation step that
  `GraphStore.resolve()` already had** — a qualified query like
  `Class::method` jumped straight from an exact-match miss to a bare-leaf
  relaxation (dropping the class qualifier entirely), burying the real match
  among unrelated same-named methods on other classes, instead of first
  trying the SCIP separator form `Class#method`. Both surfaces now try that
  substitution before falling back further (fuzzy/leaf on MCP; CLI stays
  exact-only, as before). Found via the same stress test.
- **`ambiguous_candidate_hint()` didn't normalize a qualified query** — the
  "one candidate is the type itself" / operator-detection hint (added in
  0.3.1) only fired when the caller passed a bare leaf name (e.g.
  `DocumentSource`); a qualified query copy-pasted straight from a `find`
  result (e.g. `mongo/DocumentSource#` — the realistic, common case) silently
  produced no hint at all, because the query itself was never stripped of its
  namespace prefix/trailing `#` before comparison. Fixed by normalizing the
  query the same way a candidate's leaf already is.

### Changed

- **`find`'s docstring/help now points at `include_paths`/`exclude_paths`**
  as the fix for a broad query dominated by generated-code clutter (IDL
  Spec/getter classes and the like) — the tool won't auto-detect "generated
  code", so scoping by path prefix is the actual lever.
- Documented (docstrings on `hotspots`/`line_span`/`no_incoming_calls`/
  `api_surface`, plus a `TODO.md` entry) that an unrecognized/mistyped
  parameter name is silently ignored by the underlying MCP SDK's lenient
  argument validation (confirmed via a live repro) rather than raising an
  error — a deliberately-unpatched upstream limitation (no local pydantic
  monkeypatch planned), not a cppgraph bug, so a typo'd kwarg like `path=`
  produces a silent unfiltered/global result instead of a clear failure.

## [0.3.1] - 2026-09-10

Bugfixes and hardened test coverage around store/binary/proto migrations and
name-resolution ergonomics. No schema change (still v5).

### Fixed

- **`extract_signature` misattributing a field's signature to an unrelated
  neighbour** (`cli.py`): when a field declaration had no parenthesized
  signature of its own, the lookahead used to scan past a `;`/`{` boundary and
  could pick up the next declaration's parameter list instead — e.g.
  `ShardId::_shardId` (a plain field) rendering the signature of the
  following `operator==`. Fixed by stopping the lookahead at the first
  `;`/`{` seen before an opening `(`. 12 new tests in `tests/test_cli.py`.
- **CLI and MCP raised a raw traceback instead of a clean error on a
  too-new `.graph.db` schema** (`GraphStore`'s `IncompatibleStoreError`,
  raised when a store's `schema_version` is newer than the running code
  supports): now caught at every store-opening call site on both surfaces —
  CLI (`find`/`callers`/`callees`/…, `status`, and both the explicit
  `--graph` and the auto-discovered `update` paths) prints the exception's
  own message to stderr and exits with code 2; MCP returns a
  `{"error": ...}` dict consistent with the existing `_NO_GRAPH`/`_UNKNOWN`
  shapes instead of crashing the tool call. `pipeline.py` is untouched and
  still raises — the conversion to a clean, surface-shaped error stays at
  the CLI/MCP boundary. New tests in `tests/test_cli.py` and
  `tests/test_mcp_server.py`.

### Changed

- **Sharper hint on ambiguous name resolution** (`cppgraph.filters
  .ambiguous_candidate_hint`, used by both CLI `_resolve_symbol` and MCP
  `_resolve`): when a query resolves to several candidates, the hint now
  calls out (a) any candidate that is the type itself (a symbol ending in
  `#` whose leaf matches the query — worded in the singular or plural
  depending on how many match, never implying a single answer when several
  types share the leaf name across namespaces) and (b) any candidate that is
  an operator symbol (e.g. a conversion operator) sharing the queried name.
  Investigated after a reported real-world mis-pick (a conversion operator
  chosen over the intended class); `GraphStore.resolve()`/`find()` were
  confirmed NOT at fault — they already return the full, uncapped candidate
  list with no silent guess. Payload shape (`ambiguous`/`total`/`truncated`/
  `candidates`/`hint`) unchanged, only the `hint` text is richer. New tests
  in `tests/test_cli.py` and `tests/test_mcp_server.py`.

### Tests

Closed four gaps found in a migration-test audit (store schema, scip-clang
binary versioning, `scip.proto` compatibility, CLI/MCP error surfaces) —
no behaviour change beyond the two fixes above, but each area now has a
regression test pinning the intended behaviour:

- **Store schema migrations**: added a test for an unparseable/garbage
  `schema_version` value being treated as legacy (the existing fallback
  behaviour was correct; it just had no dedicated test).
- **`scip.proto` backward/forward compatibility**: new `tests/test_scip_compat.py`
  — regression tests for the "vendored `scip.proto` is a superset of what
  any supported scip-clang emits, absent optional/repeated fields never
  error" invariant (see `AGENTS.md`): an old/minimal-shape index (no
  `enclosing_range`, access roles, or `SymbolInformation.kind`) parses and
  degrades cleanly, and an index carrying an unrecognized future field
  number still parses (forward compatibility). The module docstring scopes
  exactly what is and isn't proven.
- **scip-clang binary staleness stays advisory-only**: a new CLI test pins
  that a stale/mismatched scip-clang binary surfaces as `!` advice lines in
  `status` (exit 0), never a failure — anchoring the intentional non-blocking
  design so it isn't turned into an enforcement gate without discussion. A
  short comment was added at the call site in `cli.py`.
- **CLI/MCP error-surface coverage**: see "Fixed" above — the schema
  rejection tests are also new regression coverage for this audit.

## [0.3.0] - 2026-09-10

Store schema v4 → v5 (`refs.roles`); five new scip-clang patches and a rename
of the binary "variant" concept.

### Added

- **`Index.external_symbols` consumption** — external-package symbol identity
  (`Node.is_out_of_project`): scip-clang already emits `Index.external_symbols`
  (a repeated `SymbolInformation` listing symbols referenced from the project
  but defined in an un-indexed external package — boost/absl/stdlib/…;
  174,436 entries measured on the mongo corpus), and no scip-clang patch is
  involved: this is pure cppgraph-side consumption of a stock field. The
  builder now processes that list before `index.documents`, creating nodes for
  symbols that previously had either a file-less phantom node (referenced
  somewhere) or no node row at all (`find`/`explain` returned "unknown
  symbol"), with the same first-real-wins metadata merge as `Document.symbols`
  entries (display name, genuine doc text, fine-grained kind, recorded
  signature). Classification is three-valued: `True` = defined in an external
  package, `False` = project-native (a `Document.symbols`
  `SymbolInformation` — this write is unconditional so project evidence always
  wins, in either pass order, including the incremental-update collision
  path), `None` = a pure phantom (no `SymbolInformation` from either source).
  Node identity/metadata only — no edges are created from external
  relationships (`Graph.add_edge` needs a real project document as
  provenance). Consumed by cppgraph: `symbols.is_out_of_project` (store schema
  amends the pending v5, + the `has_external_symbols` meta flag — set by a
  FULL build only, never by `apply_update`/`enrich_references`, which cannot
  classify a whole store from a partial index), surfaced as an
  `is_out_of_project` field (true/false/null) on `explain_symbol` and on every
  `find` result — grouped overload arms agreeing on the top-level value, null
  when they disagree — a `out of project: yes/no/unknown` line on CLI
  `explain`, and an `[out-of-project]` marker on CLI `find` results (external
  only), plus a `has_external_symbols` capability line in `status`, on the CLI
  and as MCP tools alike; the key/line/marker is omitted entirely on graphs
  built before this feature. Degrades cleanly (never a crash) on older stores.
  **Cost caveat:** this is always-on at build time, not opt-in — on the mongo
  corpus the 174,436 external symbols grow the node count by ~21% (each
  carrying the metadata above), and nodes for symbols that were previously
  absent entirely.
- **`ForwardDefinition` bit fix** (`scip-clang-patches/forward-definition-on-v0.4.0.patch`):
  fixes the declaration-site phantom-caller bug on STOCK-shaped graphs too (not
  just #504 graphs, which were already protected) — a bodyless declaration
  occurrence is now tagged at the source instead of landing as an
  indistinguishable role-0 site. Verified end-to-end (stock official binary
  fabricates the phantom caller on a fixture; our patched binary doesn't). No
  cppgraph-side change needed — `build_graph` already filtered on
  `DEFINITION | FORWARD_DEFINITION` in anticipation.
- **`ReadAccess`/`WriteAccess` syntactic classifier** (`scip-clang-patches/read-write-access-on-v0.4.0.patch`):
  a scip-clang patch tagging reference occurrences as reads/writes based on
  their syntactic AST parent (assignment, compound assignment, `++`/`--`,
  overloaded-operator forms; constructor-initializer field references are
  unconditional writes). Syntactic only, no dataflow.
  Consumed by cppgraph: `Reference.roles` (store schema v5, `refs.roles` +
  `has_access_roles` meta flag), surfaced as `(write)`/`(read+write)`
  annotations and a new `--access {read,write}` filter on `cppgraph references`
  / MCP `find_references`. Degrades cleanly (no annotation, filter refused with
  a clear reason) on a graph built without this patch.
- **`SymbolInformation.kind` syntactic classifier** (`scip-clang-patches/kind-on-v0.4.0.patch`):
  a scip-clang patch filling SCIP's `SymbolInformation.kind`, which upstream
  leaves at `UnspecifiedKind` on 100% of symbols: `classifySymbolKind` in
  `Indexer.cc` maps the `clang::Decl` at each `SymbolInformation`-creating
  site to its kind (Class/Struct/Union/Enum, EnumMember, Field,
  Function/Method/StaticMethod/PureVirtualMethod/Constructor,
  Variable/StaticDataMember, Namespace, TypeAlias; macros and the synthetic
  file symbol get Macro/File), carried through the TU-merge pipeline by a new
  `SymbolInformationBuilder::kind` field. Syntactic classification only, no
  semantic analysis — local variables and parameters get no
  `SymbolInformation` emitted today, and destructors classify as Method (SCIP
  has no Destructor kind). Fourth patch in the bundle (`patchset_version` 3 →
  4 in `versions.json`), order-independent with the other three.
  Consumed by cppgraph: `symbols.scip_kind` (store schema v5, +
  `has_symbol_kind` meta flag), surfaced as a `scip_kind` field on
  `explain_symbol`/`find` and a `has_symbol_kind` capability line in `status`,
  on the CLI and as MCP tools alike. Degrades cleanly (field absent, never a
  crash) on a graph built without this patch or an older store.
- **`SymbolInformation.signature_documentation` syntactic printer**
  (`scip-clang-patches/signature-documentation-on-v0.4.0.patch`): a scip-clang
  patch filling SCIP's `SymbolInformation.signature_documentation`, which
  upstream never sets (`set_signature_documentation` appears nowhere in the
  indexer — measured 0/810,919 on the corpus): `declToSignatureText` in
  `Indexer.cc` pretty-prints the declaration of function/method decls only
  (clang `PrintingPolicy` with `TerseOutput` — no body, default arguments
  preserved) at `saveFunctionDecl` sites, carried through the TU-merge
  pipeline (`SymbolInformationBuilder`/`DocumentBuilder::merge`,
  first-non-empty-wins semantics) exactly like the `kind` patch. Scope/limit:
  only defined or pure-virtual function/method declarations get a signature —
  bodyless in-class declarations (routed through `saveForwardDeclaration`, no
  `SymbolInformation` emitted) do not, in v1. Fifth patch in the bundle
  (`patchset_version` 4 → 5 in `versions.json`), order-independent with the
  other four. Verified end-to-end with a real compiled build against a
  fixture (`int add(int a, int b = 2)` → `sig_doc.text == "int add(int a, int
  b = 2)"`). Consumed by cppgraph: `Node.signature_documentation` (store
  schema amends the pending v5 — ungated, a plain nullable column like
  `documentation`, not a `has_*`-gated one like `scip_kind`), surfaced as a
  `signature_documentation` field on `explain_symbol` and a
  `signature (stored):` line on CLI `explain`, distinct from the
  source-derived `--root` `signature:` line, on the CLI and as MCP tools
  alike. Degrades cleanly (field absent, never a crash) on a graph built
  without this patch or an older store. No proto changes needed — the field
  already exists in the vendored `scip.proto`.
- **`Relationship.is_type_definition` emitter (`typed-by` edges)**
  (`scip-clang-patches/typed-by-on-v0.4.0.patch`): a scip-clang patch filling
  SCIP's `Relationship.is_type_definition` ("go to type definition"), which
  upstream never sets (measured 0/260,223 relationships on the corpus): a
  syntactic type resolver (`tryResolveDeclaredTypeDecl` in `Indexer.cc`)
  attaches, to a field's / variable's own `SymbolInformation.relationships`, a
  `{symbol: <type>, is_type_definition: true}` entry — source = the
  field/variable, destination = its declared type — reusing
  `trySaveTypeReference`'s exact type-resolution policy (typedef/alias → the
  typedef declaration; tag type → the tag declaration; template
  specialization → the primary templated declaration, so `std::vector<Foo>`
  gets a single edge to `std::vector`; template arguments are not followed),
  after stripping pointer/reference/cv-qualifier wrappers (`const Foo&` →
  `Foo`). Builtins, unresolvable `auto`, dependent types — anything without a
  compiler-resolved SCIP symbol — silently emit no relationship (only exact,
  compiler-traced facts). Scope/limit (v1): only declarations which get a
  `SymbolInformation` today — fields (`saveFieldDecl`), file-scope variables
  and static data members (`saveVarDecl`'s `SymbolInformation` branch); local
  variables and parameters (`saveDefinition(std::nullopt, ...)`) are
  untouched. Sixth patch in the bundle (`patchset_version` 5 → 6 in
  `versions.json`), order-independent with the other five — its
  `saveFieldDecl`/`saveVarDecl` insertions are anchored on lines no other
  patch touches, and the `emitDocumentOccurrencesAndSymbols` hunk carries
  `-U2` context (its `-U1` context also closes `MacroIndexer`'s sibling
  function — same failure mode the `forward-definition` exception covers).
  Consumed by cppgraph as a new `typed-by` edge kind (field/variable → its
  declared type): `impact` on a type returns the fields/variables typed as it
  ("are typed as" in the output), `reachable-from` from a field/variable
  returns its type, `boundary-violations` accepts it as an explicit
  `--kind`/`edge_kinds` (opt-in — the default kinds stay `calls,inherits`).
  Degrades cleanly (no `typed-by` edges, never a crash) on a graph built
  without this patch or an older store.
- **Dual versioning for the scip-clang patch bundle**: `versions.json`'s
  `scip_clang.patchset_version` now tracks our own patch bundle independently
  of the upstream `version` pin (history: 1 = #504 `enclosing_range` only; 2 =
  + `ForwardDefinition`; 3 = + `ReadAccess`/`WriteAccess`; 4 = +
  `SymbolInformation.kind`; 5 = + `SymbolInformation.signature_documentation`;
  6 = + `Relationship.is_type_definition`).
  `cppgraph status`
  advises when an installed patched binary's patchset predates the pin.
- All six scip-clang patches (`enclosing_range`, `forward-definition`,
  `read-write-access`, `kind`, `signature-documentation`, `typed-by`) verified
  mutually **order-independent** — any of the 720 possible application orders
  produces byte-identical final files, and each applies standalone. Matters
  for a future independent upstream PR per patch.

### Changed

- Binary "variant" renamed `"504"` → `"patched"` throughout (sidecar JSON,
  release tag/asset names — `scip-clang-patched-v<version>-p<patchset>` — CLI
  wizard labels, scripts). Old `"504"`/`"enclosing_range-504"` sidecars are
  still recognized (`PATCHED_VARIANTS`) for backward compatibility.
- `docker/build-scip-clang` → `docker/build-scip-clang-patched-linux`,
  `scripts/build-scip-clang-macos.sh` → `build-scip-clang-patched-macos.sh`,
  `scripts/publish-scip-clang-504.sh` → `publish-scip-clang-patched.sh` — all
  three renamed for clarity (explicit about platform/variant, no longer
  accurate to say just "504" now that the bundle carries three patches).
  Patch files moved to a shared top-level `scip-clang-patches/` (used by both
  the Docker build and the macOS native build script).

## [0.2.0] - 2026-09-08

Second release: the read surface grows from graph traversal to whole-graph
analysis — ranking and stats, file/class structure, architecture facts,
`file:line` symbol resolution, and doc comments in `explain` — with the store
schema moving v2 → v4 along the way (`symbols.end_line` at v3, then
`symbols.documentation` at v4). Older stores keep working; a store rebuild
from the existing `.scip` picks up the new columns — no re-index.

### Added

- **`skills/cppgraph/SKILL.md`**, an Agent Skill steering an agent toward
  cppgraph's tools over the `Read`/`grep` reflex — for graph questions (who
  calls X, impact, references, cycles, …) and everyday structural ones
  (`outline` over `Read`, scoped `find` over `grep`, `explain_symbol` over
  opening a header) — plus a correction of an observed misconception
  (`find_references`/`who_calls` take a plain name directly, no two-step
  resolve). `cppgraph setup` now installs it automatically into
  `~/.claude/skills/cppgraph/` and/or `~/.config/opencode/skills/cppgraph/`,
  whichever agent is detected on the machine.
- **Existing-checkout guard in the install instructions** (`README.md`,
  `AGENTS.md`): an agent asked to just read/explore cppgraph's source now
  checks for an existing per-machine checkout
  (`${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo`) before cloning
  anything — reusing/pulling it instead of creating a second, divergent
  clone elsewhere (the exact confusion a stray `~/cppgraph` caused in
  practice: a stale checkout answering from an old commit).
- **No-per-project-install guard** (`README.md`, `AGENTS.md`): never
  `pip install cppgraph` into a target project's own venv — it's a single
  per-machine tool with one dedicated venv
  (`~/.local/share/cppgraph/repo/.venv`); invoke its binaries from there
  directly (an `alias cppgraph=".../repo/.venv/bin/cppgraph"` snippet is
  included). Re-installing it into another project's environment creates a
  second divergent install whose own pinned deps can conflict with the
  committed generated bindings (observed: a project venv's pinned `protobuf`
  vs cppgraph's).

### Fixed

- **`protobuf` dependency floor** (`pyproject.toml`): was `>=5.0` (no upper
  bound), but the committed generated bindings (`scip_pb2.py`, produced by
  protoc 35.1) require a Python `protobuf` runtime of major version 7
  (`ValidateProtobufRuntimeVersion(7, 35, 1, …)`) — an environment that already
  had an older `protobuf` satisfying the loose `>=5.0` floor (e.g. 5.29.3)
  broke at import with `VersionError: Detected mismatched Protobuf
  Gencode/Runtime major versions`. Tightened to `>=7.35.1,<8`, matching the
  committed gencode.


- **`no_incoming_calls`**: callable definitions with zero incoming `calls`
  edges — the exact primitive behind the "dead code" question, stated as a
  graph fact, never a verdict (vtable dispatch, exported API, templates, entry
  points all have no static caller yet are live; the MCP response carries that
  caveat as a standing `note`). Gated on the same `has_enclosing_ranges` signal
  as `line_span`: on a stock-binary graph it refuses with the reason — the
  nearest-preceding attribution fallback can fabricate a phantom caller from a
  bodyless declaration site, turning a real 0 into a false 1 — rather than
  answer unreliably. On the CLI and as an MCP tool, both backed by the same
  `GraphStore.no_incoming_calls` (a SQL anti-join over `edges`, callability
  from the shared `is_callable_symbol`); bounded output (`limit` + `total`),
  same `exclude_tests`/path-prefix filters, and only symbols with a recorded
  definition site (a test-only caller still counts as a caller).
- **`line_span`**: definitions ranked by body extent (`end_line - start line`,
  largest first) — where the biggest bodies live, from the exact
  `enclosing_range` extents, not a def→next-symbol heuristic. #504-only: on a
  stock-binary graph the tool reports `available: false` with the rebuild
  pointer (the same degrade-cleanly contract as the reference-attribution
  features) instead of a silently empty list. On the CLI and as an MCP tool,
  both backed by the same `GraphStore.line_span`; bounded output (`limit` +
  `total`) and the same `exclude_tests`/path-prefix filters as `hotspots`.
  Persists `Node.end_line` in the store — **schema v3**: older stores keep
  working (a store rebuild, or an incremental `update` that actually touches
  affected files, adds the column on demand), and an older
  cppgraph refuses a v3 store with the usual upgrade/rebuild error.
- **`stats`**: module-level aggregate counts per file (`--group-by file`) or
  rolled up per directory via `dirname` (`--group-by dir`) — symbols defined,
  `calls` edges whose call site, and reference use sites, sorted by the three
  summed descending. A "how big / how dense is this part of the codebase" view
  to size up an unfamiliar module without reading files, next to `hotspots`'
  "what's most-called". On the CLI and as an MCP tool, both backed by the same
  `GraphStore.stats` (three SQL `GROUP BY file_id` aggregations merged, dir
  rollup in Python); bounded output (`limit` + `total`), and the same
  `include_paths`/`exclude_paths` prefix filters as `hotspots`, applied to the
  counted file before aggregation.
- **Per-query staleness flag (`stale`), no auto-update**: every query tool's
  response (MCP) and every query command's stderr (CLI) now carries a cheap
  "has this graph drifted from its source commit?" signal — a single `git
  diff` against the recorded commit (reusing `changed_files_since`/dirty
  fingerprints), never a rebuild. MCP's `_call` attaches `stale` best-effort
  (a failure to compute it never crashes an otherwise-successful query); the
  CLI's `_open_store_checked` prints a one-line warning, resolving `root` from
  the store's own recorded `project_root` (falling back to `discover_graph`)
  so an explicit `--graph` pointing at another project never diffs the wrong
  checkout. External validation of the same pattern: a competing tool, Graft
  (nanonets/graft), runs an equivalent structural freshness check before every
  query — independent evidence this is worth doing cheaply rather than only
  on an explicit `status` call.
- **`hotspots`**: ranks symbols by fan-in, fan-out, or total edge count across the
  whole graph — one call instead of N manual `who_calls`/`what_it_calls` queries.
  On the CLI and as an MCP tool, both backed by the same `GraphStore.hotspots`.
  Bounded output (`limit` + a `total` count), `exclude_tests` supported like other
  query tools.
- **`include_paths`/`exclude_paths`**: per-query `Node.file`-prefix filtering
  (simple prefix match, no glob/regex) on `find`, `who_calls`, `what_it_calls`,
  `find_references`, `impact_of`, and `hotspots` — on both the CLI
  (`--include-path`/`--exclude-path`, repeatable) and the MCP tools, driven by
  the shared `cppgraph.filters.matches_path_prefix`/`filter_by_path`. Scopes a
  query to "my code, not vendored deps" (e.g. `--exclude-path vendor/`).
  `hotspots` applies the filter in SQL (a `cpg_path_ok` function registered
  alongside the existing `cpg_is_test_file`), keeping its whole-graph
  aggregation off the Python side.
- **`transport` in `status`**: `"cli"` or `"mcp"`, so a copied status block (e.g.
  from a Task subagent without direct MCP access) says which surface produced
  it instead of resting on an agent's say-so.
- **`signature` in `explain`/`explain_symbol`**: a source-derived parameter list
  for the definition, including any default argument value verbatim (e.g.
  `(bool useNullIfMissing = false)`) — invisible from the graph alone, which
  never carries a parsed signature. Needs `--root`/a configured checkout, like
  the existing `include_source`. Shares `extract_signature` with `find`'s
  overload-signature grouping (moved to `cppgraph.cli` alongside
  `read_source_snippet`, its only dependency).
- **`CLI_REFERENCE.md`: one page for the whole CLI.** `cppgraph --help` lists 30
  subcommands, each with its own `--help`, but there was no single page a
  human could read to pick the right command (QUICKSTART.md showed 3 examples).
  The new reference doc groups them by purpose (getting/keeping a graph,
  finding a symbol, call-graph traversal, types/files/members, where a symbol
  is used, whole-graph facts, reading/viewing) with one real example per
  command and the shared facts stated once (graph auto-discovery, plain-name
  resolution, `*` = required). Drift-proofing per the TODO item's own ask: the
  per-command table rows (name, aliases, purpose, arguments) are generated
  from the argparse subparsers by `scripts/gen-cli-reference.py` — it captures
  the parser `cli.main()` builds (there is no standalone build_parser to
  import), rewrites the tables between `cppgraph-gen` markers in the markdown,
  and exits non-zero naming any subcommand not yet placed in a section, so a
  new command cannot silently miss the doc. The grouping/prose/examples are
  curated in the markdown; `tests/test_gen_cli_reference.py` fails if the
  checked-in page is stale. Pointers added in README.md, QUICKSTART.md, and
  scripts/README.md. Closes the "Human-oriented CLI reference doc" TODO.
- **`status`: estimated store cost on the usage-view upgrade hint.** The
  file→symbol granularity hint said "costs extra store space" with no number;
  it now computes one from the graph's own `ref_count`. Measured A/B first (the
  TODO item's own demand — no hardcoded guess): rakiz80, a real 224-TU
  project, indexed with the local #504 binary, the same `.scip` built both
  ways — `--references` 4,681,728 B vs `--references --attributed-refs`
  4,714,496 B, the same 84,598 reference rows (16,497 attributed) ⇒ **0.39
  bytes/ref** (`BYTES_PER_ATTRIBUTED_REF` in `cppgraph.updates`; the comment
  carries the full provenance so a future change to scip-clang or the store
  schema can be re-measured against it). Scaled by the TOTAL ref count — the
  figure every `status` already reports — so no attribution-rate guess is
  needed for an arbitrary codebase. Worded as what it is, an extrapolation
  from one different-scale measurement ("larger/smaller codebases may scale
  differently"), never as a measurement of this graph — the in-place
  `enrich-refs` path measured ≈20 B/ref on mongo (+146 MB, DESIGN.md), a
  different write path, which is exactly why the hedge is explicit. Degrades
  to the old number-free wording when `ref_count` is 0/unknown (no fabricated
  "~0 bytes"), and a graph that already has attribution never sees the hint
  at all. Same sentence on CLI and MCP (`attributed_refs_cost_note`, the
  shared pure function). Closes the "Show the storage cost of the
  symbol-granularity upgrade in `status`" TODO.
- **`explain`/`explain_symbol`: doc comments straight from the graph
  (`SymbolInformation.documentation`).** scip-clang populates `documentation`
  on 99.99% of corpus symbols, but most of it is auto-generated text: the
  literal `"No documentation available."` placeholder (84.45%), `namespace X`
  / `inline namespace X` on namespace symbols, `File: Y` on the synthetic file
  symbol (SCIP_AUDIT.md). The builder now keeps only the genuine extracted
  doc comment on the node — `real_documentation`
  (`src/cppgraph/builder.py`) filters the placeholder, the namespace/File
  auto-text, and the exact `"anonymous namespace"` string (4,217 corpus
  symbols, 0.52%, which the audit's 10.61% "genuine" bucket had lumped in —
  re-measured here as 81,844 kept of 810,919, 10.09%) so no boilerplate is
  ever surfaced as if it were a doc comment (a placeholder posing as
  documentation is a fabricated-looking "fact"). The proto field is `repeated
  string`; entries are newline-joined before the filter — unobservable on
  real data, measured: every corpus `SymbolInformation` carries at most ONE
  entry. Capture is first-real-wins across a header's duplicate
  `SymbolInformation`s (a placeholder-only visit doesn't block a later real
  comment; a captured one is never overwritten). Surfaced where `signature`
  can't go without: no `--root`/checkout needed — `explain_symbol` gains a
  `documentation` field (present only when there is real text, absent never
  placeholder) and CLI `explain` a `documentation:` line, both reading the
  store. Store: new `symbols.documentation` column, schema **v4**, the exact
  `end_line`/v3 migration precedent — `write_sqlite` writes it,
  `apply_update`/`enrich_references` ALTER it in on older stores (fresh
  definition site replaces the text, cleared with the site, so a deleted
  comment doesn't linger), and `get_node` degrades on unmigrated v3/v2 stores
  (`documentation=None`) instead of crashing. No meta gate, unlike
  `has_enclosing_ranges`: a doc comment is optional payload, not a
  correctness-critical signal — `None` per symbol is the whole story.
- **Indexing progress: consume scip-clang's per-TU report instead of
  suppressing it.** `run_scip_clang` dropped `--no-progress-report` (inherited
  from the deleted `reindex.sh` with no recorded rationale) and now streams the
  binary's stdout instead of blocking on a silent `subprocess.run`. Verified
  against the v0.4.0 binary first: the report is one `[N/total] Indexed <file>`
  line per TU on **stdout** (flushed as it goes; stderr stays empty, so it is
  left inherited for live error output — a single piped stream, no deadlock),
  plus one `[N/total] Merged partial index` line per TU and a final
  `Finished indexing …` summary — so progress parses the `Indexed` prefix
  (N is a completion-order counter, space-padded) rather than counting lines,
  which the Merged lines would double. The denominator comes from the caller
  (`filter_compdb`'s `kept` on a full build, `len(matched)` on an incremental
  update), both known before the run starts. Re-emission is cadence-bound: on a
  TTY a single in-place line (`\r`, redrawn at most every 0.3 s) with
  `N/total`, elapsed and a linear ETA; under a pipe one newline-terminated line
  every 5% of the total or every 60 s, whichever comes first — ~21 lines for a
  300-TU run measured end-to-end (the raw report is ~2 lines per TU), so the
  reported ~5,955-line flood into an agent's context becomes a couple dozen
  lines whatever the TU count. A caller without the denominator
  (`total_tus=None`) degrades to a plain `N indexed` count, no percentage/ETA.
  The `Finished indexing …` summary line and the nonzero-exit `PipelineError`
  are unchanged.
- **`explain_symbol`: zero-caller reliability flag.** `explain`'s caller count
  has always reported `total` including `0` — what was missing is the
  reliability signal `no_incoming_calls` already gates on: a `0` is only
  trustworthy with exact (#504) attribution. On a stock-binary graph the
  nearest-preceding fallback can fabricate a phantom caller from a bodyless
  declaration site *and* drops a call site that precedes every callable
  definition in its document, so a reported `0` can be a false negative. When
  `callers.total == 0` and the store lacks `has_enclosing_ranges`, the MCP
  `callers` block now carries `zero_callers_reliable: false` plus a `note`
  with the reason and the rebuild pointer — the same gate `no_incoming_calls`
  refuses on, stated as a caveat that travels with the reported fact instead —
  and CLI `explain` prints the same caveat after its `0 caller(s)` line. No
  caveat anywhere else: a `0` on a #504-shaped graph is exact (containment
  attribution), and a nonzero count never carries one — over-capture is the
  documented safe direction (an extra caller is verified, never acted on by
  omission; DESIGN.md "Known limitation"). Closes the "Remaining item" of the
  declaration-site phantom-caller TODO bullet.
- **`SCIP_AUDIT.md`**: an exhaustive (not sampled) field-by-field measurement
  of what `scip-clang` v0.4.0 actually populates — `SymbolInformation.kind`,
  `documentation`/`signature_documentation`, `display_name`,
  `enclosing_symbol`, every `Relationship` flag, every `symbol_roles` bit,
  `syntax_kind`, `enclosing_range`, `external_symbols`, and more — against a
  real ~810k-symbol MongoDB index plus a #504 fixture. Written because a
  prior assumption (`is_type_definition` "already in every `.scip`") turned
  out false; corrects one in the other direction too (`documentation` was
  believed 0% populated, measured 99.99% non-empty, 10.61% genuine doc
  comments once the `"No documentation available."` placeholder is excluded).
  `TODO.md`'s `scip-clang (upstream)` bullets are updated from "suspected"
  to the verified numbers; `FOLLOWUP.md` gains a ranked upstream-PR shortlist
  distilled from the findings.
- **`global_init_references`**: which globals a global's initializer references
  — the graph fact behind the "static initialization order fiasco" (global A's
  initializer reads global B; across translation units the initialization
  order is unspecified, so the read may see an uninitialized B). A fact, never
  a verdict, per the house rule: a `constexpr`/`constinit` initializer is
  constant-initialized and safe — the tool reports the reference and the LLM
  judges the hazard (a standing `note` on every response, both surfaces). The
  builder change is one `elif`: term (global/field) definitions now also feed
  their `enclosing_range` intervals into the SAME `usage_intervals` the
  reference-attribution containment sweep already used for callables/types —
  a #504 `saveVarDecl` extent spans the whole declaration *including the
  initializer* (verified empirically on the locally-built #504 binary), so an
  initializer's read of another global is attributed to its global with zero
  new machinery. Terms are identified by SCIP descriptor (`.` but not the
  method `).`) — `is_term_symbol`, the same grammar
  `is_callable_symbol`/`is_type_symbol`/`_is_direct_member` already read —
  which is also what excludes locals: MEASURED, local variables (and
  function-scope statics) DO carry `enclosing_range` data, but as `local <id>`
  symbols, so the descriptor check keeps them from ever stealing a reference
  from their enclosing function. Term intervals feed the *usage* sweep only,
  never `callable_intervals`: a call inside a global's initializer stays an
  uncontained call site (dropped by the #504 declaration rule, as before) —
  its *reference* record, though, now attributes to the global, so the usage
  view gains "used by <global>" rows it previously left at file granularity
  (as it does for field declaration lines, which now attribute to the field —
  the innermost container — instead of the class). One honest limitation,
  measured and pinned as a test rather than assumed away: a lambda inside a
  global's initializer gets NO symbol and NO interval of its own from
  scip-clang, so a read inside the lambda body attributes to the global (the
  TODO hoped innermost-wins would pick the lambda; there is no lambda interval
  to win) — the note says such a read may run lazily or never, and the tool
  states the region fact. The query is `GraphStore.global_init_references`
  (lookup over the attributed refs: `refs.enclosing_id = <global>` where the
  referenced symbol is itself a term; one row per referenced global at its
  first use site; callable uses in the region are not global reads), gated on
  `has_attributed_refs` like the other attribution-dependent features —
  `None`/`available: false` with the exact rebuild pointer (a #504 index AND
  `--attributed-refs`/`enrich-refs`) on anything less, never a silently empty
  list; unknown/non-global symbol is an error, the `class_members` contract.
  Pure fn + `@mcp.tool()` in the MCP server, `global_init_references`
  subcommand in the CLI — same layering as `line_span`/`no_incoming_calls`.

- **`file:line` symbol resolution**: every symbol-taking tool now accepts a
  definition location (`"who calls the function at foo.cpp:120?"` works
  without knowing its name). Implemented in the one shared resolution step,
  `GraphStore.resolve` — the single name->symbol funnel the CLI's
  `_resolve_symbol` and the MCP `_resolve` both already call — so `who_calls`,
  `callers`, `callees`, `path`, `impact_of`, `explain_symbol`, … all gain it
  with zero per-tool changes (CLI/MCP parity by construction, not by
  duplication). The trailing `:<positive integer>` shape is recognized first:
  neither a SCIP symbol string (it always ends in a descriptor — `.`, `#` or
  `)`) nor a C++ name (a lone `:` is not an identifier character) can have it,
  so the recognition is unambiguous and never hijacks e.g. an
  anonymous-namespace symbol embedding `path:line:col`. The LAST colon splits
  (a Windows drive-letter path carries a `:` of its own), the file is matched
  exactly against the recorded path — `outline`'s convention, backslashes
  normalized, no fuzzy/suffix matching — and the line is 1-indexed (editor
  convention; the store keeps 0-indexed, converted once on input — the inverse
  of the `+1` every display path applies). Two-tier lookup through the same
  three-outcome contract as names: a definition whose own start line is
  exactly that line; then — only on a `has_enclosing_ranges` (#504) store —
  the innermost (narrowest-span) definition whose `[line, end_line]` contains
  the requested line, so a line inside a function body resolves to that
  function, not its enclosing class. Several symbols sharing the exact line,
  or tying on the narrowest containing span, return the ambiguous
  candidates-outcome; a body line on a stock store is an honest no-match,
  never a nearest-definition guess (the confidently-wrong failure mode
  cppgraph exists to avoid).

- **`api_surface`**: the actually-used external surface of a module — which
  definitions under a directory prefix are called or referenced from *outside*
  it. The observed surface, not the declared one: SCIP encodes no C++
  visibility, so "used from outside" is the exact fact cppgraph can state
  (facts-not-judgments: a measured surface, never an API-design verdict).
  Onboarding/module-overview use case — one call replacing N manual
  `what_it_calls` + `find_references` fan-outs over a directory. One boundary
  predicate over two already-exact use sources: a `calls` edge counts when
  the callee's own definition file is under the prefix and the call site
  (`edges.file_id`) is not; a reference counts the same way, and `refs.file_id`
  needs no attribution — it *is* the exact use-site file, which is why the
  plain `--references` index suffices here (unlike the #504-gated
  symbol-granularity features). The two are combined in a single SQL
  aggregation: `UNION ALL` of the two sources, one row per use tagged with
  its kind (the per-role shape `hotspots` uses), then one `GROUP BY` splits
  them back apart — `external_calls`/`external_refs` reported as **separate**
  counters per symbol (the per-column detail `stats` gives for symbols/edges/
  refs, not one blob number) and ranked by their **sum** descending; id-space
  until the final rows resolve to symbol strings, the `hotspots` discipline.
  On a store built `--no-references` the answer degrades cleanly to call
  sites only — the store returns `(ranked, total, has_refs_data)` and both
  surfaces carry `refs_available: false` plus a note naming the rebuild —
  never a bare `external_refs == 0` that would read as "never referenced
  outside" (a module whose surface is only types would be invisible: exactly
  the misleading-zero case). Prefix membership is `matches_path_prefix` as
  the SQL function `cpg_under_prefix` (the `boundary_violations` pattern:
  path-segment boundaries, `mod` matches `mod/util.cpp`, never
  `mods/util.cpp`); `exclude_tests` drops a use when *either* side of the
  boundary is a test file (`hotspots`' either-endpoint shape); an empty
  prefix raises `ValueError` (it would match nothing and read as "empty
  surface") — a parser error on the CLI, an error dict on MCP. CLI:
  `api-surface <prefix>` (positional, like `class-members`) with
  `--limit`/`--exclude-tests`/`--full-symbols`; MCP tool `api_surface`; both
  thin wrappers over the same `GraphStore.api_surface` — bounded output
  (`limit` + `total` + `truncated`), and a zero-result note pointing at
  `stats` (a wrong prefix is the usual cause, the `outline` convention).

- **`reachable_from`**: forward transitive reachability — the exact mirror of
  `impact_of` (reverse): from an entry point, everything it transitively
  reaches along `calls` edges ("what can this external handler trigger?" —
  attack-surface mapping; also dependency-migration scope). Per the
  `DESIGN.md` corollary the result is a **lower bound** — static
  compiler-traced edges only; virtual dispatch, function pointers and runtime
  registration have no static edge, so the true reachable set may be larger —
  and it is *worded* that way on both surfaces, never as a set the reader may
  act on by exclusion: a standing `note` on every MCP response and a printed
  caveat line on the CLI ("at least these are reachable"). Algorithmically a
  near-direct port of `GraphStore.impact`: the same id-space BFS over the
  `edges` index, walking `src_id -> dst_id` instead of `dst_id -> src_id`
  (`ix_src` instead of `ix_dst`), symbol strings materialized only for the
  final result set. `kind="inherits"` is kept for parity with `impact` and is
  coherent forward: `inherits` edges run derived -> base, so the forward walk
  from a derived type is its transitive *base hierarchy* — the mirror of
  `impact --kind inherits`'s transitive subclasses. One forward-only special
  case: `kind="calls"` on a *type* returns a notice, not a bare `total: 0` —
  a type makes no calls itself, so its reachability lives in its methods
  (pointed at `class_members`), the same "never a misleading zero" rule as
  `impact_of`'s type redirect to `find_references`. Single-symbol signature
  (`reachable_from(symbol)`), matching `impact_of`'s shape: attack-surface
  mapping is naturally one entry point at a time. CLI: `reachable-from
  <symbol>` with `--depth`/`--kind` and the shared query filters; MCP tool
  `reachable_from`; both thin wrappers over the same
  `GraphStore.reachable_from` — bounded output (`limit` + `total` +
  `truncated`), `exclude_tests` on by default, `include_paths`/
  `exclude_paths` on the result set.

- **`dependency_cost`**: call-site count against a target library — "if I
  replace/remove this library, how many call sites change?", answered as an
  exact count of `calls` edges whose *callee* is defined under one of the
  given path prefixes (e.g. `spirv_cross/`). A fact, never a risk assessment:
  it says nothing about API compatibility or migration difficulty, and — like
  every static call graph — it counts compiler-bound call sites only. Landed
  as a *mode* of `hotspots`, per the TODO's intent, but **not** "for free" as
  the TODO optimistically assumed: `hotspots`' path filtering was symmetric
  (an edge counts only if *both* endpoints' definition files pass the same
  prefix), which cannot express "callee inside the prefix, caller anywhere" —
  symmetric `include_paths=["spirv_cross/"]` counts only *library-internal*
  fan-in (and there is none: no edge has both endpoints there), a different
  question. So `GraphStore.hotspots` gained `target_paths`: the callee side
  is pinned to symbols defined under the prefixes (`cpg_target_ok`, the same
  registered-`matches_path_prefix` SQL pattern as `cpg_path_ok`), while
  `include_paths`/`exclude_paths` switch to the **caller side only** —
  "call sites into `spirv_cross/` from my code, not from other vendored users
  of it" (`exclude_paths=["vendor/"]`; vendored callers count by default).
  `kind` must be `fan_in` there (the mode *is* an incoming-call-site count;
  anything else raises `ValueError`, matching `hotspots`' existing defensive
  validation), `exclude_tests` keeps its symmetric either-endpoint shape, and
  `target_paths=None` (the default) preserves today's symmetric semantics
  exactly — a fully backward-compatible additive change (pinned by a
  dedicated regression test). `hotspots` also accepts `limit=None` now (the
  uncapped ranking, for callers that aggregate over it). The headline number
  is added only at the presentation layer: the pure
  `dependency_cost_report` / the `dependency-cost` CLI subcommand report
  `total_call_sites` (the summed "N call sites" answer) and
  `distinct_target_symbols` alongside `top_targets` (the per-symbol
  breakdown, heaviest first, bounded by `limit`; the totals are always the
  full counts, so a capped list never under-states the headline) — both
  surfaces backed by the same `GraphStore.hotspots(kind="fan_in",
  target_paths=…)`, no duplicated SQL. CLI: `dependency-cost --target-path
  PREFIX` (repeatable, required) with `--limit`/`--exclude-tests`/
  `--full-symbols` and the caller-side `--include-path`/`--exclude-path`;
  MCP tool `dependency_cost` takes `target_paths` as a list.

- **`strongly_connected_components`**: the cycles of the call graph —
  strongly-connected components of the `calls` subgraph with more than one
  member, i.e. maximal sets of symbols that can all reach each other, the
  exact primitive behind "circular dependencies". A graph fact, never a
  verdict: mutual recursion is often completely legitimate (a visitor
  pattern, a recursive-descent parser's mutually-recursive rules), and the
  standing `note` says so — the LLM decides which cycles, if any, matter.
  Algorithmically the first whole-graph traversal among the ranking tools:
  iterative Tarjan over the `calls` edges in id-space (all `(src_id, dst_id)`
  pairs fetched in one scan, adjacency walked as integers, symbol strings
  resolved only for the components actually returned — the same
  "hot topology all-integer, cold payload materialized late" discipline as
  `hotspots`; iterative because a call graph has thousands of nodes and the
  recursive form would overflow Python's ~1000-frame limit on a deep chain).
  `exclude_tests`/path filters apply to the *output*, not the edges Tarjan
  sees — a cycle genuinely involving test-only or vendored symbols is still a
  real cycle in the compiled binary, and pre-filtering edges could split or
  hide one — so a component is dropped only when *every* member is filtered
  out, and a reported component always lists all its members (redacting
  members would misrepresent the compiled dependency). Direct self-recursion
  (a degenerate 1-node cycle) is out of scope by spec: components of
  size > 1 only. On the CLI (`strongly-connected-components`) and as an MCP
  tool, both backed by the same `GraphStore.strongly_connected_components`;
  components sorted biggest first, members by definition `file:line`; bounded
  output (`limit` caps components, `total` the full count).

- **`outline` / `class_members`**: list definitions by container — two facets
  of one primitive, both exact and available on any graph (no #504 needed),
  no judgment, structure not interpretation. `outline(file)` is the outline
  of a single file: every symbol *defined* there (`Node.file == path`),
  sorted by line — a compact symbol list that replaces a `Read` of a
  1400-line file, the tool that beats the Read/grep reflex on its own turf.
  `class_members(symbol)` is every member declared on a class/struct —
  methods, fields, nested types — found by SCIP container nesting: a member's
  symbol string starts with its class's own symbol string, which ends in `#`,
  so the boundary is exact (`mongo/Foo#` prefixes `mongo/Foo#parse(a1).`,
  never `mongo/FooBar#x.`) and no new symbol parsing is involved. Named
  `class_members`, not `public_api`, because SCIP doesn't encode C++
  visibility — members that *exist* (a fact), not a public/private claim.
  On the CLI (`outline <file>`, `class-members <symbol>`, both with
  `--limit`/`--full-symbols`) and as MCP tools, both backed by the same
  `GraphStore.outline`/`GraphStore.class_members`; bounded output (`limit` +
  `total`); `outline`'s path match is exact (an empty result carries a note
  pointing at `stats`, not a bare zero); `class_members` on an unknown symbol
  follows the shared resolution convention (error dict / candidates, never a
  guess) and on a known non-type symbol returns an error dict — bad input,
  never an empty list that would read as "no members".

- **`boundary_violations`**: declared-layering conformance check — given rules
  supplied per-query by the caller (`("common/", "platform/")` = "no symbol
  defined under `common/` may call one defined under `platform/`"; the graph
  stores no intended architecture of its own), lists the `calls`/`inherits`
  edges that cross them. Each reported violation *is* a real compiler-traced
  edge — zero false positives by construction — and the standing note states
  the converse as a lower bound (0 violations means no *statically indexed*
  edge crosses the rules; runtime dispatch — virtual calls, function pointers
  — can cross a boundary with no static edge), never a proof of conformance.
  Directory membership is the endpoint's own definition file, matched on a
  path-segment boundary via the shared `cppgraph.filters.matches_path_prefix`
  (registered as a SQL function, the same pattern as `hotspots`'
  `cpg_path_ok`); a symbol with no definition site belongs to no layer. On
  the CLI (`boundary-violations --rule FROM:FORBIDDEN`, repeatable, plus
  `--kind`/`--limit`/`--full-symbols`) and as an MCP tool (`rules` as
  `[from_prefix, forbidden_prefix]` pairs), both backed by the same
  `GraphStore.boundary_violations`; an edge matching several rules is
  reported once per rule, each record naming the rule it broke; bounded
  output (`limit` + `total`); malformed rules raise/`parser.error` on the CLI
  and come back as an error dict on MCP (a typo must never silently read as
  "layering holds").

### Fixed

- **`cppgraph update`**: works with no arguments — auto-discovers the graph and
  compilation database from the working directory, re-indexes the changed
  translation units, and applies the update in place, matching every other query
  command's auto-discovery convention. `init.py`, `cli.py`, `mcp_server.py`, and
  `QUICKSTART.md` all point at this one command now, instead of four inconsistent
  (and in one case wrong) instructions. `pipeline.incremental_update` now reads
  "changed" the same way `status` does (via the dirty-fingerprint-aware
  `changed_files_since`), so the two agree on scope.

## [0.1.0] - 2026-08-14

First release. cppgraph builds an exact, compiler-grade C++ call/type graph and
serves it to humans (CLI) and to LLMs (MCP), with a focus on precise answers and
token-lean output.

### Graph
- Built from a **compiler index** (SCIP via `scip-clang`), not a syntactic AST:
  exact symbol identity, edges disambiguated across overloads, virtual dispatch,
  templates, and free functions.
- Edge kinds `calls`, `inherits`, `implements`; a definition site is recorded for
  every symbol, types included. Caller attribution is exact via `enclosing_range`
  containment when the binary emits it (a #504 build): a call site outside every
  known callable body yields no caller edge, never a guess. Without
  `enclosing_range` (stock binary), attribution falls back to the nearest-preceding
  callable definition in the same file.
- Exact **reference-location index** (`symbol → file:line`), on by default —
  answers "where is this used?" for symbols the call graph can't (e.g. a struct).
  With a #504 binary, references can be attributed to the enclosing definition
  (opt-in `--attributed-refs`, or `enrich-refs` for an existing store) for a
  **symbol-granularity usage view** — the functions that use a type, not just the
  files; `status` reports which granularity a graph carries.

### Store
- Interned **SQLite** store, queried off B-tree indexes without loading the whole
  graph into RAM.
- Incremental **`update`**: re-index only the changed translation units, in place.
- Self-describing: build provenance plus an on-disk `schema_version` that refuses
  a store newer than the code understands.

### CLI
- Queries: `find`, `callers`, `callees`, `bases`, `subtypes`, `references`,
  `path`, `impact`, `explain`.
- Auto-discovers the project graph from the working directory (`--graph`
  optional) and accepts plain names, not just raw SCIP symbol strings.
- **`status`**: provenance and drift (changed fraction, commits behind) with a
  rebuild-vs-incremental recommendation, plus level-aware tool-update advice
  (`none` / `store` / `reindex`).

### MCP server
- `cppgraph-mcp` exposes the full query surface as token-budgeted tools; one
  global registration serves every indexed project via auto-discovery.
- **Agent guidance on connect**: the `initialize` response carries an
  `instructions` block that points a connected model at the graph tools for code
  in the indexed scope — with the scope and a `status` freshness pointer baked in
  — and at its normal read/grep everywhere else (out-of-scope files, comments,
  string literals, non-indexed languages, and paging a file it has already
  located). `find` and `explain_symbol` lead their descriptions with the same
  text-search contrast.
- **Names, not just SCIP strings**: every symbol-taking tool (`who_calls`,
  `what_it_calls`, `impact_of`, `explain_symbol`, `path`, `base_classes`,
  `subclasses`, `find_references`) accepts a plain name through the shared
  `GraphStore.resolve` (also behind the CLI) — a unique name resolves, an
  ambiguous one returns candidates, `Class::method` maps to `Class#method`, and
  no symbol is ever guessed.
- **Token-lean by default**: readable `name` + `file:line` instead of raw SCIP
  strings, test noise dropped by default, and source snippets returned inline on
  request (no separate file read).
- **Query quality**: multi-term AND `find` with case/separator-insensitive and
  leaf-name fallbacks; overloads grouped with source-derived signatures; opt-in
  `hide_trivial`; and explicit notices instead of misleading empty results (type
  blast-radius, empty hierarchy, no static path).

### Export & visualization
- `export` a bounded neighbourhood as graphify-compatible JSON (dependency or
  usage view); `view` / the MCP `visualize` tool render a **self-contained**,
  offline HTML.

### Setup & platforms
- One-shot `setup.sh` (venv + deps + scip-clang, version-selectable); the
  pure-Python tool installs on every platform.
- **ARM-Linux / Windows indexing via a container** (docker or podman), resuming
  automatically into a native graph build; reuses a prebuilt `.scip` where no
  native indexer exists.

### Docs & license
- Measured comparisons vs graphify and Serena/clangd, and vs an LLM's own
  grep-and-read loop.
- Licensed **MIT**.
