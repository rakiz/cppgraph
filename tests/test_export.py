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


def test_file_usage_graph_maps_references_to_file_edges() -> None:
    from cppgraph.export import to_file_usage_graph
    from cppgraph.model import Reference

    refs = [
        Reference(symbol=A, file="a/foo.cpp", line=1),
        Reference(symbol=A, file="a/foo.cpp", line=8),  # same file -> weight 2
        Reference(symbol=A, file="b/bar.h", line=3),
    ]
    g = to_file_usage_graph(A, "ResumeTokenData", refs)

    ids = {n["id"] for n in g["nodes"]}
    assert A in ids
    assert "file:a/foo.cpp" in ids and "file:b/bar.h" in ids
    foo = next(n for n in g["nodes"] if n["id"] == "file:a/foo.cpp")
    assert foo["label"] == "foo.cpp" and foo["kind"] == "file"

    links = {lk["target"]: lk for lk in g["links"]}
    assert links["file:a/foo.cpp"]["relation"] == "references"
    assert links["file:a/foo.cpp"]["weight"] == 2
    assert links["file:b/bar.h"]["weight"] == 1


def test_file_usage_graph_empty_when_no_references() -> None:
    from cppgraph.export import to_file_usage_graph

    g = to_file_usage_graph(A, "x", [])
    assert g["nodes"] == [{"id": A, "label": "x", "_origin": "cppgraph"}]
    assert g["links"] == []


def test_is_test_file_recognizes_mongo_conventions() -> None:
    from cppgraph.export import is_test_file

    assert is_test_file("src/mongo/db/pipeline/resume_token_test.cpp")
    assert is_test_file("src/mongo/db/pipeline/change_stream_test_helpers.cpp")
    assert is_test_file("src/mongo/foo_unittest.cpp")
    assert is_test_file("src/mongo/db/s/tests/whatever.cpp")
    assert not is_test_file("src/mongo/db/pipeline/resume_token.cpp")
    assert not is_test_file("src/mongo/db/pipeline/resume_token.h")
    assert not is_test_file(None)


def test_file_usage_graph_can_exclude_tests_via_build_helper(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json
    from cppgraph.model import Graph
    from cppgraph.store import GraphStore, write_sqlite

    sym = "cxx . . $ mongo/ResumeTokenData#"
    graph = Graph()
    graph.add_node(sym, display_name="ResumeTokenData")
    graph.add_reference(sym, "src/mongo/resume_token.cpp", 1)
    graph.add_reference(sym, "src/mongo/resume_token_test.cpp", 2)
    db = tmp_path / "r.db"
    write_sqlite(graph, db)
    store = GraphStore(db)

    full = build_export_json(store, sym, mode="usage")
    assert len(full["links"]) == 2
    prod = build_export_json(store, sym, mode="usage", exclude_tests=True)
    assert {lk["target"] for lk in prod["links"]} == {"file:src/mongo/resume_token.cpp"}
