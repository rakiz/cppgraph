from __future__ import annotations

from pathlib import Path

from cppgraph.model import Graph


def test_graph_json_roundtrip(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=3)

    out = tmp_path / "graph.json"
    graph.save_json(out)
    loaded = Graph.load_json(out)

    assert set(loaded.nodes) == {"a", "b"}
    assert len(loaded.edges) == 1
    assert loaded.edges[0].kind == "calls"
    assert loaded.edges[0].src == "a"
    assert loaded.edges[0].dst == "b"


def test_graph_find_matches_symbol_or_display_name() -> None:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#makeResumeToken(a1).", display_name="makeResumeToken")
    graph.add_node("cxx . . $ mongo/Bar#other(a2).", display_name="other")

    matches = graph.find("makeResumeToken")

    assert len(matches) == 1
    assert matches[0].display_name == "makeResumeToken"


def test_graph_find_multi_term_is_order_free_and() -> None:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Pipeline#buildPipeline(a1).", display_name="buildPipeline")
    graph.add_node("cxx . . $ mongo/changeStream/buildPipeline(a2).", display_name="buildPipeline")
    graph.add_node("cxx . . $ mongo/Pipeline#other(a3).", display_name="other")

    # Both tokens must be present (order-free); only the changeStream one matches.
    matches = graph.find("buildPipeline changeStream")
    assert len(matches) == 1
    assert "changeStream" in matches[0].symbol

    # Reversed order matches the same node.
    assert graph.find("changeStream buildPipeline") == matches

    # A single token stays a plain substring match (two here).
    assert len(graph.find("buildPipeline")) == 2


def test_graph_find_no_match_returns_empty() -> None:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#bar(a1).", display_name="bar")

    assert graph.find("nope") == []


def test_shortest_call_path_direct() -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=1)

    chain = graph.shortest_call_path("a", "b")

    assert chain is not None
    assert [e.dst for e in chain] == ["b"]


def test_shortest_call_path_multi_hop_picks_shortest() -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="foo.cpp", line=1)
    graph.add_edge("calls", "b", "c", file="foo.cpp", line=2)
    graph.add_edge("calls", "a", "c", file="foo.cpp", line=3)  # shortcut

    chain = graph.shortest_call_path("a", "c")

    assert chain is not None
    assert [e.dst for e in chain] == ["c"]  # takes the direct edge, not via b


def test_shortest_call_path_same_symbol_is_empty_chain() -> None:
    graph = Graph()
    graph.add_node("a")

    assert graph.shortest_call_path("a", "a") == []


def test_shortest_call_path_no_path_returns_none() -> None:
    graph = Graph()
    graph.add_node("a")
    graph.add_node("b")

    assert graph.shortest_call_path("a", "b") is None


def test_shortest_call_path_unknown_symbol_returns_none() -> None:
    graph = Graph()
    graph.add_node("a")

    assert graph.shortest_call_path("a", "nope") is None


def test_impact_transitive_callers() -> None:
    graph = Graph()
    graph.add_edge("calls", "grandparent", "parent", file="foo.cpp", line=1)
    graph.add_edge("calls", "parent", "target", file="foo.cpp", line=2)
    graph.add_edge("calls", "unrelated", "other", file="foo.cpp", line=3)

    affected = graph.impact("target")

    assert affected == {"parent", "grandparent"}


def test_impact_respects_max_depth() -> None:
    graph = Graph()
    graph.add_edge("calls", "grandparent", "parent", file="foo.cpp", line=1)
    graph.add_edge("calls", "parent", "target", file="foo.cpp", line=2)

    assert graph.impact("target", max_depth=1) == {"parent"}
    assert graph.impact("target", max_depth=2) == {"parent", "grandparent"}


def test_impact_unknown_symbol_returns_empty() -> None:
    graph = Graph()
    assert graph.impact("nope") == set()
