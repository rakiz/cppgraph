from __future__ import annotations

from cppgraph.model import Graph


def test_graph_accumulates_and_dedups() -> None:
    """The kept surface: `Graph` is the builder's accumulation buffer — node
    interning, edge/reference dedup — consumed by `write_sqlite` (the query
    engine that used to live here is gone; queries are `GraphStore`'s)."""
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=3)
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=3)  # exact dup dropped
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=4)  # other line kept
    graph.add_reference("t", "foo.cpp", line=7, enclosing_symbol="a")
    graph.add_reference("t", "foo.cpp", line=7)  # same (symbol, file, line) dropped

    assert set(graph.nodes) == {"a", "b", "t"}
    assert len(graph.edges) == 2
    assert [(r.line, r.enclosing_symbol) for r in graph.references_of("t")] == [(7, "a")]
    assert [e.line for e in graph.callers_of("b")] == [3, 4]


def test_graph_add_node_backfills_display_name() -> None:
    graph = Graph()
    first = graph.add_node("cxx . . $ mongo/Foo#bar(a1).")
    again = graph.add_node("cxx . . $ mongo/Foo#bar(a1).", display_name="bar")

    assert first is again
    assert graph.nodes["cxx . . $ mongo/Foo#bar(a1)."].display_name == "bar"
