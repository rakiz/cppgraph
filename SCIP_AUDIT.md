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

Why this document exists: a field's *name* doesn't tell you whether it
carries real data — check its row here first, before assuming a feature
can be built on it ("verify before promising"). Rows for fields we've
already patched/consumed point at `CHANGELOG.md`/`TODO.md` for the detail
instead of repeating it — this file's job is the raw measurement, not the
feature writeup.

## Summary table

`Status` vocabulary: **WORKING** (carries data cppgraph could rely on),
**DORMANT** (never emitted by this indexer — 0%), **PARTIAL** (emitted for
some constructs), **PLACEHOLDER** (non-empty but constant/filler text).

| Field | Status | Measured (corpus; #504 fixture) | Notes |
|---|---|---|---|
| `SymbolInformation.kind` | stock DORMANT / **patched WORKING** | `UnspecifiedKind` 810,919/810,919 (100%) stock | Consumed — see `CHANGELOG.md`'s `SymbolInformation.kind` entry, `scip-clang-patches/kind-on-v0.4.0.patch`. |
| `SymbolInformation.documentation` | WORKING, PLACEHOLDER-skewed | non-empty 810,832/810,919 (99.99%): genuine doc-comment 86,061 (10.61%), auto `namespace`/`File:` text 4.9%, literal placeholder `"No documentation available."` 84.45% | Already consumed by `explain_symbol` — no patch needed, stock field. |
| `SymbolInformation.signature_documentation` | stock DORMANT / **patched WORKING** | 0/810,919 (0.00%) stock | Consumed — see `CHANGELOG.md`, `scip-clang-patches/signature-documentation-on-v0.4.0.patch`. |
| `SymbolInformation.display_name` | DORMANT | 0/810,919 (0.00%); `''` on fixture | Not pursued — `short_label` derivation already covers this need. |
| `SymbolInformation.enclosing_symbol` | DORMANT | 0/810,919 (0.00%) | Local-symbol hierarchy; cppgraph excludes locals by design, not pursued. |
| `Relationship.is_implementation` | WORKING | 260,223/260,223 (100%): 176,363 impl-only + 83,860 impl+ref | Powers `inherits`/`implements` — stock field, no patch. |
| `Relationship.is_reference` | WORKING | 83,860/260,223 (32.2%) | Pairs an override method with its virtual base — stock field. |
| `Relationship.is_type_definition` | stock DORMANT / **patched WORKING** | 0/260,223 (0.00%) stock | Consumed — see `CHANGELOG.md`'s `typed-by` entry, `scip-clang-patches/typed-by-on-v0.4.0.patch`. |
| `Relationship.is_definition` | DORMANT | 0/260,223 (0.00%) | Proto semantics target mixin-style definition overrides — moot under C++'s one-definition rule, not pursued. |
| `Occurrence.symbol_roles` (`WriteAccess`/`ReadAccess`) | stock DORMANT / **patched WORKING** | 0 of 15,176,411 stock | Consumed — see `CHANGELOG.md`, `scip-clang-patches/read-write-access-on-v0.4.0.patch`. |
| `Occurrence.symbol_roles` (`ForwardDefinition`) | stock DORMANT / **patched WORKING** | 0 of 15,176,411 stock | Consumed — see `CHANGELOG.md`, `scip-clang-patches/forward-definition-on-v0.4.0.patch`; fixes the declaration-vs-call ambiguity on stock-shaped graphs. |
| `Occurrence.symbol_roles` (`Test`) | DORMANT | 0 of 15,176,411 | Not worth filing — `exclude_tests`'s path heuristic already covers this, see `TODO.md`. |
| `Occurrence.syntax_kind` | PARTIAL — macros only | `Unspecified` 96.33%; `IdentifierMacro`/`IdentifierMacroDefinition` set only in the macro save path | Identifier kinds → syntax highlighting, not cppgraph's mission. |
| `Occurrence.override_documentation` | DORMANT | 0/15,176,411 (0.00%) | Hover specialization — not our mission. |
| `Occurrence.diagnostics` | DORMANT | 0 non-empty | — |
| `Occurrence.range` encoding | deprecated form only | deprecated `repeated int32` on 100% of occurrences; `typed_range` oneof never used, including by #504's `enclosing_range` | Consumers must read the deprecated field (`builder.py`'s `_occurrence_enclosing_range` already does). |
| `Occurrence.enclosing_range` | stock DORMANT / **#504 WORKING** | 0/15,176,411 stock. #504: emitted on definitions (callable, type, term/global incl. initializer span, fields, file statics, locals); absent on enum constants, macro definitions, the file symbol; never on references; **lambdas get no symbol or interval at all** | Already consumed on #504 graphs: exact caller/reference attribution, `line_span`, `global_init_references`. The lambda gap is a still-open scip-clang ask — see `TODO.md`. |
| `Document.language` | WORKING | `'CPP'` on 16,664/16,664 | — |
| `Document.position_encoding` | NON-COMPLIANT | `UnspecifiedPositionEncoding` on 100%; proto recommends `UTF8CodeUnitOffsetFromLineStart` for a new indexer | Nothing for cppgraph (line numbers only) — spec-compliance drive-by, not pursued. |
| `Document.text` | DORMANT | 0/16,664 non-empty (proto: not expected by default) | — |
| `Index.external_symbols` | WORKING, consumed | 174,436 on the corpus (all `UnspecifiedKind`; documentation mix same as `Document.symbols`; 867 relationships) | Consumed — see `CHANGELOG.md`'s `Node.is_out_of_project` entry; the 867 relationships deliberately not consumed (no project document to give them edge provenance). |
| `Metadata.tool_info` / `project_root` / `text_document_encoding` | WORKING | name+version+8 CLI args; `file://` root; UTF8 | Already recorded in store `meta`. |
| `Metadata.version` (`ProtocolVersion`) | — | `UnspecifiedProtocolVersion` — the enum's only value | Not a gap. |

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

Source citations for the patched fields are from the #504-patched scip-clang
checkout at `~/.cache/cppgraph/scip-clang-src` (v0.4.0 base — the same
checkout `scripts/build-scip-clang-patched-macos.sh` builds from), verified
by reading the cited lines, not from memory.
