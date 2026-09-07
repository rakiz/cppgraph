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


def test_reloading_store_reopens_when_file_changes(tmp_path: Path) -> None:
    """The long-lived server must not keep answering from the graph held open at
    launch after a reindex overwrites it on disk."""
    import os
    import time

    path = tmp_path / "graph.db"
    g1 = Graph()
    g1.nodes[FOO] = Node(symbol=FOO, display_name="foo", file="a.cpp", line=1)
    write_sqlite(g1, path)

    rs = mcp_server._ReloadingStore(path)
    assert rs.get().has_symbol(FOO)
    assert not rs.get().has_symbol(CALLER)

    # Overwrite with a different graph; force a newer mtime (same-second writes
    # might not advance it).
    g2 = Graph()
    g2.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="b.cpp", line=2)
    write_sqlite(g2, path)
    future = time.time() + 5
    os.utime(path, (future, future))

    assert rs.get().has_symbol(CALLER)  # reloaded


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


def test_callers_exclude_paths_drops_vendored_caller(tmp_path: Path) -> None:
    prod = "cxx . . $ mongo/Foo#prodCaller(a4)."
    vendored = "cxx . . $ mongo/Foo#vendorCaller(a5)."
    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    graph.nodes[prod] = Node(
        symbol=prod, display_name="prodCaller", file="src/myproject/foo.cpp", line=9
    )
    graph.nodes[vendored] = Node(
        symbol=vendored, display_name="vendorCaller", file="vendor/somelib/foo.cpp", line=3
    )
    graph.add_edge("calls", prod, FOO, file="foo.cpp", line=11)
    graph.add_edge("calls", vendored, FOO, file="foo.cpp", line=5)
    path = tmp_path / "t.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    result = mcp_server.callers(st, FOO, exclude_paths=["vendor/"])
    assert {c["name"] for c in result["callers"]} == {"prodCaller"}
    assert result["exclude_paths"] == ["vendor/"]

    result = mcp_server.callers(st, FOO, include_paths=["src/myproject/"])
    assert {c["name"] for c in result["callers"]} == {"prodCaller"}
    assert result["include_paths"] == ["src/myproject/"]


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


def test_hotspot_ranking_fan_in_orders_by_incoming_calls(store: GraphStore) -> None:
    # fixture: caller -> mid -> makeResumeToken, so FOO and MID each have
    # exactly one caller (fan_in == 1); this proves the MCP wrapper is driven
    # by the same GraphStore.hotspots used by the CLI (parity, not reimplementation).
    result = mcp_server.hotspot_ranking(store, kind="fan_in")
    assert result["kind"] == "fan_in"
    assert result["total"] == 2
    names = {h["name"] for h in result["hotspots"]}
    assert names == {"makeResumeToken", "mid"}
    counts = {h["name"]: h["count"] for h in result["hotspots"]}
    assert counts["makeResumeToken"] == 1
    assert counts["mid"] == 1


def test_hotspot_ranking_limit_truncates(store: GraphStore) -> None:
    result = mcp_server.hotspot_ranking(store, kind="fan_in", limit=1)
    assert result["total"] == 2
    assert len(result["hotspots"]) == 1
    assert result["truncated"] is True


def test_hotspot_ranking_matches_store_hotspots_directly(store: GraphStore) -> None:
    """Parity check: the MCP tool's ranking is exactly `store.hotspots`, not a
    reimplementation — same symbols in the same order, not just same counts."""
    result = mcp_server.hotspot_ranking(store, kind="fan_out", full_symbols=True)
    expected_ranked, expected_total = store.hotspots(kind="fan_out")
    assert result["total"] == expected_total
    assert [(h["symbol"], h["count"]) for h in result["hotspots"]] == expected_ranked


def test_hotspot_ranking_exclude_paths_matches_store_hotspots_directly(tmp_path: Path) -> None:
    """Parity check for the path-prefix filter, same shape as the exclude_tests
    parity test above."""
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "target", file="src/myproject/foo.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "target", file="src/myproject/foo.cpp", line=2)
    graph.nodes["proj_caller"].file = "src/myproject/foo.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/foo.cpp"
    graph.nodes["target"].file = "src/myproject/foo.cpp"
    path = tmp_path / "hs.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    result = mcp_server.hotspot_ranking(
        st, kind="fan_in", full_symbols=True, exclude_paths=["vendor/"]
    )
    expected_ranked, expected_total = st.hotspots(kind="fan_in", exclude_paths=["vendor/"])
    assert result["total"] == expected_total
    assert [(h["symbol"], h["count"]) for h in result["hotspots"]] == expected_ranked
    assert expected_ranked == [("target", 1)]


def _dependency_cost_graph() -> tuple[Path, Graph]:
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "lib_hot", file="src/myproject/foo.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "lib_hot", file="src/myproject/foo.cpp", line=2)
    graph.add_edge("calls", "vendor_caller", "lib_warm", file="src/myproject/foo.cpp", line=3)
    graph.add_edge("calls", "proj_caller", "proj_helper", file="src/myproject/foo.cpp", line=4)
    graph.nodes["proj_caller"].file = "src/myproject/foo.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/foo.cpp"
    graph.nodes["lib_hot"].file = "spirv_cross/hot.cpp"
    graph.nodes["lib_warm"].file = "spirv_cross/warm.cpp"
    graph.nodes["proj_helper"].file = "src/myproject/foo.cpp"
    return graph


def _dependency_cost_store(tmp_path: Path, graph: Graph) -> GraphStore:
    path = tmp_path / "dep_cost.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_dependency_cost_report_counts_call_sites_into_target(tmp_path: Path) -> None:
    """The report's headline number is the SUM over all target symbols —
    "if I replace this library, how many call sites change?" — with the
    per-symbol breakdown as detail. Vendored callers count by default (the
    asymmetric filter restricts the callee side only)."""
    st = _dependency_cost_store(tmp_path, _dependency_cost_graph())
    result = mcp_server.dependency_cost_report(st, target_paths=["spirv_cross/"])
    assert result["target_paths"] == ["spirv_cross/"]
    assert result["total_call_sites"] == 3  # 2 into lib_hot + 1 into lib_warm
    assert result["distinct_target_symbols"] == 2
    assert result["truncated"] is False
    counts = {t["name"]: t["count"] for t in result["top_targets"]}
    assert counts == {"lib_hot": 2, "lib_warm": 1}
    assert all("proj_helper" != t["name"] for t in result["top_targets"])


def test_dependency_cost_report_limit_truncates_but_totals_are_full(tmp_path: Path) -> None:
    st = _dependency_cost_store(tmp_path, _dependency_cost_graph())
    result = mcp_server.dependency_cost_report(st, target_paths=["spirv_cross/"], limit=1)
    assert len(result["top_targets"]) == 1
    assert result["truncated"] is True
    # The aggregate answers stay full — never implied by the capped list.
    assert result["total_call_sites"] == 3
    assert result["distinct_target_symbols"] == 2


def test_dependency_cost_report_exclude_paths_filters_caller_side_only(tmp_path: Path) -> None:
    st = _dependency_cost_store(tmp_path, _dependency_cost_graph())
    result = mcp_server.dependency_cost_report(
        st, target_paths=["spirv_cross/"], exclude_paths=["vendor/"]
    )
    assert result["total_call_sites"] == 1  # only proj_caller's edge remains
    assert result["exclude_paths"] == ["vendor/"]


def test_dependency_cost_report_matches_store_hotspots_directly(tmp_path: Path) -> None:
    """Parity check, same shape as the hotspots ones: the MCP report's
    breakdown is exactly `store.hotspots(kind='fan_in', target_paths=…,
    limit=None)`, not a reimplementation."""
    st = _dependency_cost_store(tmp_path, _dependency_cost_graph())
    result = mcp_server.dependency_cost_report(st, target_paths=["spirv_cross/"], full_symbols=True)
    expected_ranked, expected_total = st.hotspots(
        limit=None, kind="fan_in", target_paths=["spirv_cross/"]
    )
    assert result["distinct_target_symbols"] == expected_total
    assert [(t["symbol"], t["count"]) for t in result["top_targets"]] == expected_ranked
    assert result["total_call_sites"] == sum(n for _, n in expected_ranked)


def test_dependency_cost_report_requires_target_paths(tmp_path: Path) -> None:
    """An empty target list would silently read as the whole graph's fan-in —
    reject it explicitly rather than report a number for a different question."""
    st = _dependency_cost_store(tmp_path, _dependency_cost_graph())
    with pytest.raises(ValueError):
        mcp_server.dependency_cost_report(st, target_paths=[])


def test_stats_summary_groups_per_file(store: GraphStore) -> None:
    # fixture: every definition and call site lives in foo.cpp — 3 symbols,
    # 2 call edges, no refs. Parity with the CLI via the same GraphStore.stats.
    result = mcp_server.stats_summary(store, group_by="file")
    assert result["group_by"] == "file"
    assert result["total"] == 1
    assert result["truncated"] is False
    assert result["stats"] == [{"file": "foo.cpp", "symbols": 3, "edges": 2, "refs": 0}]


def test_stats_summary_dir_rollup(store: GraphStore) -> None:
    result = mcp_server.stats_summary(store, group_by="dir")
    assert result["group_by"] == "dir"
    assert result["stats"] == [{"dir": ".", "symbols": 3, "edges": 2, "refs": 0}]


def test_stats_summary_limit_truncates(store: GraphStore) -> None:
    result = mcp_server.stats_summary(store, group_by="file", limit=0)
    assert result["total"] == 1
    assert result["stats"] == []
    assert result["truncated"] is True


def test_stats_summary_matches_store_stats_directly(tmp_path: Path) -> None:
    """Parity check, same shape as the hotspots ones: the MCP wrapper is exactly
    `store.stats`, not a reimplementation — same groups in the same order."""
    graph = Graph()
    graph.nodes["a"] = Node(symbol="a", file="src/a.cpp", line=1)
    graph.nodes["b"] = Node(symbol="b", file="vendor/b.cpp", line=1)
    graph.add_edge("calls", "a", "b", file="src/a.cpp", line=2)
    graph.add_reference("b", "src/a.cpp", 3)
    path = tmp_path / "stats.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    result = mcp_server.stats_summary(st, group_by="dir", exclude_paths=["vendor/"])
    expected_groups, expected_total = st.stats(group_by="dir", exclude_paths=["vendor/"])
    assert result["total"] == expected_total
    assert result["stats"] == expected_groups
    assert expected_groups == [{"dir": "src", "symbols": 1, "edges": 1, "refs": 1}]


def test_stats_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to `stats_summary` (the
    pure functions above are covered directly; this covers the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    stats_tool = server._tool_manager._tools["stats"].fn
    assert stats_tool(group_by="dir")["stats"] == [
        {"dir": ".", "symbols": 1, "edges": 0, "refs": 0}
    ]


BIG = "cxx . . $ app/big(b1)."
MIDFN = "cxx . . $ app/mid(m1)."
TINY = "cxx . . $ app/tiny(t1)."
NEVER = "cxx . . $ app/never_called(n1)."
WIDGET = "cxx . . $ app/Widget#"


@pytest.fixture
def spans_store(tmp_path: Path) -> GraphStore:
    """A #504-shaped store: definitions carry body extents, one real call edge
    (big -> mid), plus a never-called callable and a type."""
    graph = Graph()
    graph.nodes[BIG] = Node(
        symbol=BIG, display_name="big", file="src/app.cpp", line=10, end_line=110
    )
    graph.nodes[MIDFN] = Node(
        symbol=MIDFN, display_name="mid", file="src/app.cpp", line=200, end_line=250
    )
    graph.nodes[TINY] = Node(
        symbol=TINY, display_name="tiny", file="src/app.cpp", line=300, end_line=302
    )
    graph.nodes[NEVER] = Node(
        symbol=NEVER, display_name="never_called", file="src/lib.cpp", line=5, end_line=8
    )
    graph.nodes[WIDGET] = Node(symbol=WIDGET, display_name="Widget", file="src/lib.cpp", line=1)
    graph.add_edge("calls", BIG, MIDFN, file="src/app.cpp", line=15)
    path = tmp_path / "spans.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_line_span_ranking_orders_by_span_descending(spans_store: GraphStore) -> None:
    result = mcp_server.line_span_ranking(spans_store)
    assert result["total"] == 4
    assert [(d["name"], d["span"]) for d in result["definitions"]] == [
        ("big", 100),
        ("mid", 50),
        ("never_called", 3),
        ("tiny", 2),
    ]
    assert result["definitions"][0]["file"] == "src/app.cpp"
    assert result["definitions"][0]["line"] == 11  # 0-indexed 10 -> 1-indexed


def test_line_span_ranking_limit_truncates(spans_store: GraphStore) -> None:
    result = mcp_server.line_span_ranking(spans_store, limit=1)
    assert result["total"] == 4
    assert len(result["definitions"]) == 1
    assert result["truncated"] is True


def test_line_span_ranking_unavailable_reports_reason(store: GraphStore) -> None:
    """The default fixture is a stock-binary graph (no body extents): the tool
    says so explicitly — the `available: false` convention `references` uses —
    rather than a silently empty list."""
    result = mcp_server.line_span_ranking(store)
    assert result["available"] is False
    assert "#504" in result["reason"]


def test_line_span_ranking_matches_store_line_span_directly(spans_store: GraphStore) -> None:
    """Parity check: the MCP tool's ranking is exactly `store.line_span`, not a
    reimplementation."""
    result = mcp_server.line_span_ranking(spans_store, full_symbols=True)
    expected_ranked, expected_total = spans_store.line_span()
    assert result["total"] == expected_total
    assert [(d["symbol"], d["span"]) for d in result["definitions"]] == expected_ranked


def test_line_span_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    from cppgraph.mcp_server import build_server

    path = tmp_path / "g.db"
    write_sqlite(Graph(), path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["line_span"].fn
    assert tool()["available"] is False  # no enclosing-range data in this store


def test_no_incoming_calls_report_lists_zero_caller_callables(
    spans_store: GraphStore,
) -> None:
    result = mcp_server.no_incoming_calls_report(spans_store)
    assert result["total"] == 3
    assert [d["name"] for d in result["definitions"]] == ["big", "tiny", "never_called"]


def test_no_incoming_calls_report_states_fact_not_verdict(
    spans_store: GraphStore,
) -> None:
    """Every response carries the caveat: 0 static callers is a fact, not proof
    of dead code (vtable dispatch / exported API / templates / entry points)."""
    result = mcp_server.no_incoming_calls_report(spans_store)
    assert "not proof of dead code" in result["note"]
    assert "vtable" in result["note"]


def test_no_incoming_calls_report_limit_truncates(spans_store: GraphStore) -> None:
    result = mcp_server.no_incoming_calls_report(spans_store, limit=1)
    assert result["total"] == 3
    assert result["truncated"] is True


def test_no_incoming_calls_report_unavailable_reports_reason(store: GraphStore) -> None:
    """Refuses on a stock-binary graph, explaining why: a phantom caller from a
    mis-attributed declaration site can turn a real 0 into a false 1."""
    result = mcp_server.no_incoming_calls_report(store)
    assert result["available"] is False
    assert "phantom caller" in result["reason"]
    assert "#504" in result["reason"]


def test_no_incoming_calls_report_matches_store_directly(spans_store: GraphStore) -> None:
    result = mcp_server.no_incoming_calls_report(spans_store, full_symbols=True)
    expected, expected_total = spans_store.no_incoming_calls()
    assert result["total"] == expected_total
    assert [d["symbol"] for d in result["definitions"]] == expected


def test_no_incoming_calls_tool_registered_and_routes_through_call(
    tmp_path: Path,
) -> None:
    from cppgraph.mcp_server import build_server

    path = tmp_path / "g.db"
    write_sqlite(Graph(), path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["no_incoming_calls"].fn
    assert tool()["available"] is False  # stock-binary store: refused, not guessed


# --- boundary_violations -----------------------------------------------------


@pytest.fixture
def boundary_store(tmp_path: Path) -> GraphStore:
    """One legal downward edge (`platform/` may call `common/`) and one that
    crosses the declared rule (`common/` must not call `platform/`)."""
    graph = Graph()
    graph.add_edge("calls", "platform_fn", "common_fn", file="platform/io.cpp", line=1)
    graph.add_edge("calls", "common_fn", "platform_secret", file="common/util.cpp", line=9)
    graph.nodes["common_fn"].file = "common/util.cpp"
    graph.nodes["platform_fn"].file = "platform/io.cpp"
    graph.nodes["platform_secret"].file = "platform/hidden.cpp"
    path = tmp_path / "boundary.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_boundary_violation_report_lists_violations_with_rule(
    boundary_store: GraphStore,
) -> None:
    result = mcp_server.boundary_violation_report(boundary_store, [["common/", "platform/"]])
    assert result["total"] == 1
    assert result["truncated"] is False
    v = result["violations"][0]
    assert v["rule"] == "common/ -> platform/"
    assert v["kind"] == "calls"
    assert v["src"] == "common_fn"
    assert v["dst"] == "platform_secret"
    assert v["file"] == "common/util.cpp"
    assert v["line"] == 10  # 0-indexed 9 -> 1-indexed


def test_boundary_violation_report_zero_is_a_lower_bound_not_conformance(
    boundary_store: GraphStore,
) -> None:
    """The standing note frames both directions: each hit is a real edge (zero
    false positives), and 0 hits means no *statically indexed* edge crosses —
    never a proof the layering holds (facts, not judgments)."""
    result = mcp_server.boundary_violation_report(boundary_store, [["platform/", "projects/"]])
    assert result["total"] == 0
    assert result["violations"] == []
    assert "zero false positives" in result["note"]
    assert "statically indexed" in result["note"]


def test_boundary_violation_report_limit_truncates(boundary_store: GraphStore) -> None:
    result = mcp_server.boundary_violation_report(
        boundary_store, [["common/", "platform/"]], limit=0
    )
    assert result["total"] == 1
    assert result["violations"] == []
    assert result["truncated"] is True


def test_boundary_violation_report_edge_kinds_restrict(boundary_store: GraphStore) -> None:
    result = mcp_server.boundary_violation_report(
        boundary_store, [["common/", "platform/"]], edge_kinds=["inherits"]
    )
    assert result["total"] == 0  # the violating edge is a call, not an inheritance


def test_boundary_violation_report_matches_store_directly(boundary_store: GraphStore) -> None:
    """Parity check, same shape as the hotspots/stats ones: the MCP report is
    exactly `store.boundary_violations`, not a reimplementation (line is
    1-indexed here, 0-indexed in the store, like every other tool)."""
    result = mcp_server.boundary_violation_report(
        boundary_store, [["common/", "platform/"]], full_symbols=True
    )
    expected, expected_total = boundary_store.boundary_violations([("common/", "platform/")])
    assert result["total"] == expected_total
    assert [
        (v["kind"], v["src"], v["dst"], v["file"], v["rule"]) for v in result["violations"]
    ] == [(e["kind"], e["src"], e["dst"], e["file"], e["rule"]) for e in expected]
    assert result["violations"][0]["line"] == expected[0]["line"] + 1


def test_boundary_violation_report_invalid_rules_are_error_dicts(
    boundary_store: GraphStore,
) -> None:
    """A malformed rule (empty list, degenerate, empty prefix, not a pair)
    comes back as an error dict showing the expected shape — not an
    exception."""
    for bad in ([], [["common/", "common/"]], [["", "platform/"]], [["common/"]]):
        result = mcp_server.boundary_violation_report(boundary_store, bad)
        assert "error" in result, bad
        assert "[from_prefix, forbidden_prefix]" in result["hint"]


def test_boundary_violations_tool_registered_and_routes_through_call(
    tmp_path: Path,
) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to
    `boundary_violation_report` (the pure function is covered directly; this
    covers the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.add_edge("calls", "common_fn", "platform_secret", file="common/util.cpp", line=9)
    graph.nodes["common_fn"].file = "common/util.cpp"
    graph.nodes["platform_secret"].file = "platform/hidden.cpp"
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["boundary_violations"].fn
    result = tool(rules=[["common/", "platform/"]])
    assert result["total"] == 1
    assert result["violations"][0]["dst"] == "platform_secret"


# --- outline / class_members -------------------------------------------------

FOO_TYPE = "cxx . . $ mongo/Foo#"
FOO_PARSE = "cxx . . $ mongo/Foo#parse(a1)."
FOO_COUNT = "cxx . . $ mongo/Foo#count."
FOO_INNER = "cxx . . $ mongo/Foo#Inner#"
MAKE_FOO = "cxx . . $ mongo/makeFoo(a2)."
FOOBAR = "cxx . . $ mongo/FooBar#"
FOOBAR_PARSE = "cxx . . $ mongo/FooBar#parse(a3)."


@pytest.fixture
def members_store(tmp_path: Path) -> GraphStore:
    """One file (`mongo/foo.h`) holding class `Foo` (a method, a field, a
    nested type), a free function, and the sibling class `FooBar` whose
    members must not leak into `Foo`'s."""
    graph = Graph()
    for symbol, line in (
        (MAKE_FOO, 30),
        (FOOBAR_PARSE, 41),
        (FOO_TYPE, 10),
        (FOO_INNER, 13),
        (FOOBAR, 40),
        (FOO_COUNT, 12),
        (FOO_PARSE, 11),
    ):
        graph.add_node(symbol)
        graph.nodes[symbol].file = "mongo/foo.h"
        graph.nodes[symbol].line = line
    path = tmp_path / "members.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_file_outline_report_lists_definitions_in_line_order(
    members_store: GraphStore,
) -> None:
    result = mcp_server.file_outline_report(members_store, "mongo/foo.h")
    assert result["total"] == 7
    assert result["truncated"] is False
    assert [d["line"] for d in result["definitions"]] == [11, 12, 13, 14, 31, 41, 42]
    assert result["definitions"][0]["name"] == "mongo/Foo#"  # label from the SCIP string
    assert result["definitions"][0]["file"] == "mongo/foo.h"


def test_file_outline_report_unknown_file_is_a_note_not_a_bare_zero(
    members_store: GraphStore,
) -> None:
    """A wrong path is the usual cause of an empty outline (exact match, the
    index's relative paths) — the response says so instead of a bare 0."""
    result = mcp_server.file_outline_report(members_store, "src/nope.cpp")
    assert result["total"] == 0
    assert result["definitions"] == []
    assert "exactly" in result["note"]
    assert "stats" in result["note"]


def test_file_outline_report_limit_truncates(members_store: GraphStore) -> None:
    result = mcp_server.file_outline_report(members_store, "mongo/foo.h", limit=2)
    assert result["total"] == 7
    assert len(result["definitions"]) == 2
    assert result["truncated"] is True


def test_file_outline_report_matches_store_directly(members_store: GraphStore) -> None:
    """Parity check, same shape as the other report/store pairs: the MCP
    outline is exactly `store.outline`, not a reimplementation (line is
    1-indexed here, 0-indexed in the store, like every other tool)."""
    result = mcp_server.file_outline_report(members_store, "mongo/foo.h", full_symbols=True)
    expected, expected_total = members_store.outline("mongo/foo.h")
    assert result["total"] == expected_total
    assert [(d["symbol"], d["file"], d["line"]) for d in result["definitions"]] == [
        (n.symbol, n.file, n.line + 1) for n in expected
    ]


def test_class_members_report_lists_methods_fields_and_nested_types(
    members_store: GraphStore,
) -> None:
    result = mcp_server.class_members_report(members_store, FOO_TYPE)
    assert result["total"] == 3
    assert result["truncated"] is False
    assert [d["name"] for d in result["members"]] == [
        "mongo/Foo#parse(a1).",
        "mongo/Foo#count.",
        "mongo/Foo#Inner#",
    ]


def test_class_members_report_does_not_confuse_foo_with_foobar(
    members_store: GraphStore,
) -> None:
    result = mcp_server.class_members_report(members_store, FOO_TYPE, full_symbols=True)
    symbols = [d["symbol"] for d in result["members"]]
    assert symbols == [FOO_PARSE, FOO_COUNT, FOO_INNER]
    assert FOOBAR_PARSE not in symbols


def test_class_members_report_unknown_symbol_is_error_dict(members_store: GraphStore) -> None:
    """The shared unknown-symbol convention (same as `who_calls`/`references`):
    an error dict pointing at `find`, never a guessed answer."""
    result = mcp_server.class_members_report(members_store, "cxx . . $ mongo/Nope#")
    assert "error" in result
    assert "find" in result["error"]


def test_class_members_report_ambiguous_name_lists_candidates(
    members_store: GraphStore,
) -> None:
    """`Foo` matches the class *and* its own members (their SCIP strings all
    contain `Foo`), plus `FooBar`'s — the no-guessing convention returns the
    candidate list; the caller re-picks the `#`-terminated entry."""
    result = mcp_server.class_members_report(members_store, "Foo")
    assert "ambiguous" in result
    assert result["total"] == 7
    assert FOO_TYPE in [c["symbol"] for c in result["candidates"]]


def test_class_members_report_non_type_is_error_dict(members_store: GraphStore) -> None:
    """A known non-type symbol (a method, a free function) is bad input, not
    an empty member list that would read as 'memberless class'."""
    result = mcp_server.class_members_report(members_store, "makeFoo")
    assert "error" in result
    assert "not a type" in result["error"]
    result = mcp_server.class_members_report(members_store, FOO_PARSE)
    assert "error" in result


def test_class_members_report_limit_truncates(members_store: GraphStore) -> None:
    result = mcp_server.class_members_report(members_store, FOO_TYPE, limit=1)
    assert result["total"] == 3
    assert len(result["members"]) == 1
    assert result["truncated"] is True


def test_class_members_report_matches_store_directly(members_store: GraphStore) -> None:
    result = mcp_server.class_members_report(members_store, FOO_TYPE, full_symbols=True)
    expected, expected_total = members_store.class_members(FOO_TYPE)
    assert result["total"] == expected_total
    assert [(d["symbol"], d["file"], d["line"]) for d in result["members"]] == [
        (m.symbol, m.file, m.line + 1) for m in expected
    ]


def test_outline_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to
    `file_outline_report` (the pure function is covered directly; this covers
    the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.add_node("cxx . . $ src/app#main(m1).")
    graph.nodes["cxx . . $ src/app#main(m1)."].file = "src/app.cpp"
    graph.nodes["cxx . . $ src/app#main(m1)."].line = 3
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["outline"].fn
    result = tool(file="src/app.cpp")
    assert result["total"] == 1
    assert result["definitions"][0]["name"] == "src/app#main(m1)."


def test_class_members_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to
    `class_members_report` (the pure function is covered directly; this
    covers the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    for symbol, line in ((FOO_TYPE, 10), (FOO_PARSE, 11), (FOO_COUNT, 12)):
        graph.add_node(symbol)
        graph.nodes[symbol].file = "mongo/foo.h"
        graph.nodes[symbol].line = line
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["class_members"].fn
    result = tool(symbol=FOO_TYPE)
    assert result["total"] == 2
    assert [d["name"] for d in result["members"]] == [
        "mongo/Foo#parse(a1).",
        "mongo/Foo#count.",
    ]


PARSE = "cxx . . $ src/parse(r1)."
LEX = "cxx . . $ src/lex(r2)."
PEEK = "cxx . . $ src/peek(r3)."
ENTER = "cxx . . $ src/enter(r4)."
EXIT = "cxx . . $ src/exit(r5)."
SELFREC = "cxx . . $ src/selfrec(r6)."


@pytest.fixture
def scc_store(tmp_path: Path) -> GraphStore:
    """A 3-cycle (parse->lex->peek->parse) and a 2-cycle (enter<->exit), plus
    a directly self-recursive symbol that must NOT surface as a component."""
    graph = Graph()
    for src, dst, line in (
        (PARSE, LEX, 1),
        (LEX, PEEK, 2),
        (PEEK, PARSE, 3),
        (ENTER, EXIT, 4),
        (EXIT, ENTER, 5),
        (SELFREC, SELFREC, 6),
    ):
        graph.add_edge("calls", src, dst, file="src/parser.cpp", line=line)
    for symbol, line in (
        (PARSE, 10),
        (LEX, 11),
        (PEEK, 12),
        (ENTER, 20),
        (EXIT, 21),
        (SELFREC, 30),
    ):
        graph.nodes[symbol].file = "src/parser.cpp"
        graph.nodes[symbol].line = line
    path = tmp_path / "scc.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_scc_report_lists_components_biggest_first(scc_store: GraphStore) -> None:
    result = mcp_server.scc_report(scc_store)
    assert result["total"] == 2
    assert result["truncated"] is False
    names = [[d["name"] for d in comp] for comp in result["components"]]
    assert names == [
        ["src/parse(r1).", "src/lex(r2).", "src/peek(r3)."],
        ["src/enter(r4).", "src/exit(r5)."],
    ]
    # members are node dicts: 1-indexed definition line like every other tool
    assert result["components"][0][0]["file"] == "src/parser.cpp"
    assert result["components"][0][0]["line"] == 11


def test_scc_report_states_fact_not_verdict(scc_store: GraphStore) -> None:
    """The standing note carries the facts-not-judgments caveat: mutual
    recursion is often legitimate, the reader judges which cycles matter."""
    result = mcp_server.scc_report(scc_store)
    assert "fact" in result["note"]
    assert "legitimate" in result["note"]


def test_scc_report_limit_truncates(scc_store: GraphStore) -> None:
    result = mcp_server.scc_report(scc_store, limit=1)
    assert result["total"] == 2
    assert len(result["components"]) == 1
    assert result["truncated"] is True


def test_scc_report_matches_store_directly(scc_store: GraphStore) -> None:
    """Parity check, same shape as the other report/store pairs: the MCP
    components are exactly `store.strongly_connected_components`, not a
    reimplementation."""
    result = mcp_server.scc_report(scc_store, full_symbols=True)
    expected, expected_total = scc_store.strongly_connected_components()
    assert result["total"] == expected_total
    assert [[d["symbol"] for d in comp] for comp in result["components"]] == expected


def test_scc_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to `scc_report` (the
    pure function is covered directly; this covers the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.add_edge("calls", "a", "b", file="src/a.cpp", line=1)
    graph.add_edge("calls", "b", "a", file="src/a.cpp", line=2)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["strongly_connected_components"].fn
    result = tool()
    assert result["total"] == 1
    assert [d["name"] for d in result["components"][0]] == ["a", "b"]


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


def test_explain_signature_includes_default_argument_value(
    store: GraphStore, tmp_path: Path
) -> None:
    """From real use: a defaulted param (`useNullIfMissing = false`) a caller
    omits was invisible without opening the header — `signature` surfaces it
    verbatim, source-derived, since the graph itself has no parsed signature."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "foo.cpp").write_text(
        "\n".join(f"line {i}" for i in range(234))
        + "\nvoid makeResumeToken(const Document& doc, bool useNullIfMissing = false) {}\n"
    )
    result = mcp_server.explain(store, FOO, root=str(root))
    assert result["signature"] == "(const Document& doc, bool useNullIfMissing = false)"


def test_explain_signature_absent_without_root(store: GraphStore) -> None:
    result = mcp_server.explain(store, FOO)
    assert "signature" not in result


def test_explain_signature_is_none_not_absent_when_root_given_but_unextractable(
    store: GraphStore, tmp_path: Path
) -> None:
    """With `root` given, `signature` mirrors `source`'s convention: the key is
    always present, `None` means "tried, couldn't extract" (missing file here)
    — distinct from being absent entirely when `root` wasn't given at all."""
    result = mcp_server.explain(store, FOO, root=str(tmp_path))  # no foo.cpp there
    assert "signature" in result
    assert result["signature"] is None


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
    assert result["transport"] == "mcp"


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


def test_call_attaches_stale_flag(tmp_path: Path) -> None:
    """A tool response routed through `build_server`'s `_call` wrapper carries a
    `stale` bool driven by the real git drift state — not just the pure
    `(store, ...) -> dict` functions tested elsewhere in this file."""
    from cppgraph.mcp_server import build_server

    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("int main(){}\n")
    commit = _init_repo(root)
    graph = Graph()
    graph.add_node(FOO, display_name="makeResumeToken")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta={"source_commit": commit})

    server = build_server(str(path), root=str(root))
    find_tool = server._tool_manager._tools["find"].fn
    assert find_tool(query="makeResumeToken")["stale"] is False

    (root / "a.cpp").write_text("int main(){return 1;}\n")  # drift
    assert find_tool(query="makeResumeToken")["stale"] is True


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


def test_find_fuzzy_fallback_separator_insensitive(tmp_path: Path) -> None:
    # "changestream" (no separator) must match change_stream / changeStream via
    # the fuzzy fallback, and the response is flagged relaxed.
    a = "cxx . . $ mongo/change_stream/buildPipeline(a1)."
    b = "cxx . . $ mongo/DocumentSourceChangeStream#foo(a2)."
    graph = Graph()
    graph.nodes[a] = Node(symbol=a, file="cs.cpp", line=1)
    graph.nodes[b] = Node(symbol=b, file="cs.cpp", line=2)
    path = tmp_path / "fuzzy.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    r = mcp_server.find_symbols(st, "changestream")
    assert r["total"] == 2
    assert r["relaxed"] is True
    assert "insensitive" in r["note"]


def test_find_fuzzy_multi_term(tmp_path: Path) -> None:
    a = "cxx . . $ mongo/change_stream/pipeline_helpers/buildPipeline(a1)."
    graph = Graph()
    graph.nodes[a] = Node(symbol=a, file="ph.cpp", line=1)
    path = tmp_path / "fz2.db"
    write_sqlite(graph, path)
    r = mcp_server.find_symbols(GraphStore(path), "buildPipeline changestream")
    assert r["total"] == 1
    assert r["relaxed"] is True


def test_explain_hide_trivial(tmp_path: Path) -> None:
    tgt = "cxx . . $ mongo/Foo#run(a1)."
    real = "cxx . . $ mongo/Foo#doDomainWork(a2)."
    op = "cxx . . $ mongo/Foo#operator!=(a3)."
    graph = Graph()
    for s in (tgt, real, op):
        graph.nodes[s] = Node(symbol=s, file="foo.cpp", line=1)
    graph.add_edge("calls", tgt, real, file="foo.cpp", line=2)
    graph.add_edge("calls", tgt, op, file="foo.cpp", line=3)
    path = tmp_path / "ex.db"
    write_sqlite(graph, path)
    st = GraphStore(path)

    full = mcp_server.explain(st, tgt)
    assert full["callees"]["total"] == 2
    assert "trivial_hidden" not in full["callees"]

    filtered = mcp_server.explain(st, tgt, hide_trivial=True)
    assert filtered["callees"]["total"] == 1
    assert filtered["callees"]["trivial_hidden"] == 1


def test_subclasses_zero_has_hint(hierarchy: GraphStore) -> None:
    r = mcp_server.subtypes(hierarchy, LEAF)  # a leaf: no subclasses
    assert r["total"] == 0
    assert "note" in r
    r2 = mcp_server.subtypes(hierarchy, BASE)  # has a subclass: no note
    assert r2["total"] == 1
    assert "note" not in r2


def test_base_classes_zero_has_hint(hierarchy: GraphStore) -> None:
    r = mcp_server.bases(hierarchy, BASE)  # a root: no bases
    assert r["total"] == 0
    assert "note" in r


def test_find_overload_signature_from_source(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "rt.h").write_text(
        "namespace mongo {\n"
        "ResumeToken parse(const Document& doc);\n"
        "ResumeToken parse(const BSONObj& obj, bool strict);\n"
        "}\n"
    )
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    graph.nodes[p1] = Node(symbol=p1, file="rt.h", line=1)  # 0-indexed line 1
    graph.nodes[p2] = Node(symbol=p2, file="rt.h", line=2)
    path = tmp_path / "sig.db"
    write_sqlite(graph, path)
    r = mcp_server.find_symbols(GraphStore(path), "parse", root=str(src_dir))
    entry = r["results"][0]
    assert entry["overloads"] == 2
    sigs = {s["signature"] for s in entry["signatures"]}
    assert "(const Document& doc)" in sigs
    assert "(const BSONObj& obj, bool strict)" in sigs


def test_resolve_unique_name(store: GraphStore) -> None:
    """A relational tool accepts a bare name that resolves to exactly one symbol,
    so no separate `find` round-trip is needed."""
    by_name = mcp_server.callers(store, "makeResumeToken")
    by_symbol = mcp_server.callers(store, FOO)
    assert by_name["symbol"] == FOO == by_symbol["symbol"]
    assert by_name["total"] == by_symbol["total"] == 1


def test_resolve_normalizes_colons(store: GraphStore) -> None:
    """`Class::method` (C++ syntax) is normalized to SCIP's `Class#method`."""
    r = mcp_server.callers(store, "Foo::makeResumeToken")
    assert r["symbol"] == FOO
    assert r["total"] == 1


def test_resolve_ambiguous_returns_candidates_not_a_guess(store: GraphStore) -> None:
    """An ambiguous name returns the candidate list instead of guessing a symbol
    (a wrong guess would be a confidently-wrong answer)."""
    r = mcp_server.callers(store, "Foo")  # substring of FOO, CALLER and MID
    assert r.get("ambiguous") == "Foo"
    assert r["total"] == 3
    assert {c["symbol"] for c in r["candidates"]} == {FOO, CALLER, MID}
    assert "callers" not in r  # it did not proceed on a guessed symbol


def test_resolve_unknown_name_errors(store: GraphStore) -> None:
    r = mcp_server.callers(store, "does_not_exist")
    assert "error" in r
