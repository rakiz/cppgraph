from __future__ import annotations

from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.model import Graph
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, write_sqlite


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#makeResumeToken(a1).", display_name="makeResumeToken")
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#caller(a2).",
        "cxx . . $ mongo/Foo#makeResumeToken(a1).",
        file="foo.cpp",
        line=9,
    )
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return path


def test_find_reports_matches(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["find", "--graph", str(graph_path), "makeResumeToken"]) == 0
    out = capsys.readouterr().out
    assert "makeResumeToken" in out


def test_find_no_match_returns_nonzero(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["find", "--graph", str(graph_path), "nope"]) == 1


def test_callers_lists_caller_with_location(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["callers", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "cxx . . $ mongo/Foo#caller(a2)." in out
    assert "foo.cpp:10" in out  # stored 0-indexed line 9 -> displayed as 1-indexed 10


def test_callees_lists_callee(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["callees", "--graph", str(graph_path), "cxx . . $ mongo/Foo#caller(a2)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "makeResumeToken(a1)." in out


def test_callers_unknown_symbol_errors(graph_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["callers", "--graph", str(graph_path), "nonexistent"])


def test_path_reports_call_chain(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "path",
            "--graph",
            str(graph_path),
            "cxx . . $ mongo/Foo#caller(a2).",
            "cxx . . $ mongo/Foo#makeResumeToken(a1).",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 hop(s)" in out
    assert "makeResumeToken(a1)." in out


def test_path_no_path_returns_nonzero(graph_path: Path) -> None:
    exit_code = main(
        [
            "path",
            "--graph",
            str(graph_path),
            "cxx . . $ mongo/Foo#makeResumeToken(a1).",
            "cxx . . $ mongo/Foo#caller(a2).",
        ]
    )
    assert exit_code == 1


def test_build_records_source_commit_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Minimal synthetic .scip: a callable definition, so the graph is non-empty.
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///some/repo"
    doc = index.documents.add(relative_path="foo.cpp")
    occ = doc.occurrences.add(symbol="cxx . . $ mongo/Foo#bar(a1).",
                              symbol_roles=scip_pb2.SymbolRole.Definition)
    occ.range.extend([0, 0, 3])
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    out = tmp_path / "graph.db"

    exit_code = main(
        ["build", "--scip", str(scip_path), "--out", str(out),
         "--source-commit", "cafebabe", "--source-dirty"]
    )
    assert exit_code == 0
    assert "cafebabe" in capsys.readouterr().out

    meta = GraphStore(out).meta()
    assert meta["source_commit"] == "cafebabe"
    assert meta["source_dirty"] == "true"
    assert meta["project_root"] == "file:///some/repo"


def test_update_applies_partial_reindex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Start from a store where foo.cpp has a() calling old().
    original = Graph()
    original.add_edge("calls", "cxx . . $ mongo/Foo#a(a1).",
                      "cxx . . $ mongo/Foo#old(o1).", file="foo.cpp", line=5)
    db = tmp_path / "graph.db"
    write_sqlite(original, db)

    # Partial re-index of foo.cpp: a() now calls new().
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///some/repo"
    doc = index.documents.add(relative_path="foo.cpp")
    d = doc.occurrences.add(symbol="cxx . . $ mongo/Foo#a(a1).",
                            symbol_roles=scip_pb2.SymbolRole.Definition)
    d.range.extend([2, 0, 3])
    c = doc.occurrences.add(symbol="cxx . . $ mongo/Foo#new(n1).")
    c.range.extend([6, 0, 3])
    scip_path = tmp_path / "partial.scip"
    scip_path.write_bytes(index.SerializeToString())

    exit_code = main(["update", "--graph", str(db), "--scip", str(scip_path),
                      "--source-commit", "newsha"])
    assert exit_code == 0

    store = GraphStore(db)
    assert [e.dst for e in store.callees_of("cxx . . $ mongo/Foo#a(a1).")] == [
        "cxx . . $ mongo/Foo#new(n1)."
    ]
    assert not store.has_symbol("cxx . . $ mongo/Foo#old(o1).")
    assert store.meta()["source_commit"] == "newsha"


@pytest.fixture
def explain_graph(tmp_path: Path) -> Path:
    """A graph whose symbol has a real definition site (file + line)."""
    graph = Graph()
    node = graph.add_node("cxx . . $ mongo/Foo#bar(a1).", display_name="bar")
    node.file = "src/foo.cpp"
    node.line = 3  # 0-indexed -> source line 4
    # one caller and one callee so explain can summarize both directions
    graph.add_edge("calls", "cxx . . $ mongo/Foo#caller(a2).",
                   "cxx . . $ mongo/Foo#bar(a1).", file="src/foo.cpp", line=20)
    graph.add_edge("calls", "cxx . . $ mongo/Foo#bar(a1).",
                   "cxx . . $ mongo/Foo#callee(a3).", file="src/foo.cpp", line=5)
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return path


def _write_source(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "foo.cpp").write_text(
        "line0\nline1\nline2\nint Foo::bar() {\n  return callee();\n}\nline6\n"
    )


def test_explain_shows_definition_and_source_snippet(
    explain_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "checkout"
    _write_source(root)
    exit_code = main(
        ["explain", "--graph", str(explain_graph),
         "cxx . . $ mongo/Foo#bar(a1).", "--root", str(root)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "src/foo.cpp:4" in out          # def location, 1-indexed
    assert "int Foo::bar() {" in out        # the snippet line
    assert "1 caller(s)" in out
    assert "1 callee(s)" in out


def test_explain_missing_source_is_graceful(
    explain_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --root points nowhere useful: still reports location + counts, no crash.
    exit_code = main(
        ["explain", "--graph", str(explain_graph),
         "cxx . . $ mongo/Foo#bar(a1).", "--root", str(tmp_path / "absent")]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "src/foo.cpp:4" in out
    assert "source not found" in out.lower()


def test_explain_unknown_symbol_errors(explain_graph: Path) -> None:
    with pytest.raises(SystemExit):
        main(["explain", "--graph", str(explain_graph), "nonexistent", "--root", "/tmp"])


def test_impact_lists_transitive_callers(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["impact", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 symbol(s)" in out
    assert "caller(a2)." in out
