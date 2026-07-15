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
