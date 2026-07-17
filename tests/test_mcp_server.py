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


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `status_report` offline-deterministic: never hit the version registry
    unless a test opts in. (The update logic itself is tested in test_updates.py.)"""
    monkeypatch.setenv("CPPGRAPH_NO_UPDATE_CHECK", "1")


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
    # compact by default: human name, not the SCIP string, and no `symbol` key
    assert result["callers"][0]["name"] == "mid"
    assert "symbol" not in result["callers"][0]
    assert result["callers"][0]["line"] == 52  # edge line 51 -> 1-indexed


def test_callers_full_symbols_restores_scip(store: GraphStore) -> None:
    result = mcp_server.callers(store, FOO, full_symbols=True)
    assert result["callers"][0]["symbol"] == MID
    assert result["callers"][0]["name"] == "mid"


def test_callees_lists_edges(store: GraphStore) -> None:
    result = mcp_server.callees(store, MID)
    assert result["total"] == 1
    assert result["callees"][0]["name"] == "makeResumeToken"


def test_callers_exclude_tests_by_default(tmp_path: Path) -> None:
    # a production caller and a test caller of FOO; the test one is dropped by
    # default and comes back only with exclude_tests=False.
    prod = "cxx . . $ mongo/Foo#prodCaller(a4)."
    testc = "cxx . . $ mongo/FooTest#SomeCase_Test#~SomeCase_Test(a5)."
    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    graph.nodes[prod] = Node(symbol=prod, display_name="prodCaller", file="foo.cpp", line=9)
    graph.nodes[testc] = Node(
        symbol=testc, display_name="~SomeCase_Test", file="foo_test.cpp", line=3
    )
    graph.add_edge("calls", prod, FOO, file="foo.cpp", line=11)
    graph.add_edge("calls", testc, FOO, file="foo_test.cpp", line=5)
    path = tmp_path / "t.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    default = mcp_server.callers(st, FOO)
    assert default["excluded_tests"] is True
    assert {c["name"] for c in default["callers"]} == {"prodCaller"}

    including = mcp_server.callers(st, FOO, exclude_tests=False)
    assert {c["name"] for c in including["callers"]} == {"prodCaller", "~SomeCase_Test"}


def test_short_label_strips_scip_noise() -> None:
    # scheme prefix, anonymous-namespace file path, overload hash, back-ticks
    raw = (
        "cxx . . $ mongo/`$anonymous_namespace_src/mongo/db/pipeline/foo_test.cpp`"
        "/SomeCase_Test#`~SomeCase_Test`(49f6e7a06ebc5aa8)."
    )
    assert mcp_server._short_label(raw) == "mongo/SomeCase_Test#~SomeCase_Test."
    plain = "cxx . . $ mongo/PlanExecutorPipeline#_initializeResumableScanState(49f6e7a06ebc5aa8)."
    assert (
        mcp_server._short_label(plain)
        == "mongo/PlanExecutorPipeline#_initializeResumableScanState."
    )


def test_callers_derives_label_without_display_name(tmp_path: Path) -> None:
    # callers with no indexed display_name still get a readable name (not the
    # raw SCIP string), and the SCIP string is gone from the compact payload.
    callee = "cxx . . $ mongo/Foo#target(a1)."
    caller = "cxx . . $ mongo/Bar#doWork(deadbeef1234)."
    graph = Graph()
    graph.nodes[callee] = Node(symbol=callee, file="foo.cpp", line=1)
    graph.nodes[caller] = Node(symbol=caller, file="bar.cpp", line=1)
    graph.add_edge("calls", caller, callee, file="bar.cpp", line=9)
    path = tmp_path / "d.db"
    write_sqlite(graph, path)
    r = mcp_server.callers(GraphStore(path), callee)
    item = r["callers"][0]
    assert item["name"] == "mongo/Bar#doWork."
    assert "symbol" not in item


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
    names = {r["name"] for r in result["reached_by"]}
    assert names == {"caller", "mid"}
    assert result["total"] == 2
    assert result["kind"] == "calls"


def test_impact_depth_bounds_walk(store: GraphStore) -> None:
    result = mcp_server.impact(store, FOO, depth=1)
    names = {r["name"] for r in result["reached_by"]}
    assert names == {"mid"}  # only the direct caller at depth 1


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
    result = mcp_server.bases(hierarchy, DERIVED, full_symbols=True)
    assert [b["symbol"] for b in result["bases"]] == [BASE]
    # no indexed display name -> readable label derived from the SCIP string
    assert result["bases"][0]["name"] == "mongo/Base#"


def test_subtypes_lists_direct_subclasses(hierarchy: GraphStore) -> None:
    result = mcp_server.subtypes(hierarchy, BASE, full_symbols=True)
    assert [s["symbol"] for s in result["subtypes"]] == [DERIVED]


def test_bases_unknown_symbol_is_error(hierarchy: GraphStore) -> None:
    assert "error" in mcp_server.bases(hierarchy, "nope")


TYPE = "cxx . . $ mongo/ResumeTokenData#"


@pytest.fixture
def refs_store(tmp_path: Path) -> GraphStore:
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 10)
    graph.add_reference(TYPE, "b.cpp", 41)
    path = tmp_path / "r.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_references_lists_use_sites(refs_store: GraphStore) -> None:
    result = mcp_server.references(refs_store, TYPE)
    assert result["available"] is True
    assert result["total"] == 2
    assert {(u["file"], u["line"]) for u in result["uses"]} == {("a.cpp", 11), ("b.cpp", 42)}
    assert all("source" not in u for u in result["uses"])  # coordinates only


def test_references_with_root_reads_snippets(refs_store: GraphStore, tmp_path: Path) -> None:
    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("\n".join(f"line {i}" for i in range(50)))
    (root / "b.cpp").write_text("\n".join(f"line {i}" for i in range(50)))
    result = mcp_server.references(refs_store, TYPE, root=str(root), include_source=True, context=0)
    a = next(u for u in result["uses"] if u["file"] == "a.cpp")
    assert a["lines"] == [11]
    assert a["source"] == [{"line": 11, "text": "line 10", "is_use": True}]


def test_references_merges_overlapping_windows(tmp_path: Path) -> None:
    # two hits 3 lines apart in the same file, context=2 -> windows overlap; the
    # shared lines must be sent once, not duplicated per hit.
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 10)
    graph.add_reference(TYPE, "a.cpp", 12)
    path = tmp_path / "m.db"
    write_sqlite(graph, path)
    store = GraphStore(path)
    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("\n".join(f"line {i}" for i in range(50)))
    result = mcp_server.references(store, TYPE, root=str(root), include_source=True, context=2)
    (a,) = result["uses"]  # one grouped entry for the file
    assert a["lines"] == [11, 13]
    nums = [ln["line"] for ln in a["source"]]
    assert nums == [9, 10, 11, 12, 13, 14, 15]  # merged, no repeats
    assert nums == sorted(set(nums))
    assert [ln["line"] for ln in a["source"] if ln["is_use"]] == [11, 13]


def test_references_unavailable_when_not_built(store: GraphStore) -> None:
    # the `store` fixture has no reference index
    result = mcp_server.references(store, FOO)
    assert result["available"] is False


def test_references_unknown_symbol_is_error(refs_store: GraphStore) -> None:
    assert "error" in mcp_server.references(refs_store, "nope")


def test_impact_over_inherits_gives_all_descendants(hierarchy: GraphStore) -> None:
    result = mcp_server.impact(hierarchy, BASE, kind="inherits", full_symbols=True)
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


def test_explain_source_requested_but_missing_is_graceful(
    store: GraphStore, tmp_path: Path
) -> None:
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
        cwd=root,
        check=True,
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


def test_make_export_deps_returns_subgraph(store: GraphStore) -> None:
    g = mcp_server.make_export(store, FOO, mode="deps", depth=1, direction="in")
    ids = {n["id"] for n in g["nodes"]}
    assert FOO in ids and MID in ids  # depth-1 in-neighbour
    assert any(lk["relation"] == "calls" for lk in g["links"])


def test_make_export_usage_returns_file_graph(tmp_path: Path) -> None:
    sym = "cxx . . $ mongo/ResumeTokenData#"
    graph = Graph()
    graph.nodes[sym] = Node(symbol=sym, display_name="ResumeTokenData")
    graph.add_reference(sym, "a/foo.cpp", 1)
    graph.add_reference(sym, "b/bar.h", 3)
    path = tmp_path / "refs.db"
    write_sqlite(graph, path)
    g = mcp_server.make_export(GraphStore(path), sym, mode="usage")
    assert {lk["target"] for lk in g["links"]} == {"file:a/foo.cpp", "file:b/bar.h"}


def test_make_export_unknown_symbol_is_none(store: GraphStore) -> None:
    assert mcp_server.make_export(store, "nope") is None


def test_discover_graph_finds_nearest_cppgraph(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".cppgraph").mkdir(parents=True)
    (proj / ".cppgraph" / "proj.graph.db").write_bytes(b"x")
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    found = mcp_server.discover_graph(sub)  # from a nested dir
    assert found is not None
    graph, root = found
    assert graph.name == "proj.graph.db"
    assert root == proj.resolve()


def test_discover_graph_picks_newest(tmp_path: Path) -> None:
    import os
    import time

    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    old = cpg / "old.graph.db"
    old.write_bytes(b"o")
    time.sleep(0.01)
    new = cpg / "new.graph.db"
    new.write_bytes(b"n")
    os.utime(old, (1, 1))  # force old to be older
    graph, _ = mcp_server.discover_graph(tmp_path)
    assert graph.name == "new.graph.db"


def test_discover_graph_none_when_absent(tmp_path: Path) -> None:
    assert mcp_server.discover_graph(tmp_path) is None


# --- 0.1.0 query-quality items -------------------------------------------


def test_find_multi_term_and(store: GraphStore) -> None:
    # "Foo makeResumeToken" — both tokens present only in FOO.
    result = mcp_server.find_symbols(store, "Foo makeResumeToken")
    assert result["total"] == 1
    assert result["results"][0]["symbol"] == FOO


def test_find_groups_overloads(tmp_path: Path) -> None:
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    graph.nodes[p1] = Node(symbol=p1, display_name="parse", file="rt.h", line=1)
    graph.nodes[p2] = Node(symbol=p2, display_name="parse", file="rt.cpp", line=2)
    path = tmp_path / "ov.db"
    write_sqlite(graph, path)
    result = mcp_server.find_symbols(GraphStore(path), "parse")
    assert result["total"] == 2  # raw matches
    assert result["groups"] == 1  # one qualified name
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["overloads"] == 2
    assert {s["symbol"] for s in entry["signatures"]} == {p1, p2}


def test_impact_on_type_redirects_to_references(tmp_path: Path) -> None:
    ty = "cxx . . $ mongo/ResumeTokenData#"
    graph = Graph()
    graph.nodes[ty] = Node(symbol=ty, display_name="ResumeTokenData", file="rt.h", line=1)
    graph.add_reference(ty, "a.cpp", 5)
    graph.add_reference(ty, "b.cpp", 9)
    path = tmp_path / "ty.db"
    write_sqlite(graph, path)
    result = mcp_server.impact(GraphStore(path), ty)
    assert result["is_type"] is True
    assert result["total"] == 0
    assert result["reference_sites"] == 2
    assert "find_references" in result["notice"]


def test_what_it_calls_hide_trivial(tmp_path: Path) -> None:
    caller = "cxx . . $ mongo/Foo#run(a1)."
    real = "cxx . . $ mongo/Foo#doDomainWork(a2)."
    op = "cxx . . $ mongo/Foo#operator==(a3)."
    tassert = "cxx . . $ mongo/tassert(a4)."
    graph = Graph()
    for s in (caller, real, op, tassert):
        graph.nodes[s] = Node(symbol=s, file="foo.cpp", line=1)
    graph.add_edge("calls", caller, real, file="foo.cpp", line=2)
    graph.add_edge("calls", caller, op, file="foo.cpp", line=3)
    graph.add_edge("calls", caller, tassert, file="foo.cpp", line=4)
    path = tmp_path / "tr.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    full = mcp_server.callees(st, caller)
    assert full["total"] == 3

    filtered = mcp_server.callees(st, caller, hide_trivial=True)
    assert filtered["total"] == 1
    assert filtered["trivial_hidden"] == 2
    assert "doDomainWork" in filtered["callees"][0]["name"]


def test_path_not_found_carries_dispatch_hint(store: GraphStore) -> None:
    result = mcp_server.call_path(store, FOO, CALLER)
    assert result["found"] is False
    assert "hint" in result
    assert "dispatch" in result["hint"] or "factory" in result["hint"]


def test_find_hide_trivial_drops_generated(tmp_path: Path) -> None:
    real = "cxx . . $ mongo/DocumentSourceChangeStream#buildPipeline(a1)."
    lam = "cxx . . $ mongo/`$anonymous_type_7`#operator()(a2)."
    op = "cxx . . $ mongo/Pipeline#operator==(a3)."
    graph = Graph()
    for s in (real, lam, op):
        graph.nodes[s] = Node(symbol=s, file="cs.cpp", line=1)
    path = tmp_path / "noise.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    full = mcp_server.find_symbols(st, "mongo")
    assert full["total"] == 3
    assert "trivial_hidden" not in full  # lossless by default

    filtered = mcp_server.find_symbols(st, "mongo", hide_trivial=True)
    assert filtered["total"] == 1
    assert filtered["trivial_hidden"] == 2
    assert filtered["results"][0]["symbol"] == real


def test_find_relaxes_qualified_zero_hit(tmp_path: Path) -> None:
    # A free function; a guess qualifying it under a class finds nothing exactly,
    # so find retries on the leaf name and flags the result relaxed.
    free = "cxx . . $ mongo/change_stream/pipeline_helpers/buildPipeline(a1)."
    graph = Graph()
    graph.nodes[free] = Node(symbol=free, file="ph.cpp", line=1)
    path = tmp_path / "relax.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    r = mcp_server.find_symbols(st, "DocumentSourceChangeStream#buildPipeline")
    assert r["total"] == 1
    assert r["relaxed"] is True
    assert r["relaxed_query"] == "buildPipeline"
    assert r["results"][0]["symbol"] == free


def test_find_no_relax_when_exact_hits(store: GraphStore) -> None:
    # An exact hit must not trigger the relaxed retry.
    r = mcp_server.find_symbols(store, "Foo#makeResumeToken")
    assert r["total"] >= 1
    assert "relaxed" not in r


def test_find_no_relax_for_bare_leaf(tmp_path: Path) -> None:
    # A bare name with no qualifier separator and no hits stays a plain 0.
    graph = Graph()
    graph.nodes["cxx . . $ mongo/Foo#bar(a1)."] = Node(
        symbol="cxx . . $ mongo/Foo#bar(a1).", file="f.cpp", line=1
    )
    path = tmp_path / "bare.db"
    write_sqlite(graph, path)
    r = mcp_server.find_symbols(GraphStore(path), "nonexistent")
    assert r["total"] == 0
    assert "relaxed" not in r
