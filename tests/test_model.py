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
