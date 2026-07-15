"""Tests for the graphify-compatible graph.json export (Phase 4 / viz).

The export must (a) map cppgraph's Node/Edge onto graphify's `nodes`/`links`
schema (so the same file opens in graphify *and* in our own viz) and (b) let us
scope a viewable subgraph around a focus symbol, since the full mongo graph is
far too large to render.
"""

from __future__ import annotations

from pathlib import Path

from cppgraph.export import to_graphify_graph
from cppgraph.model import Edge, Graph, Node
from cppgraph.store import GraphStore, write_sqlite

A = "cxx . . $ mongo/Foo#a()."
B = "cxx . . $ mongo/Foo#b()."
C = "cxx . . $ mongo/Foo#c()."
D = "cxx . . $ mongo/Foo#d()."


def test_mapper_emits_graphify_schema() -> None:
    nodes = [Node(symbol=A, display_name="a", file="foo.cpp", line=9)]
    edges = [Edge(kind="calls", src=B, dst=A, file="bar.cpp", line=41)]
    g = to_graphify_graph(nodes, edges)

    assert set(g) >= {"nodes", "links"}
    (n,) = g["nodes"]
    assert n["id"] == A
    assert n["label"] == "a"
    assert n["source_file"] == "foo.cpp"
    assert n["source_location"] == "L10"  # model line is 0-indexed -> 1-based

    (link,) = g["links"]
    assert link["source"] == B
    assert link["target"] == A
    assert link["relation"] == "calls"
    assert link["source_location"] == "L42"


def test_mapper_label_falls_back_to_symbol_when_no_display_name() -> None:
    g = to_graphify_graph([Node(symbol=A)], [])
    assert g["nodes"][0]["label"]  # non-empty even without display_name


def _store(tmp_path: Path) -> GraphStore:
    # a -> b -> c  (calls), and d -> a ; so around `a`: in={d}, out={b} at depth 1
    graph = Graph()
    for s, name in [(A, "a"), (B, "b"), (C, "c"), (D, "d")]:
        graph.add_node(s, display_name=name)
    graph.add_edge("calls", A, B, file="f.cpp", line=1)
    graph.add_edge("calls", B, C, file="f.cpp", line=2)
    graph.add_edge("calls", D, A, file="f.cpp", line=3)
    db = tmp_path / "g.db"
    write_sqlite(graph, db)
    return GraphStore(db)


def test_subgraph_depth1_both_directions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    nodes, edges = store.subgraph(A, depth=1, direction="both")
    syms = {n.symbol for n in nodes}
    assert syms == {A, B, D}  # a plus its immediate in/out neighbours, not c
    # only edges whose BOTH endpoints are in the node set are induced
    pairs = {(e.src, e.dst) for e in edges}
    assert (A, B) in pairs and (D, A) in pairs
    assert (B, C) not in pairs


def test_subgraph_depth2_reaches_further(tmp_path: Path) -> None:
    store = _store(tmp_path)
    nodes, _ = store.subgraph(A, depth=2, direction="out")
    assert {n.symbol for n in nodes} == {A, B, C}


def test_subgraph_unknown_symbol_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.subgraph("nope", depth=2) == ([], [])
