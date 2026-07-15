"""Tests for the Phase 2 SQLite-backed store (interned symbols, indexed topology).

The store must answer the exact same queries as the in-memory `Graph`
(callers/callees/find/path/impact) but off a SQLite file, without loading the
whole graph into RAM. See DESIGN.md § Store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cppgraph.model import Graph
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, build_provenance, write_sqlite

METHOD = "cxx . . $ mongo/Foo#makeResumeToken(a1)."
CALLER = "cxx . . $ mongo/Foo#caller(a2)."
OTHER = "cxx . . $ mongo/Bar#other(b1)."


def _store(tmp_path: Path, graph: Graph) -> GraphStore:
    db = tmp_path / "graph.db"
    write_sqlite(graph, db)
    return GraphStore(db)


def _sample(tmp_path: Path) -> GraphStore:
    graph = Graph()
    graph.add_node(METHOD, display_name="makeResumeToken")
    graph.add_edge("calls", CALLER, METHOD, file="foo.cpp", line=9)
    return _store(tmp_path, graph)


def test_callers_resolves_symbol_file_and_line(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    edges = store.callers_of(METHOD)
    assert [e.src for e in edges] == [CALLER]
    assert edges[0].file == "foo.cpp"
    assert edges[0].line == 9


def test_callees_resolves_symbol(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    edges = store.callees_of(CALLER)
    assert [e.dst for e in edges] == [METHOD]


def test_find_substring_matches_symbol_or_display_name(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    matches = store.find("makeResumeToken")
    assert len(matches) == 1
    assert matches[0].symbol == METHOD
    assert matches[0].display_name == "makeResumeToken"


def test_find_is_case_sensitive_like_in_memory(tmp_path: Path) -> None:
    """The in-memory `Graph.find` uses Python `in` (case-sensitive); the SQLite
    store must match that, not SQLite's default case-insensitive LIKE."""
    store = _sample(tmp_path)
    assert store.find("makeresumetoken") == []
    assert len(store.find("makeResumeToken")) == 1


def test_find_no_match_returns_empty(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    assert store.find("nope") == []


def test_has_symbol(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    assert store.has_symbol(METHOD)
    assert not store.has_symbol("cxx . . $ mongo/Nope#x(z9).")


def test_get_node_returns_definition_location(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node(METHOD, display_name="makeResumeToken")
    graph.nodes[METHOD].file = "foo.cpp"
    graph.nodes[METHOD].line = 41
    store = _store(tmp_path, graph)
    node = store.get_node(METHOD)
    assert node is not None
    assert node.display_name == "makeResumeToken"
    assert node.file == "foo.cpp"
    assert node.line == 41


def test_shortest_call_path_multi_hop(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="f.cpp", line=1)
    graph.add_edge("calls", "b", "c", file="f.cpp", line=2)
    store = _store(tmp_path, graph)
    chain = store.shortest_call_path("a", "c")
    assert chain is not None
    assert [e.dst for e in chain] == ["b", "c"]


def test_shortest_call_path_picks_shortcut(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="f.cpp", line=1)
    graph.add_edge("calls", "b", "c", file="f.cpp", line=2)
    graph.add_edge("calls", "a", "c", file="f.cpp", line=3)
    store = _store(tmp_path, graph)
    chain = store.shortest_call_path("a", "c")
    assert chain is not None
    assert [e.dst for e in chain] == ["c"]


def test_shortest_call_path_same_symbol_is_empty(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node("a")
    store = _store(tmp_path, graph)
    assert store.shortest_call_path("a", "a") == []


def test_shortest_call_path_no_path_returns_none(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node("a")
    graph.add_node("b")
    store = _store(tmp_path, graph)
    assert store.shortest_call_path("a", "b") is None


def test_shortest_call_path_unknown_symbol_returns_none(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node("a")
    store = _store(tmp_path, graph)
    assert store.shortest_call_path("a", "nope") is None


def test_impact_transitive_callers(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "grandparent", "parent", file="f.cpp", line=1)
    graph.add_edge("calls", "parent", "target", file="f.cpp", line=2)
    graph.add_edge("calls", "unrelated", "other", file="f.cpp", line=3)
    store = _store(tmp_path, graph)
    assert store.impact("target") == {"parent", "grandparent"}


def test_impact_respects_max_depth(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "grandparent", "parent", file="f.cpp", line=1)
    graph.add_edge("calls", "parent", "target", file="f.cpp", line=2)
    store = _store(tmp_path, graph)
    assert store.impact("target", max_depth=1) == {"parent"}
    assert store.impact("target", max_depth=2) == {"parent", "grandparent"}


def test_impact_unknown_symbol_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path, Graph())
    assert store.impact("nope") == set()


def test_store_persists_and_reopens(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    graph = Graph()
    graph.add_edge("calls", CALLER, METHOD, file="foo.cpp", line=9)
    write_sqlite(graph, db)
    # A fresh handle on the same file (no in-memory state carried over).
    reopened = GraphStore(db)
    assert reopened.has_symbol(METHOD)
    assert [e.src for e in reopened.callers_of(METHOD)] == [CALLER]


def test_write_sqlite_overwrites_existing_file(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    first = Graph()
    first.add_edge("calls", CALLER, METHOD, file="foo.cpp", line=9)
    write_sqlite(first, db)
    second = Graph()
    second.add_edge("calls", "x", "y", file="g.cpp", line=1)
    write_sqlite(second, db)
    store = GraphStore(db)
    assert not store.has_symbol(METHOD)
    assert store.has_symbol("y")


def test_meta_records_node_and_edge_counts(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    meta = store.meta()
    assert meta["node_count"] == "2"  # caller + method
    assert meta["edge_count"] == "1"


def test_meta_roundtrips_provided_provenance(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    graph = Graph()
    graph.add_edge("calls", CALLER, METHOD, file="foo.cpp", line=9)
    write_sqlite(graph, db, meta={"source_commit": "deadbeef", "project_root": "file:///x"})
    meta = GraphStore(db).meta()
    assert meta["source_commit"] == "deadbeef"
    assert meta["project_root"] == "file:///x"


def test_meta_empty_for_store_without_meta_table(tmp_path: Path) -> None:
    """A store written before the meta table existed must not crash `meta()`."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE symbols(id INTEGER PRIMARY KEY, symbol TEXT, "
        "display_name TEXT, file_id INTEGER, line INTEGER);"
        "CREATE TABLE edges(kind TEXT, src_id INTEGER, dst_id INTEGER, "
        "file_id INTEGER, line INTEGER);"
    )
    con.commit()
    con.close()
    assert GraphStore(db).meta() == {}


def _index_with_metadata(project_root: str, *, tool_version: str = "0.4.0") -> scip_pb2.Index:
    index = scip_pb2.Index()
    index.metadata.project_root = project_root
    index.metadata.tool_info.name = "scip-clang"
    index.metadata.tool_info.version = tool_version
    return index


def test_build_provenance_uses_explicit_commit_and_copies_scip_metadata() -> None:
    index = _index_with_metadata("file:///some/repo")
    meta = build_provenance(index, source_commit="abc123", source_dirty=True)
    assert meta["source_commit"] == "abc123"
    assert meta["source_dirty"] == "true"
    assert meta["project_root"] == "file:///some/repo"
    assert meta["index_tool"] == "scip-clang"
    assert meta["index_tool_version"] == "0.4.0"
    assert "built_at" in meta


def test_build_provenance_omits_commit_when_root_is_not_a_git_repo(tmp_path: Path) -> None:
    # A real, existing, non-git directory: git rev-parse fails, no commit stored.
    index = _index_with_metadata(f"file://{tmp_path}")
    meta = build_provenance(index)
    assert "source_commit" not in meta
    assert meta["project_root"] == f"file://{tmp_path}"


def test_build_provenance_autodetects_commit_on_this_repo() -> None:
    """When project_root IS a git checkout, the commit is auto-detected — proven
    against cppgraph's own repo (the test runner's checkout)."""
    repo_root = Path(__file__).resolve().parent.parent
    index = _index_with_metadata(f"file://{repo_root}")
    meta = build_provenance(index)
    assert "source_commit" in meta
    assert len(meta["source_commit"]) == 40  # full SHA-1
    assert meta["source_dirty"] in ("true", "false")


def test_implements_edges_are_stored(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("implements", "cxx . . $ mongo/Dog#sound(d1).",
                   "cxx . . $ mongo/Animal#sound(a1).", file="animal.h")
    store = _store(tmp_path, graph)
    # implements edges don't participate in call queries, but must round-trip.
    assert store.has_symbol("cxx . . $ mongo/Dog#sound(d1).")
    assert store.callers_of("cxx . . $ mongo/Animal#sound(a1).") == []
