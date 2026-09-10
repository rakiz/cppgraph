"""Tests for the MCP server's query layer.

The MCP transport (stdio) is thin FastMCP wiring; the substance is the pure
`(store, ...) -> dict` functions that turn GraphStore results into
token-budgeted, JSON-serialisable payloads. We test those directly against a
tiny fixture store — no transport needed.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from cppgraph import mcp_server
from cppgraph.model import Graph, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import SCHEMA_VERSION, GraphStore, write_sqlite
from cppgraph.updates import BYTES_PER_ATTRIBUTED_REF


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


def test_reachable_from_transitive_callees(store: GraphStore) -> None:
    result = mcp_server.reachable_from_report(store, CALLER)
    names = {r["name"] for r in result["reaches"]}
    assert names == {"mid", "makeResumeToken"}
    assert result["total"] == 2
    assert result["kind"] == "calls"


def test_reachable_from_depth_bounds_walk(store: GraphStore) -> None:
    result = mcp_server.reachable_from_report(store, CALLER, depth=1)
    names = {r["name"] for r in result["reaches"]}
    assert names == {"mid"}  # only the direct callee at depth 1


def test_reachable_from_limit_truncates(store: GraphStore) -> None:
    result = mcp_server.reachable_from_report(store, CALLER, limit=1)
    assert result["total"] == 2
    assert len(result["reaches"]) == 1
    assert result["truncated"] is True


def test_reachable_from_response_states_lower_bound(store: GraphStore) -> None:
    """Per the DESIGN.md corollary: a forward-reachability result under-reports
    runtime reachability, so every response says so — never a set to act on by
    exclusion."""
    result = mcp_server.reachable_from_report(store, CALLER)
    assert "lower bound" in result["note"]
    assert "at least these are reachable" in result["note"]


def test_reachable_from_exclude_tests_drops_test_defined(tmp_path: Path) -> None:
    entry = "cxx . . $ mongo/handler#run(a1)."
    prod = "cxx . . $ mongo/Foo#doWork(a2)."
    helper = "cxx . . $ mongo/Foo#assertState(a3)."
    graph = Graph()
    graph.nodes[entry] = Node(symbol=entry, display_name="run", file="handler.cpp", line=1)
    graph.nodes[prod] = Node(symbol=prod, display_name="doWork", file="foo.cpp", line=2)
    graph.nodes[helper] = Node(
        symbol=helper, display_name="assertState", file="foo_test.cpp", line=3
    )
    graph.add_edge("calls", entry, prod, file="handler.cpp", line=10)
    graph.add_edge("calls", entry, helper, file="handler.cpp", line=11)
    path = tmp_path / "rt.db"
    write_sqlite(graph, path)
    st = GraphStore(path)
    default = mcp_server.reachable_from_report(st, entry)
    assert {r["name"] for r in default["reaches"]} == {"doWork"}  # test-defined dropped
    kept = mcp_server.reachable_from_report(st, entry, exclude_tests=False)
    assert {r["name"] for r in kept["reaches"]} == {"doWork", "assertState"}


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


# --- global_init_references (attributed-refs gated) ---------------------------


G_A = "cxx . . $ app/g_a."
G_B = "cxx . . $ app/g_b."
HELPER = "cxx . . $ app/helper(h1)."


@pytest.fixture
def globals_store(tmp_path: Path) -> GraphStore:
    """`int g_a = helper(); static int g_b = g_a + 1;` — g_b's initializer reads
    g_a (term -> term); g_a's region use is a callable call, not a global read."""
    graph = Graph()
    graph.nodes[G_A] = Node(symbol=G_A, display_name="g_a", file="src/g.cpp", line=1, end_line=1)
    graph.nodes[G_B] = Node(symbol=G_B, display_name="g_b", file="src/g.cpp", line=2, end_line=2)
    graph.add_reference(HELPER, "src/g.cpp", line=1, enclosing_symbol=G_A)
    graph.add_reference(G_A, "src/g.cpp", line=2, enclosing_symbol=G_B)
    path = tmp_path / "globals.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_global_init_references_report_lists_referenced_globals(
    globals_store: GraphStore,
) -> None:
    result = mcp_server.global_init_references_report(globals_store, G_B)
    assert result["symbol"] == G_B
    assert result["total"] == 1
    assert result["truncated"] is False
    assert [r["name"] for r in result["references"]] == ["g_a"]
    assert result["references"][0]["file"] == "src/g.cpp"
    assert result["references"][0]["line"] == 3  # 0-indexed 2 -> 1-indexed


def test_global_init_references_report_resolves_a_plain_name(
    globals_store: GraphStore,
) -> None:
    """The shared resolve step: `g_b` (a unique name) works in one call."""
    result = mcp_server.global_init_references_report(globals_store, "g_b")
    assert result["total"] == 1


def test_global_init_references_report_states_fact_not_verdict(
    globals_store: GraphStore,
) -> None:
    """Every response carries the caveat: a constexpr/constinit init is safe —
    the tool reports the reference, the LLM judges the hazard."""
    result = mcp_server.global_init_references_report(globals_store, G_B)
    assert "constexpr" in result["note"]
    assert "not a verdict" in result["note"]


def test_global_init_references_report_empty_for_callable_only_region_uses(
    globals_store: GraphStore,
) -> None:
    """g_a's initializer calls a function — no global reads, an honest empty
    list (not None: the store carries attributed refs)."""
    result = mcp_server.global_init_references_report(globals_store, G_A)
    assert result["total"] == 0
    assert result["references"] == []


def test_global_init_references_report_unavailable_reports_reason(
    store: GraphStore,
) -> None:
    """The default fixture store carries no attributed refs: `available: false`
    with the rebuild pointer — never a silently empty list."""
    result = mcp_server.global_init_references_report(store, FOO)
    assert result["available"] is False
    assert "attributed" in result["reason"]
    assert "--attributed-refs" in result["reason"]


def test_global_init_references_report_non_term_is_error_dict(
    globals_store: GraphStore,
) -> None:
    """A callable has no initializer region: an error dict, the class_members
    contract — bad input, never an empty list that would read as 'references
    nothing'."""
    result = mcp_server.global_init_references_report(globals_store, HELPER)
    assert "error" in result
    assert "not a global" in result["error"]


def test_global_init_references_report_unknown_symbol_is_error_dict(
    store: GraphStore,
) -> None:
    result = mcp_server.global_init_references_report(store, "missing_symbol_xyz")
    assert "error" in result


def test_global_init_references_report_matches_store_directly(
    globals_store: GraphStore,
) -> None:
    """Parity check: the MCP tool's answer is exactly
    `store.global_init_references`, not a reimplementation."""
    result = mcp_server.global_init_references_report(globals_store, G_B, full_symbols=True)
    expected, expected_total = globals_store.global_init_references(G_B)
    assert result["total"] == expected_total
    assert [r["symbol"] for r in result["references"]] == [r.symbol for r in expected]


def test_global_init_references_tool_registered_and_routes_through_call(
    tmp_path: Path,
) -> None:
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.add_node("cxx . . $ app/g.")
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["global_init_references"].fn
    assert tool("g")["available"] is False  # no attributed refs: refused, not guessed


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


# --- api_surface ---------------------------------------------------------------


@pytest.fixture
def module_store(tmp_path: Path) -> GraphStore:
    """`mod/` with `both_fn` (2 external calls + 1 external ref) and
    `called_fn` (1 external call), both used from `app/` outside it."""
    graph = Graph()
    graph.add_edge("calls", "app_main", "called_fn", file="app/main.cpp", line=5)
    graph.add_edge("calls", "app_main", "both_fn", file="app/main.cpp", line=6)
    graph.add_edge("calls", "app_other", "both_fn", file="app/other.cpp", line=7)
    graph.nodes["called_fn"].file = "mod/util.cpp"
    graph.nodes["both_fn"].file = "mod/api.cpp"
    graph.nodes["both_fn"].line = 4
    graph.nodes["app_main"].file = "app/main.cpp"
    graph.nodes["app_other"].file = "app/other.cpp"
    graph.add_reference("both_fn", "app/main.cpp", 21)
    path = tmp_path / "module.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_api_surface_report_lists_separate_counts_ranked_by_sum(
    module_store: GraphStore,
) -> None:
    result = mcp_server.api_surface_report(module_store, "mod/")
    assert result["total"] == 2
    assert result["truncated"] is False
    assert result["refs_available"] is True
    assert [item["name"] for item in result["surface"]] == ["both_fn", "called_fn"]
    both = result["surface"][0]
    assert both["external_calls"] == 2
    assert both["external_refs"] == 1
    assert both["file"] == "mod/api.cpp"
    assert "note" not in result  # refs data present: no degrade note


def test_api_surface_report_without_refs_notes_calls_only(tmp_path: Path) -> None:
    """The `--references`-gated degrade path: the surface is still answered
    (call sites only) but says so — `refs_available: false` plus a note naming
    the rebuild, never a bare `external_refs: 0` that reads as 'never
    referenced outside'."""
    graph = Graph()
    graph.add_edge("calls", "app_main", "called_fn", file="app/main.cpp", line=5)
    graph.nodes["called_fn"].file = "mod/util.cpp"
    graph.nodes["app_main"].file = "app/main.cpp"
    path = tmp_path / "norefs.db"
    write_sqlite(graph, path)
    store = GraphStore(path)
    result = mcp_server.api_surface_report(store, "mod/")
    assert result["total"] == 1
    assert result["refs_available"] is False
    assert result["surface"][0]["external_calls"] == 1
    assert result["surface"][0]["external_refs"] == 0
    assert "--no-references" in result["note"]


def test_api_surface_report_limit_truncates(module_store: GraphStore) -> None:
    result = mcp_server.api_surface_report(module_store, "mod/", limit=1)
    assert result["total"] == 2
    assert len(result["surface"]) == 1
    assert result["truncated"] is True


def test_api_surface_report_matches_store_directly(module_store: GraphStore) -> None:
    """Parity check, same shape as the boundary ones: the report is exactly
    `store.api_surface`, not a reimplementation (line is 1-indexed here,
    0-indexed in the store, like every other tool)."""
    result = mcp_server.api_surface_report(module_store, "mod/", full_symbols=True)
    expected, expected_total, expected_refs = module_store.api_surface("mod/")
    assert result["total"] == expected_total
    assert result["refs_available"] == expected_refs
    assert [
        (i["symbol"], i["file"], i["external_calls"], i["external_refs"]) for i in result["surface"]
    ] == [(e["symbol"], e["file"], e["external_calls"], e["external_refs"]) for e in expected]
    assert result["surface"][0]["line"] == expected[0]["line"] + 1


def test_api_surface_report_empty_prefix_is_an_error_dict(module_store: GraphStore) -> None:
    """Bad input comes back as an error dict showing the expected shape, not
    an exception (the `boundary_violation_report` convention)."""
    result = mcp_server.api_surface_report(module_store, "")
    assert "error" in result
    assert "module" in result["hint"].lower()


def test_api_surface_tool_registered_and_routes_through_call(tmp_path: Path) -> None:
    """The `@mcp.tool()` wrapper exists and delegates to `api_surface_report`
    (the pure function is covered directly; this covers the wiring)."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.add_edge("calls", "app_main", "called_fn", file="app/main.cpp", line=5)
    graph.nodes["called_fn"].file = "mod/util.cpp"
    graph.nodes["app_main"].file = "app/main.cpp"
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    tool = server._tool_manager._tools["api_surface"].fn
    result = tool(module_prefix="mod/")
    assert result["total"] == 1
    assert result["surface"][0]["name"] == "called_fn"


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


def test_references_access_filter_and_annotation(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_reference(TYPE, "a.cpp", 10, roles=scip_pb2.SymbolRole.WriteAccess)
    graph.add_reference(
        TYPE,
        "a.cpp",
        12,
        roles=scip_pb2.SymbolRole.ReadAccess | scip_pb2.SymbolRole.WriteAccess,
    )
    graph.add_reference(TYPE, "b.cpp", 41)  # plain read — no annotation, kept by access="read"
    path = tmp_path / "acc.db"
    write_sqlite(graph, path)
    store = GraphStore(path)

    all_uses = mcp_server.references(store, TYPE)
    tags = {(u["file"], u["line"]): u.get("access") for u in all_uses["uses"]}
    assert tags[("a.cpp", 11)] == "write"
    assert tags[("a.cpp", 13)] == "read+write"
    assert tags[("b.cpp", 42)] is None  # a plain read stays silent

    writes = mcp_server.references(store, TYPE, access="write")
    assert {(u["file"], u["line"]) for u in writes["uses"]} == {("a.cpp", 11), ("a.cpp", 13)}

    reads = mcp_server.references(store, TYPE, access="read")
    assert [(u["file"], u["line"]) for u in reads["uses"]] == [("b.cpp", 42)]


def test_references_enclosing_symbol_and_access_annotate_together(tmp_path: Path) -> None:
    # A reference can carry both #504 attribution and access roles; the returned
    # item must expose both keys together, neither clobbering the other.
    graph = Graph()
    graph.add_node("cxx . . $ mongo/render(r1).")
    graph.add_reference(
        TYPE,
        "a.cpp",
        10,
        enclosing_symbol="cxx . . $ mongo/render(r1).",
        roles=scip_pb2.SymbolRole.WriteAccess,
    )
    path = tmp_path / "both.db"
    write_sqlite(graph, path)
    store = GraphStore(path)

    result = mcp_server.references(store, TYPE)
    (use,) = result["uses"]
    assert use["used_by"] == "mongo/render(r1)."
    assert use["access"] == "write"


def test_references_access_filter_without_role_data_reports(refs_store: GraphStore) -> None:
    # refs_store has a reference index, but no role data (stock build) — an
    # access filter must report rather than pretend every site is a read.
    result = mcp_server.references(refs_store, TYPE, access="write")
    assert result["available"] is False
    assert "read/write access data" in result["reason"]


def test_impact_over_inherits_gives_all_descendants(hierarchy: GraphStore) -> None:
    result = mcp_server.impact(hierarchy, BASE, kind="inherits", full_symbols=True)
    assert {r["symbol"] for r in result["reached_by"]} == {DERIVED, LEAF}
    assert result["kind"] == "inherits"


def test_reachable_from_over_inherits_gives_transitive_ancestors(
    hierarchy: GraphStore,
) -> None:
    result = mcp_server.reachable_from_report(hierarchy, LEAF, kind="inherits", full_symbols=True)
    assert {r["symbol"] for r in result["reaches"]} == {DERIVED, BASE}
    assert result["kind"] == "inherits"


@pytest.fixture
def typed_by_store(tmp_path: Path) -> GraphStore:
    graph = Graph()
    graph.add_edge(
        "typed-by", "cxx . . $ mongo/Outer#f.", "cxx . . $ mongo/Value#", file="outer.h", line=4
    )
    graph.add_edge(
        "calls", "cxx . . $ mongo/fn(a1).", "cxx . . $ mongo/other_fn(b1).", file="f.cpp", line=1
    )
    path = tmp_path / "typed_by.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_impact_over_typed_by_gives_fields_typed_as_the_type(
    typed_by_store: GraphStore,
) -> None:
    result = mcp_server.impact(
        typed_by_store, "cxx . . $ mongo/Value#", kind="typed-by", full_symbols=True
    )
    assert result["kind"] == "typed-by"
    assert {r["symbol"] for r in result["reached_by"]} == {"cxx . . $ mongo/Outer#f."}


def test_reachable_from_over_typed_by_gives_the_declared_type(
    typed_by_store: GraphStore,
) -> None:
    result = mcp_server.reachable_from_report(
        typed_by_store, "cxx . . $ mongo/Outer#f.", kind="typed-by", full_symbols=True
    )
    assert result["kind"] == "typed-by"
    assert {r["symbol"] for r in result["reaches"]} == {"cxx . . $ mongo/Value#"}


def test_typed_by_isolated_from_calls_traversals(typed_by_store: GraphStore) -> None:
    """A typed-by edge never leaks into a calls traversal (and vice versa) —
    one edge kind per call."""
    assert mcp_server.impact(typed_by_store, "cxx . . $ mongo/Value#")["total"] == 0
    assert (
        mcp_server.reachable_from_report(
            typed_by_store, "cxx . . $ mongo/fn(a1).", kind="typed-by"
        )["total"]
        == 0
    )


def test_boundary_violation_report_typed_by_kind(
    tmp_path: Path,
) -> None:
    """edge_kinds=["typed-by"] checks type-usage crossing the boundary; the
    default kinds (calls, inherits) exclude it."""
    graph = Graph()
    graph.add_edge("typed-by", "common_widget", "platform_value", file="common/widget.h", line=7)
    graph.nodes["common_widget"].file = "common/widget.h"
    graph.nodes["platform_value"].file = "platform/value.h"
    path = tmp_path / "tbb.db"
    write_sqlite(graph, path)
    store = GraphStore(path)
    rules = [["common/", "platform/"]]
    assert mcp_server.boundary_violation_report(store, rules)["total"] == 0
    result = mcp_server.boundary_violation_report(store, rules, edge_kinds=["typed-by"])
    assert result["total"] == 1
    v = result["violations"][0]
    assert v["kind"] == "typed-by"
    assert v["src"] == "common_widget"
    assert v["dst"] == "platform_value"


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


def test_explain_includes_documentation_from_the_graph(tmp_path: Path) -> None:
    """A genuine doc comment is carried by the store (extracted at index time,
    placeholder/auto-text filtered by the builder), so it needs no `root` and
    no source read — unlike `signature`. Present only when there is real
    text: absent, never the placeholder."""
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        documentation="/** Extracts the change stream resume token. */",
    )
    write_sqlite(graph, path)
    result = mcp_server.explain(GraphStore(path), FOO)
    assert result["documentation"] == "/** Extracts the change stream resume token. */"


def test_explain_omits_documentation_when_none(store: GraphStore) -> None:
    result = mcp_server.explain(store, FOO)
    assert "documentation" not in result


def test_explain_includes_scip_kind(tmp_path: Path) -> None:
    """The fine-grained SCIP kind (kind-patch binary, `has_symbol_kind`):
    present only when the node carries one — absent, never null (the same
    presence convention as `documentation`)."""
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        scip_kind="StaticMethod",
    )
    write_sqlite(graph, path)
    result = mcp_server.explain(GraphStore(path), FOO)
    assert result["scip_kind"] == "StaticMethod"


def test_explain_omits_scip_kind_when_absent(store: GraphStore) -> None:
    """A stock-binary graph carries no kinds: the key is absent entirely (no
    data collected), not None (data collected, negative)."""
    result = mcp_server.explain(store, FOO)
    assert "scip_kind" not in result


def test_explain_includes_signature_documentation_from_the_graph(tmp_path: Path) -> None:
    """A recorded signature is carried by the store (extracted at index time
    from `SymbolInformation.signature_documentation`), so it needs no `root`
    and no source read — unlike `signature`. Present only when the graph
    carries text: absent, never null (the same presence convention as
    `documentation`)."""
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        signature_documentation="void makeResumeToken(const Document& doc)",
    )
    write_sqlite(graph, path)
    result = mcp_server.explain(GraphStore(path), FOO)
    assert result["signature_documentation"] == "void makeResumeToken(const Document& doc)"


def test_explain_omits_signature_documentation_when_none(store: GraphStore) -> None:
    result = mcp_server.explain(store, FOO)
    assert "signature_documentation" not in result


def test_explain_signature_and_signature_documentation_coexist(
    store: GraphStore, tmp_path: Path
) -> None:
    """The two keys are genuinely distinct and can coexist: `signature` stays
    the source-derived extraction (root only), `signature_documentation` the
    graph-stored one — neither shadows nor overwrites the other."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "foo.cpp").write_text(
        "\n".join(f"line {i}" for i in range(234))
        + "\nvoid makeResumeToken(const Document& doc, bool useNullIfMissing = false) {}\n"
    )
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        signature_documentation="Signature makeResumeToken(Document)",
    )
    write_sqlite(graph, path)

    result = mcp_server.explain(GraphStore(path), FOO, root=str(root))
    assert result["signature_documentation"] == "Signature makeResumeToken(Document)"
    assert result["signature"] == "(const Document& doc, bool useNullIfMissing = false)"

    without_root = mcp_server.explain(GraphStore(path), FOO)
    assert without_root["signature_documentation"] == "Signature makeResumeToken(Document)"
    assert "signature" not in without_root  # source-derived needs a root


def test_find_includes_scip_kind_when_graph_has_it(tmp_path: Path) -> None:
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        scip_kind="StaticMethod",
    )
    write_sqlite(graph, path)
    result = mcp_server.find_symbols(GraphStore(path), "makeResumeToken")
    assert result["results"][0]["scip_kind"] == "StaticMethod"


def test_find_omits_scip_kind_when_absent(store: GraphStore) -> None:
    result = mcp_server.find_symbols(store, "makeResumeToken")
    assert "scip_kind" not in result["results"][0]


# --- is_out_of_project / has_external_symbols ---------------------------------


@pytest.mark.parametrize("classified", [True, False, None], ids=["external", "native", "phantom"])
def test_explain_includes_is_out_of_project_when_capability_present(
    tmp_path: Path, classified: bool | None
) -> None:
    """A graph with external-package symbol metadata (`has_external_symbols`)
    classifies every resolved symbol: true (external package), false
    (project-native) or null (phantom — no SymbolInformation from either
    source). Null is a real VALUE here, never omitted: "classified, no
    evidence" is an answer."""
    path = tmp_path / "graph.db"
    graph = Graph()
    graph.nodes[FOO] = Node(
        symbol=FOO,
        display_name="makeResumeToken",
        file="foo.cpp",
        line=234,
        is_out_of_project=classified,
    )
    write_sqlite(graph, path, meta={"has_external_symbols": "true"})
    result = mcp_server.explain(GraphStore(path), FOO)
    assert result["is_out_of_project"] is classified


def test_explain_omits_is_out_of_project_when_capability_absent(store: GraphStore) -> None:
    """A graph built before the feature carries neither the flag nor the
    column: the key is omitted entirely — "not classified", never conflated
    with the null "classified, no evidence"."""
    result = mcp_server.explain(store, FOO)
    assert "is_out_of_project" not in result


def test_find_uniform_overloads_share_the_is_out_of_project_value(tmp_path: Path) -> None:
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    graph.nodes[p1] = Node(
        symbol=p1, display_name="parse", file="rt.h", line=1, is_out_of_project=True
    )
    graph.nodes[p2] = Node(
        symbol=p2, display_name="parse", file="rt.cpp", line=2, is_out_of_project=True
    )
    path = tmp_path / "ov.db"
    write_sqlite(graph, path, meta={"has_external_symbols": "true"})

    result = mcp_server.find_symbols(GraphStore(path), "parse")

    entry = result["results"][0]
    assert entry["is_out_of_project"] is True  # all arms agree -> the shared value
    assert {s["symbol"]: s["is_out_of_project"] for s in entry["signatures"]} == {
        p1: True,
        p2: True,
    }


def test_find_mixed_overloads_get_null_is_out_of_project(tmp_path: Path) -> None:
    """Overload arms classified differently (one external, one project-native
    — e.g. a native symbol that also appears in `external_symbols`): the
    top-level value is null (never one arm's value picked silently); each arm
    keeps its own."""
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    graph.nodes[p1] = Node(
        symbol=p1, display_name="parse", file="rt.h", line=1, is_out_of_project=True
    )
    graph.nodes[p2] = Node(
        symbol=p2, display_name="parse", file="rt.cpp", line=2, is_out_of_project=False
    )
    path = tmp_path / "ov.db"
    write_sqlite(graph, path, meta={"has_external_symbols": "true"})

    result = mcp_server.find_symbols(GraphStore(path), "parse")

    entry = result["results"][0]
    assert entry["is_out_of_project"] is None  # disagreement -> null
    assert {s["symbol"]: s["is_out_of_project"] for s in entry["signatures"]} == {
        p1: True,
        p2: False,
    }


@pytest.mark.parametrize(
    "meta",
    [{"has_external_symbols": "true"}, {}],
    ids=["with-feature", "pre-feature"],
)
def test_status_reports_external_symbols_flag(tmp_path: Path, meta: dict[str, str]) -> None:
    """`graph_meta.has_external_symbols` mirrors the meta flag as a bool, the
    same way `has_symbol_kind` is surfaced: true only when the graph carries
    the classification, false when it doesn't — never missing."""
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta=meta)
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    expected = meta.get("has_external_symbols") == "true"
    assert result["graph_meta"]["has_external_symbols"] is expected


def test_explain_limit_is_overridable(store: GraphStore) -> None:
    # FOO has one caller (mid); force a limit of 0 to prove the cap is honored
    # and truncation flagged, so an LLM can raise it back when it needs more.
    result = mcp_server.explain(store, FOO, limit=0)
    assert result["callers"]["items"] == []
    assert result["callers"]["truncated"] is True
    assert result["callers"]["total"] == 1


def test_explain_zero_callers_reliable_on_504_graph(spans_store: GraphStore) -> None:
    """A 0-caller count on a #504-shaped graph is exact (containment
    attribution): no caveat fields, the 0 speaks for itself."""
    result = mcp_server.explain(spans_store, TINY)
    assert result["callers"]["total"] == 0
    assert "zero_callers_reliable" not in result["callers"]
    assert "note" not in result["callers"]


def test_explain_zero_callers_unreliable_on_stock_graph(store: GraphStore) -> None:
    """The default fixture is a stock-binary graph: a reported 0 is not
    guaranteed there — the nearest-preceding fallback can fabricate a phantom
    caller from a bodyless declaration site or drop a call site with no
    preceding callable definition in its document (the same asymmetry that
    makes `no_incoming_calls` refuse) — so the count carries the caveat."""
    result = mcp_server.explain(store, CALLER)
    assert result["callers"]["total"] == 0
    assert result["callers"]["zero_callers_reliable"] is False
    assert "phantom caller" in result["callers"]["note"]
    assert "#504" in result["callers"]["note"]


def test_explain_nonzero_callers_carry_no_caveat(
    store: GraphStore, spans_store: GraphStore
) -> None:
    """A nonzero count needs no caveat on either graph type: over-capture is
    the documented safe direction (an extra caller is verified, never acted on
    by omission) — the caveat exists for the 0, the dangerous direction."""
    stock = mcp_server.explain(store, FOO)  # 1 caller, stock-binary graph
    shaped = mcp_server.explain(spans_store, MIDFN)  # 1 caller, #504-shaped graph
    for result in (stock, shaped):
        assert result["callers"]["total"] > 0
        assert "zero_callers_reliable" not in result["callers"]
        assert "note" not in result["callers"]


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


def test_status_upgrade_hint_estimates_cost_from_ref_count(tmp_path: Path) -> None:
    """The file-granularity upgrade hint carries a store-cost estimate computed
    from THIS graph's ref_count times the measured per-ref constant — asserted
    against the number recomputed from the constant, so refining it after a
    re-measurement keeps both the hint and this test honest."""
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta={"has_references": "true", "ref_count": "84598"})
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    upgrade = result["usage_view"]["upgrade"]
    extra = 84_598 * BYTES_PER_ATTRIBUTED_REF
    assert f"~{extra / 1024:.0f} KB extra" in upgrade
    assert f"{BYTES_PER_ATTRIBUTED_REF:.2f} bytes/ref" in upgrade  # provenance travels with it
    assert "extrapolated" in upgrade  # never worded as a measurement of THIS graph


@pytest.mark.parametrize(
    "meta",
    [{"has_references": "true", "ref_count": "0"}, {"has_references": "true"}],
    ids=["ref-count-zero", "ref-count-missing"],
)
def test_status_upgrade_hint_omits_estimate_without_ref_count(
    tmp_path: Path, meta: dict[str, str]
) -> None:
    """ref_count 0/unknown: the upgrade path is still surfaced, but no estimate
    is fabricated (no crash, no nonsensical '~0 bytes')."""
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta=meta)
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    upgrade = result["usage_view"]["upgrade"]
    assert "enrich-refs" in upgrade
    assert "extrapolated" not in upgrade


def test_status_attributed_graph_gets_no_upgrade_hint(tmp_path: Path) -> None:
    """Already symbol-granularity: nothing to upgrade to — no hint (and so no
    cost estimate) at all; the pre-existing behavior."""
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(
        graph,
        path,
        meta={
            "has_references": "true",
            "has_attributed_refs": "true",
            "ref_count": "84598",
            "attributed_ref_count": "16497",
        },
    )
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    assert result["usage_view"]["granularity"] == "symbol"
    assert "upgrade" not in result["usage_view"]


@pytest.mark.parametrize(
    "meta",
    [{"has_symbol_kind": "true"}, {}],
    ids=["kind-patched", "stock-binary"],
)
def test_status_reports_symbol_kind_flag(tmp_path: Path, meta: dict[str, str]) -> None:
    """`graph_meta.has_symbol_kind` mirrors the meta flag as a bool, the same
    way `has_access_roles` is surfaced: true only when the graph carries
    kinds, false when it doesn't — never missing."""
    graph = Graph()
    graph.add_node(FOO, display_name="x")
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta=meta)
    with GraphStore(path) as st:
        result = mcp_server.status_report(st)
    expected = meta.get("has_symbol_kind") == "true"
    assert result["graph_meta"]["has_symbol_kind"] is expected


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


def test_tool_reports_too_new_schema_as_clean_error_dict(tmp_path: Path) -> None:
    """A store written by a NEWER cppgraph: opening it raises
    `IncompatibleStoreError` at the `GraphStore` level. The tool layer must turn
    that into a clean `{"error": …}` dict — the exception's own message, the
    same shape as the no-index notice — instead of letting the exception escape
    the tool call as a traceback. (The server also starts without crashing.)"""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="a.cpp", line=1)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    con = sqlite3.connect(path)
    con.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),)
    )
    con.commit()
    con.close()

    server = build_server(str(path))
    find_tool = server._tool_manager._tools["find"].fn
    result = find_tool(query="makeResumeToken")
    assert set(result) == {"error"}
    assert "newer than this cppgraph" in result["error"]
    assert "upgrade cppgraph or rebuild the graph" in result["error"]


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


def test_visualize_tool_resolves_plain_unique_name(tmp_path: Path) -> None:
    """`visualize` accepts a plain unique name like every other tool (CLI/MCP
    parity with `export`/`view`): it resolves it, not just exact SCIP strings."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("makeResumeToken", open_browser=False)
    assert "error" not in r
    assert "path" in r
    assert r["nodes"] == 1


def test_visualize_tool_ambiguous_name_lists_candidates(tmp_path: Path) -> None:
    """An ambiguous name gets the same candidate-list reply as the other tools —
    not a plain unknown-symbol error — and no graph is built for a guess."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    graph.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="foo.cpp", line=9)
    graph.nodes[MID] = Node(symbol=MID, display_name="mid", file="foo.cpp", line=49)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    server = build_server(str(path))
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("Foo", open_browser=False)
    assert r.get("ambiguous") == "Foo"
    assert {"ambiguous", "total", "candidates", "hint"} <= r.keys()
    assert "path" not in r


def _chain_server(tmp_path: Path):
    """A graph with the caller -> mid -> makeResumeToken chain, as a built server."""
    from cppgraph.mcp_server import build_server

    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=234)
    graph.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="foo.cpp", line=9)
    graph.nodes[MID] = Node(symbol=MID, display_name="mid", file="foo.cpp", line=49)
    graph.add_edge("calls", CALLER, MID, file="foo.cpp", line=11)
    graph.add_edge("calls", MID, FOO, file="foo.cpp", line=51)
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    return build_server(str(path))


def test_visualize_tool_path_mode_renders_chain(tmp_path: Path) -> None:
    """mode="path" renders the shortest chain between symbol and dst — both
    accepted as plain unique names, like every other tool parameter."""
    server = _chain_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("caller", mode="path", dst="makeResumeToken", open_browser=False)
    assert "error" not in r
    assert r["mode"] == "path"
    assert r["nodes"] == 3 and r["edges"] == 2
    assert r["hops"] == 2
    assert r["dst"] == FOO  # resolved from the plain name
    assert Path(r["path"]).exists()
    assert "window.GRAPH" in Path(r["path"]).read_text(encoding="utf-8")


def test_visualize_tool_path_mode_requires_dst(tmp_path: Path) -> None:
    server = _chain_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("caller", mode="path", open_browser=False)
    assert set(r) == {"error"}
    assert "mode='path' requires dst" in r["error"]


def test_visualize_tool_path_mode_unknown_dst_is_error(tmp_path: Path) -> None:
    server = _chain_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("caller", mode="path", dst="does::not::exist", open_browser=False)
    assert "error" in r
    assert "does::not::exist" in r["error"]


def test_visualize_tool_path_mode_ambiguous_dst_lists_candidates(tmp_path: Path) -> None:
    server = _chain_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("caller", mode="path", dst="Foo", open_browser=False)
    assert r.get("ambiguous") == "Foo"
    assert {"ambiguous", "total", "candidates", "hint"} <= r.keys()


def test_visualize_tool_path_mode_no_path_says_found_false(tmp_path: Path) -> None:
    """No static chain between valid symbols: `found: False` + the shared hint,
    NOT a written/opened near-empty HTML the caller can't tell from success."""
    server = _chain_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz("makeResumeToken", mode="path", dst="caller", open_browser=False)
    assert r["found"] is False
    assert r["src"] == FOO and r["dst"] == CALLER
    assert "runtime-dispatch" in r["hint"]
    assert r["path"] is None  # no chain (same shape as the `path` tool), no HTML


def _diamond_server(tmp_path: Path, *, with_sibling: bool = False):
    """The `src -> {left, right} -> dst` diamond as a built server;
    `with_sibling` adds a src -> sibling dead-end (context, not corridor)."""
    from cppgraph.mcp_server import build_server

    d_src = "cxx . . $ mongo/Flow#src(a1)."
    d_left = "cxx . . $ mongo/Flow#left()."
    d_right = "cxx . . $ mongo/Flow#right()."
    d_dst = "cxx . . $ mongo/Flow#dst(a2)."
    d_sib = "cxx . . $ mongo/Flow#sibling()."
    graph = Graph()
    for sym, name, line in [
        (d_src, "src", 1),
        (d_left, "left", 2),
        (d_right, "right", 3),
        (d_dst, "dst", 4),
        *([(d_sib, "sibling", 5)] if with_sibling else []),
    ]:
        graph.nodes[sym] = Node(symbol=sym, display_name=name, file="f.cpp", line=line)
    for src, dst in [
        (d_src, d_left),
        (d_src, d_right),
        (d_left, d_dst),
        (d_right, d_dst),
    ]:
        graph.add_edge("calls", src, dst, file="f.cpp", line=11)
    if with_sibling:
        graph.add_edge("calls", d_src, d_sib, file="f.cpp", line=15)
    path = tmp_path / "diamond.db"
    write_sqlite(graph, path)
    return build_server(str(path))


def test_visualize_tool_path_mode_expand_paths_renders_corridor(tmp_path: Path) -> None:
    """expand_paths=True: the corridor — both diamond routes at once, not just
    the BFS-first chain (which would be 3 nodes / 2 edges)."""
    server = _diamond_server(tmp_path)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz(
        "cxx . . $ mongo/Flow#src(a1).",
        mode="path",
        dst="cxx . . $ mongo/Flow#dst(a2).",
        expand_paths=True,
        open_browser=False,
    )
    assert "error" not in r
    assert r["mode"] == "path"
    assert r["nodes"] == 4 and r["edges"] == 4
    assert r["hops"] == 4  # corridor edges, reported via core_edges
    assert r["truncated"] is False and r["total"] == 4
    assert Path(r["path"]).exists()
    assert "window.GRAPH" in Path(r["path"]).read_text(encoding="utf-8")
    # the embedded graph stays a pure graphify container (metadata stripped)
    assert '"truncated"' not in Path(r["path"]).read_text(encoding="utf-8")


def test_visualize_tool_path_mode_depth_expands_context(tmp_path: Path) -> None:
    """depth=1 in path mode unions every chain/corridor node's neighbourhood
    (the deps-mode walk) into the graph — the dead-end sibling joins it."""
    server = _diamond_server(tmp_path, with_sibling=True)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz(
        "cxx . . $ mongo/Flow#src(a1).",
        mode="path",
        dst="cxx . . $ mongo/Flow#dst(a2).",
        depth=1,
        open_browser=False,
    )
    assert r["nodes"] == 5 and r["edges"] == 5
    assert r["hops"] == 2  # the chain proper is unchanged by context expansion


def test_visualize_tool_path_mode_omitted_depth_stays_pure(tmp_path: Path) -> None:
    """Backward-compatibility guard: visualize's depth default (2 in deps mode)
    must NOT leak into path mode — omitting depth keeps the pure chain, so the
    pre-feature callers see exactly what they saw before."""
    server = _diamond_server(tmp_path, with_sibling=True)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz(
        "cxx . . $ mongo/Flow#src(a1).",
        mode="path",
        dst="cxx . . $ mongo/Flow#dst(a2).",
        open_browser=False,
    )
    assert r["nodes"] == 3 and r["edges"] == 2  # chain only, no sibling context


def test_visualize_tool_path_mode_limit_truncation_reported(tmp_path: Path) -> None:
    """limit caps the merged node count (corridor kept whole, context cut) and
    the response reports the `truncated`/`total` pair like the other capped
    tools."""
    server = _diamond_server(tmp_path, with_sibling=True)
    viz = server._tool_manager._tools["visualize"].fn
    r = viz(
        "cxx . . $ mongo/Flow#src(a1).",
        mode="path",
        dst="cxx . . $ mongo/Flow#dst(a2).",
        expand_paths=True,
        depth=1,
        limit=4,
        open_browser=False,
    )
    assert r["nodes"] == 4 and r["truncated"] is True and r["total"] == 5
    assert r["hops"] == 4


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
    graph.nodes[p1] = Node(symbol=p1, display_name="parse", file="rt.h", line=1, scip_kind="Method")
    graph.nodes[p2] = Node(
        symbol=p2, display_name="parse", file="rt.cpp", line=2, scip_kind="StaticMethod"
    )
    path = tmp_path / "ov.db"
    write_sqlite(graph, path)
    result = mcp_server.find_symbols(GraphStore(path), "parse")
    assert result["total"] == 2  # raw matches
    assert result["groups"] == 1  # one qualified name
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["overloads"] == 2
    assert {s["symbol"] for s in entry["signatures"]} == {p1, p2}
    # Each arm keeps its own fine-grained kind (kind-patched graph only) —
    # grouping must not collapse the arms to the first one's kind.
    kinds = {s["symbol"]: s["scip_kind"] for s in entry["signatures"]}
    assert kinds == {p1: "Method", p2: "StaticMethod"}


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


def test_reachable_from_on_type_redirects_to_methods(tmp_path: Path) -> None:
    """Forward mirror of the impact redirect: a type makes no calls itself, so
    `kind="calls"` on one would be a bare 0 that reads as "this class's code
    reaches nothing" — its reachability lives in its methods."""
    ty = "cxx . . $ mongo/ResumeTokenData#"
    graph = Graph()
    graph.nodes[ty] = Node(symbol=ty, display_name="ResumeTokenData", file="rt.h", line=1)
    path = tmp_path / "ty.db"
    write_sqlite(graph, path)
    result = mcp_server.reachable_from_report(GraphStore(path), ty)
    assert result["is_type"] is True
    assert result["total"] == 0
    assert "class_members" in result["notice"]


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


def test_find_colon_relaxation_tries_scip_separator_before_leaf(tmp_path: Path) -> None:
    # `Class::method` (C++ spelling) must first be retried as `Class#method`
    # (SCIP's separator) — which hits exactly — instead of loosening to the bare
    # leaf `caller`, which would also drag in same-named methods on other classes.
    foo = "cxx . . $ mongo/Foo#caller(a0)."
    other = "cxx . . $ mongo/Bar#caller(a1)."
    graph = Graph()
    graph.nodes[foo] = Node(symbol=foo, file="f.cpp", line=1)
    graph.nodes[other] = Node(symbol=other, file="f.cpp", line=2)
    path = tmp_path / "colon.db"
    write_sqlite(graph, path)

    r = mcp_server.find_symbols(GraphStore(path), "Foo::caller")
    assert r["total"] == 1
    assert r["results"][0]["symbol"] == foo
    assert r["relaxed"] is True
    assert r["relaxed_query"] == "Foo#caller"
    assert "member separator" in r["note"]


def test_find_colon_relaxation_falls_through_when_qualified_misses(tmp_path: Path) -> None:
    # `Class::method` where neither spelling exists: the `::`->`#` retry misses
    # too, so the cascade continues (fuzzy, then bare leaf) exactly as before.
    free = "cxx . . $ mongo/change_stream/pipeline_helpers/buildPipeline(a1)."
    graph = Graph()
    graph.nodes[free] = Node(symbol=free, file="ph.cpp", line=1)
    path = tmp_path / "colon-miss.db"
    write_sqlite(graph, path)

    r = mcp_server.find_symbols(GraphStore(path), "Pipeline::buildPipeline")
    assert r["total"] == 1
    assert r["results"][0]["symbol"] == free
    assert r["relaxed"] is True
    assert r["relaxed_query"] == "buildPipeline"  # the leaf kind, not the colon kind
    assert "loosened" in r["note"]


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


def test_resolve_ambiguous_hint_flags_type_and_operator(tmp_path: Path) -> None:
    """Among lookalike candidates, the hint calls out the type itself (symbol
    ending in `#`) and a conversion operator sharing the name — still without
    picking any of them."""
    foo = "cxx . . $ mongo/Foo#"
    bar = "cxx . . $ mongo/Foo#bar(a0)."
    conv = "cxx . . $ mongo/OtherClass#operator Foo()(a0)."
    graph = Graph()
    graph.nodes[foo] = Node(symbol=foo, file="foo.h", line=1)
    graph.nodes[bar] = Node(symbol=bar, file="foo.h", line=2)
    graph.nodes[conv] = Node(symbol=conv, file="other.h", line=3)
    path = tmp_path / "lookalike.db"
    write_sqlite(graph, path)
    r = mcp_server.callers(GraphStore(path), "Foo")
    assert r.get("ambiguous") == "Foo"
    assert {"ambiguous", "total", "truncated", "candidates", "hint"} <= r.keys()
    assert "type itself" in r["hint"] and "Foo#" in r["hint"]
    assert "operator" in r["hint"]


def test_resolve_ambiguous_hint_plural_when_several_types_match(tmp_path: Path) -> None:
    """When 2+ type-shaped candidates match the query, the hint says several
    types share the name — it must not claim a single one ("use that one")."""
    foo1 = "cxx . . $ ns1/Foo#"
    foo2 = "cxx . . $ ns2/Foo#"
    bar = "cxx . . $ ns1/Foo#bar(a0)."
    graph = Graph()
    graph.nodes[foo1] = Node(symbol=foo1, file="a.h", line=1)
    graph.nodes[foo2] = Node(symbol=foo2, file="b.h", line=2)
    graph.nodes[bar] = Node(symbol=bar, file="a.h", line=3)
    path = tmp_path / "two_types.db"
    write_sqlite(graph, path)
    r = mcp_server.callers(GraphStore(path), "Foo")
    assert r.get("ambiguous") == "Foo"
    hint = r["hint"]
    assert "types themselves" in hint
    assert "ns1/Foo#" in hint and "ns2/Foo#" in hint
    assert "that one" not in hint


def test_resolve_unknown_name_errors(store: GraphStore) -> None:
    r = mcp_server.callers(store, "does_not_exist")
    assert "error" in r
