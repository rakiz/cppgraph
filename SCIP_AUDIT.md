# SCIP_AUDIT — what scip-clang actually populates, field by field

Measured 2026-09-07 against scip-clang **v0.4.0**, from two real evidence
sources (numbers, not impressions — see §Method for exact inputs):

- **Corpus** — `scratch/mongo_src_tests.scip`, the MongoDB `src/`+`tests/`
  index: 16,664 documents, 810,919 symbols, 15,176,411 occurrences, 260,223
  relationships, 174,436 external symbols. Indexed with the **stock**
  v0.4.0 binary (`cppgraph status` on its store: "this graph indexed with
  stock"), so corpus percentages describe stock emission at scale.
- **#504 binary** — `~/.local/share/cppgraph/bin/scip-clang` (sidecar
  `scip-clang.json`: version 0.4.0, variant `enclosing_range-504`), verified
  on a hand-built fixture TU (§Method). The #504 patch adds `enclosing_range`
  only; every other field measured identical between the stock corpus and
  the #504 fixture.

Why this document exists: the `is_type_definition` work assumed that field
carried data somewhere in a real `.scip`; measured, it is set on **0 of
260,223 relationships** — correct code over a field that never carries data,
reverted (`FOLLOWUP.md` §typed-by). The same audit just caught an error in
the opposite direction: TODO.md claimed `documentation` was "0% on the mongo
index"; measured, it is populated on 99.99% of symbols, **10.61% with real
extracted doc-comment text**. Before building a feature on a SCIP field,
check its row here first ("verify before promising").

## Summary table

`Status` vocabulary: **WORKING** (carries data cppgraph could rely on),
**DORMANT** (never emitted by this indexer — 0%), **PARTIAL** (emitted for
some constructs), **PLACEHOLDER** (non-empty but constant/filler text).

| Field | Status | Measured (corpus; #504 fixture) | If populated, unblocks in cppgraph |
|---|---|---|---|
| `SymbolInformation.kind` | DORMANT | `UnspecifiedKind` 810,919/810,919 (100%); 16/16 on fixture | exact node typing (enum vs class vs struct, static vs global, parameter vs field) instead of descriptor-suffix derivation |
| `SymbolInformation.documentation` | WORKING, PLACEHOLDER-skewed | non-empty 810,832/810,919 (99.99%): **genuine doc-comment 86,061 (10.61%)**, auto `namespace X` 23,285 (2.87%) + `inline namespace X` 591 (0.07%), auto `File: Y` 16,129 (1.99%), literal placeholder `"No documentation available."` 684,766 (84.45%), empty 87 (0.01%) | nothing upstream — doc text for `explain_symbol` is consumable **today** (cppgraph-side work) |
| `SymbolInformation.signature_documentation` | DORMANT | 0/810,919 (0.00%); absent on fixture | signatures in `explain_symbol` without a source read |
| `SymbolInformation.display_name` | DORMANT | 0/810,919 (0.00%); `''` on fixture | dropping `short_label` derivation (low value — it already works) |
| `SymbolInformation.enclosing_symbol` | DORMANT | 0/810,919 (0.00%) | local-symbol hierarchy (cppgraph excludes locals by design) |
| `Relationship.is_implementation` | WORKING | 260,223/260,223 (100%): 176,363 impl-only + 83,860 impl+ref | — powers `inherits`/`implements` |
| `Relationship.is_reference` | WORKING | 83,860/260,223 (32.2%) | — pairs override methods with their virtual base |
| `Relationship.is_type_definition` | DORMANT | 0/260,223 (0.00%) | `typed-by` edges (`FOLLOWUP.md` §typed-by) |
| `Relationship.is_definition` | DORMANT | 0/260,223 (0.00%) | nothing cppgraph-relevant — proto semantics target mixin-style definition overrides; moot under C++'s one-definition rule |
| `Occurrence.symbol_roles` | PARTIAL — `Definition` only | role 0: 13,280,325 (87.51%); `Definition` (0x1): 1,896,086 (12.49%); `Import` (0x2), `WriteAccess` (0x4), `ReadAccess` (0x8), `Generated` (0x10), `Test` (0x20), `ForwardDefinition` (0x40): **0 each**. Fixture: an unambiguous write (`g = 5;`) and read of a global both come back role 0 | `Write`/`Read` bits → mutation analysis ("who *writes* this global/field?"); `Test` bit → exact test filtering; `ForwardDefinition` bit → declaration-vs-call separation on stock graphs (see `FOLLOWUP.md`) |
| `Occurrence.syntax_kind` | PARTIAL — macros only | `Unspecified` 14,618,844 (96.33%); `IdentifierMacro` 519,044 (3.42%); `IdentifierMacroDefinition` 38,523 (0.25%) — set only in the macro save path (`Indexer.cc:98,101`); fixture: macro def/use tagged, all else 0 | identifier kinds → syntax highlighting, not cppgraph's mission |
| `Occurrence.override_documentation` | DORMANT | 0/15,176,411 (0.00%) | hover specialization — not our mission |
| `Occurrence.diagnostics` | DORMANT | 0 non-empty | — |
| `Occurrence.range` encoding | deprecated form only | deprecated `repeated int32` on 15,176,411/15,176,411 (100%); `typed_range` oneof never used; #504's `enclosing_range` likewise uses the deprecated int32 form | — consumers must read the deprecated field (`builder.py:_occurrence_enclosing_range` already does) |
| `Occurrence.enclosing_range` | stock DORMANT / **#504 WORKING** | corpus (stock): 0/15,176,411 (0.00%). #504 fixture: emitted **on definitions** (callable, type, term/global incl. the initializer span, fields, file statics, locals) in the deprecated int32 form; absent on enum constants, macro definitions, the file symbol; never on references; lambdas get no symbol or interval at all (`FOLLOWUP.md` §lambda) | already consumed on #504 graphs: exact caller/reference attribution, `line_span`, `global_init_references` |
| `Document.language` | WORKING | `'CPP'` on 16,664/16,664 | — |
| `Document.position_encoding` | NON-COMPLIANT | `UnspecifiedPositionEncoding` on 16,664/16,664; the proto says that value "should not be used by new SCIP indexers" and a C++ indexer should emit `UTF8CodeUnitOffsetFromLineStart` | nothing for cppgraph (line numbers only) — spec-compliance drive-by fix upstream |
| `Document.text` | DORMANT | 0/16,664 non-empty (proto: not expected by default) | — |
| `Index.external_symbols` | WORKING, unconsumed | 174,436 on the corpus (out-of-project definitions, e.g. boost/absl; all `UnspecifiedKind`, documentation in the same mix: 15,756 real / 158,675 placeholder; 867 relationships); 0 on the self-contained fixture | doc text for external symbols in `explain_symbol` — cppgraph-side consumption, no upstream change needed |
| `Metadata.tool_info` / `project_root` / `text_document_encoding` | WORKING | name+version+8 CLI args; `file://` root; UTF8 | — already recorded in store `meta` |
| `Metadata.version` (`ProtocolVersion`) | — | `UnspecifiedProtocolVersion` — the enum's only value | — not a gap |

Session findings re-verified by this pass: `is_implementation` 260,223 and
`is_reference` 83,860 (match); `is_type_definition` 0 (match; note the
earlier note's "0 of 810,919 relationships" conflated counts — 810,919 is
the *symbol* count, relationships are 260,223); `enclosing_range` present
on #504 for callable/type/term definitions, absent for lambdas (match,
fixture above).

## Findings that change standing assumptions

1. **`documentation` works — TODO.md's "0%" claim is wrong.** v0.4.0
   extracts attached doc comments into `SymbolInformation.documentation`
   (`getRawCommentForDeclNoCache`/`getRawCommentForAnyRedecl`,
   `Indexer.cc:1324-1327`): 86,061 symbols (10.61%) carry genuine doc text on
   the corpus, and the fixture extracts `/** ... */` comments for a class, a
   free function and a global. Two traps that likely produced the old claim:
   (a) the placeholder — `saveDefinition` writes the literal string
   `"No documentation available."` whenever nothing was found
   (`ScipExtras.h:86-87`, `Indexer.cc:1201-1203`), so a non-empty check
   passes on 99.99% of symbols while saying nothing; (b) early corpus
   samples (boost headers) are dominated by placeholder/`namespace`/`File`
   auto-text. Quality caveat, measured on the fixture: trailing `//`
   comments attach to fields (`Widget#field_` → "field with initializer")
   but not to in-class method declarations (placeholder there).
2. **Only `SymbolRole::Definition` is ever passed.** `set_symbol_roles` is
   called once, with the OR'd roles built at `Indexer.cc:97/432/1206`; no
   call site anywhere passes any other bit — consistent with 15,176,411
   occurrences showing exactly {0, 0x1}.
3. **The forward-declaration machinery exists but drops the role on the
   floor.** Bodyless declarations (e.g. an in-class method declaration) are
   routed via `saveFunctionDecl` → `saveForwardDeclaration`
   (`Indexer.cc:565` → `:1151`) through an internal per-TU
   `ForwardDeclIndex` (`proto/fwd_decls.proto`, emitted by
   `ForwardDeclMap::emit`, `Indexer.cc:363`) and re-emitted into documents
   by `ForwardDeclOccurrence::addTo` (`ScipExtras.cc:243-249`) — which sets
   symbol and range and never touches `symbol_roles`, landing as plain
   role-0. That role-0 shape is exactly the declaration-vs-call
   indistinguishability that forces cppgraph's stock-binary over-capture
   (`DESIGN.md` §Building calls: "no separating signal exists"). One bit at
   the source fixes what no consumer can recover — see `FOLLOWUP.md`.
4. **Upstream has a TODO at the exact read/write site.** `saveDeclRefExpr`
   (`Indexer.cc:1018`) carries the literal comment
   *"TODO: Add read-write access to the symbol role here"* — the PR would
   land on already-marked ground (`saveMemberExpr`, `Indexer.cc:1024`, is
   the sibling site).
5. **`syntax_kind` is not uniformly unpopulated** — it is emitted for macro
   occurrences only (`Indexer.cc:98,101`). `DESIGN.md`'s "unpopulated"
   (about identifiers) is accurate for every non-macro construct; the
   emitter pattern to extend exists.
6. **`external_symbols` is emitted and cppgraph ignores it** (no reference
   in `src/cppgraph/`): 174,436 symbols with doc text on the corpus. Using
   them for external-symbol documentation in `explain_symbol` is a
   cppgraph-side change, not an upstream ask.

## Method

Corpus counting: single pass over the parsed index
(`scip_pb2.Index().ParseFromString` — same read path as `pipeline.py`),
counting every field above in one sweep; `documentation` values then
categorized (placeholder / `namespace ` / `inline namespace ` / `File: ` /
other) in a second pass. No sampling — all counts are exhaustive.

#504 fixture (reproducible):

```cpp
// audit_tu.cpp
#define AUDIT_MACRO(x) ((x) + 1)
/** Doc comment on a class. */
class Widget {
public:
    int frobnicate(int x);  // in-class declaration, defined below
    int field_ = 0;         // field with initializer
private:
    int hidden_ = 1;
};
namespace ns {
/** Doc comment on a global. */
int global_doc = 42;
int global_plain = 7;
}  // namespace ns
enum class Color { Red, Green };
static int file_static = 3;  // file-scope static
int Widget::frobnicate(int x) { return AUDIT_MACRO(x); }
/** Doc comment on a free function. */
int freefn(int a, int b) { return a + b; }
int consumer() {
    ns::global_plain = 5;        // unambiguous WRITE of a global
    int local = ns::global_doc;  // unambiguous READ of a global
    static int func_static = 2;
    file_static += local;
    Widget w;
    return freefn(w.frobnicate(local), (Color::Red == Color::Green) ? 1 : 0);
}
int with_lambda() {
    int base = 1;
    auto lam = [base]() { return ns::global_plain + base; };  // lambda
    return lam();
}
```

```sh
# compile_commands.json: one entry, arguments ["clang++","-std=c++17","-c","audit_tu.cpp"]
~/.local/share/cppgraph/bin/scip-clang --compdb-path compile_commands.json \
    --index-output-path audit_tu.scip -j 1 --no-progress-report
# then dump every field of every SymbolInformation/Occurrence via cppgraph.proto.scip_pb2
```

Source citations are from the #504-patched scip-clang checkout at
`~/.cache/cppgraph/scip-clang-src` (v0.4.0 base — the same checkout
`scripts/build-scip-clang-macos.sh` builds from), verified by reading the
cited lines, not from memory.
