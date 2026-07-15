"""Tests for the Phase 2 SQLite-backed store (interned symbols, indexed topology).

The store must answer the exact same queries as the in-memory `Graph`
(callers/callees/find/path/impact) but off a SQLite file, without loading the
whole graph into RAM. See DESIGN.md § Store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cppgraph.builder import build_graph
from cppgraph.model import Graph, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, build_provenance, update_store, write_sqlite

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


# --- inheritance queries ---------------------------------------------------

BASE = "cxx . . $ mongo/Base#"
DERIVED = "cxx . . $ mongo/Derived#"
LEAF = "cxx . . $ mongo/Leaf#"


def _hierarchy(tmp_path: Path) -> GraphStore:
    # Leaf -> Derived -> Base (edge src = derived, dst = base)
    graph = Graph()
    graph.nodes[BASE] = Node(symbol=BASE, display_name="Base", file="b.h", line=1)
    graph.add_edge("inherits", DERIVED, BASE, file="d.h", line=3)
    graph.add_edge("inherits", LEAF, DERIVED, file="l.h", line=4)
    graph.add_edge("calls", "cxx . . $ mongo/x#f().", DERIVED, file="x.cpp", line=1)
    return _store(tmp_path, graph)


def test_bases_of_lists_direct_supertypes_with_def_site(tmp_path: Path) -> None:
    store = _hierarchy(tmp_path)
    bases = store.bases_of(DERIVED)
    assert [n.symbol for n in bases] == [BASE]
    # the base type's own definition site, not the (line-less) inherits edge
    assert bases[0].file == "b.h"
    assert bases[0].line == 1


def test_subtypes_of_lists_direct_subtypes(tmp_path: Path) -> None:
    store = _hierarchy(tmp_path)
    subs = store.subtypes_of(BASE)
    assert [n.symbol for n in subs] == [DERIVED]


TYPE = "cxx . . $ mongo/ResumeTokenData#"


def test_references_of_returns_locations(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 11)
    graph.add_reference(TYPE, "a.cpp", 40)
    graph.add_reference(TYPE, "b.cpp", 7)
    store = _store(tmp_path, graph)
    refs = store.references_of(TYPE)
    assert [(r.file, r.line) for r in refs] == [("a.cpp", 11), ("a.cpp", 40), ("b.cpp", 7)]
    assert store.meta().get("has_references") == "true"


def test_references_empty_when_not_built(tmp_path: Path) -> None:
    # a graph with no references at all -> no has_references flag, empty query
    store = _sample(tmp_path)
    assert store.references_of(METHOD) == []
    assert "has_references" not in store.meta()


def test_references_unknown_symbol_returns_empty(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 1)
    store = _store(tmp_path, graph)
    assert store.references_of("does::not::exist") == []


def test_update_replaces_references_for_changed_file(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 5)
    graph.add_reference(TYPE, "b.cpp", 9)
    db = tmp_path / "g.db"
    write_sqlite(graph, db)

    # re-index a.cpp: the type now used at a different line there
    partial = scip_pb2.Index()
    doc = partial.documents.add(relative_path="a.cpp")
    doc.occurrences.add(symbol=TYPE).range.extend([21, 0, 5])
    store = GraphStore(db)
    store.apply_update(build_graph(partial, include_references=True), ["a.cpp"])
    refs = store.references_of(TYPE)
    # a.cpp:5 replaced by a.cpp:21; b.cpp:9 untouched
    assert sorted((r.file, r.line) for r in refs) == [("a.cpp", 21), ("b.cpp", 9)]


def test_impact_over_inherits_gives_transitive_descendants(tmp_path: Path) -> None:
    store = _hierarchy(tmp_path)
    # everything that transitively derives from Base
    assert store.impact(BASE, kind="inherits") == {DERIVED, LEAF}
    # calls-space impact of Base is empty (no calls edges into it)
    assert store.impact(BASE) == set()


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


# --- incremental update -----------------------------------------------------
#
# A partial re-index (only changed TUs) must replace exactly the changed files'
# contributions and leave everything else byte-for-byte intact — the whole point
# of the document-local builder (see DESIGN.md § "Keeping the graph up to date").


def _partial_index(*paths: str) -> scip_pb2.Index:
    """A partial SCIP index containing (empty) Documents for `paths`, so the
    update knows which files were re-indexed even when they now produce no
    edges. Callers add occurrences to the returned documents as needed."""
    index = scip_pb2.Index()
    for p in paths:
        index.documents.add(relative_path=p)
    return index


def _add_call(doc: scip_pb2.Document, caller: str, callee: str, *, def_line: int, call_line: int) -> None:
    """Add a callable definition and a call to `callee` attributed to it."""
    d = doc.occurrences.add(symbol=caller, symbol_roles=scip_pb2.SymbolRole.Definition)
    d.range.extend([def_line, 0, 3])
    c = doc.occurrences.add(symbol=callee)
    c.range.extend([call_line, 0, 3])


def test_update_replaces_only_changed_files_edges(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "old().", file="foo.cpp", line=5)
    original.add_edge("calls", "c().", "d().", file="bar.cpp", line=9)
    write_sqlite(original, db)

    # foo.cpp re-indexed: a() now calls new() instead of old(). bar.cpp untouched.
    partial = _partial_index("foo.cpp")
    _add_call(partial.documents[0], "a().", "new().", def_line=2, call_line=6)
    update_store(db, partial)

    store = GraphStore(db)
    assert [e.dst for e in store.callees_of("a().")] == ["new()."]
    # bar.cpp's edge is left exactly as it was.
    assert [e.dst for e in store.callees_of("c().")] == ["d()."]


def test_update_garbage_collects_orphaned_symbols(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "old().", file="foo.cpp", line=5)
    write_sqlite(original, db)
    assert GraphStore(db).has_symbol("old().")

    # foo.cpp re-indexed with no reference to old() anymore.
    partial = _partial_index("foo.cpp")
    _add_call(partial.documents[0], "a().", "new().", def_line=2, call_line=6)
    update_store(db, partial)

    store = GraphStore(db)
    # old() is now referenced by nothing and defined nowhere -> gone from `find`.
    assert not store.has_symbol("old().")
    assert store.find("old().") == []


def test_update_keeps_symbol_still_referenced_elsewhere(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "shared().", file="foo.cpp", line=5)
    original.add_edge("calls", "b().", "shared().", file="bar.cpp", line=7)
    write_sqlite(original, db)

    # foo.cpp re-indexed: a() no longer calls shared(). But bar.cpp still does.
    partial = _partial_index("foo.cpp")
    _add_call(partial.documents[0], "a().", "other().", def_line=2, call_line=6)
    update_store(db, partial)

    store = GraphStore(db)
    assert store.has_symbol("shared().")  # kept: bar.cpp still calls it
    assert [e.src for e in store.callers_of("shared().")] == ["b()."]


def test_update_adds_brand_new_file(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "b().", file="foo.cpp", line=5)
    write_sqlite(original, db)

    partial = _partial_index("baz.cpp")
    _add_call(partial.documents[0], "e().", "f().", def_line=2, call_line=6)
    update_store(db, partial)

    store = GraphStore(db)
    assert [e.dst for e in store.callees_of("e().")] == ["f()."]
    assert [e.dst for e in store.callees_of("a().")] == ["b()."]  # untouched


def test_update_removes_deleted_file(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "b().", file="foo.cpp", line=5)
    original.add_edge("calls", "c().", "d().", file="gone.cpp", line=9)
    write_sqlite(original, db)

    # gone.cpp deleted from the tree: no Document in the partial index, passed
    # explicitly as deleted.
    update_store(db, _partial_index(), deleted_files=["gone.cpp"])

    store = GraphStore(db)
    assert not store.has_symbol("c().")
    assert not store.has_symbol("d().")
    assert [e.dst for e in store.callees_of("a().")] == ["b()."]  # foo.cpp untouched


def test_update_clears_stale_edges_when_file_now_empty(tmp_path: Path) -> None:
    """A changed file that no longer produces any edge must still have its old
    edges cleared — the re-indexed Document is present even with no occurrences,
    which is why the changed-file set comes from the index, not the partial graph."""
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "b().", file="foo.cpp", line=5)
    write_sqlite(original, db)

    update_store(db, _partial_index("foo.cpp"))  # foo.cpp now empty

    store = GraphStore(db)
    assert store.callees_of("a().") == []
    assert not store.has_symbol("b().")


def test_update_recomputes_meta_counts_and_provenance(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "b().", file="foo.cpp", line=5)
    write_sqlite(original, db, meta={"source_commit": "oldcommit"})

    partial = _partial_index("foo.cpp")
    _add_call(partial.documents[0], "a().", "b().", def_line=2, call_line=6)
    _add_call(partial.documents[0], "a().", "c().", def_line=2, call_line=7)
    update_store(db, partial, meta={"source_commit": "newcommit"})

    meta = GraphStore(db).meta()
    assert meta["source_commit"] == "newcommit"
    assert meta["edge_count"] == "2"  # a->b, a->c
    # nodes: a, b, c
    assert meta["node_count"] == "3"


def test_implements_edges_are_stored(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("implements", "cxx . . $ mongo/Dog#sound(d1).",
                   "cxx . . $ mongo/Animal#sound(a1).", file="animal.h")
    store = _store(tmp_path, graph)
    # implements edges don't participate in call queries, but must round-trip.
    assert store.has_symbol("cxx . . $ mongo/Dog#sound(d1).")
    assert store.callers_of("cxx . . $ mongo/Animal#sound(a1).") == []
