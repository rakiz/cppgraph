"""Tests for `cppgraph.messages` — the shared wording/cap contract.

The CLI and the MCP surface must emit the SAME capability-gate reasons,
standing caveats, and budget caps. These tests pin both surfaces to the one
shared constant (never a re-worded inline copy), so the wording can't drift
again.
"""

from __future__ import annotations

import pytest

from cppgraph import messages
from cppgraph.model import Graph
from cppgraph.store import GraphStore, references_capability_gap, write_sqlite


def test_mcp_surface_aliases_are_the_shared_constants() -> None:
    """Every MCP module-level note/reason alias IS the `messages` constant —
    the two surfaces render one wording, not two."""
    from cppgraph import mcp_server

    assert mcp_server._NO_ENCLOSING_RANGES is messages.NO_ENCLOSING_RANGES
    assert mcp_server._STOCK_ATTRIBUTION_UNRELIABLE is messages.STOCK_ATTRIBUTION_UNRELIABLE
    assert mcp_server._ZERO_CALLERS_STOCK_NOTE is messages.ZERO_CALLERS_STOCK_NOTE
    assert mcp_server._NO_ATTRIBUTED_REFS is messages.NO_ATTRIBUTED_REFS
    assert mcp_server._REACHABLE_NOTE is messages.REACHABLE_NOTE
    assert mcp_server._SCC_NOTE is messages.SCC_NOTE
    assert mcp_server._BOUNDARY_NOTE is messages.BOUNDARY_NOTE
    assert mcp_server._GLOBAL_INIT_NOTE is messages.GLOBAL_INIT_NOTE
    assert mcp_server._NO_STATIC_CHAIN_HINT is messages.NO_STATIC_CHAIN_HINT
    assert mcp_server._NO_CYCLE_NOTE is messages.NO_CYCLE_NOTE


def test_shared_caps_are_one_constant_per_category() -> None:
    from cppgraph import mcp_server

    assert messages.DEFAULT_LIMIT == 40 == mcp_server.DEFAULT_LIMIT
    assert messages.EXPLAIN_LIMIT == 10 == mcp_server.EXPLAIN_LIMIT
    assert messages.AMBIGUOUS_CANDIDATE_LIMIT == 10


# --- references_capability_gap (shared gate, finding #4) ---------------------


def _metaless_store(tmp_path, meta: dict[str, str] | None = None) -> GraphStore:
    graph = Graph()
    graph.add_edge("calls", "caller", "target", file="src/a.cpp", line=1)
    path = tmp_path / "g.db"
    write_sqlite(graph, path, meta=meta)
    return GraphStore(path)


def test_references_gate_silent_when_graph_has_references(tmp_path) -> None:
    store = _metaless_store(tmp_path, meta={"has_references": "true"})
    assert references_capability_gap(store, refs_count=0, access=None) is None
    assert references_capability_gap(store, refs_count=3, access=None) is None


def test_references_gate_no_references_flag(tmp_path) -> None:
    """An empty result on a `--no-references` graph is a missing capability,
    not a real 'no use sites'."""
    store = _metaless_store(tmp_path)
    assert references_capability_gap(store, refs_count=0, access=None) == (
        messages.NO_REFERENCES_REASON
    )
    # With refs present the flag gap is irrelevant (can't happen, but the gate
    # must not mask a non-empty answer either way).
    assert references_capability_gap(store, refs_count=2, access=None) is None


def test_references_gate_access_without_role_data(tmp_path) -> None:
    store = _metaless_store(tmp_path, meta={"has_references": "true"})
    gap = references_capability_gap(store, refs_count=1, access="write")
    assert gap == messages.NO_ACCESS_ROLES_REASON.format(access="write")
    assert "ReadAccess/WriteAccess patch" in gap


def test_references_gate_access_with_role_data(tmp_path) -> None:
    store = _metaless_store(tmp_path, meta={"has_references": "true", "has_access_roles": "true"})
    assert references_capability_gap(store, refs_count=1, access="write") is None


# --- CLI renders the shared bodies -------------------------------------------


@pytest.fixture()
def reachable_graph(tmp_path) -> str:
    graph = Graph()
    graph.add_edge("calls", "mid", "entry", file="src/a.cpp", line=1)
    graph.add_edge("calls", "leaf", "mid", file="src/b.cpp", line=2)
    path = tmp_path / "r.db"
    write_sqlite(graph, path, meta={"has_references": "true"})
    return str(path)


def test_cli_reachable_from_prints_the_shared_note(
    reachable_graph: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from cppgraph.cli import main

    assert main(["reachable-from", "--graph", reachable_graph, "entry"]) == 0
    out = capsys.readouterr().out
    assert f"  note: {messages.REACHABLE_NOTE}" in out


def test_cli_hotspots_and_dependency_cost_default_to_the_shared_cap(tmp_path) -> None:
    """CLI default limit == 40 == the MCP DEFAULT_LIMIT (surface parity): a
    35-row ranking shows all 35 rows without --limit (the old CLI default of
    20 truncated at 20; the shared 40 does not)."""
    from cppgraph.cli import main

    graph = Graph()
    for i in range(35):
        graph.add_edge(
            "calls",
            f"caller_{i}",
            f"tgt_{i}",
            file=f"src/lib/caller_{i}.cpp",
            line=i,
        )
        graph.nodes[f"tgt_{i}"].file = f"src/lib/tgt_{i}.cpp"
    path = tmp_path / "caps.db"
    write_sqlite(graph, path)

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["hotspots", "--graph", str(path)]) == 0
    hot_out = buf.getvalue()
    assert "raise --limit" not in hot_out
    assert "tgt_34" in hot_out  # the 35th row survived the default cap

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["dependency-cost", "--graph", str(path), "--target-path", "src/lib/"]) == 0
    dep_out = buf.getvalue()
    assert "raise --limit" not in dep_out
    assert "tgt_34" in dep_out
