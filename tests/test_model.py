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


def test_graph_find_no_match_returns_empty() -> None:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#bar(a1).", display_name="bar")

    assert graph.find("nope") == []
