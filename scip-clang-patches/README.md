# scip-clang-patches — our patches on top of upstream scip-clang v0.4.0

Shared by both build paths — `docker/build-scip-clang-patched-linux/` (Docker,
Linux binary) and `scripts/build-scip-clang-patched-macos.sh` (native, macOS
binary) — hence living here at the repo root instead of inside either one.

## Files

- `enclosing_range-on-v0.4.0.patch` — [PR #504](https://github.com/sourcegraph/scip-clang/pull/504)
  (`enclosing_range`), rebased onto `v0.4.0`, plus a same-file hardening guard
  over the raw PR (see the patch's own header / `docker/build-scip-clang-patched-linux/README.md`
  for why). Upstream PR in progress. Generated with reduced diff context
  (`-U1`) — see "Independence" below.
- `forward-definition-on-v0.4.0.patch` — the `ForwardDefinition` bit fix (see
  `TODO.md`'s scip-clang section). Not yet upstreamed. Generated with reduced
  diff context (`-U1`).
- `read-write-access-on-v0.4.0.patch` — the `ReadAccess`/`WriteAccess` syntactic
  classifier (`classifyAccessRoles` in `indexer/Indexer.cc`, plus test fixtures).
  Tags `symbol_roles` with `WriteAccess` when the reference site's syntactic AST
  parent is an assignment, compound assignment, `++`/`--`, or their
  overloaded-operator forms (with `ReadAccess` set alongside `WriteAccess` on
  read-modify-write sites); constructor-initializer field references are
  unconditional writes; plain reads stay untagged. Not yet upstreamed. Generated
  with reduced diff context (`-U1`).
- `kind-on-v0.4.0.patch` — fills SCIP's `SymbolInformation.kind` (field 5),
  which upstream leaves at `UnspecifiedKind` on 100% of symbols. A syntactic
  classifier (`classifySymbolKind` in `indexer/Indexer.cc`, same style as
  `classifyAccessRoles`) maps the `clang::Decl` at each
  `SymbolInformation`-creating site to its kind: Class/Struct/Union/Enum,
  EnumMember, Field, Function/Method/StaticMethod/PureVirtualMethod/
  Constructor, Variable/StaticDataMember, Namespace, TypeAlias — plus Macro
  and File for the non-`Decl` sites. The kind survives the TU-merge pipeline
  via a new `SymbolInformationBuilder::kind` field. Symbols which get no
  `SymbolInformation` emitted today (local variables, parameters) are
  unaffected. Not yet upstreamed. Generated with reduced diff context
  (`-U1`).
- `signature-documentation-on-v0.4.0.patch` — fills SCIP's
  `SymbolInformation.signature_documentation` (field 7, a `Document` message
  with `language` + `text`), which upstream leaves unset on 100% of symbols.
  For every function/method declaration that reaches `saveFunctionDecl`'s
  `SymbolInformation` branch (i.e. defined or pure-virtual — the only
  `FunctionDecl`s which get one today), `declToSignatureText` in
  `indexer/Indexer.cc` prints the declaration back out via the AST
  declaration printer (`clang::PrintingPolicy` with `TerseOutput` — body and
  ctor initializer lists suppressed, parameter default arguments, specifiers
  and `= 0`/`= delete`/`= default` markers kept), and the text survives the
  TU-merge pipeline via a new `SymbolInformationBuilder::signatureDocumentation`
  field (first non-empty wins, a later duplicate can fill an earlier empty
  one — same semantics as `documentation` merging). Bodyless in-class
  declarations never get a `SymbolInformation` (they route through
  `saveForwardDeclaration`), so they get no signature — accepted v1
  limitation. Not yet upstreamed. Generated with reduced diff context
  (`-U1`).
- `typed-by-on-v0.4.0.patch` — fills SCIP's `Relationship.is_type_definition`
  (field 4, "go to type definition"), which upstream never sets. A syntactic
  type resolver (`tryResolveDeclaredTypeDecl` in `indexer/Indexer.cc`) records,
  on a field's / variable's own `SymbolInformation`, a
  `{symbol: <type>, is_type_definition: true}` relationship — source = the
  field/variable, destination = its declared type — reusing
  `trySaveTypeReference`'s exact type-resolution policy (typedef/alias → the
  typedef declaration; tag type → the tag declaration; template specialization
  → the primary templated declaration, so `std::vector<Foo>` gets a single
  edge to `std::vector`), after stripping pointer/reference/cv-qualifier
  wrappers. Builtins, unresolvable `auto`, dependent types — anything without
  a compiler-resolved SCIP symbol — emit no relationship at all. Scope
  (accepted v1 limitation): only declarations which get a `SymbolInformation`
  today — fields, file-scope variables, static data members; locals and
  parameters are untouched. Carries its own test-snapshot updates under
  `test/index/`. Not yet upstreamed. Generated with reduced diff context
  (`-U1`).
- `enclosing-range-macro-on-v0.4.0.patch` — closes a gap the #504 patch leaves
  open: `enclosing_range` is recorded from `decl.getSourceRange()`, whose *begin*
  is the macro's spelling location for a definition introduced through a macro
  (a gtest `TEST`/`TEST_F` body, or a function carrying a leading macro like
  `MONGO_COMPILER_ALWAYS_INLINE`). That range is cross-file, so #504's same-file
  guard skips it and the occurrence carries no `enclosing_range` at all — and
  cppgraph then drops every call inside the definition. This patch adds
  `TuIndexer::getEnclosingRange`, which returns the declaration's own source
  range when that is single-file (so every ordinary definition is byte-identical
  to #504) and otherwise falls back to the function *body* extent: the body is
  written where the definition is, so it is single-file and containment
  attribution works. Anything still cross-file — and every non-function
  declaration — yields an empty range, which `saveOccurrence` already treats
  exactly like the cross-file range it would otherwise have received (no
  `enclosing_range`, an optional field). Measured on a full MongoDB `src/mongo`
  #504 index: gtest test bodies (callers of a test helper: 1 → 122, matching its
  122 references) and `MONGO_COMPILER_ALWAYS_INLINE` inlines
  (`Ordering::verifyCardinality`: 0 → 8 callees). **This is the one patch here
  with a prerequisite: it requires `enclosing_range` (#504)**, whose
  `saveDefinition(...)` arguments it rewrites — but it is order-free with
  respect to the other five (see "Independence"). Not yet upstreamed. Generated
  with reduced diff context (`-U1`).

## Independence

The **first six** patches each apply **standalone** on a clean `v0.4.0`
checkout, and all six apply cleanly together in **any of the 720 possible
orderings** — verified empirically (every ordering tried against a fresh
`v0.4.0` checkout; the resulting files are byte-identical regardless of order).
None of the six requires any of the others.

The **seventh** (`enclosing-range-macro`) has exactly one prerequisite,
`enclosing_range` (#504), whose `saveDefinition(...)` arguments it rewrites — it
is a *follow-on to that PR* and can only be upstreamed on top of it. It is
**order-free with respect to the other five**: it touches no line any of them
touches, and at `-U1` none of their edits can invalidate its anchors. Verified
empirically, same method as above:

- all **720** orderings of the six non-#504 patches, each applied after #504,
  apply cleanly and produce byte-identical files (13 patch-touched files
  compared by SHA-256);
- all **112** subset/position combinations — every subset of the other five
  (2⁵ = 32) with the seventh inserted at every position — apply cleanly,
  including `#504 + enclosing-range-macro` **alone**;
- on that standalone `v0.4.0 + #504 + enclosing-range-macro` tree, scip-clang
  builds and its whole snapshot/unit test suite passes (11/11), i.e. it is
  submittable as its own upstream PR with no dependency on anything else here.

This holds because every hunk is anchored on context unique to its own patch,
even at the reduced `-U1` diff context used throughout (`-U2` on the two hunks
noted below, where `-U1`'s context would otherwise also match a different,
nearby location in the file):

- `forward-definition`'s `ForwardDeclOccurrence::addTo` hunk anchors on the
  unique `occ.add_range(this->range[i]);` line (`-U2`) rather than its
  original `  }` + `}` context, which also closes
  `DocumentBuilder::populateForwardDeclResolver` elsewhere in the file.
- `typed-by`'s `emitDocumentOccurrencesAndSymbols` change is two hunks: the
  one inserting the relationship-merge pass before `extractTransform(` stays
  at `-U1` (its context, a bare `extractTransform(` immediately preceded by
  `}`, is unique on its own); the one appending unmerged fallback extras after
  the main symbol-emission loop anchors on the unique
  `*scipDocument.add_symbols() = std::move(symInfo);` line (`-U2`) rather than
  its `  }));` + `}` context, which also closes
  `MacroIndexer::emitDocumentOccurrencesAndSymbols`. The `saveDefinition(...)`
  calls that would otherwise serve as trailing context there are rewritten by
  `enclosing_range`, so this hunk cannot anchor on them without becoming
  order-dependent.
- `kind`'s insertions anchor on the `scip::SymbolInformation symbolInfo{};` /
  `getDocComment(...)` lines inside `saveFieldDecl`/`saveTypedefNameDecl`/
  `saveVarDecl` — lines no other patch touches, even though they sit only two
  lines from a `read-write-access` edit.
- `enclosing-range-macro`'s eleven call-site hunks each anchor on the
  `X.getSourceRange()` line `enclosing_range` itself introduced — a line
  unique in the file and touched by no other patch — plus one line of context
  which is likewise untouched; its helper insertion anchors on the unique
  `PartialDocument &TuIndexer::saveOccurrence(SymbolNameRef symbol,` signature.
  It carries none of the `kind` / `signature-documentation` / `typed-by` lines
  that the earlier `-U3` form of this patch picked up (those sit 2-3 lines away
  from several of the call sites, which is exactly why `-U3` made it look
  order-dependent).

This matters for upstreaming: a maintainer reviewing these independently (in
whatever order they pick, possibly rejecting one) won't hit an artificial
"depends on patch X" requirement that isn't semantically real. The seventh's
dependency on #504 is the only real one, and it is real: without #504 there is
no `enclosing_range` argument to rewrite.

## Apply order

Our own build applies them in a fixed order — `enclosing_range` →
`forward-definition` → `read-write-access` → `kind` →
`signature-documentation` → `typed-by` → `enclosing-range-macro` — purely for
consistency (it's the one sequence that's actually end-to-end tested), not
because any position is required, beyond the seventh having to come after
`enclosing_range`:

```sh
git apply enclosing_range-on-v0.4.0.patch                 # order-independent
git apply forward-definition-on-v0.4.0.patch              # order-independent
git apply read-write-access-on-v0.4.0.patch               # order-independent
git apply kind-on-v0.4.0.patch                            # order-independent
git apply signature-documentation-on-v0.4.0.patch         # order-independent
git apply typed-by-on-v0.4.0.patch                        # order-independent
git apply enclosing-range-macro-on-v0.4.0.patch           # after #504; free vs the other five
```

Each of the first six is ready to become an independent upstream PR without
regenerating it, regardless of whether or in what order the others land. The
seventh is likewise ready as its own PR *on top of #504* — nothing else in this
bundle needs to land first.

## Versioning

The upstream scip-clang tag these patches are rebased on (`v0.4.0`) and *our
own* patch bundle version (`patchset_version`, bumped whenever a patch here is
added/changed) are two independent numbers — see `versions.json`'s
`scip_clang` object and its `$comment_patchset` for the current history.

## Consumers

- `docker/build-scip-clang-patched-linux/Dockerfile` — copies all seven patches
  into the Docker build context and applies them in order.
- `scripts/build-scip-clang-patched-macos.sh` — applies them directly to a
  local clone, in the same order.
- `scripts/publish-scip-clang-patched.sh` — publishes the resulting binary as a
  GitHub Release asset; does not touch these files itself.
</content>
