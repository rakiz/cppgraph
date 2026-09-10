"""Tests for the graphify-compatible graph.json export (Phase 4 / viz).

The export must (a) map cppgraph's Node/Edge onto graphify's `nodes`/`links`
schema (so the same file opens in graphify *and* in our own viz) and (b) let us
scope a viewable subgraph around a focus symbol, since the full mongo graph is
far too large to render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_symbol_usage_graph_maps_references_to_enclosing_symbols() -> None:
    from cppgraph.export import to_symbol_usage_graph
    from cppgraph.model import Reference

    render = "cxx . . $ pkg/render(r1)."
    layout = "cxx . . $ pkg/layout(l1)."
    refs = [
        Reference(symbol=A, file="a.cpp", line=1, enclosing_symbol=render),
        Reference(symbol=A, file="a.cpp", line=8, enclosing_symbol=render),  # weight 2
        Reference(symbol=A, file="b.cpp", line=3, enclosing_symbol=layout),
        Reference(symbol=A, file="c.cpp", line=9),  # unattributed -> file fallback
    ]
    g = to_symbol_usage_graph(A, "ResumeTokenData", refs)

    links = {lk["target"]: lk for lk in g["links"]}
    assert links[render]["relation"] == "used_by" and links[render]["weight"] == 2
    assert links[layout]["weight"] == 1
    # The unattributed one still appears, at file granularity.
    assert links["file:c.cpp"]["relation"] == "references"


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


# --- mode="path": the shortest call chain between two symbols as a viewable graph ---


def test_path_mode_build_helper_returns_the_chain(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json

    store = _store(tmp_path)
    # D -> A -> B -> C: 4 nodes, 3 calls edges, in chain order.
    g = build_export_json(store, D, mode="path", dst=C)
    assert g is not None
    ids = [n["id"] for n in g["nodes"]]
    assert ids == [D, A, B, C]
    pairs = [(lk["source"], lk["target"]) for lk in g["links"]]
    assert pairs == [(D, A), (A, B), (B, C)]
    assert all(lk["relation"] == "calls" for lk in g["links"])


def test_path_mode_src_equals_dst_is_single_node(tmp_path: Path) -> None:
    # `shortest_call_path` returns [] for src==dst -> a one-node graph, no edges
    # (not the "no path" empty graph, which has 0 nodes).
    from cppgraph.cli import build_export_json

    store = _store(tmp_path)
    g = build_export_json(store, A, mode="path", dst=A)
    assert g is not None
    assert [n["id"] for n in g["nodes"]] == [A]
    assert g["links"] == []


def test_path_mode_no_path_is_empty_graph_not_none(tmp_path: Path) -> None:
    # C has no outgoing edges: C -> D is valid symbols, no chain -> an EMPTY
    # graph (distinct from the None "unknown symbol" reply, so callers can tell
    # the two apart).
    from cppgraph.cli import build_export_json

    store = _store(tmp_path)
    g = build_export_json(store, C, mode="path", dst=D)
    assert g is not None
    assert g["nodes"] == []
    assert g["links"] == []


def test_path_mode_unknown_symbol_is_none(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json

    store = _store(tmp_path)
    assert build_export_json(store, "nope", mode="path", dst=C) is None
    assert build_export_json(store, D, mode="path", dst="nope") is None


def test_path_mode_requires_dst(tmp_path: Path) -> None:
    # A programmer error (the CLI/MCP boundary enforces --dst): ValueError.
    from cppgraph.cli import build_export_json

    store = _store(tmp_path)
    with pytest.raises(ValueError, match="mode='path' requires dst"):
        build_export_json(store, D, mode="path")


# --- the corridor (`expand_paths`): every node/edge on SOME src->dst path -------


P_SRC = "cxx . . $ mongo/Flow#src(a1)."
P_LEFT = "cxx . . $ mongo/Flow#left()."
P_RIGHT = "cxx . . $ mongo/Flow#right()."
P_DST = "cxx . . $ mongo/Flow#dst(a2)."
P_SIB = "cxx . . $ mongo/Flow#sibling()."

_DIAMOND_EDGES = [
    (P_SRC, P_LEFT),
    (P_SRC, P_RIGHT),
    (P_LEFT, P_DST),
    (P_RIGHT, P_DST),
]


def _diamond_store(tmp_path: Path, *, with_sibling: bool = False) -> GraphStore:
    """src -> {left, right} -> dst (two disjoint routes that reconverge);
    `with_sibling` adds a src -> sibling dead-end that never reaches dst."""
    graph = Graph()
    for s, name in [
        (P_SRC, "src"),
        (P_LEFT, "left"),
        (P_RIGHT, "right"),
        (P_DST, "dst"),
        *([(P_SIB, "sibling")] if with_sibling else []),
    ]:
        graph.add_node(s, display_name=name)
    for i, (src, dst) in enumerate(_DIAMOND_EDGES):
        graph.add_edge("calls", src, dst, file="f.cpp", line=i + 1)
    if with_sibling:
        graph.add_edge("calls", P_SRC, P_SIB, file="f.cpp", line=9)
    db = tmp_path / "d.db"
    write_sqlite(graph, db)
    return GraphStore(db)


def test_call_corridor_diamond_includes_all_four_nodes_and_edges(tmp_path: Path) -> None:
    store = _diamond_store(tmp_path)
    nodes, edges, truncated = store.call_corridor(P_SRC, P_DST)
    assert {n.symbol for n in nodes} == {P_SRC, P_LEFT, P_RIGHT, P_DST}
    assert {(e.src, e.dst) for e in edges} == set(_DIAMOND_EDGES)
    assert all(e.kind == "calls" for e in edges)
    assert truncated is False


def test_call_corridor_excludes_nodes_that_cannot_reach_dst(tmp_path: Path) -> None:
    # The correctness-critical half: `sibling` IS reachable from src, but reaches
    # nothing on the way to dst — a forward-BFS-only "corridor" would include it.
    store = _diamond_store(tmp_path, with_sibling=True)
    nodes, edges, _ = store.call_corridor(P_SRC, P_DST)
    assert P_SIB not in {n.symbol for n in nodes}
    assert (P_SRC, P_SIB) not in {(e.src, e.dst) for e in edges}


def test_call_corridor_edges_are_induced_not_just_bfs_walked(tmp_path: Path) -> None:
    # A chord between two corridor nodes belongs to the corridor even though
    # neither BFS pass needs it as a tree edge (both endpoints are on routes,
    # so the chord itself completes a real src->dst path through it).
    graph = Graph()
    for s, name in [(P_SRC, "src"), (P_LEFT, "left"), (P_RIGHT, "right"), (P_DST, "dst")]:
        graph.add_node(s, display_name=name)
    for src, dst in _DIAMOND_EDGES:
        graph.add_edge("calls", src, dst, file="f.cpp", line=1)
    graph.add_edge("calls", P_LEFT, P_RIGHT, file="f.cpp", line=2)
    db = tmp_path / "chord.db"
    write_sqlite(graph, db)
    store = GraphStore(db)

    nodes, edges, truncated = store.call_corridor(P_SRC, P_DST)
    assert {n.symbol for n in nodes} == {P_SRC, P_LEFT, P_RIGHT, P_DST}
    assert {(e.src, e.dst) for e in edges} == {*_DIAMOND_EDGES, (P_LEFT, P_RIGHT)}
    assert truncated is False


def test_call_corridor_src_equals_dst_is_single_node(tmp_path: Path) -> None:
    # Same shape the chain path produces for src == dst: one node, no edges.
    store = _diamond_store(tmp_path)
    nodes, edges, truncated = store.call_corridor(P_SRC, P_SRC)
    assert [n.symbol for n in nodes] == [P_SRC]
    assert edges == []
    assert truncated is False


def test_call_corridor_no_path_is_empty(tmp_path: Path) -> None:
    store = _diamond_store(tmp_path, with_sibling=True)
    assert store.call_corridor(P_SIB, P_DST) == ([], [], False)  # sibling dead-ends
    assert store.call_corridor(P_DST, P_SRC) == ([], [], False)  # direction matters


def test_call_corridor_unknown_symbols_are_empty(tmp_path: Path) -> None:
    # Callers that must tell "unknown" from "no path" check has_symbol first
    # (as build_export_json does) — the store method itself just stays empty.
    store = _diamond_store(tmp_path)
    assert store.call_corridor("nope", P_DST) == ([], [], False)
    assert store.call_corridor(P_SRC, "nope") == ([], [], False)


def test_call_corridor_bfs_cap_truncates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Safety valve: with the cap monkeypatched down to 5, each BFS pass of an
    # 8-node chain stops with unexplored nodes left — the corridor is a strict
    # subset (forward saw n0..n4, backward n3..n7) and `truncated` says so
    # instead of silently returning a partial answer.
    import cppgraph.store as store_module

    graph = Graph()
    syms = [f"cxx . . $ mongo/Chain#n{i}(a{i})." for i in range(8)]
    for i, s in enumerate(syms):
        graph.add_node(s, display_name=f"n{i}")
    for a, b in zip(syms, syms[1:]):
        graph.add_edge("calls", a, b, file="f.cpp", line=1)
    db = tmp_path / "c.db"
    write_sqlite(graph, db)
    store = GraphStore(db)

    monkeypatch.setattr(store_module, "_MAX_CORRIDOR_BFS_NODES", 5)
    nodes, edges, truncated = store.call_corridor(syms[0], syms[-1])
    assert truncated is True
    assert {n.symbol for n in nodes} == {syms[3], syms[4]}
    assert [(e.src, e.dst) for e in edges] == [(syms[3], syms[4])]


def test_call_corridor_cap_exact_exhaustion_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A graph fully explored AT the cap is complete, not truncated: the flag
    # fires only when a NEW node is discovered while already at the cap.
    import cppgraph.store as store_module

    store = _diamond_store(tmp_path)
    monkeypatch.setattr(store_module, "_MAX_CORRIDOR_BFS_NODES", 4)
    nodes, edges, truncated = store.call_corridor(P_SRC, P_DST)
    assert truncated is False
    assert len(nodes) == 4 and len(edges) == 4


# --- mode="path" wiring: expand_paths / depth-context / limit -------------------


def test_path_mode_expand_paths_returns_the_corridor(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path)
    g = build_export_json(store, P_SRC, mode="path", dst=P_DST, expand_paths=True)
    assert g is not None
    assert {n["id"] for n in g["nodes"]} == {P_SRC, P_LEFT, P_RIGHT, P_DST}
    assert {(lk["source"], lk["target"]) for lk in g["links"]} == set(_DIAMOND_EDGES)
    assert g["truncated"] is False
    assert g["total"] == 4
    assert g["core_edges"] == 4


def test_path_mode_without_expand_paths_gives_one_chain(tmp_path: Path) -> None:
    # The same diamond without the flag stays the (pre-feature) single chain.
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path)
    g = build_export_json(store, P_SRC, mode="path", dst=P_DST)
    assert len(g["nodes"]) == 3  # src, left, dst (BFS's first route)
    assert len(g["links"]) == 2


def test_path_mode_depth_expands_context(tmp_path: Path) -> None:
    # depth=1 pulls the sibling (a dead-end neighbour of src) in around the
    # chain — the same mixed-edge-kind neighbourhood walk deps mode uses.
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path, with_sibling=True)
    g = build_export_json(store, P_SRC, mode="path", dst=P_DST, depth=1)
    assert {n["id"] for n in g["nodes"]} == {P_SRC, P_LEFT, P_RIGHT, P_DST, P_SIB}
    assert {(lk["source"], lk["target"]) for lk in g["links"]} == {
        *_DIAMOND_EDGES,
        (P_SRC, P_SIB),
    }
    assert g["core_edges"] == 2  # the shortest chain proper, before the context union


def test_path_mode_default_depth_stays_pure_chain(tmp_path: Path) -> None:
    # THE backward-compatibility guard: `depth`'s deps-mode default (2) must NOT
    # leak into path mode — omitting it (or passing 0) yields the pure chain,
    # however many context neighbours are one hop away.
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path, with_sibling=True)
    omitted = build_export_json(store, P_SRC, mode="path", dst=P_DST)
    explicit_zero = build_export_json(store, P_SRC, mode="path", dst=P_DST, depth=0)
    assert omitted is not None and explicit_zero is not None
    assert omitted == explicit_zero
    assert {n["id"] for n in omitted["nodes"]} == {P_SRC, P_LEFT, P_DST}
    assert P_SIB not in {n["id"] for n in omitted["nodes"]}
    assert P_RIGHT not in {n["id"] for n in omitted["nodes"]}


def test_path_mode_corridor_plus_depth_unions_both(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path, with_sibling=True)
    g = build_export_json(store, P_SRC, mode="path", dst=P_DST, expand_paths=True, depth=1)
    assert {n["id"] for n in g["nodes"]} == {P_SRC, P_LEFT, P_RIGHT, P_DST, P_SIB}
    assert len(g["links"]) == 5
    assert g["core_edges"] == 4


def test_path_mode_limit_truncates_and_reports(tmp_path: Path) -> None:
    from cppgraph.cli import build_export_json

    store = _diamond_store(tmp_path, with_sibling=True)
    g = build_export_json(store, P_SRC, mode="path", dst=P_DST, expand_paths=True, depth=1, limit=4)
    assert g is not None
    # The corridor (the actual answer) is kept whole; the context sibling is the
    # overflow that gets cut, and its edge goes with it.
    assert {n["id"] for n in g["nodes"]} == {P_SRC, P_LEFT, P_RIGHT, P_DST}
    assert {(lk["source"], lk["target"]) for lk in g["links"]} == set(_DIAMOND_EDGES)
    assert g["truncated"] is True
    assert g["total"] == 5


def test_path_mode_limit_core_alone_exceeding_keeps_a_prefix(tmp_path: Path) -> None:
    # If even the chain can't fit, the cap keeps a deterministic core prefix and
    # flags it — no silent oversized output.
    from cppgraph.cli import build_export_json

    graph = Graph()
    syms = [f"cxx . . $ mongo/Chain#n{i}(a{i})." for i in range(6)]
    for i, s in enumerate(syms):
        graph.add_node(s, display_name=f"n{i}")
    for a, b in zip(syms, syms[1:]):
        graph.add_edge("calls", a, b, file="f.cpp", line=1)
    db = tmp_path / "chain.db"
    write_sqlite(graph, db)
    store = GraphStore(db)

    g = build_export_json(store, syms[0], mode="path", dst=syms[-1], limit=4)
    assert g is not None
    assert [n["id"] for n in g["nodes"]] == syms[:4]
    assert {(lk["source"], lk["target"]) for lk in g["links"]} == {
        (syms[0], syms[1]),
        (syms[1], syms[2]),
        (syms[2], syms[3]),
    }
    assert g["truncated"] is True
    assert g["total"] == 6
    assert g["core_edges"] == 5
