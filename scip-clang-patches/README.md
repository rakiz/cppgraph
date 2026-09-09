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
  parameters are untouched. Not yet upstreamed. Generated with reduced diff
  context (`-U1`).

## Independence

Each patch applies **standalone** on a clean `v0.4.0` checkout, and all six
apply cleanly together in **any of the 720 possible orderings** — verified
empirically (every ordering tried against a fresh `v0.4.0` checkout; the
resulting files are byte-identical regardless of order). None of the six
requires any of the others.

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

This matters for upstreaming: a maintainer reviewing these independently (in
whatever order they pick, possibly rejecting one) won't hit an artificial
"depends on patch X" requirement that isn't semantically real.

## Apply order

Our own build applies them in a fixed order — `enclosing_range` →
`forward-definition` → `read-write-access` → `kind` →
`signature-documentation` → `typed-by` — purely for consistency (it's the one
sequence that's actually end-to-end tested), not because any of them requires
it:

```sh
git apply enclosing_range-on-v0.4.0.patch                 # order-independent
git apply forward-definition-on-v0.4.0.patch              # order-independent
git apply read-write-access-on-v0.4.0.patch               # order-independent
git apply kind-on-v0.4.0.patch                            # order-independent
git apply signature-documentation-on-v0.4.0.patch         # order-independent
git apply typed-by-on-v0.4.0.patch                        # order-independent
```

Each is also ready to become an independent upstream PR without regenerating
it, regardless of whether or in what order the others land.

## Versioning

The upstream scip-clang tag these patches are rebased on (`v0.4.0`) and *our
own* patch bundle version (`patchset_version`, bumped whenever a patch here is
added/changed) are two independent numbers — see `versions.json`'s
`scip_clang` object and its `$comment_patchset` for the current history.

## Consumers

- `docker/build-scip-clang-patched-linux/Dockerfile` — copies all six patches
  into the Docker build context and applies them in order.
- `scripts/build-scip-clang-patched-macos.sh` — applies them directly to a
  local clone, in the same order.
- `scripts/publish-scip-clang-patched.sh` — publishes the resulting binary as a
  GitHub Release asset; does not touch these files itself.
</content>
