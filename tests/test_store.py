"""Tests for the Phase 2 SQLite-backed store (interned symbols, indexed topology).

The store must answer the exact same queries as the in-memory `Graph`
(callers/callees/find/path/impact) but off a SQLite file, without loading the
whole graph into RAM. See DESIGN.md § Store.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cppgraph.builder import build_graph
from cppgraph.model import Graph, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import (
    SCHEMA_VERSION,
    GraphStore,
    IncompatibleStoreError,
    build_provenance,
    changed_files_since,
    is_stale,
    read_dirty_fingerprints,
    update_store,
    write_sqlite,
)

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


def test_reachable_from_transitive_callees(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "entry", "mid", file="f.cpp", line=1)
    graph.add_edge("calls", "mid", "target", file="f.cpp", line=2)
    graph.add_edge("calls", "unrelated", "other", file="f.cpp", line=3)
    store = _store(tmp_path, graph)
    assert store.reachable_from("entry") == {"mid", "target"}


def test_reachable_from_respects_max_depth(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "entry", "mid", file="f.cpp", line=1)
    graph.add_edge("calls", "mid", "target", file="f.cpp", line=2)
    store = _store(tmp_path, graph)
    assert store.reachable_from("entry", max_depth=1) == {"mid"}
    assert store.reachable_from("entry", max_depth=2) == {"mid", "target"}


def test_reachable_from_cycle_terminates(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="f.cpp", line=1)
    graph.add_edge("calls", "b", "a", file="f.cpp", line=2)
    graph.add_edge("calls", "b", "c", file="f.cpp", line=3)
    store = _store(tmp_path, graph)
    assert store.reachable_from("a") == {"b", "c"}


def test_reachable_from_unknown_symbol_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path, Graph())
    assert store.reachable_from("nope") == set()


# --- hotspots ----------------------------------------------------------------


def _hotspots_graph() -> Graph:
    graph = Graph()
    # "hot" is called 5 times, "warm" 3 times, "cold" once.
    for i, caller in enumerate(["c1", "c2", "c3", "c4", "c5"]):
        graph.add_edge("calls", caller, "hot", file="f.cpp", line=i)
    for i, caller in enumerate(["c1", "c2", "c3"]):
        graph.add_edge("calls", caller, "warm", file="f.cpp", line=10 + i)
    graph.add_edge("calls", "c1", "cold", file="f.cpp", line=20)
    return graph


def test_hotspots_fan_in_ranks_by_incoming_call_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _hotspots_graph())
    ranked, total = store.hotspots(kind="fan_in")
    assert ranked[:3] == [("hot", 5), ("warm", 3), ("cold", 1)]
    assert total == 3


def test_hotspots_fan_out_ranks_by_outgoing_call_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _hotspots_graph())
    ranked, total = store.hotspots(kind="fan_out")
    # c1 calls hot+warm+cold = 3, c2/c3 call hot+warm = 2, c4/c5 call hot = 1.
    assert ranked[0] == ("c1", 3)
    assert total == 5


def test_hotspots_edges_sums_fan_in_and_fan_out(tmp_path: Path) -> None:
    store = _store(tmp_path, _hotspots_graph())
    ranked, _total = store.hotspots(kind="edges")
    by_symbol = dict(ranked)
    # "hot" has 5 incoming, 0 outgoing.
    assert by_symbol["hot"] == 5
    # "c1" has 0 incoming, 3 outgoing.
    assert by_symbol["c1"] == 3


def test_hotspots_edges_counts_a_self_loop_twice_like_graph_degree(tmp_path: Path) -> None:
    """A recursive symbol's own edge to itself is one edge but two structural
    facts (it's called, and it calls) — `edges` sums both, per the standard
    graph-degree convention for a self-loop, not a double-counting bug."""
    graph = Graph()
    graph.add_edge("calls", "recurse", "recurse", file="foo.cpp", line=1)
    store = _store(tmp_path, graph)
    ranked, _total = store.hotspots(kind="edges")
    assert ranked == [("recurse", 2)]
    fan_in, _ = store.hotspots(kind="fan_in")
    fan_out, _ = store.hotspots(kind="fan_out")
    assert fan_in == [("recurse", 1)]
    assert fan_out == [("recurse", 1)]


def test_hotspots_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _hotspots_graph())
    with pytest.raises(ValueError):
        store.hotspots(limit=-1, kind="fan_in")


def test_hotspots_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _hotspots_graph())
    ranked, total = store.hotspots(limit=1, kind="fan_in")
    assert ranked == [("hot", 5)]
    assert total == 3


def test_hotspots_exclude_tests_drops_edges_touching_a_test_defined_symbol(
    tmp_path: Path,
) -> None:
    """`exclude_tests` follows the same convention as `filters.drop_test_edges`:
    a symbol's own *definition* file decides test-ness, not the call site."""
    graph = Graph()
    graph.add_edge("calls", "prod_caller", "target", file="src/foo.cpp", line=1)
    graph.add_edge("calls", "test_helper_caller", "target", file="src/foo.cpp", line=2)
    graph.nodes["test_helper_caller"].file = "src/foo_test.cpp"
    store = _store(tmp_path, graph)
    ranked, total = store.hotspots(kind="fan_in", exclude_tests=True)
    assert ranked == [("target", 1)]
    assert total == 1


def test_hotspots_exclude_paths_drops_edges_touching_a_vendored_symbol(
    tmp_path: Path,
) -> None:
    """`exclude_paths` mirrors `exclude_tests`' symmetric shape: an edge counts
    only if *both* endpoints' definition files pass the prefix filter."""
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "target", file="src/myproject/foo.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "target", file="src/myproject/foo.cpp", line=2)
    graph.nodes["proj_caller"].file = "src/myproject/foo.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/foo.cpp"
    graph.nodes["target"].file = "src/myproject/foo.cpp"
    store = _store(tmp_path, graph)
    ranked, total = store.hotspots(kind="fan_in", exclude_paths=["vendor/"])
    assert ranked == [("target", 1)]
    assert total == 1


def test_hotspots_include_paths_keeps_only_project_edges(
    tmp_path: Path,
) -> None:
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "target", file="src/myproject/foo.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "target", file="src/myproject/foo.cpp", line=2)
    graph.nodes["proj_caller"].file = "src/myproject/foo.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/foo.cpp"
    graph.nodes["target"].file = "src/myproject/foo.cpp"
    store = _store(tmp_path, graph)
    ranked, total = store.hotspots(kind="fan_in", include_paths=["src/myproject/"])
    assert ranked == [("target", 1)]
    assert total == 1


def test_hotspots_unknown_kind_raises(tmp_path: Path) -> None:
    store = _store(tmp_path, Graph())
    with pytest.raises(ValueError):
        store.hotspots(kind="bogus")


def _target_paths_graph() -> Graph:
    graph = Graph()
    # Call sites into the "library" under spirv_cross/ from a project caller
    # and a vendored caller, plus one project-internal edge — the
    # asymmetric-filter fixture (callee definition under the target prefix,
    # callers anywhere).
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


def test_hotspots_target_paths_counts_cross_boundary_call_sites(tmp_path: Path) -> None:
    """The asymmetric mode: only the callee side is pinned to the target
    prefix, so cross-boundary call sites (project/vendored code calling INTO
    the library) are counted — the question symmetric filtering cannot ask."""
    store = _store(tmp_path, _target_paths_graph())
    ranked, total = store.hotspots(kind="fan_in", target_paths=["spirv_cross/"])
    assert ranked == [("lib_hot", 2), ("lib_warm", 1)]
    assert total == 2  # proj_helper (non-target callee) is not counted
    # The symmetric mode answers a *different* question: with BOTH endpoints
    # required under the prefix, no edge survives (none is library-internal) —
    # which is why TODO.md's "falls out of hotspots + path filtering for free"
    # needed this real extension to the filter model, not just wiring.
    sym_ranked, sym_total = store.hotspots(kind="fan_in", include_paths=["spirv_cross/"])
    assert sym_ranked == []
    assert sym_total == 0


def test_hotspots_target_paths_path_filters_apply_to_caller_side_only(
    tmp_path: Path,
) -> None:
    """In target mode, include/exclude filter the CALLER side only — e.g.
    "count call sites into the library, but not from other vendored code that
    also uses it" — never the target side (that's target_paths' job)."""
    store = _store(tmp_path, _target_paths_graph())
    # Exclude vendored callers: lib_warm loses its only caller and drops out.
    ranked, total = store.hotspots(
        kind="fan_in", target_paths=["spirv_cross/"], exclude_paths=["vendor/"]
    )
    assert ranked == [("lib_hot", 1)]
    assert total == 1
    # Include only project callers: same answer from the other side.
    ranked, total = store.hotspots(
        kind="fan_in", target_paths=["spirv_cross/"], include_paths=["src/myproject/"]
    )
    assert ranked == [("lib_hot", 1)]
    assert total == 1


def test_hotspots_target_paths_exclude_tests_drops_test_defined_callers(
    tmp_path: Path,
) -> None:
    """`exclude_tests` keeps its symmetric either-endpoint shape in target
    mode (a test-DEFINED symbol on either end drops the edge), unchanged from
    plain hotspots."""
    graph = _target_paths_graph()
    graph.add_edge("calls", "test_caller", "lib_hot", file="src/myproject/foo.cpp", line=9)
    graph.nodes["test_caller"].file = "src/myproject/foo_test.cpp"
    store = _store(tmp_path, graph)
    ranked, _total = store.hotspots(kind="fan_in", target_paths=["spirv_cross/"])
    assert ranked == [("lib_hot", 3), ("lib_warm", 1)]
    ranked, _total = store.hotspots(
        kind="fan_in", target_paths=["spirv_cross/"], exclude_tests=True
    )
    assert ranked == [("lib_hot", 2), ("lib_warm", 1)]


def test_hotspots_target_paths_requires_fan_in_kind(tmp_path: Path) -> None:
    """`target_paths` counts *incoming* call sites into the library, so the
    other kinds are a nonsensical combination — rejected explicitly, the same
    defensive-validation style as the unknown-kind ValueError above."""
    store = _store(tmp_path, _target_paths_graph())
    for kind in ("fan_out", "edges"):
        with pytest.raises(ValueError):
            store.hotspots(kind=kind, target_paths=["spirv_cross/"])


def test_hotspots_target_paths_none_reproduces_symmetric_behavior(
    tmp_path: Path,
) -> None:
    """Backward compatibility: `target_paths=None` (the default) keeps today's
    symmetric filter semantics exactly — the explicit None is indistinguishable
    from not passing it, across every filter combination."""
    store = _store(tmp_path, _target_paths_graph())
    for kwargs in (
        {},
        {"exclude_paths": ["vendor/"]},
        {"include_paths": ["src/myproject/"]},
        {"exclude_tests": True},
        {"kind": "fan_out"},
        {"kind": "edges"},
    ):
        assert store.hotspots(target_paths=None, **kwargs) == store.hotspots(**kwargs)
    # And a pinned concrete result: symmetric exclude_paths drops every edge
    # touching the vendored endpoint, whichever side of the edge it is on.
    ranked, total = store.hotspots(kind="fan_in", exclude_paths=["vendor/"])
    assert dict(ranked) == {"lib_hot": 1, "proj_helper": 1}
    assert total == 2


def test_hotspots_limit_none_returns_the_full_ranking(tmp_path: Path) -> None:
    """`limit=None` = no cap, so a caller can aggregate over the whole ranking
    (dependency_cost sums it) — same rows, same order, just unsliced."""
    store = _store(tmp_path, _target_paths_graph())
    ranked, total = store.hotspots(limit=None, kind="fan_in")
    assert len(ranked) == total == 3


# --- stats -------------------------------------------------------------------


def _stats_graph() -> Graph:
    graph = Graph()
    # src/big.cpp: 2 symbols, 2 `calls` call sites (the `inherits` edge must
    # not count), 3 ref use sites -> total 7.
    graph.nodes["big1"] = Node(symbol="big1", file="src/big.cpp", line=1)
    graph.nodes["big2"] = Node(symbol="big2", file="src/big.cpp", line=2)
    graph.add_edge("calls", "big1", "big2", file="src/big.cpp", line=5)
    graph.add_edge("calls", "big1", "helper", file="src/big.cpp", line=6)
    graph.add_edge("inherits", "big2", "base", file="src/big.cpp", line=9)
    for line in (10, 11, 12):
        graph.add_reference("TYPE", "src/big.cpp", line)
    # src/small.cpp: 1 symbol, 1 call site, no refs -> total 2.
    graph.nodes["small"] = Node(symbol="small", file="src/small.cpp", line=1)
    graph.add_edge("calls", "small", "helper", file="src/small.cpp", line=2)
    # Top level and vendored files: 1 symbol each, nothing else -> total 1.
    graph.nodes["root"] = Node(symbol="root", file="root.cpp", line=1)
    graph.nodes["vend"] = Node(symbol="vend", file="vendor/tiny.cpp", line=1)
    return graph


def test_stats_per_file_counts_symbols_calls_edges_and_refs(tmp_path: Path) -> None:
    store = _store(tmp_path, _stats_graph())
    groups, total = store.stats(group_by="file")
    assert total == 4
    assert groups[0] == {"file": "src/big.cpp", "symbols": 2, "edges": 2, "refs": 3}
    by_file = {g["file"]: g for g in groups}
    assert by_file["src/small.cpp"] == {
        "file": "src/small.cpp",
        "symbols": 1,
        "edges": 1,
        "refs": 0,
    }
    assert by_file["root.cpp"] == {"file": "root.cpp", "symbols": 1, "edges": 0, "refs": 0}


def test_stats_dir_rollup_sums_per_directory(tmp_path: Path) -> None:
    store = _store(tmp_path, _stats_graph())
    groups, total = store.stats(group_by="dir")
    assert total == 3  # src, vendor, "."
    assert groups[0] == {"dir": "src", "symbols": 3, "edges": 3, "refs": 3}
    by_dir = {g["dir"]: g for g in groups}
    assert by_dir["."] == {"dir": ".", "symbols": 1, "edges": 0, "refs": 0}
    assert by_dir["vendor"] == {"dir": "vendor", "symbols": 1, "edges": 0, "refs": 0}


def test_stats_exclude_paths_drops_vendored_files(tmp_path: Path) -> None:
    store = _store(tmp_path, _stats_graph())
    groups, total = store.stats(group_by="file", exclude_paths=["vendor/"])
    assert total == 3
    assert {g["file"] for g in groups} == {"src/big.cpp", "src/small.cpp", "root.cpp"}


def test_stats_include_paths_keeps_only_scoped_files(tmp_path: Path) -> None:
    store = _store(tmp_path, _stats_graph())
    groups, total = store.stats(group_by="file", include_paths=["src/"])
    assert total == 2
    assert {g["file"] for g in groups} == {"src/big.cpp", "src/small.cpp"}


def test_stats_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _stats_graph())
    groups, total = store.stats(group_by="file", limit=1)
    assert groups == [{"file": "src/big.cpp", "symbols": 2, "edges": 2, "refs": 3}]
    assert total == 4


def test_stats_unknown_group_by_raises(tmp_path: Path) -> None:
    store = _store(tmp_path, Graph())
    with pytest.raises(ValueError):
        store.stats(group_by="bogus")


def test_stats_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, Graph())
    with pytest.raises(ValueError):
        store.stats(limit=-1)


# --- boundary_violations -----------------------------------------------------


def _layered_graph() -> Graph:
    """A layering that conforms: `common/` is the base, `platform/` builds on
    it, `projects/` on that — calls only point down toward the base, so the
    rules "common must not call platform/projects, platform must not call
    projects" all hold."""
    graph = Graph()
    graph.add_edge("calls", "platform_fn", "common_fn", file="platform/io.cpp", line=1)
    graph.add_edge("calls", "projects_fn", "platform_fn", file="projects/main.cpp", line=2)
    graph.nodes["common_fn"].file = "common/util.cpp"
    graph.nodes["platform_fn"].file = "platform/io.cpp"
    graph.nodes["projects_fn"].file = "projects/main.cpp"
    return graph


def test_boundary_violations_clean_layering_reports_none(tmp_path: Path) -> None:
    """Every edge points the allowed way (down toward the base); all three
    forbidden-direction rules hold, so the tool reports exactly nothing."""
    store = _store(tmp_path, _layered_graph())
    rules = [("common/", "platform/"), ("platform/", "projects/"), ("common/", "projects/")]
    violations, total = store.boundary_violations(rules)
    assert (violations, total) == ([], 0)


def test_boundary_violations_reports_an_edge_that_crosses(tmp_path: Path) -> None:
    graph = _layered_graph()
    graph.add_edge("calls", "common_fn", "platform_secret", file="common/util.cpp", line=9)
    graph.nodes["platform_secret"].file = "platform/hidden.cpp"
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations([("common/", "platform/")])
    assert violations == [
        {
            "kind": "calls",
            "src": "common_fn",
            "dst": "platform_secret",
            "file": "common/util.cpp",
            "line": 9,
            "rule": "common/ -> platform/",
        }
    ]
    assert total == 1


def test_boundary_violations_prefix_matches_on_segment_boundaries(tmp_path: Path) -> None:
    """The `matches_path_prefix` contract: `common/` never matches a sibling
    directory that merely shares characters (`commons/`), so an edge out of
    `commons/` is not an edge out of `common/`."""
    graph = Graph()
    graph.add_edge("calls", "sibling_fn", "plat_fn", file="commons/util.cpp", line=1)
    graph.nodes["sibling_fn"].file = "commons/util.cpp"
    graph.nodes["plat_fn"].file = "platform/io.cpp"
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations([("common/", "platform/")])
    assert (violations, total) == ([], 0)  # commons/ is not under common/
    # the exact sibling prefix does fire, proving the zero above is the prefix
    # semantics, not a dead query
    violations, total = store.boundary_violations([("commons/", "platform/")])
    assert total == 1


def test_boundary_violations_ignores_symbols_without_definition_file(tmp_path: Path) -> None:
    """A symbol with no recorded definition site belongs to no layer: it can
    be neither the from- nor the forbidden-side of a violation."""
    graph = _layered_graph()
    graph.add_edge("calls", "nodef_src", "platform_fn", file="common/util.cpp", line=5)
    graph.add_edge("calls", "common_fn", "nodef_dst", file="common/util.cpp", line=6)
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations([("common/", "platform/")])
    assert (violations, total) == ([], 0)


def test_boundary_violations_inherits_edges_checked_by_default(tmp_path: Path) -> None:
    graph = _layered_graph()
    graph.add_edge("inherits", "CommonWidget", "PlatformBase", file="common/widget.h", line=3)
    graph.nodes["CommonWidget"].file = "common/widget.h"
    graph.nodes["PlatformBase"].file = "platform/base.h"
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations([("common/", "platform/")])
    assert total == 1
    assert violations[0]["kind"] == "inherits"
    # edge_kinds restricts what is checked
    calls_only, calls_total = store.boundary_violations(
        [("common/", "platform/")], edge_kinds=("calls",)
    )
    assert (calls_only, calls_total) == ([], 0)


def test_boundary_violations_edge_matching_two_rules_reported_once_per_rule(
    tmp_path: Path,
) -> None:
    """A general rule and a stricter sub-layer rule both fire on the same edge:
    one record per rule, each naming the rule it broke (rule-major order)."""
    graph = Graph()
    graph.add_edge("calls", "sub_fn", "plat_fn", file="common/sub/util.cpp", line=4)
    graph.nodes["sub_fn"].file = "common/sub/util.cpp"
    graph.nodes["plat_fn"].file = "platform/io.cpp"
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations(
        [("common/", "platform/"), ("common/sub/", "platform/")]
    )
    assert total == 2
    assert [v["rule"] for v in violations] == [
        "common/ -> platform/",
        "common/sub/ -> platform/",
    ]
    assert {v["src"] for v in violations} == {"sub_fn"}
    assert {v["dst"] for v in violations} == {"plat_fn"}


def test_boundary_violations_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    graph = _layered_graph()
    for i in range(3):
        graph.add_edge(
            "calls", f"common_fn{i}", "platform_secret", file="common/util.cpp", line=10 + i
        )
        graph.nodes[f"common_fn{i}"].file = "common/util.cpp"
    graph.nodes["platform_secret"].file = "platform/hidden.cpp"
    store = _store(tmp_path, graph)
    violations, total = store.boundary_violations([("common/", "platform/")], limit=2)
    assert len(violations) == 2
    assert total == 3


def test_boundary_violations_rejects_bad_input(tmp_path: Path) -> None:
    """Defensive validation in the `hotspots` unknown-kind style: a malformed
    rule must raise, not silently match nothing (a false 'layering holds')."""
    store = _store(tmp_path, _layered_graph())
    with pytest.raises(ValueError):  # no rules at all
        store.boundary_violations([])
    with pytest.raises(ValueError):  # a layer forbidden to itself
        store.boundary_violations([("common/", "common/")])
    with pytest.raises(ValueError):  # same after prefix normalization
        store.boundary_violations([("common", "common/")])
    with pytest.raises(ValueError):  # empty prefix matches nothing
        store.boundary_violations([("", "platform/")])
    with pytest.raises(ValueError):  # not a (from, forbidden) pair
        store.boundary_violations([("common/",)])
    with pytest.raises(ValueError):
        store.boundary_violations([("common/", "platform/")], limit=-1)
    with pytest.raises(ValueError):  # empty kinds would silently match nothing
        store.boundary_violations([("common/", "platform/")], edge_kinds=())
    with pytest.raises(ValueError):  # a typo'd kind must not read as "clean"
        store.boundary_violations([("common/", "platform/")], edge_kinds=("telepathy",))


# --- api_surface ---------------------------------------------------------------


def _module_graph(with_refs: bool = True) -> Graph:
    """`mod/` is the module under inspection; `app/` is outside code using it.

    - `internal_fn` (mod/): called only from inside mod/ — not on the surface.
    - `called_fn` (mod/): one external call from `app/`, one internal call.
    - `refd_type` (mod/): one external reference from `app/`, one internal.
    - `both_fn` (mod/): two external calls + one external reference — ranks first.
    """
    graph = Graph()
    graph.add_edge("calls", "internal_fn", "called_fn", file="mod/util.cpp", line=10)
    graph.add_edge("calls", "app_main", "called_fn", file="app/main.cpp", line=5)
    graph.add_edge("calls", "app_main", "both_fn", file="app/main.cpp", line=6)
    graph.add_edge("calls", "app_other", "both_fn", file="app/other.cpp", line=7)
    graph.nodes["internal_fn"].file = "mod/util.cpp"
    graph.nodes["called_fn"].file = "mod/util.cpp"
    graph.nodes["both_fn"].file = "mod/api.cpp"
    graph.nodes["both_fn"].line = 4
    graph.nodes["app_main"].file = "app/main.cpp"
    graph.nodes["app_other"].file = "app/other.cpp"
    graph.nodes["refd_type"] = Node(symbol="refd_type", file="mod/types.h", line=3)
    if with_refs:
        graph.add_reference("refd_type", "app/main.cpp", 20)
        graph.add_reference("refd_type", "mod/util.cpp", 30)  # internal: never counts
        graph.add_reference("both_fn", "app/main.cpp", 21)
    return graph


def test_api_surface_ranks_by_sum_with_calls_and_refs_shown_separately(
    tmp_path: Path,
) -> None:
    """Separate counters per symbol (the `stats` convention), ranked by their
    sum: `both_fn` (2 calls + 1 ref = 3) first, then the two 1-use symbols."""
    store = _store(tmp_path, _module_graph())
    ranked, total, has_refs = store.api_surface("mod/")
    assert [r["symbol"] for r in ranked] == ["both_fn", "called_fn", "refd_type"]
    assert ranked[0] == {
        "symbol": "both_fn",
        "file": "mod/api.cpp",
        "line": 4,
        "external_calls": 2,
        "external_refs": 1,
    }
    by_symbol = {r["symbol"]: r for r in ranked}
    assert by_symbol["called_fn"]["external_calls"] == 1  # calls-only external use
    assert by_symbol["called_fn"]["external_refs"] == 0
    assert by_symbol["refd_type"]["external_calls"] == 0  # refs-only external use
    assert by_symbol["refd_type"]["external_refs"] == 1
    assert total == 3
    assert has_refs is True


def test_api_surface_symbol_used_only_internally_is_excluded(tmp_path: Path) -> None:
    """The surface is *external* uses: `internal_fn` (called only from inside
    `mod/`) never appears, and the internal call/reference to `called_fn` /
    `refd_type` counted for nothing (each still shows exactly its 1 external)."""
    store = _store(tmp_path, _module_graph())
    ranked, total, _ = store.api_surface("mod/")
    assert "internal_fn" not in {r["symbol"] for r in ranked}
    by_symbol = {r["symbol"]: r for r in ranked}
    assert by_symbol["called_fn"]["external_calls"] == 1
    assert by_symbol["refd_type"]["external_refs"] == 1
    assert total == 3


def test_api_surface_without_refs_data_degrades_to_calls_only(tmp_path: Path) -> None:
    """A store built `--no-references` has no refs to count: the surface is
    call sites only, and the caller is told so (`has_refs` False) rather than
    reading `external_refs == 0` as 'never referenced outside'."""
    store = _store(tmp_path, _module_graph(with_refs=False))
    ranked, total, has_refs = store.api_surface("mod/")
    assert has_refs is False
    assert [r["symbol"] for r in ranked] == ["both_fn", "called_fn"]
    assert all(r["external_refs"] == 0 for r in ranked)
    assert total == 2


def test_api_surface_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _module_graph())
    ranked, total, _ = store.api_surface("mod/", limit=1)
    assert [r["symbol"] for r in ranked] == ["both_fn"]
    assert total == 3


def test_api_surface_exclude_tests_drops_test_uses_on_either_side(
    tmp_path: Path,
) -> None:
    """`exclude_tests` drops a use when *either* side of the boundary is a test
    file: a test calling into the module, and a test-defined symbol inside the
    module used from outside — neither is production surface."""
    graph = _module_graph()
    graph.add_edge("calls", "test_main", "called_fn", file="src/t_test.cpp", line=1)
    graph.nodes["test_main"] = Node(symbol="test_main", file="src/t_test.cpp", line=1)
    graph.add_edge("calls", "app_main", "test_helper", file="app/main.cpp", line=8)
    graph.nodes["test_helper"] = Node(symbol="test_helper", file="mod/util_test.cpp", line=1)
    store = _store(tmp_path, graph)
    ranked, _, _ = store.api_surface("mod/", exclude_tests=True)
    by_symbol = {r["symbol"]: r for r in ranked}
    assert by_symbol["called_fn"]["external_calls"] == 1  # the app/ call; tests/ dropped
    assert "test_helper" not in by_symbol  # its own definition file is a test file


def test_api_surface_prefix_matches_on_segment_boundaries(tmp_path: Path) -> None:
    """The `matches_path_prefix` contract: `mod/` never matches a sibling that
    merely shares characters (`mods/`)."""
    graph = _module_graph()
    graph.add_edge("calls", "sib_main", "sib_fn", file="app/main.cpp", line=9)
    graph.nodes["sib_fn"].file = "mods/util.cpp"
    store = _store(tmp_path, graph)
    ranked, _, _ = store.api_surface("mod/")
    assert "sib_fn" not in {r["symbol"] for r in ranked}
    ranked, total, _ = store.api_surface("mods/")
    assert [r["symbol"] for r in ranked] == ["sib_fn"]
    assert total == 1


def test_api_surface_rejects_bad_input(tmp_path: Path) -> None:
    """Defensive validation in the `boundary_violations` style: an empty prefix
    (which would match nothing) must raise, never read as 'empty surface'."""
    store = _store(tmp_path, _module_graph())
    with pytest.raises(ValueError):  # empty prefix matches nothing
        store.api_surface("")
    with pytest.raises(ValueError):  # same after separator normalization
        store.api_surface("/")
    with pytest.raises(ValueError):
        store.api_surface("mod/", limit=-1)


# --- outline / class_members -------------------------------------------------

FOO = "cxx . . $ mongo/Foo#"
FOO_PARSE = "cxx . . $ mongo/Foo#parse(a1)."  # method
FOO_COUNT = "cxx . . $ mongo/Foo#count."  # field (a term descriptor)
FOO_INNER = "cxx . . $ mongo/Foo#Inner#"  # nested type
MAKE_FOO = "cxx . . $ mongo/makeFoo(a2)."  # free function in the same file
FOOBAR = "cxx . . $ mongo/FooBar#"
FOOBAR_PARSE = "cxx . . $ mongo/FooBar#parse(a3)."
INNER_METHOD = "cxx . . $ mongo/Foo#Inner#method()."  # Inner's own method (1 level nested)
INNER_INNERMOST = "cxx . . $ mongo/Foo#Inner#Innermost#"  # doubly-nested type's own symbol
INNERMOST_METHOD = "cxx . . $ mongo/Foo#Inner#Innermost#method()."  # doubly-nested method


def _container_graph() -> Graph:
    """One file (`mongo/foo.h`) holding class `Foo` (a method, a field, a
    nested type), a free function, and the sibling class `FooBar` whose
    members must not leak into `Foo`'s. Added out of line order so the
    store's ORDER BY line is actually exercised."""
    graph = Graph()
    for symbol, line in (
        (MAKE_FOO, 30),
        (FOOBAR_PARSE, 41),
        (FOO, 10),
        (FOO_INNER, 13),
        (FOOBAR, 40),
        (FOO_COUNT, 12),
        (FOO_PARSE, 11),
    ):
        graph.add_node(symbol)
        graph.nodes[symbol].file = "mongo/foo.h"
        graph.nodes[symbol].line = line
    return graph


def test_outline_lists_definitions_in_line_order(tmp_path: Path) -> None:
    store = _store(tmp_path, _container_graph())
    nodes, total = store.outline("mongo/foo.h")
    assert total == 7
    assert [n.line for n in nodes] == [10, 11, 12, 13, 30, 40, 41]
    assert nodes[0].symbol == FOO
    assert nodes[0].file == "mongo/foo.h"


def test_outline_exact_path_match_not_prefix(tmp_path: Path) -> None:
    """The file argument is an exact path match, never a prefix: a misspelled
    or unindexed path is an empty outline, not a fuzzy guess."""
    store = _store(tmp_path, _container_graph())
    assert store.outline("mongo/foo") == ([], 0)
    assert store.outline("mongo/other.cpp") == ([], 0)


def test_outline_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _container_graph())
    nodes, total = store.outline("mongo/foo.h", limit=3)
    assert total == 7
    assert [n.line for n in nodes] == [10, 11, 12]


def test_outline_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _container_graph())
    with pytest.raises(ValueError):
        store.outline("mongo/foo.h", limit=-1)


def test_outline_normalizes_backslashes_in_the_lookup_path(tmp_path: Path) -> None:
    """A path given with Windows-style separators must find the same file as
    one given with `/`, matching `filters.matches_path_prefix`'s normalization."""
    store = _store(tmp_path, _container_graph())
    nodes, total = store.outline("mongo\\foo.h")
    assert total == 7
    assert nodes[0].symbol == FOO


def test_class_members_lists_methods_fields_and_nested_types(tmp_path: Path) -> None:
    """Members are the symbols whose SCIP string starts with the class's own
    (container nesting): method, field, nested type — sorted by line, and the
    class itself is never its own member."""
    store = _store(tmp_path, _container_graph())
    members, total = store.class_members(FOO)
    assert [m.symbol for m in members] == [FOO_PARSE, FOO_COUNT, FOO_INNER]
    assert total == 3
    assert all(m.file == "mongo/foo.h" for m in members)


def test_class_members_does_not_confuse_foo_with_foobar(tmp_path: Path) -> None:
    """The container boundary is the class's own `#` descriptor: `mongo/Foo#`
    prefixes `mongo/Foo#parse(...)` but never `mongo/FooBar#parse(...)` (after
    `Foo` comes `B`, not `#`) — and symmetrically for `FooBar`."""
    store = _store(tmp_path, _container_graph())
    foo_members, _ = store.class_members(FOO)
    assert FOOBAR_PARSE not in [m.symbol for m in foo_members]
    foobar_members, foobar_total = store.class_members(FOOBAR)
    assert [m.symbol for m in foobar_members] == [FOOBAR_PARSE]
    assert foobar_total == 1
    assert FOO_PARSE not in [m.symbol for m in foobar_members]


def test_class_members_unknown_symbol_returns_none(tmp_path: Path) -> None:
    """Unknown symbol is bad input (None), not an empty list that would read
    as 'a class with no members'."""
    store = _store(tmp_path, _container_graph())
    assert store.class_members("cxx . . $ mongo/Nope#") is None


def test_class_members_non_type_symbol_returns_none(tmp_path: Path) -> None:
    """A method or a free function is not a container: None (bad input), not
    an empty member list."""
    store = _store(tmp_path, _container_graph())
    assert store.class_members(FOO_PARSE) is None
    assert store.class_members(MAKE_FOO) is None


def test_class_members_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _container_graph())
    members, total = store.class_members(FOO, limit=1)
    assert total == 3
    assert [m.symbol for m in members] == [FOO_PARSE]


def test_class_members_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _container_graph())
    with pytest.raises(ValueError):
        store.class_members(FOO, limit=-1)


def _nested_container_graph() -> Graph:
    """`Foo` contains `Inner` (one level), which contains `Innermost` (two
    levels) — pins the direct-member rule against 3 levels of nesting."""
    graph = _container_graph()
    for symbol, line in (
        (INNER_METHOD, 14),
        (INNER_INNERMOST, 15),
        (INNERMOST_METHOD, 16),
    ):
        graph.add_node(symbol)
        graph.nodes[symbol].file = "mongo/foo.h"
        graph.nodes[symbol].line = line
    return graph


def test_class_members_excludes_nested_class_members_but_includes_its_own_symbol(
    tmp_path: Path,
) -> None:
    """`class_members(Foo)` must not leak `Foo::Inner`'s or
    `Foo::Inner::Innermost`'s own members (the bug), but DOES include
    `Inner`'s own type symbol as one of `Foo`'s direct members (the documented
    choice: a nested type's symbol is a direct member of its enclosing class;
    what's declared inside it is not)."""
    store = _store(tmp_path, _nested_container_graph())
    members, total = store.class_members(FOO)
    symbols = [m.symbol for m in members]
    assert symbols == [FOO_PARSE, FOO_COUNT, FOO_INNER]
    assert total == 3
    assert INNER_METHOD not in symbols
    assert INNER_INNERMOST not in symbols
    assert INNERMOST_METHOD not in symbols


def test_class_members_of_singly_nested_class_excludes_doubly_nested_members(
    tmp_path: Path,
) -> None:
    """`class_members(Foo::Inner)` lists `Inner`'s own direct members —
    including `Innermost`'s own type symbol — but not `Innermost`'s members."""
    store = _store(tmp_path, _nested_container_graph())
    members, total = store.class_members(FOO_INNER)
    symbols = [m.symbol for m in members]
    assert symbols == [INNER_METHOD, INNER_INNERMOST]
    assert total == 2
    assert INNERMOST_METHOD not in symbols


# --- strongly_connected_components -------------------------------------------


def _scc_graph() -> Graph:
    """Two disjoint cycles — a<->b (2 nodes) and x->y->z->x (3 nodes) — plus a
    one-way bridge a->x (must NOT merge them: x cannot reach back to a), an
    acyclic caller of a cycle, and a directly self-recursive symbol (a
    degenerate 1-node cycle, out of this tool's scope)."""
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="src/a.cpp", line=1)
    graph.add_edge("calls", "b", "a", file="src/a.cpp", line=2)
    graph.add_edge("calls", "x", "y", file="src/x.cpp", line=1)
    graph.add_edge("calls", "y", "z", file="src/x.cpp", line=2)
    graph.add_edge("calls", "z", "x", file="src/x.cpp", line=3)
    graph.add_edge("calls", "a", "x", file="src/a.cpp", line=3)  # one-way bridge
    graph.add_edge("calls", "spur", "x", file="src/a.cpp", line=4)  # acyclic caller
    graph.add_edge("calls", "selfrec", "selfrec", file="src/s.cpp", line=1)
    for symbol, file, line in (
        ("a", "src/a.cpp", 10),
        ("b", "src/a.cpp", 11),
        ("x", "src/x.cpp", 20),
        ("y", "src/x.cpp", 21),
        ("z", "src/x.cpp", 22),
        ("spur", "src/a.cpp", 5),
        ("selfrec", "src/s.cpp", 30),
    ):
        graph.nodes[symbol].file = file
        graph.nodes[symbol].line = line
    return graph


def test_strongly_connected_components_reports_cycles_biggest_first(
    tmp_path: Path,
) -> None:
    """Both cycles found, sorted biggest first; the one-way bridge between
    them does not merge two SCCs into one (x cannot reach a); members are
    sorted by definition file:line; neither the acyclic caller nor the
    self-loop surfaces as a component."""
    store = _store(tmp_path, _scc_graph())
    components, total = store.strongly_connected_components()
    assert components == [["x", "y", "z"], ["a", "b"]]
    assert total == 2


def test_strongly_connected_components_no_cycles_is_empty(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="f.cpp", line=1)
    graph.add_edge("calls", "b", "c", file="f.cpp", line=2)
    store = _store(tmp_path, graph)
    assert store.strongly_connected_components() == ([], 0)


def test_strongly_connected_components_self_loop_is_not_a_reportable_component(
    tmp_path: Path,
) -> None:
    """Direct self-recursion is a degenerate 1-node cycle: per the tool's spec
    (components of size > 1) it is out of scope, not conflated into a
    'component' — `hotspots`' `edges` kind already surfaces self-loops."""
    graph = Graph()
    graph.add_edge("calls", "selfrec", "selfrec", file="f.cpp", line=1)
    store = _store(tmp_path, graph)
    assert store.strongly_connected_components() == ([], 0)


def test_strongly_connected_components_exclude_tests_drops_all_test_cycle(
    tmp_path: Path,
) -> None:
    """Filters apply to the OUTPUT, not the edges Tarjan sees: a component is
    dropped only when every member is defined in a test file — a test-only
    cycle is still a real cycle, but not one this filtered view reports."""
    graph = _scc_graph()
    graph.add_edge("calls", "t1", "t2", file="src/t.cpp", line=1)
    graph.add_edge("calls", "t2", "t1", file="src/t.cpp", line=2)
    graph.nodes["t1"].file = "src/handler_test.cpp"
    graph.nodes["t1"].line = 1
    graph.nodes["t2"].file = "src/handler_test.cpp"
    graph.nodes["t2"].line = 2
    store = _store(tmp_path, graph)
    components, total = store.strongly_connected_components(exclude_tests=True)
    assert components == [["x", "y", "z"], ["a", "b"]]
    assert total == 2


def test_strongly_connected_components_reports_a_mixed_component_whole(
    tmp_path: Path,
) -> None:
    """A cycle with a MIX of test and production members is still a real cycle
    in the compiled binary: it is reported (at least one member survives the
    filter) and reported WHOLE — redacting the filtered member would
    misrepresent the actual dependency."""
    graph = Graph()
    graph.add_edge("calls", "prod", "twin", file="src/core.cpp", line=1)
    graph.add_edge("calls", "twin", "prod", file="src/t.cpp", line=2)
    graph.nodes["prod"].file = "src/core.cpp"
    graph.nodes["prod"].line = 10
    graph.nodes["twin"].file = "src/handler_test.cpp"
    graph.nodes["twin"].line = 3
    store = _store(tmp_path, graph)
    components, total = store.strongly_connected_components(exclude_tests=True)
    assert components == [["prod", "twin"]]  # core.cpp:10 before handler_test.cpp:3
    assert total == 1


def test_strongly_connected_components_exclude_paths_drops_vendored_cycle(
    tmp_path: Path,
) -> None:
    graph = _scc_graph()
    graph.add_edge("calls", "v1", "v2", file="v.cpp", line=1)
    graph.add_edge("calls", "v2", "v1", file="v.cpp", line=2)
    graph.nodes["v1"].file = "vendor/lib/v.cpp"
    graph.nodes["v1"].line = 1
    graph.nodes["v2"].file = "vendor/lib/v.cpp"
    graph.nodes["v2"].line = 2
    store = _store(tmp_path, graph)
    components, total = store.strongly_connected_components(exclude_paths=["vendor/"])
    assert components == [["x", "y", "z"], ["a", "b"]]
    assert total == 2


def test_strongly_connected_components_include_paths_keeps_only_project_cycles(
    tmp_path: Path,
) -> None:
    graph = _scc_graph()
    graph.add_edge("calls", "v1", "v2", file="v.cpp", line=1)
    graph.add_edge("calls", "v2", "v1", file="v.cpp", line=2)
    graph.nodes["v1"].file = "vendor/lib/v.cpp"
    graph.nodes["v1"].line = 1
    graph.nodes["v2"].file = "vendor/lib/v.cpp"
    graph.nodes["v2"].line = 2
    store = _store(tmp_path, graph)
    components, total = store.strongly_connected_components(include_paths=["src/"])
    assert components == [["x", "y", "z"], ["a", "b"]]
    assert total == 2


def test_strongly_connected_components_limit_truncates_but_total_is_full_count(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, _scc_graph())
    components, total = store.strongly_connected_components(limit=1)
    assert components == [["x", "y", "z"]]
    assert total == 2


def test_strongly_connected_components_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _scc_graph())
    with pytest.raises(ValueError):
        store.strongly_connected_components(limit=-1)


def test_strongly_connected_components_deep_chain_does_not_recursion_error(
    tmp_path: Path,
) -> None:
    """Tarjan runs iteratively: a 3000-deep edge chain (deeper than Python's
    ~1000 default recursion limit) must not raise RecursionError — the reason
    the textbook recursive form is not used here."""
    graph = Graph()
    for i in range(3000):
        graph.add_edge("calls", f"n{i}", f"n{i + 1}", file="deep.cpp", line=i)
    store = _store(tmp_path, graph)
    assert store.strongly_connected_components() == ([], 0)


# --- line_span / no_incoming_calls (enclosing-range gated) -------------------

BIG = "cxx . . $ app/big(b1)."
MIDFN = "cxx . . $ app/mid(m1)."
TINY = "cxx . . $ app/tiny(t1)."
NEVER = "cxx . . $ app/never_called(n1)."
WIDGET = "cxx . . $ app/Widget#"
MENTIONED = "cxx . . $ app/address_taken(a1)."


def _spans_graph() -> Graph:
    """A #504-shaped graph: definitions carry body extents (`end_line`), and
    one call edge so `no_incoming_calls` has a real caller to exclude."""
    graph = Graph()
    graph.nodes[BIG] = Node(symbol=BIG, file="src/app.cpp", line=10, end_line=110)  # span 100
    graph.nodes[MIDFN] = Node(symbol=MIDFN, file="src/app.cpp", line=200, end_line=250)  # 50
    graph.nodes[TINY] = Node(symbol=TINY, file="src/app.cpp", line=300, end_line=302)  # 2
    graph.add_edge("calls", BIG, MIDFN, file="src/app.cpp", line=15)  # mid has a caller
    return graph


def _no_callers_graph() -> Graph:
    """`_spans_graph` plus: a never-called callable, a type (never callable),
    and a callable merely mentioned (address-taken) with no definition site."""
    graph = _spans_graph()
    graph.nodes[NEVER] = Node(symbol=NEVER, file="src/lib.cpp", line=5, end_line=8)
    graph.nodes[WIDGET] = Node(symbol=WIDGET, file="src/lib.cpp", line=1)
    graph.nodes[MENTIONED] = Node(symbol=MENTIONED)  # no def site in this index
    graph.add_reference(MENTIONED, "src/app.cpp", line=12)
    return graph


def test_line_span_ranks_by_body_extent_descending(tmp_path: Path) -> None:
    store = _store(tmp_path, _spans_graph())
    ranked, total = store.line_span()
    assert ranked == [(BIG, 100), (MIDFN, 50), (TINY, 2)]
    assert total == 3
    assert store.meta().get("has_enclosing_ranges") == "true"


def test_line_span_unavailable_without_enclosing_range_data(tmp_path: Path) -> None:
    """A stock-binary graph carries no `end_line`: None (explicitly
    unavailable), never an empty list that would read as 'no definitions'."""
    store = _store(tmp_path, _hotspots_graph())
    assert store.line_span() is None
    assert store.meta().get("has_enclosing_ranges") is None


def test_line_span_limit_truncates_but_total_is_full_count(tmp_path: Path) -> None:
    store = _store(tmp_path, _spans_graph())
    ranked, total = store.line_span(limit=1)
    assert ranked == [(BIG, 100)]
    assert total == 3


def test_line_span_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _spans_graph())
    with pytest.raises(ValueError):
        store.line_span(limit=-1)


def test_line_span_exclude_tests_drops_test_defined_symbols(tmp_path: Path) -> None:
    graph = _spans_graph()
    graph.nodes[TINY].file = "src/app_test.cpp"
    store = _store(tmp_path, graph)
    ranked, total = store.line_span(exclude_tests=True)
    assert ranked == [(BIG, 100), (MIDFN, 50)]
    assert total == 2


def test_line_span_exclude_paths_drops_vendored_definitions(tmp_path: Path) -> None:
    graph = _spans_graph()
    graph.nodes[TINY].file = "vendor/lib/tiny.cpp"
    store = _store(tmp_path, graph)
    ranked, total = store.line_span(exclude_paths=["vendor/"])
    assert ranked == [(BIG, 100), (MIDFN, 50)]
    assert total == 2


def test_no_incoming_calls_lists_defined_callables_with_zero_callers(
    tmp_path: Path,
) -> None:
    """BIG and TINY have no callers, NEVER none; MIDFN is called; a type and a
    definition-less mention are never listed. Ordered by definition site."""
    store = _store(tmp_path, _no_callers_graph())
    symbols, total = store.no_incoming_calls()
    assert symbols == [BIG, TINY, NEVER]  # src/app.cpp:10, src/app.cpp:300, src/lib.cpp:5
    assert total == 3


def test_no_incoming_calls_unavailable_without_enclosing_range_data(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, _hotspots_graph())
    assert store.no_incoming_calls() is None


def test_no_incoming_calls_counts_test_callers_as_callers(tmp_path: Path) -> None:
    """`exclude_tests` scopes which definitions are listed, not which edges
    count: a symbol called only from a test file still has callers — the fact
    the tool states."""
    graph = _no_callers_graph()
    graph.add_edge("calls", "cxx . . $ app/t(t1).", NEVER, file="never_test.cpp", line=1)
    store = _store(tmp_path, graph)
    symbols, total = store.no_incoming_calls(exclude_tests=True)
    assert symbols == [BIG, TINY]
    assert total == 2


def test_no_incoming_calls_exclude_tests_drops_test_defined_symbols(
    tmp_path: Path,
) -> None:
    graph = _no_callers_graph()
    graph.nodes[NEVER].file = "src/lib_test.cpp"
    store = _store(tmp_path, graph)
    symbols, total = store.no_incoming_calls(exclude_tests=True)
    assert symbols == [BIG, TINY]
    assert total == 2


def test_no_incoming_calls_path_filters(tmp_path: Path) -> None:
    graph = _no_callers_graph()
    graph.nodes[NEVER].file = "vendor/lib.cpp"
    store = _store(tmp_path, graph)
    symbols, _total = store.no_incoming_calls(exclude_paths=["vendor/"])
    assert symbols == [BIG, TINY]
    symbols, _total = store.no_incoming_calls(include_paths=["vendor/"])
    assert symbols == [NEVER]


def test_no_incoming_calls_limit_truncates_but_total_is_full_count(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, _no_callers_graph())
    symbols, total = store.no_incoming_calls(limit=1)
    assert symbols == [BIG]
    assert total == 3


def test_no_incoming_calls_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _no_callers_graph())
    with pytest.raises(ValueError):
        store.no_incoming_calls(limit=-1)


# --- global_init_references (attributed-refs gated) ---------------------------


G_A = "cxx . . $ app/g_a."
G_B = "cxx . . $ app/g_b."
HELPER = "cxx . . $ app/helper(h1)."


def _globals_graph() -> Graph:
    """A #504-shaped attributed graph mirroring the measured fixture
    (`int g_a = helper(); static int g_b = g_a + 1;`): g_b's initializer reads
    g_a (term -> term, the global_init_references fact); g_a's initializer
    calls helper (a callable use in the region, not a global read)."""
    graph = Graph()
    graph.nodes[G_A] = Node(symbol=G_A, file="src/g.cpp", line=1, end_line=1)
    graph.nodes[G_B] = Node(symbol=G_B, file="src/g.cpp", line=2, end_line=2)
    graph.add_reference(HELPER, "src/g.cpp", line=1, enclosing_symbol=G_A)
    graph.add_reference(G_A, "src/g.cpp", line=2, enclosing_symbol=G_B)
    return graph


def test_global_init_references_finds_globals_read_in_the_initializer(
    tmp_path: Path,
) -> None:
    """g_b's initializer reads g_a -> listed with the use site; g_a's
    initializer calls a function -> not a global read, an honest empty list."""
    store = _store(tmp_path, _globals_graph())
    assert store.meta().get("has_attributed_refs") == "true"  # the gate flipped

    refs, total = store.global_init_references(G_B)
    assert [(r.symbol, r.file, r.line, r.enclosing_symbol) for r in refs] == [
        (G_A, "src/g.cpp", 2, G_B)
    ]
    assert total == 1

    refs, total = store.global_init_references(G_A)
    assert refs == []  # helper is a callable use, not a term read
    assert total == 0


def test_global_init_references_dedupes_a_global_read_twice(tmp_path: Path) -> None:
    """Two reads of the same global in one initializer (e.g. `g_a + g_a`) are
    one referenced-global row, with the first use site."""
    graph = _globals_graph()
    graph.add_reference(G_A, "src/g.cpp", line=3, enclosing_symbol=G_B)
    store = _store(tmp_path, graph)

    refs, total = store.global_init_references(G_B)
    assert [r.symbol for r in refs] == [G_A]
    assert refs[0].line == 2  # the first site
    assert total == 1


def test_global_init_references_empty_not_none_for_a_global_with_no_reads(
    tmp_path: Path,
) -> None:
    """A known global whose initializer references no other global: `([], 0)`,
    distinct from the None that means 'data unavailable'."""
    graph = _globals_graph()
    graph.nodes[G_A].end_line = None  # even an extent-less term is queryable
    store = _store(tmp_path, graph)
    refs, total = store.global_init_references(G_A)  # its region use is a callable
    assert refs == []
    assert total == 0


def test_global_init_references_unavailable_without_attributed_refs(
    tmp_path: Path,
) -> None:
    """A store whose references carry no enclosing attribution (stock binary,
    or built without --attributed-refs): None — explicitly unavailable, never
    an empty list that would read as 'references nothing'."""
    graph = Graph()
    graph.nodes[G_B] = Node(symbol=G_B, file="src/g.cpp", line=2)
    graph.add_reference(G_A, "src/g.cpp", line=2)  # unattributed location
    store = _store(tmp_path, graph)
    assert store.meta().get("has_attributed_refs") is None
    assert store.global_init_references(G_B) is None


def test_global_init_references_unknown_symbol_raises(tmp_path: Path) -> None:
    """Unknown symbol is bad input, not an empty answer."""
    store = _store(tmp_path, _globals_graph())
    with pytest.raises(ValueError, match="unknown"):
        store.global_init_references("cxx . . $ app/missing.")


def test_global_init_references_non_term_symbol_raises(tmp_path: Path) -> None:
    """A callable has no initializer region — bad input, distinct from the
    None that means 'data unavailable' (the class_members contract)."""
    store = _store(tmp_path, _globals_graph())
    with pytest.raises(ValueError, match="not a global"):
        store.global_init_references(HELPER)


def test_global_init_references_limit_truncates_but_total_is_full_count(
    tmp_path: Path,
) -> None:
    """g_b already reads g_a (1 row from the fixture); three more globals make
    4 referenced terms, of which `limit=2` shows 2."""
    graph = _globals_graph()
    for i in range(3):
        other = f"cxx . . $ app/g_{i}."
        graph.add_reference(other, "src/g.cpp", line=4 + i, enclosing_symbol=G_B)
    store = _store(tmp_path, graph)
    refs, total = store.global_init_references(G_B, limit=2)
    assert len(refs) == 2
    assert total == 4


def test_global_init_references_rejects_negative_limit(tmp_path: Path) -> None:
    store = _store(tmp_path, _globals_graph())
    with pytest.raises(ValueError):
        store.global_init_references(G_B, limit=-1)


def test_update_upgrades_a_v2_store_adding_end_line(tmp_path: Path) -> None:
    """An older (schema v2) store has no `end_line` column; an incremental
    update from a #504 partial must add it on demand (the same ALTER pattern
    `enrich_references` uses for `refs.enclosing_id`), write the extents, and
    flip `has_enclosing_ranges`."""
    db = tmp_path / "graph.db"
    write_sqlite(_no_callers_graph(), db)
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE symbols DROP COLUMN end_line")
    con.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    # A real v2 store predates the feature entirely — it never had this key.
    con.execute("DELETE FROM meta WHERE key = 'has_enclosing_ranges'")
    con.commit()
    con.close()

    fn = "cxx . . $ app/patched(p1)."
    partial = _partial_index("src/app.cpp")
    d = partial.documents[0].occurrences.add(symbol=fn, symbol_roles=scip_pb2.SymbolRole.Definition)
    d.range.extend([40, 0, 3])
    d.enclosing_range.extend([40, 0, 60, 0])
    update_store(db, partial)

    store = GraphStore(db)
    assert store.schema_version() == SCHEMA_VERSION
    assert store.meta().get("has_enclosing_ranges") == "true"
    ranked, _total = store.line_span()
    assert (fn, 20) in ranked


def test_update_clears_end_line_when_definition_site_is_removed(tmp_path: Path) -> None:
    """A symbol's definition can be cleared (file re-indexed with no occurrence
    for it anymore) while it survives GC because something elsewhere still
    calls it. `end_line` must clear alongside file_id/line -- not linger as a
    stale extent paired with a NULL line, which would make `line_span` compute
    `end_line - line` as NULL and crash CLI formatting."""
    db = tmp_path / "graph.db"
    original = Graph()
    original.nodes["shared()."] = Node(symbol="shared().", file="foo.cpp", line=10, end_line=60)
    original.add_edge("calls", "b().", "shared().", file="bar.cpp", line=7)
    write_sqlite(original, db)

    # foo.cpp re-indexed with no occurrence of shared() at all (its definition
    # was deleted from the source); bar.cpp (still calling it) is untouched.
    update_store(db, _partial_index("foo.cpp"))

    store = GraphStore(db)
    assert store.has_symbol("shared().")  # kept: bar.cpp still calls it
    node = store.get_node("shared().")
    assert node is not None
    assert node.file is None
    assert node.line is None
    assert node.end_line is None  # not left stale
    ranked, _total = store.line_span()
    assert all(symbol != "shared()." for symbol, _span in ranked)


def test_update_does_not_leak_stale_end_line_across_a_redefinition_site(
    tmp_path: Path,
) -> None:
    """A symbol already defined (with a body extent) in an *untouched* file,
    re-indexed as a bodyless occurrence in a *changed* file, must not keep
    the old extent: end_line must travel with file_id/line as one fact from
    one occurrence, not be independently COALESCEd from a stale prior row."""
    db = tmp_path / "graph.db"
    original = Graph()
    original.nodes["sym()."] = Node(symbol="sym().", file="untouched.h", line=10, end_line=60)
    write_sqlite(original, db)

    partial = _partial_index("moved.h")
    d = partial.documents[0].occurrences.add(
        symbol="sym().", symbol_roles=scip_pb2.SymbolRole.Definition
    )
    d.range.extend([3, 0, 3])  # no enclosing_range: a bodyless occurrence
    update_store(db, partial)

    store = GraphStore(db)
    node = store.get_node("sym().")
    assert node is not None
    assert node.file == "moved.h"
    assert node.line == 3
    assert node.end_line is None  # not the stale 60 from the old site


# --- inheritance queries ---------------------------------------------------

# --- schema versioning -----------------------------------------------------


def test_write_stamps_schema_version(tmp_path: Path) -> None:
    store = _sample(tmp_path)
    assert store.meta()["schema_version"] == str(SCHEMA_VERSION)
    assert store.schema_version() == SCHEMA_VERSION


def test_open_rejects_a_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "future.db"
    write_sqlite(_graph_with_edge(), db)
    # simulate a store written by a future cppgraph
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),)
    )
    con.commit()
    con.close()
    with pytest.raises(IncompatibleStoreError):
        GraphStore(db)


def test_legacy_store_without_version_opens(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    write_sqlite(_graph_with_edge(), db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM meta WHERE key = 'schema_version'")
    con.commit()
    con.close()
    store = GraphStore(db)  # must not raise
    assert store.schema_version() is None


def _graph_with_edge() -> Graph:
    graph = Graph()
    graph.add_edge("calls", CALLER, METHOD, file="foo.cpp", line=1)
    return graph


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


def test_attributed_references_round_trip(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node(CALLER)  # the enclosing definition is interned as a node
    graph.add_reference(TYPE, "a.cpp", 11, enclosing_symbol=CALLER)
    graph.add_reference(TYPE, "b.cpp", 7)  # unattributed — mixed coverage
    store = _store(tmp_path, graph)
    refs = store.references_of(TYPE)
    assert [(r.file, r.enclosing_symbol) for r in refs] == [("a.cpp", CALLER), ("b.cpp", None)]
    assert store.meta().get("has_attributed_refs") == "true"
    assert store.meta().get("attributed_ref_count") == "1"


def test_enrich_references_backfills_from_scip(tmp_path: Path) -> None:
    """A store built without attribution is upgraded in place from a #504 .scip:
    the enclosing ranges attribute the already-stored references, no rebuild."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."
    doc = scip_pb2.Document(relative_path="render.cpp")
    # enclosing_range lives on the DEFINITION (render's body 5..20); the use of
    # Widget at line 8 is attributed to it by containment.
    user_def = scip_pb2.Occurrence(symbol=user, symbol_roles=scip_pb2.SymbolRole.Definition)
    user_def.range.extend([5, 0, 10])
    user_def.enclosing_range.extend([5, 0, 20, 0])
    use = scip_pb2.Occurrence(symbol=typ)
    use.range.extend([8, 0, 6])
    doc.occurrences.extend([user_def, use])
    index = scip_pb2.Index(documents=[doc])

    # Build WITHOUT attribution first (file granularity), then enrich.
    graph = build_graph(index, attribute_references=False)
    db = tmp_path / "g.db"
    write_sqlite(graph, db)
    assert GraphStore(db).meta().get("has_attributed_refs") is None

    from cppgraph.store import enrich_references

    attributed, total = enrich_references(db, index)
    assert (attributed, total) == (1, 1)

    store = GraphStore(db)
    assert [r.enclosing_symbol for r in store.references_of(typ)] == [user]
    assert store.meta().get("has_attributed_refs") == "true"


def test_enrich_references_errors_without_reference_index(tmp_path: Path) -> None:
    from cppgraph.store import enrich_references

    store = _sample(tmp_path)  # built with no references
    store.close()
    with pytest.raises(ValueError, match="no reference index"):
        enrich_references(tmp_path / "graph.db", scip_pb2.Index())


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


def test_reachable_from_over_inherits_gives_transitive_ancestors(tmp_path: Path) -> None:
    store = _hierarchy(tmp_path)
    # the base hierarchy above Leaf (inherits edges run derived -> base)
    assert store.reachable_from(LEAF, kind="inherits") == {DERIVED, BASE}
    # calls-space reachability of a type is empty (types make no calls)
    assert store.reachable_from(BASE) == set()


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


def test_build_provenance_records_index_scope() -> None:
    index = _index_with_metadata("file:///some/repo")
    meta = build_provenance(index, index_filter="src/mongo", index_excludes_tests=True)
    assert meta["index_filter"] == "src/mongo"
    assert meta["index_tests"] == "excluded"

    # Empty filter (whole tree) is still recorded — explicit, not "unknown".
    meta = build_provenance(index, index_filter="", index_excludes_tests=False)
    assert meta["index_filter"] == ""
    assert meta["index_tests"] == "included"


def test_build_provenance_omits_index_scope_when_not_given() -> None:
    index = _index_with_metadata("file:///some/repo")
    meta = build_provenance(index)
    assert "index_filter" not in meta
    assert "index_tests" not in meta


def test_build_provenance_omits_commit_when_root_is_not_a_git_repo(tmp_path: Path) -> None:
    # A real, existing, non-git directory: git rev-parse fails, no commit stored.
    index = _index_with_metadata(f"file://{tmp_path}")
    meta = build_provenance(index)
    assert "source_commit" not in meta
    assert meta["project_root"] == f"file://{tmp_path}"


def test_dirty_fingerprints_prevent_false_stale(tmp_path: Path) -> None:
    """A graph built from a dirty tree records the uncommitted files' content
    hashes; a later staleness check must NOT report those files as changed while
    their content is unchanged since indexing — the false-stale we're killing."""
    import subprocess as sp

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    f = tmp_path / "a.cpp"
    f.write_text("int a() { return 0; }\n")
    git("add", "a.cpp")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    # The state that actually gets indexed: an uncommitted edit.
    f.write_text("int a() { return 1; }\n")

    index = _index_with_metadata(f"file://{tmp_path}")
    meta = build_provenance(index, source_commit=commit, source_dirty=True)
    fps = read_dirty_fingerprints(meta)
    assert fps is not None and "a.cpp" in fps

    # With the fingerprints, the dirty-at-build file is not stale.
    changed, _ = changed_files_since(tmp_path, commit, dirty_fingerprints=fps)
    assert "a.cpp" not in changed
    # Without them (legacy graph), the old behaviour flags it — the false stale.
    changed_naive, _ = changed_files_since(tmp_path, commit)
    assert "a.cpp" in changed_naive

    # Edited *further* after indexing -> genuinely changed -> reported again.
    f.write_text("int a() { return 2; }\n")
    changed_more, _ = changed_files_since(tmp_path, commit, dirty_fingerprints=fps)
    assert "a.cpp" in changed_more

    # Reverted to the committed version: the tree now matches the commit, so a
    # naive diff sees nothing — but the index still holds the DIRTY content, so it
    # is stale and must be reported (the additive fingerprint check).
    f.write_text("int a() { return 0; }\n")
    changed_revert, _ = changed_files_since(tmp_path, commit, dirty_fingerprints=fps)
    assert "a.cpp" in changed_revert
    # sanity: a naive diff (no fingerprints) would wrongly call it up to date
    changed_naive_revert, _ = changed_files_since(tmp_path, commit)
    assert "a.cpp" not in changed_naive_revert


def test_is_stale(tmp_path: Path) -> None:
    """The cheap per-query drift flag: False right after indexing, True once a
    tracked C++ file changes, None with no recorded commit — matching what the
    MCP `stale` field / CLI stderr warning need without `status`'s full report."""
    import subprocess as sp

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.cpp").write_text("int a() { return 0; }\n")
    git("add", "a.cpp")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    def store_with(commit: str | None) -> GraphStore:
        # meta is provenance stamped at write time (no in-place mutator), so each
        # case gets a fresh store built with its own meta dict.
        graph = Graph()
        graph.add_node(METHOD, display_name="makeResumeToken")
        db = tmp_path / "graph.db"
        write_sqlite(graph, db, meta={"source_commit": commit} if commit else None)
        return GraphStore(db)

    store = store_with(commit)
    assert is_stale(store, tmp_path, (".cpp", ".h")) is False

    (tmp_path / "a.cpp").write_text("int a() { return 1; }\n")
    assert is_stale(store, tmp_path, (".cpp", ".h")) is True

    # A non-source-extension change doesn't count as drift.
    store = store_with(commit)
    git("checkout", "--", "a.cpp")
    (tmp_path / "README.md").write_text("docs\n")
    assert is_stale(store, tmp_path, (".cpp", ".h")) is False

    # No recorded commit -> unknown, not a false "up to date".
    store = store_with(None)
    assert is_stale(store, tmp_path, (".cpp", ".h")) is None


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


def _add_call(
    doc: scip_pb2.Document, caller: str, callee: str, *, def_line: int, call_line: int
) -> None:
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


def test_update_preserves_recorded_index_scope(tmp_path: Path) -> None:
    """An incremental update stamps only the keys it provides (source_commit, ...),
    so the scope recorded at build time survives — the graph stays self-describing
    and an incremental update can keep reading it."""
    db = tmp_path / "graph.db"
    original = Graph()
    original.add_edge("calls", "a().", "b().", file="foo.cpp", line=5)
    write_sqlite(
        original,
        db,
        meta={"source_commit": "old", "index_filter": "src/mongo", "index_tests": "excluded"},
    )

    partial = _partial_index("foo.cpp")
    _add_call(partial.documents[0], "a().", "b().", def_line=2, call_line=6)
    update_store(db, partial, meta={"source_commit": "new"})

    meta = GraphStore(db).meta()
    assert meta["source_commit"] == "new"
    assert meta["index_filter"] == "src/mongo"  # untouched by the update
    assert meta["index_tests"] == "excluded"


def test_implements_edges_are_stored(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge(
        "implements",
        "cxx . . $ mongo/Dog#sound(d1).",
        "cxx . . $ mongo/Animal#sound(a1).",
        file="animal.h",
    )
    store = _store(tmp_path, graph)
    # implements edges don't participate in call queries, but must round-trip.
    assert store.has_symbol("cxx . . $ mongo/Dog#sound(d1).")
    assert store.callers_of("cxx . . $ mongo/Animal#sound(a1).") == []


def test_staleness_verdict_up_to_date() -> None:
    from cppgraph.store import staleness_verdict

    v = staleness_verdict(0, 0, indexed_files=1000)
    assert v["up_to_date"] is True


def test_staleness_verdict_small_drift_recommends_update() -> None:
    from cppgraph.store import staleness_verdict

    v = staleness_verdict(changed=10, deleted=2, indexed_files=1000, commits_behind=3)
    assert v["up_to_date"] is False
    assert v["recommend"] == "update"
    assert v["changed_fraction"] == 0.012
    assert v["commits_behind"] == 3


def test_staleness_verdict_large_drift_recommends_rebuild() -> None:
    from cppgraph.store import REBUILD_FILE_FRACTION, staleness_verdict

    heavy = int(1000 * REBUILD_FILE_FRACTION) + 5
    v = staleness_verdict(changed=heavy, deleted=0, indexed_files=1000)
    assert v["recommend"] == "rebuild"


def test_staleness_verdict_unknown_denominator_defaults_to_update() -> None:
    from cppgraph.store import staleness_verdict

    v = staleness_verdict(changed=5, deleted=0, indexed_files=0)
    assert v["recommend"] == "update"
    assert v["changed_fraction"] is None


def test_indexed_file_count(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_edge("calls", CALLER, METHOD, file="a.cpp", line=1)
    graph.add_edge("calls", METHOD, OTHER, file="b.cpp", line=2)
    store = _store(tmp_path, graph)
    assert store.indexed_file_count() == 2


def _multi(tmp_path: Path) -> GraphStore:
    """Three distinct symbols: two share the `Foo#` qualifier, one is `Bar#`."""
    graph = Graph()
    graph.add_node(METHOD, display_name="makeResumeToken")
    graph.add_node(CALLER, display_name="caller")
    graph.add_node(OTHER, display_name="other")
    return _store(tmp_path, graph)


def test_resolve_exact_symbol_is_used_as_is(tmp_path: Path) -> None:
    assert _multi(tmp_path).resolve(METHOD) == (METHOD, [])


def test_resolve_unique_name(tmp_path: Path) -> None:
    resolved, candidates = _multi(tmp_path).resolve("makeResumeToken")
    assert resolved == METHOD
    assert candidates == []


def test_resolve_normalizes_double_colon_to_hash(tmp_path: Path) -> None:
    resolved, candidates = _multi(tmp_path).resolve("Foo::makeResumeToken")
    assert resolved == METHOD
    assert candidates == []


def test_resolve_ambiguous_lists_candidates_without_guessing(tmp_path: Path) -> None:
    resolved, candidates = _multi(tmp_path).resolve("Foo")  # matches METHOD and CALLER
    assert resolved is None
    assert {n.symbol for n in candidates} == {METHOD, CALLER}


def test_resolve_no_match_is_empty(tmp_path: Path) -> None:
    assert _multi(tmp_path).resolve("does_not_exist") == (None, [])


def test_resolve_falls_back_to_fuzzy(tmp_path: Path) -> None:
    """A case/separator miss still resolves: the exact substring fails, then the
    case/separator-insensitive fuzzy match lands on the one symbol."""
    resolved, candidates = _multi(tmp_path).resolve("makeresumetoken")
    assert resolved == METHOD
    assert candidates == []


# --- resolve: `file:line` input form (definition-site resolution) ------------

WIDGET_T = "cxx . . $ app/Widget#"
WIDGET_M = "cxx . . $ app/Widget#paint(p1)."
HELPER_F = "cxx . . $ app/helper(h1)."
TERM_A = "cxx . . $ app/a(a1)."
TERM_B = "cxx . . $ app/b(b1)."
LIB_OTHER = "cxx . . $ lib/other(o1)."
ANON_SYM = "cxx . . $ src/app.cpp:14:2/helper()."


def _located_graph() -> Graph:
    """#504-shaped, nested extents in `src/app.cpp` (0-indexed lines, as the
    store records them): a class [9, 39] containing a method [14, 29], a free
    function [50, 59], two terms sharing start line 70, and a same-line symbol
    in another file (must never leak into an `src/app.cpp` query)."""
    graph = Graph()
    graph.nodes[WIDGET_T] = Node(symbol=WIDGET_T, file="src/app.cpp", line=9, end_line=39)
    graph.nodes[WIDGET_M] = Node(symbol=WIDGET_M, file="src/app.cpp", line=14, end_line=29)
    graph.nodes[HELPER_F] = Node(symbol=HELPER_F, file="src/app.cpp", line=50, end_line=59)
    graph.nodes[TERM_A] = Node(symbol=TERM_A, file="src/app.cpp", line=70)
    graph.nodes[TERM_B] = Node(symbol=TERM_B, file="src/app.cpp", line=70)
    graph.nodes[LIB_OTHER] = Node(symbol=LIB_OTHER, file="src/lib.cpp", line=14, end_line=20)
    return graph


def test_resolve_file_line_exact_start_line(tmp_path: Path) -> None:
    """1-indexed input 15 -> stored 0-indexed line 14; the same stored line in
    another file (lib/other) must not leak into the answer."""
    store = _store(tmp_path, _located_graph())
    assert store.resolve("src/app.cpp:15") == (WIDGET_M, [])


def test_resolve_file_line_inside_body_is_innermost_definition(tmp_path: Path) -> None:
    """Line 21 (1-indexed) sits inside both the method [15, 30] and its class
    [10, 40] (1-indexed, ends inclusive): the innermost definition wins."""
    store = _store(tmp_path, _located_graph())
    assert store.resolve("src/app.cpp:21") == (WIDGET_M, [])


def test_resolve_file_line_stock_graph_refuses_body_lines(tmp_path: Path) -> None:
    """Without body extents (stock binary) an exact start line still resolves,
    but a body line is an honest no-match — never a nearest-definition guess."""
    graph = _located_graph()
    for node in graph.nodes.values():
        node.end_line = None
    store = _store(tmp_path, graph)
    assert store.meta().get("has_enclosing_ranges") is None
    assert store.resolve("src/app.cpp:15") == (WIDGET_M, [])
    assert store.resolve("src/app.cpp:21") == (None, [])


def test_resolve_file_line_no_symbol_at_line_is_no_match(tmp_path: Path) -> None:
    store = _store(tmp_path, _located_graph())
    assert store.resolve("src/app.cpp:80") == (None, [])


def test_resolve_file_line_same_line_multiple_symbols_is_ambiguous(tmp_path: Path) -> None:
    store = _store(tmp_path, _located_graph())
    resolved, candidates = store.resolve("src/app.cpp:71")
    assert resolved is None
    assert {n.symbol for n in candidates} == {TERM_A, TERM_B}


def test_resolve_file_line_tied_innermost_spans_are_ambiguous(tmp_path: Path) -> None:
    """Two definitions with the same narrowest containing span are candidates
    to pick from, not a guess."""
    graph = _located_graph()
    graph.nodes[TERM_A].end_line = 74
    graph.nodes[TERM_B].end_line = 74
    store = _store(tmp_path, graph)
    resolved, candidates = store.resolve("src/app.cpp:73")  # inside both [70, 74]
    assert resolved is None
    assert {n.symbol for n in candidates} == {TERM_A, TERM_B}


def test_resolve_file_line_unknown_file_is_no_match(tmp_path: Path) -> None:
    store = _store(tmp_path, _located_graph())
    assert store.resolve("src/missing.cpp:15") == (None, [])


def test_resolve_file_line_backslashes_normalized(tmp_path: Path) -> None:
    store = _store(tmp_path, _located_graph())
    assert store.resolve("src\\app.cpp:15") == (WIDGET_M, [])


def test_resolve_file_line_windows_drive_letter_path(tmp_path: Path) -> None:
    """A drive-letter path carries a `:` of its own: only the LAST colon splits
    the line off, and backslash normalization matches the stored forward
    slashes."""
    graph = _located_graph()
    graph.nodes[WIDGET_M].file = "C:/proj/src/app.cpp"
    store = _store(tmp_path, graph)
    assert store.resolve("C:\\proj\\src\\app.cpp:15") == (WIDGET_M, [])


def test_resolve_scip_symbol_with_colons_not_treated_as_file_line(tmp_path: Path) -> None:
    """An anonymous-namespace SCIP symbol embeds `path:line:col` but ends in a
    descriptor — the `file:line` shape must not hijack it."""
    graph = _located_graph()
    graph.nodes[ANON_SYM] = Node(symbol=ANON_SYM, file="src/app.cpp", line=13)
    store = _store(tmp_path, graph)
    assert store.resolve(ANON_SYM) == (ANON_SYM, [])
