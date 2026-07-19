"""Tests for the guided `cppgraph init` wizard.

The pure helpers are tested directly; the interactive `run_init` is driven with
injected input/print so the whole flow is scripted without touching real stdio.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cppgraph.init import (
    IndexPlan,
    artifact_status,
    build_reindex_argv,
    build_update_argv,
    find_compdb,
    run_init,
    scip_clang_info,
)

_ENTRIES = [
    {"file": "/repo/src/mongo/db/query.cpp"},
    {"file": "/repo/src/mongo/db/query_test.cpp"},
    {"file": "/repo/src/mongo/client/conn.cpp"},
]


def _write_compdb(path: Path, entries=_ENTRIES) -> Path:
    path.write_text(json.dumps(entries))
    return path


def _scripted_input(answers: list[str]):
    """An input_fn that returns queued answers, then '' (empty = accept default)."""
    q = list(answers)

    def _input(_prompt: str) -> str:
        return q.pop(0) if q else ""

    return _input


def _capturing_print():
    lines: list[str] = []

    def _print(*args) -> None:
        lines.append(" ".join(str(a) for a in args))

    return lines, _print


# --- pure helpers -----------------------------------------------------------


def test_find_compdb_walks_up_from_a_subdir(tmp_path: Path) -> None:
    _write_compdb(tmp_path / "compile_commands.json")
    deep = tmp_path / "src" / "mongo"
    deep.mkdir(parents=True)
    assert find_compdb(deep) == (tmp_path / "compile_commands.json").resolve()


def test_find_compdb_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_compdb(tmp_path) is None


def test_scip_clang_info_reads_variant_sidecar(tmp_path: Path) -> None:
    binary = tmp_path / "scip-clang"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (tmp_path / "scip-clang.json").write_text(json.dumps({"variant": "enclosing_range-504"}))
    assert scip_clang_info(tmp_path) == (True, "enclosing_range-504")


def test_scip_clang_info_absent(tmp_path: Path) -> None:
    assert scip_clang_info(tmp_path) == (False, None)


def test_artifact_status_detects_stages(tmp_path: Path) -> None:
    (tmp_path / "proj.scip").write_text("")
    st = artifact_status(tmp_path, "proj")
    assert st == {"compdb": False, "scip": True, "graph": False}


def test_build_reindex_argv_orders_flags_then_positionals(tmp_path: Path) -> None:
    plan = IndexPlan(
        compdb=Path("/c/cc.json"),
        project_root=Path("/c"),
        name="proj",
        src_filter="src/mongo",
        no_tests=True,
        attributed_refs=True,
    )
    argv = build_reindex_argv(Path("/s/reindex.sh"), plan)
    assert argv == [
        "/s/reindex.sh",
        "--attributed-refs",
        "--no-tests",
        "/c/cc.json",
        "src/mongo",
        "proj",
        "/c",
    ]


def test_build_reindex_argv_whole_tree_no_flags() -> None:
    plan = IndexPlan(
        compdb=Path("/c/cc.json"),
        project_root=Path("/c"),
        name="proj",
        src_filter="",
        no_tests=False,
        attributed_refs=False,
    )
    argv = build_reindex_argv(Path("/s/reindex.sh"), plan)
    assert argv == ["/s/reindex.sh", "/c/cc.json", "", "proj", "/c"]


def test_build_update_argv() -> None:
    argv = build_update_argv(
        Path("/s/reindex.sh"), Path("/p/.cppgraph/proj.graph.db"), Path("/p/cc.json")
    )
    assert argv == ["/s/reindex.sh", "--update", "/p/.cppgraph/proj.graph.db", "/p/cc.json"]


# --- interactive flow -------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_local_scip_clang(tmp_path_factory, monkeypatch):
    """Point scip-clang discovery at an empty dir so `_ask_attributed` is
    deterministic (stock/absent -> no attribution, no prompt) regardless of the
    test machine."""
    monkeypatch.setenv("CPPGRAPH_BIN_DIR", str(tmp_path_factory.mktemp("nobin")))


def test_run_init_assembles_full_build_command(tmp_path: Path) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    # answers: filter (empty=whole tree), use-scope (accept), exclude-tests (no)
    lines, prnt = _capturing_print()
    rc = run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,  # print only
        input_fn=_scripted_input(["", "", "n"]),
        print_fn=prnt,
    )
    assert rc == 0
    out = "\n".join(lines)
    assert "translation unit(s)" in out  # the summary was shown
    assert "reindex.sh" in out
    assert "proj" in out
    assert "--no-tests" not in out  # declined
    assert "--attributed-refs" not in out  # no #504 binary
    assert "Not run" in out


def test_run_init_offers_update_when_graph_exists(tmp_path: Path) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    (cpg / "proj.graph.db").write_text("")  # existing graph
    lines, prnt = _capturing_print()
    rc = run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        input_fn=_scripted_input(["u"]),  # choose incremental update
        print_fn=prnt,
    )
    assert rc == 0
    out = "\n".join(lines)
    assert "already exists" in out
    assert "--update" in out


def test_run_init_errors_without_compdb(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no compile_commands.json here or above (tmp)
    lines, prnt = _capturing_print()
    rc = run_init(run=False, input_fn=_scripted_input([]), print_fn=prnt)
    # Either finds nothing (rc 1) — assert it did not crash and reported.
    assert rc == 1
    assert any("compile_commands.json" in line for line in lines)
