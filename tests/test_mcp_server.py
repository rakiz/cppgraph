"""Tests for the MCP server's query layer.

The MCP transport (stdio) is thin FastMCP wiring; the substance is the pure
`(store, ...) -> dict` functions that turn GraphStore results into
token-budgeted, JSON-serialisable payloads. We test those directly against a
tiny fixture store — no transport needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cppgraph import mcp_server
from cppgraph.model import Graph, Node
from cppgraph.store import GraphStore, write_sqlite

FOO = "cxx . . $ mongo/Foo#makeResumeToken(a1)."
CALLER = "cxx . . $ mongo/Foo#caller(a2)."
MID = "cxx . . $ mongo/Foo#mid(a3)."


@pytest.fixture
def store(tmp_path: Path) -> GraphStore:
    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    graph.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="foo.cpp", line=9)
    graph.nodes[MID] = Node(symbol=MID, display_name="mid", file="foo.cpp", line=49)
    # caller -> mid -> makeResumeToken
    graph.add_edge("calls", CALLER, MID, file="foo.cpp", line=11)
    graph.add_edge("calls", MID, FOO, file="foo.cpp", line=51)
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_find_returns_budgeted_matches(store: GraphStore) -> None:
    result = mcp_server.find_symbols(store, "makeResumeToken")
    assert result["query"] == "makeResumeToken"
    assert result["total"] == 1
    assert result["results"][0]["symbol"] == FOO
    assert result["results"][0]["line"] == 235  # 0-indexed 234 -> 1-indexed


def test_find_caps_and_flags_truncation(store: GraphStore) -> None:
    # "Foo" matches all three symbols; a limit of 2 should truncate.
    result = mcp_server.find_symbols(store, "Foo", limit=2)
    assert result["total"] == 3
    assert len(result["results"]) == 2
    assert result["truncated"] is True


def test_find_no_match(store: GraphStore) -> None:
    result = mcp_server.find_symbols(store, "nope")
    assert result["total"] == 0
    assert result["results"] == []
    assert result["truncated"] is False


def test_callers_lists_edges(store: GraphStore) -> None:
    result = mcp_server.callers(store, FOO)
    assert result["symbol"] == FOO
    assert result["total"] == 1
    assert result["callers"][0]["symbol"] == MID
    assert result["callers"][0]["line"] == 52  # edge line 51 -> 1-indexed


def test_callees_lists_edges(store: GraphStore) -> None:
    result = mcp_server.callees(store, MID)
    assert result["total"] == 1
    assert result["callees"][0]["symbol"] == FOO


def test_callers_unknown_symbol_is_error(store: GraphStore) -> None:
    result = mcp_server.callers(store, "does::not::exist")
    assert "error" in result
    assert "find" in result["error"]  # points the LLM at the lookup tool


def test_path_reports_chain(store: GraphStore) -> None:
    result = mcp_server.call_path(store, CALLER, FOO)
    assert result["found"] is True
    assert result["hops"] == 2
    assert result["path"][0]["symbol"] == CALLER
    assert result["path"][-1]["symbol"] == FOO


def test_path_none_when_no_chain(store: GraphStore) -> None:
    result = mcp_server.call_path(store, FOO, CALLER)
    assert result["found"] is False
    assert result.get("path") in (None, [])


def test_impact_transitive_callers(store: GraphStore) -> None:
    result = mcp_server.impact(store, FOO)
    syms = {r["symbol"] for r in result["reached_by"]}
    assert syms == {CALLER, MID}
    assert result["total"] == 2
    assert result["kind"] == "calls"


def test_impact_depth_bounds_walk(store: GraphStore) -> None:
    result = mcp_server.impact(store, FOO, depth=1)
    syms = {r["symbol"] for r in result["reached_by"]}
    assert syms == {MID}  # only the direct caller at depth 1


BASE = "cxx . . $ mongo/Base#"
DERIVED = "cxx . . $ mongo/Derived#"
LEAF = "cxx . . $ mongo/Leaf#"


@pytest.fixture
def hierarchy(tmp_path: Path) -> GraphStore:
    graph = Graph()
    graph.add_edge("inherits", DERIVED, BASE, file="d.h", line=2)
    graph.add_edge("inherits", LEAF, DERIVED, file="l.h", line=3)
    path = tmp_path / "h.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_bases_lists_direct_supertypes(hierarchy: GraphStore) -> None:
    result = mcp_server.bases(hierarchy, DERIVED)
    assert [b["symbol"] for b in result["bases"]] == [BASE]


def test_subtypes_lists_direct_subclasses(hierarchy: GraphStore) -> None:
    result = mcp_server.subtypes(hierarchy, BASE)
    assert [s["symbol"] for s in result["subtypes"]] == [DERIVED]


def test_bases_unknown_symbol_is_error(hierarchy: GraphStore) -> None:
    assert "error" in mcp_server.bases(hierarchy, "nope")


def test_impact_over_inherits_gives_all_descendants(hierarchy: GraphStore) -> None:
    result = mcp_server.impact(hierarchy, BASE, kind="inherits")
    assert {r["symbol"] for r in result["reached_by"]} == {DERIVED, LEAF}
    assert result["kind"] == "inherits"


def test_explain_coordinates_only_by_default(store: GraphStore) -> None:
    result = mcp_server.explain(store, FOO)
    assert result["symbol"] == FOO
    assert result["name"] == "makeResumeToken"
    assert result["defined_at"]["file"] == "foo.cpp"
    assert result["defined_at"]["line"] == 235
    assert "source" not in result  # no snippet unless include_source
    assert result["callers"]["total"] == 1


def test_explain_includes_source_when_requested(store: GraphStore, tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "foo.cpp").write_text("\n".join(f"line {i}" for i in range(300)))
    result = mcp_server.explain(store, FOO, root=str(root), include_source=True, context=1)
    assert "source" in result
    lines = {entry["line"] for entry in result["source"]}
    assert 235 in lines


def test_explain_source_requested_but_missing_is_graceful(store: GraphStore, tmp_path: Path) -> None:
    result = mcp_server.explain(store, FOO, root=str(tmp_path), include_source=True)
    assert result["source"] is None  # requested, file absent -> explicit None, no crash


def test_explain_unknown_symbol_is_error(store: GraphStore) -> None:
    result = mcp_server.explain(store, "does::not::exist")
    assert "error" in result


def test_explain_limit_is_overridable(store: GraphStore) -> None:
    # FOO has one caller (mid); force a limit of 0 to prove the cap is honored
    # and truncation flagged, so an LLM can raise it back when it needs more.
    result = mcp_server.explain(store, FOO, limit=0)
    assert result["callers"]["items"] == []
    assert result["callers"]["truncated"] is True
    assert result["callers"]["total"] == 1


def _init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root, check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_status_reports_commit_without_root(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node(FOO, display_name="makeResumeToken")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta={"source_commit": "abc123", "project_root": "file:///x"})
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    assert result["source_commit"] == "abc123"
    assert result["drift"]["checked"] is False


def test_status_up_to_date_with_root(tmp_path: Path) -> None:
    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("int main(){}\n")
    commit = _init_repo(root)
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta={"source_commit": commit})
    with GraphStore(path) as st:
        result = mcp_server.status_report(st, root=str(root))
    assert result["drift"]["checked"] is True
    assert result["drift"]["up_to_date"] is True


def test_status_detects_stale(tmp_path: Path) -> None:
    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("int main(){}\n")
    commit = _init_repo(root)
    (root / "a.cpp").write_text("int main(){return 1;}\n")  # drift
    (root / "notes.md").write_text("ignored\n")  # non-source, must not count
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta={"source_commit": commit})
    with GraphStore(path) as st:
        result = mcp_server.status_report(st, root=str(root))
    assert result["drift"]["up_to_date"] is False
    assert "a.cpp" in result["drift"]["changed"]
    assert "notes.md" not in result["drift"]["changed"]
