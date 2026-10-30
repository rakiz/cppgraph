"""Tests for the query commands' `--json` flag: schema:1 documents on stdout.

Every query command answers a question, and its `--json` output is a VERSIONED
CONTRACT (schema: 1, one schema per command, bumped independently) — the
workspace convention from NEXT-SESSION.md. The documents are built from the
SAME pure functions the MCP tools use (CLI/MCP parity, AGENTS.md rule), so a
given query gives the same JSON on either surface.

Consumers these tests pin (do NOT design for them here — make the documents
complete): masora's proof-query replay reads `callers --json` (per-symbol
caller identity, bounded, truncation visible) and `find --json` for anchor
resolution (exact SCIP identity per match, candidates on ambiguity, an
unambiguous none/unique/multiple signal).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cppgraph import mcp_server
from cppgraph.cli import main
from cppgraph.model import Graph
from cppgraph.store import write_sqlite

CALLER = "cxx . . $ mongo/Foo#caller(a2)."
TARGET = "cxx . . $ mongo/Foo#makeResumeToken(a1)."
CALLEE = "cxx . . $ mongo/Foo#domain(d1)."


def _graph() -> Graph:
    graph = Graph()
    graph.nodes[TARGET] = graph.add_node(TARGET, display_name="makeResumeToken")
    graph.nodes[TARGET].file = "foo.cpp"
    graph.nodes[TARGET].line = 4
    graph.nodes[CALLER] = graph.add_node(CALLER)
    graph.nodes[CALLER].file = "foo.cpp"
    graph.nodes[CALLER].line = 0
    graph.nodes[CALLEE] = graph.add_node(CALLEE)
    graph.nodes[CALLEE].file = "foo.cpp"
    graph.nodes[CALLEE].line = 8
    graph.add_edge("calls", CALLER, TARGET, file="foo.cpp", line=9)
    graph.add_edge("calls", TARGET, CALLEE, file="foo.cpp", line=5)
    return graph


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    path = tmp_path / "graph.db"
    write_sqlite(_graph(), path)
    return path


def _run_json(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[dict[str, Any], int]:
    code = main([*args, "--json"])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["schema"] == 1  # every --json document is a versioned contract
    return doc, code


def test_callers_json_document_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Masora's proof-query replay contract: per-symbol caller identity
    (exact SCIP string + file:line), bounded, truncation visible."""
    doc, code = _run_json(capsys, "callers", "--graph", str(graph_path), TARGET)
    assert code == 0
    assert doc["command"] == "callers"
    assert doc["symbol"] == TARGET
    assert doc["total"] == 1
    assert doc["truncated"] is False
    assert doc["callers"] == [
        {"name": "mongo/Foo#caller(a2).", "symbol": CALLER, "file": "foo.cpp", "line": 10}
    ]


def test_callers_json_truncation_visible_on_fan_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = Graph()
    for i in range(3):
        c = f"cxx . . $ mongo/Foo#caller{i}(c{i})."
        graph.nodes[c] = graph.add_node(c)
        graph.nodes[c].file = "foo.cpp"
        graph.nodes[c].line = i
        graph.add_edge("calls", c, TARGET, file="foo.cpp", line=9)
    path = tmp_path / "fan.db"
    write_sqlite(graph, path)
    doc, code = _run_json(
        capsys, "callers", "--graph", str(path), "--no-exclude-tests", "--limit", "2", TARGET
    )
    assert code == 0
    assert doc["total"] == 3  # the true total, not the shown count
    assert doc["truncated"] is True  # the cut is VISIBLE, never silent
    assert len(doc["callers"]) == 2
    assert all(c["symbol"].startswith("cxx . . $ mongo/Foo#caller") for c in doc["callers"])


def test_find_json_unique_match_names_the_symbol(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Masora's anchor-resolution contract: exactly one match is an unambiguous
    `match_state: "unique"` carrying the exact SCIP symbol."""
    doc, code = _run_json(capsys, "find", "--graph", str(graph_path), "makeResumeToken")
    assert code == 0
    assert doc["command"] == "find"
    assert doc["match_state"] == "unique"
    assert doc["unique_symbol"] == TARGET
    assert doc["total"] == 1
    assert doc["results"][0]["symbol"] == TARGET  # exact SCIP identity per match
    assert doc["results"][0]["file"] == "foo.cpp"
    assert doc["results"][0]["line"] == 5
    assert "candidates" not in doc


def test_find_json_no_match_is_explicit_none(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(capsys, "find", "--graph", str(graph_path), "nope")
    assert code == 1
    assert doc["match_state"] == "none"
    assert doc["total"] == 0
    assert doc["results"] == []
    assert "unique_symbol" not in doc
    assert "candidates" not in doc


def test_find_json_overload_group_is_multiple_with_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two same-named overloads: match_state `multiple`, and the candidate list
    carries EVERY arm's exact SCIP symbol — no silent pick."""
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    graph.nodes[p1] = graph.add_node(p1, display_name="parse")
    graph.nodes[p1].file = "rt.h"
    graph.nodes[p1].line = 0
    graph.nodes[p2] = graph.add_node(p2, display_name="parse")
    graph.nodes[p2].file = "rt.h"
    graph.nodes[p2].line = 1
    path = tmp_path / "ov.db"
    write_sqlite(graph, path)
    doc, code = _run_json(capsys, "find", "--graph", str(path), "parse")
    assert code == 0
    assert doc["match_state"] == "multiple"
    assert "unique_symbol" not in doc
    assert doc["candidates"] == [p1, p2]
    assert doc["results"][0]["signatures"][0]["symbol"] == p1
    assert doc["results"][0]["signatures"][1]["symbol"] == p2


def test_find_json_limit_interaction(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, _code = _run_json(capsys, "find", "--graph", str(graph_path), "--limit", "1", "Foo")
    assert doc["total"] == 3  # true total survives the cap
    assert doc["truncated"] is True
    assert len(doc["results"]) == 1


def test_path_json_round_trips_chain(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "path", "--graph", str(graph_path), CALLER, TARGET)
    assert code == 0
    assert doc["command"] == "path"
    assert doc["found"] is True
    assert doc["hops"] == 1
    assert [n["symbol"] for n in doc["path"]] == [CALLER, TARGET]
    assert doc["path"][1]["file"] == "foo.cpp"
    assert doc["path"][1]["line"] == 10


def test_path_json_no_path_reports_found_false(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(capsys, "path", "--graph", str(graph_path), TARGET, CALLER)
    assert code == 1
    assert doc["found"] is False
    assert doc["path"] is None
    assert doc["hint"]


def test_status_json_is_cli_transport(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "status", "--graph", str(graph_path))
    assert code == 0
    assert doc["command"] == "status"
    assert doc["transport"] == "cli"  # a copied status block says which surface made it
    assert doc["graph_path"] == str(graph_path)
    assert doc["drift"] == {"checked": False}


def test_explain_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "explain", "--graph", str(graph_path), TARGET)
    assert code == 0
    assert doc["command"] == "explain"
    assert doc["symbol"] == TARGET
    assert doc["defined_at"] == {"file": "foo.cpp", "line": 5}
    assert doc["callers"]["total"] == 1
    assert doc["callers"]["items"][0]["symbol"] == CALLER
    assert doc["callees"]["total"] == 1
    assert doc["callees"]["items"][0]["symbol"] == CALLEE


def test_references_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    typ = "cxx . . $ mongo/Widget#"
    graph = Graph()
    graph.add_reference(typ, "a.cpp", 10)
    path = tmp_path / "ref.db"
    write_sqlite(graph, path)
    doc, code = _run_json(capsys, "references", "--graph", str(path), typ)
    assert code == 0
    assert doc["command"] == "references"
    assert doc["symbol"] == typ
    assert doc["available"] is True
    assert doc["total"] == 1
    assert doc["uses"] == [{"file": "a.cpp", "line": 11}]


def test_impact_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "impact", "--graph", str(graph_path), TARGET)
    assert code == 0
    assert doc["command"] == "impact"
    assert doc["symbol"] == TARGET
    assert doc["total"] == 1
    assert doc["reached_by"][0]["symbol"] == CALLER


def test_reachable_from_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "reachable-from", "--graph", str(graph_path), TARGET)
    assert code == 0
    assert doc["command"] == "reachable-from"
    assert doc["total"] == 1
    assert doc["reaches"][0]["symbol"] == CALLEE


def test_hotspots_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "hot.db"
    write_sqlite(_graph(), path)
    doc, code = _run_json(capsys, "hotspots", "--graph", str(path))
    assert code == 0
    assert doc["command"] == "hotspots"
    assert doc["kind"] == "fan_in"
    top = doc["hotspots"][0]
    assert top["symbol"] == TARGET
    assert top["count"] == 1
    assert top["file"] == "foo.cpp"
    assert top["line"] == 5


def test_outline_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "out.db"
    write_sqlite(_graph(), path)
    doc, code = _run_json(capsys, "outline", "--graph", str(path), "foo.cpp")
    assert code == 0
    assert doc["command"] == "outline"
    assert doc["file"] == "foo.cpp"
    assert doc["total"] == 3
    assert [d["symbol"] for d in doc["definitions"]] == [CALLER, TARGET, CALLEE]


def test_subtypes_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "subtypes", "--graph", str(graph_path), TARGET)
    assert code == 0
    assert doc["command"] == "subtypes"
    assert doc["symbol"] == TARGET
    assert doc["total"] == 0
    assert doc["subtypes"] == []
    assert doc["note"]  # a bare 0 is annotated, never silent


def test_bases_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, _code = _run_json(capsys, "bases", "--graph", str(graph_path), TARGET)
    assert doc["command"] == "bases"
    assert doc["bases"] == []


def test_include_path_filters_json_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` composes with the path filters; an unsatisfiable prefix stays a
    strict error (exit 2), not a silent empty answer."""
    graph = Graph()
    for s, f in ((CALLER, "src/myproject/foo.cpp"), (TARGET, "vendor/somelib/foo.cpp")):
        graph.nodes[s] = graph.add_node(s)
        graph.nodes[s].file = f
        graph.nodes[s].line = 0
    graph.add_edge("calls", CALLER, TARGET, file="foo.cpp", line=1)
    path = tmp_path / "pf.db"
    write_sqlite(graph, path)
    doc, code = _run_json(
        capsys, "callers", "--graph", str(path), "--include-path", "src/myproject/", TARGET
    )
    assert code == 0
    assert doc["total"] == 1
    assert doc["include_paths"] == ["src/myproject/"]
    assert doc["callers"][0]["symbol"] == CALLER
    with pytest.raises(SystemExit) as exc:
        main(["callers", "--graph", str(path), "--json", "--include-path", "nope/", TARGET])
    assert exc.value.code == 2


def test_unknown_flag_still_errors_strictly(graph_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["callers", "--graph", str(graph_path), "--json", "--nope", TARGET])
    assert exc.value.code == 2


def test_action_commands_have_no_json_flag(graph_path: Path) -> None:
    """The workspace convention: commands that DO something get no --json."""
    for cmd in ("build", "enrich-refs", "export", "view", "update", "compdb-summary"):
        with pytest.raises(SystemExit) as exc:
            main([cmd, "--json"])
        assert exc.value.code == 2


# --- zero default-behavior change: text output is byte-identical --------------


def test_text_output_byte_identical_without_json_flag(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pinned golden TEXT for three commands: adding --json must not perturb a
    single byte of the default output."""
    assert main(["callers", "--graph", str(graph_path), TARGET]) == 0
    assert (
        capsys.readouterr().out
        == "[cppgraph] 1 caller(s) of cxx . . $ mongo/Foo#makeResumeToken(a1)."
        " (excluding tests)\n"
        "  mongo/Foo#caller(a2).  (foo.cpp:10)\n"
    )
    assert main(["find", "--graph", str(graph_path), "makeResumeToken"]) == 0
    assert (
        capsys.readouterr().out == "[cppgraph] 1 match(es)\n"
        f"  {TARGET}  (makeResumeToken @ foo.cpp:5)\n"
    )
    assert main(["path", "--graph", str(graph_path), CALLER, TARGET]) == 0
    assert (
        capsys.readouterr().out == f"[cppgraph] 1 hop(s) from {CALLER} to {TARGET}\n"
        f"  {CALLER}\n"
        f"  -> {TARGET}  (foo.cpp:10)\n"
    )


# --- MCP parity: same pure functions, same document --------------------------


def _mcp_payload(server: Any, tool: str, arguments: dict) -> dict:
    result = server.call_tool(tool, arguments)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_callers_json_matches_mcp_who_calls_payload(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = mcp_server.build_server(str(graph_path))
    mcp_doc = _mcp_payload(server, "who_calls", {"symbol": TARGET, "full_symbols": True})
    doc, _code = _run_json(capsys, "callers", "--graph", str(graph_path), TARGET)
    inner = {k: v for k, v in doc.items() if k not in ("schema", "command")}
    assert inner == mcp_doc


def test_find_json_core_matches_mcp_find_payload(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = mcp_server.build_server(str(graph_path))
    mcp_doc = _mcp_payload(server, "find", {"query": "makeResumeToken"})
    doc, _code = _run_json(capsys, "find", "--graph", str(graph_path), "makeResumeToken")
    derived = ("schema", "command", "match_state", "unique_symbol", "candidates")
    inner = {k: v for k, v in doc.items() if k not in derived}
    assert inner == mcp_doc


# --- review minors: error documents, full shape coverage, more text goldens ---


def test_json_unknown_symbol_is_an_error_document(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(capsys, "callers", "--graph", str(graph_path), "no such thing")
    assert code == 1
    assert doc["schema"] == 1
    assert doc["command"] == "callers"
    assert "error" in doc


def test_json_ambiguous_symbol_is_a_candidates_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ambiguous NAME in --json mode is a schema:1 candidates document with
    exit 1 — never the text path's stderr list an agent can't read."""
    p1 = "cxx . . $ mongo/ResumeToken#parse(aaaaaa)."
    p2 = "cxx . . $ mongo/ResumeToken#parse(bbbbbb)."
    graph = Graph()
    for s in (p1, p2):
        graph.nodes[s] = graph.add_node(s, display_name="parse")
        graph.nodes[s].file = "rt.h"
        graph.nodes[s].line = 0
    path = tmp_path / "amb.db"
    write_sqlite(graph, path)
    doc, code = _run_json(capsys, "callers", "--graph", str(path), "parse")
    assert code == 1
    assert doc["schema"] == 1
    assert doc["command"] == "callers"
    assert doc["ambiguous"] == "parse"
    assert doc["total"] == 2
    assert {c["symbol"] for c in doc["candidates"]} == {p1, p2}


def test_callees_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "callees", "--graph", str(graph_path), CALLER)
    assert code == 0
    assert doc["command"] == "callees"
    assert doc["symbol"] == CALLER
    assert doc["total"] == 1
    assert doc["truncated"] is False
    assert doc["callees"][0]["symbol"] == TARGET
    assert doc["callees"][0]["line"] == 10  # edge line 9, 1-indexed


def test_dependency_cost_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A callee defined under the target prefix: the exact call-site count with
    the per-target breakdown (`top_targets`), segment-boundary matched."""
    tgt = "cxx . . $ lib/F#t(a1)."
    caller = "cxx . . $ app/F#c(a2)."
    graph = Graph()
    graph.nodes[tgt] = graph.add_node(tgt)
    graph.nodes[tgt].file = "lib/x.cpp"
    graph.nodes[tgt].line = 3
    graph.nodes[caller] = graph.add_node(caller)
    graph.nodes[caller].file = "app/y.cpp"
    graph.nodes[caller].line = 0
    graph.add_edge("calls", caller, tgt, file="app/y.cpp", line=4)
    path = tmp_path / "dc.db"
    write_sqlite(graph, path)
    doc, code = _run_json(capsys, "dependency-cost", "--graph", str(path), "--target-path", "lib")
    assert code == 0
    assert doc["command"] == "dependency-cost"
    assert doc["target_paths"] == ["lib"]
    assert doc["total_call_sites"] == 1
    assert doc["distinct_target_symbols"] == 1
    assert doc["top_targets"][0]["symbol"] == tgt
    assert doc["top_targets"][0]["count"] == 1


def test_stats_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "stats", "--graph", str(graph_path), "--limit", "5")
    assert code == 0
    assert doc["command"] == "stats"
    assert doc["group_by"] == "file"
    assert doc["total"] == 1
    assert doc["truncated"] is False
    assert doc["stats"][0]["symbols"] == 3
    assert doc["stats"][0]["edges"] == 2


def test_line_span_json_unavailable_on_stock_graph(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No enclosing ranges (a plain graph): the contract's clean refusal shape."""
    doc, code = _run_json(capsys, "line-span", "--graph", str(graph_path))
    assert code == 1
    assert doc["schema"] == 1
    assert doc["command"] == "line-span"
    assert doc["available"] is False
    assert doc["reason"]


def test_no_incoming_calls_json_unavailable_on_stock_graph(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(capsys, "no-incoming-calls", "--graph", str(graph_path))
    assert code == 1
    assert doc["schema"] == 1
    assert doc["command"] == "no-incoming-calls"
    assert doc["available"] is False
    assert doc["reason"]


def test_global_init_references_json_unavailable_on_stock_graph(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(capsys, "global-init-references", "--graph", str(graph_path), TARGET)
    assert code == 1
    assert doc["schema"] == 1
    assert doc["command"] == "global-init-references"
    assert doc["available"] is False
    assert doc["reason"]


def test_boundary_violations_json_shape(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc, code = _run_json(
        capsys,
        "boundary-violations",
        "--graph",
        str(graph_path),
        "--rule",
        "foo.cpp:nomatch/",
    )
    assert code == 0
    assert doc["schema"] == 1
    assert doc["command"] == "boundary-violations"
    assert doc["total"] == 0
    assert doc["truncated"] is False
    assert doc["violations"] == []
    assert doc["note"]


def test_api_surface_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc, code = _run_json(capsys, "api-surface", "--graph", str(graph_path), "foo.cpp")
    assert code == 0
    assert doc["schema"] == 1
    assert doc["command"] == "api-surface"
    assert doc["module_prefix"] == "foo.cpp"
    assert doc["total"] == 0  # every call site is inside the module
    assert doc["truncated"] is False
    assert doc["surface"] == []
    assert doc["refs_available"] is False  # a graph with no references
    assert doc["note"]  # the refs_available degradation is explained, never bare


def test_class_members_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cls = "cxx . . $ mongo/W#"
    member = "cxx . . $ mongo/W#m(a1)."
    graph = Graph()
    graph.nodes[cls] = graph.add_node(cls)
    graph.nodes[cls].file = "w.h"
    graph.nodes[cls].line = 0
    graph.nodes[member] = graph.add_node(member)
    graph.nodes[member].file = "w.h"
    graph.nodes[member].line = 2
    path = tmp_path / "cm.db"
    write_sqlite(graph, path)
    doc, code = _run_json(capsys, "class-members", "--graph", str(path), cls)
    assert code == 0
    assert doc["schema"] == 1
    assert doc["command"] == "class-members"
    assert doc["symbol"] == cls
    assert doc["total"] == 1
    assert doc["truncated"] is False
    assert doc["members"][0]["symbol"] == member


def test_scc_json_shape(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An acyclic graph: zero components, the standing note present."""
    doc, code = _run_json(capsys, "strongly-connected-components", "--graph", str(graph_path))
    assert code == 0
    assert doc["schema"] == 1
    assert doc["command"] == "strongly-connected-components"
    assert doc["total"] == 0
    assert doc["truncated"] is False
    assert doc["components"] == []
    assert doc["note"]


def test_text_output_byte_identical_stats_and_api_surface(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two more families with pinned golden TEXT: --json must not perturb the
    default output by so much as a byte."""
    assert main(["stats", "--graph", str(graph_path), "--limit", "5"]) == 0
    assert (
        capsys.readouterr().out == "[cppgraph] top 1 of 1 file(s) by symbols+edges+refs\n"
        "       3 sym      2 edges      0 refs  foo.cpp\n"
    )
    assert main(["api-surface", "--graph", str(graph_path), "foo.cpp"]) == 0
    # This fixture store carries no references: the no-refs degradation note.
    assert capsys.readouterr().out == (
        "[cppgraph] top 0 of 0 externally-used symbol(s) under foo.cpp\n"
        "  note: this graph carries no reference index (built --no-references) — "
        "call sites only, a type used outside the module is invisible to this "
        "list; rebuild with references to count those uses too\n"
    )
