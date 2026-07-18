"""Unit tests for cppgraph.builder using synthetic SCIP indexes.

Synthetic instead of a checked-in real .scip: keeps tests fast and focused on
the attribution logic itself, independent of scip-clang's specific quirks
(covered separately by the MongoDB acceptance script in scratch/).
"""

from __future__ import annotations

from cppgraph.builder import build_graph, is_callable_symbol
from cppgraph.proto import scip_pb2

DEFINITION = scip_pb2.SymbolRole.Definition


def _occurrence(symbol: str, line: int, *, roles: int = 0) -> scip_pb2.Occurrence:
    occ = scip_pb2.Occurrence(symbol=symbol, symbol_roles=roles)
    occ.range.extend([line, 0, 10])
    return occ


def test_is_callable_symbol_uses_scip_method_descriptor_suffix() -> None:
    assert is_callable_symbol("cxx . . $ mongo/Foo#bar(abc123).")
    assert not is_callable_symbol("cxx . . $ mongo/Foo#field.")
    assert not is_callable_symbol("cxx . . $ mongo/Foo#")
    assert not is_callable_symbol("cxx . . $ mongo/namespace/")


def test_over_capture_two_distinct_makeresumetoken_symbols() -> None:
    """The real-world case that motivated cppgraph: a method and an unrelated
    free function share a display name but must remain separate nodes, each
    with its own, correctly attributed callers."""
    method = "cxx . . $ mongo/ChangeStreamEventTransformation#makeResumeToken(m1)."
    helper = "cxx . . $ mongo/change_stream_test_helper/makeResumeToken(h1)."
    caller_a = "cxx . . $ mongo/ChangeStreamDefaultEventTransformation#applyTransformation(a1)."
    caller_b = "cxx . . $ mongo/SomeTest_Test#TestBody(t1)."

    doc = scip_pb2.Document(relative_path="change_stream_event_transform.cpp")
    doc.occurrences.extend(
        [
            _occurrence(caller_a, line=10, roles=DEFINITION),
            _occurrence(method, line=20, roles=DEFINITION),
            _occurrence(method, line=15),  # call from caller_a's body
        ]
    )

    test_doc = scip_pb2.Document(relative_path="change_stream_test_helpers.cpp")
    test_doc.occurrences.extend(
        [
            _occurrence(caller_b, line=1, roles=DEFINITION),
            _occurrence(helper, line=5, roles=DEFINITION),
            _occurrence(helper, line=3),  # call from caller_b's body
        ]
    )

    index = scip_pb2.Index(documents=[doc, test_doc])
    graph = build_graph(index)

    assert method in graph.nodes
    assert helper in graph.nodes
    assert method != helper

    assert [e.src for e in graph.callers_of(method)] == [caller_a]
    assert [e.src for e in graph.callers_of(helper)] == [caller_b]


def test_call_attributed_to_nearest_preceding_function_definition() -> None:
    """Stands in for a virtual-dispatch call site: cppgraph attributes the
    edge purely from the SCIP-resolved callee symbol, never from the
    call-site syntax (e.g. `ptr->method()`), so dispatch through a pointer
    is captured exactly like any other call."""
    outer = "cxx . . $ mongo/Foo#outer(o1)."
    base_virtual = "cxx . . $ mongo/Base#virtualMethod(v1)."

    doc = scip_pb2.Document(relative_path="foo.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=1, roles=DEFINITION),
            _occurrence(base_virtual, line=3),  # e.g. `ptr->virtualMethod()`
        ]
    )
    index = scip_pb2.Index(documents=[doc])
    graph = build_graph(index)

    assert [e.src for e in graph.callers_of(base_virtual)] == [outer]


def _occ_enclosing(
    symbol: str, line: int, enclosing_start: int, enclosing_end: int
) -> scip_pb2.Occurrence:
    """A (non-definition) call occurrence carrying an `enclosing_range` — the
    range of its enclosing definition — as a #504-built scip-clang emits it
    (deprecated field 7, packed [startLine, startCol, endLine, endCol])."""
    occ = scip_pb2.Occurrence(symbol=symbol)
    occ.range.extend([line, 0, 10])
    occ.enclosing_range.extend([enclosing_start, 0, enclosing_end, 0])
    return occ


def test_enclosing_range_attributes_caller_exactly_over_nearest_preceding() -> None:
    """When occurrences carry `enclosing_range` (a #504-built binary), the caller
    is the definition that *contains* the call site — not merely the nearest
    preceding one. A nested callable (a lambda's `operator()`) defined inside an
    outer function would fool nearest-preceding: a call after the nested def but
    still in the outer body sits closer to the inner def by line. `enclosing_range`
    names the true container, so the edge goes to the outer function."""
    outer = "cxx . . $ pkg/Outer#run(o1)."
    nested = "cxx . . $ pkg/Outer#run/lambda#operator()(l1)."
    helper = "cxx . . $ pkg/helper(h1)."

    doc = scip_pb2.Document(relative_path="outer.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=5, roles=DEFINITION),  # run() spans lines 5..30
            _occurrence(nested, line=10, roles=DEFINITION),  # inner lambda at 10..12
            _occ_enclosing(helper, line=20, enclosing_start=5, enclosing_end=30),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    # Exact: the call at line 20 is inside run()'s range (5..30), not the lambda.
    assert [e.src for e in graph.callers_of(helper)] == [outer]


def test_calls_fall_back_to_nearest_preceding_without_enclosing_range() -> None:
    """A stock binary (no #504) emits no `enclosing_range`. Attribution must
    degrade to the nearest-preceding heuristic, unchanged — so the same graph as
    before is produced. Here that (deliberately) attributes to the inner lambda."""
    outer = "cxx . . $ pkg/Outer#run(o1)."
    nested = "cxx . . $ pkg/Outer#run/lambda#operator()(l1)."
    helper = "cxx . . $ pkg/helper(h1)."

    doc = scip_pb2.Document(relative_path="outer.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=5, roles=DEFINITION),
            _occurrence(nested, line=10, roles=DEFINITION),
            _occurrence(helper, line=20),  # no enclosing_range
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert [e.src for e in graph.callers_of(helper)] == [nested]


def test_attribute_references_records_enclosing_symbol() -> None:
    """With attribute_references and an enclosing_range-carrying occurrence, a
    reference to a type is attributed to the definition that uses it — the
    'type -> the function that uses it' edge the usage view draws."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend(
        [
            _occurrence(user, line=5, roles=DEFINITION),  # render() spans 5..20
            _occ_enclosing(typ, line=10, enclosing_start=5, enclosing_end=20),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    refs = graph.references_of(typ)
    assert [r.enclosing_symbol for r in refs] == [user]


def test_references_unattributed_without_the_flag() -> None:
    """attribute_references defaults off: references stay pure locations even when
    the occurrence carries an enclosing_range, so the default build is unchanged."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend(
        [
            _occurrence(user, line=5, roles=DEFINITION),
            _occ_enclosing(typ, line=10, enclosing_start=5, enclosing_end=20),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))  # no attribute_references

    assert [r.enclosing_symbol for r in graph.references_of(typ)] == [None]


def test_duplicate_occurrences_from_header_merge_are_deduped() -> None:
    """A header included by multiple TUs can surface identical occurrences
    once per TU after scip-clang merges partial indexes (verified on real
    MongoDB data). Edges must be deduped by (kind, src, dst, file, line)."""
    caller = "cxx . . $ mongo/Foo#outer(o1)."
    callee = "cxx . . $ mongo/Foo#helper(h1)."

    doc = scip_pb2.Document(relative_path="foo.h")
    doc.occurrences.extend(
        [
            _occurrence(caller, line=1, roles=DEFINITION),
            _occurrence(callee, line=2),
            _occurrence(callee, line=2),  # duplicate from a second including TU
        ]
    )
    index = scip_pb2.Index(documents=[doc])
    graph = build_graph(index)

    assert len(graph.callers_of(callee)) == 1


def test_implements_relationship_becomes_an_edge() -> None:
    base = "cxx . . $ mongo/Animal#sound(a1)."
    override = "cxx . . $ mongo/Dog#sound(d1)."

    doc = scip_pb2.Document(relative_path="animal.h")
    sym_info = scip_pb2.SymbolInformation(symbol=override)
    sym_info.relationships.add(symbol=base, is_implementation=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    implements = [e for e in graph.edges if e.kind == "implements"]
    assert len(implements) == 1
    assert implements[0].src == override
    assert implements[0].dst == base
    # method override is `implements`, never `inherits`
    assert not [e for e in graph.edges if e.kind == "inherits"]


def test_references_collected_by_default_and_skippable() -> None:
    typ = "cxx . . $ mongo/ResumeTokenData#"
    user = "cxx . . $ mongo/Consumer#use()."
    doc = scip_pb2.Document(relative_path="consumer.cpp")
    doc.occurrences.append(_occurrence(user, 10, roles=DEFINITION))
    doc.occurrences.append(_occurrence(typ, 12))  # a plain use of the type
    doc.occurrences.append(_occurrence(typ, 20))
    index = scip_pb2.Index(documents=[doc])

    # on by default
    graph = build_graph(index)
    refs = graph.references_of(typ)
    assert {r.line for r in refs} == {12, 20}
    assert all(r.file == "consumer.cpp" for r in refs)
    # the referenced type becomes a node so it's interned/findable
    assert typ in graph.nodes

    # skippable for a leaner store
    assert build_graph(index, include_references=False).references == []


def test_references_exclude_definitions_and_locals() -> None:
    sym = "cxx . . $ mongo/Foo#"
    local = "local 4"
    doc = scip_pb2.Document(relative_path="f.cpp")
    doc.occurrences.append(_occurrence(sym, 5, roles=DEFINITION))  # def, not a ref
    doc.occurrences.append(_occurrence(sym, 9))  # a real ref
    doc.occurrences.append(_occurrence(local, 9))  # local, skipped
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index, include_references=True)
    assert {r.line for r in graph.references_of(sym)} == {9}
    assert graph.references_of(local) == []


def test_references_deduped_across_header_includes() -> None:
    sym = "cxx . . $ mongo/Foo#"
    # same occurrence surfacing from two TUs after scip-clang merges indexes
    docs = [scip_pb2.Document(relative_path="foo.h") for _ in range(2)]
    for d in docs:
        d.occurrences.append(_occurrence(sym, 3))
    index = scip_pb2.Index(documents=docs)
    graph = build_graph(index, include_references=True)
    assert len(graph.references_of(sym)) == 1


def test_type_definition_site_is_recorded() -> None:
    # A class definition occurrence should set the node's file/line, so
    # `find`/`explain`/`bases`/`subtypes` can locate a type — not just callables.
    cls = "cxx . . $ mongo/Widget#"
    doc = scip_pb2.Document(relative_path="widget.h")
    doc.symbols.append(scip_pb2.SymbolInformation(symbol=cls, display_name="Widget"))
    doc.occurrences.append(_occurrence(cls, 41, roles=DEFINITION))
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    node = graph.nodes[cls]
    assert node.file == "widget.h"
    assert node.line == 41


def test_class_inheritance_becomes_inherits_edge() -> None:
    # scip-clang emits the same is_implementation relationship for class
    # inheritance as for method override; the two are told apart by the SCIP
    # descriptor kind (type `#` vs method `).`). A derived class carries a
    # relationship pointing at its base. src = derived, dst = base.
    base = "cxx . . $ mongo/ServerParameter#"
    derived = "cxx . . $ mongo/IDLServerParameterWithStorage#"

    doc = scip_pb2.Document(relative_path="server_parameter.h")
    sym_info = scip_pb2.SymbolInformation(symbol=derived)
    sym_info.relationships.add(symbol=base, is_implementation=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    inherits = [e for e in graph.edges if e.kind == "inherits"]
    assert len(inherits) == 1
    assert inherits[0].src == derived
    assert inherits[0].dst == base
    # class inheritance is `inherits`, never `implements`
    assert not [e for e in graph.edges if e.kind == "implements"]
