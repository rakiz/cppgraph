from __future__ import annotations

from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.model import Graph


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
    path = tmp_path / "graph.json"
    graph.save_json(path)
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


def test_impact_lists_transitive_callers(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        ["impact", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 symbol(s)" in out
    assert "caller(a2)." in out
