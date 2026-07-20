"""Tests for the guided `cppgraph init` wizard.

The pure helpers are tested directly; the interactive `run_init` is driven with
injected input/print so the whole flow is scripted without touching real stdio.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.init import (
    artifact_status,
    find_compdb,
    onboarding_plan,
    run_init,
    scip_clang_info,
)


def _boom_input(_prompt: str) -> str:
    raise AssertionError("non-interactive mode must not prompt for input")


def _make_504_bindir(tmp_path: Path) -> Path:
    d = tmp_path / "bin504"
    d.mkdir()
    binary = d / "scip-clang"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (d / "scip-clang.json").write_text(json.dumps({"variant": "enclosing_range-504"}))
    return d


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
    assert "Plan:" in out
    assert "whole tree" in out
    assert "(no tests)" not in out  # declined
    assert "(attributed-refs)" not in out  # no #504 binary
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
    assert "incremental update" in out


def test_project_root_is_git_toplevel_not_build_dir(tmp_path: Path) -> None:
    """A compile_commands.json in build/ must NOT make build/ the project root —
    that drops every source under ../src/. The root is the git top-level."""
    import subprocess as sp

    sp.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    build = tmp_path / "build"
    build.mkdir()
    compdb = _write_compdb(build / "compile_commands.json")

    lines, prnt = _capturing_print()
    run_init(
        compdb=str(compdb),
        run=False,
        filter="",  # non-interactive
        input_fn=_boom_input,
        print_fn=prnt,
    )
    # The graph output path must sit under the repo root, not build/.
    graph_line = next(line for line in lines if line.strip().startswith("graph:"))
    assert str(tmp_path.resolve()) in graph_line
    assert "/build/" not in graph_line


def test_run_init_errors_without_compdb(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no compile_commands.json here or above (tmp)
    lines, prnt = _capturing_print()
    rc = run_init(run=False, input_fn=_scripted_input([]), print_fn=prnt)
    # Either finds nothing (rc 1) — assert it did not crash and reported.
    assert rc == 1
    assert any("compile_commands.json" in line for line in lines)


# --- non-interactive (mode a) -----------------------------------------------


def test_run_init_non_interactive_assembles_command_without_prompting(tmp_path: Path) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    lines, prnt = _capturing_print()
    rc = run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        filter="src/mongo",  # implies non-interactive
        no_tests=True,
        input_fn=_boom_input,  # must never be called
        print_fn=prnt,
    )
    assert rc == 0
    out = "\n".join(lines)
    assert "(no tests)" in out
    assert "src/mongo" in out
    assert "Plan:" in out


def test_non_interactive_keeps_existing_graph_by_default(tmp_path: Path) -> None:
    """A -y run must NOT rebuild (overwrite) an existing graph — the safety that the
    agent path relies on. It reuses; only --from-scratch redoes."""
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    (cpg / "proj.scip").write_text("")
    (cpg / "proj.graph.db").write_text("")
    lines, prnt = _capturing_print()
    run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        filter="",  # non-interactive
        input_fn=_boom_input,
        print_fn=prnt,
    )
    out = "\n".join(lines)
    assert "keeping it" in out
    assert "scip: reuse existing" in out
    assert "graph: reuse existing" in out


def test_non_interactive_from_scratch_rebuilds(tmp_path: Path) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    (cpg / "proj.scip").write_text("")
    (cpg / "proj.graph.db").write_text("")
    lines, prnt = _capturing_print()
    run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        filter="",
        from_scratch=True,
        input_fn=_boom_input,
        print_fn=prnt,
    )
    out = "\n".join(lines)
    assert "scip: recompute" in out
    assert "graph: rebuild" in out


def test_run_init_non_interactive_gates_attribution(tmp_path: Path, monkeypatch) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")

    # With a #504 binary: --attributed-refs survives.
    monkeypatch.setenv("CPPGRAPH_BIN_DIR", str(_make_504_bindir(tmp_path)))
    lines, prnt = _capturing_print()
    run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        non_interactive=True,
        filter="",
        attributed_refs=True,
        input_fn=_boom_input,
        print_fn=prnt,
    )
    assert "(attributed-refs)" in "\n".join(lines)

    # Without one (empty bin dir): dropped, with a warning.
    monkeypatch.setenv("CPPGRAPH_BIN_DIR", str(tmp_path / "empty"))
    lines, prnt = _capturing_print()
    run_init(
        compdb=str(compdb),
        project_root=str(tmp_path),
        name="proj",
        run=False,
        non_interactive=True,
        filter="",
        attributed_refs=True,
        input_fn=_boom_input,
        print_fn=prnt,
    )
    out = "\n".join(lines)
    assert "(attributed-refs)" not in out  # dropped from the plan
    assert any("warning" in line.lower() for line in lines)


# --- structured output (mode b) ---------------------------------------------


def test_onboarding_plan_shape(tmp_path: Path) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    from cppgraph.compdb import load_compdb

    plan = onboarding_plan(compdb, load_compdb(str(compdb)), tmp_path, "proj")
    assert plan["summary"]["total"] == 3
    assert plan["summary"]["tests"] == 1
    assert plan["summary"]["tests_pct"] == 33
    assert plan["scip_clang"]["supports_attribution"] is False  # empty CPPGRAPH_BIN_DIR
    keys = [q["key"] for q in plan["questions"]]
    assert keys == ["filter", "no_tests", "attributed_refs"]
    attr_q = plan["questions"][2]
    assert attr_q["available"] is False
    # The filter question carries concrete options from the breakdown, so the
    # agent presents real choices (whole tree + each subtree) rather than inventing.
    filter_values = [o["value"] for o in plan["questions"][0]["options"]]
    assert "" in filter_values  # whole tree
    assert "db" in filter_values  # a subtree from the breakdown


def test_onboarding_plan_offers_reuse_when_indexed(tmp_path: Path) -> None:
    """When a project is already indexed, the plan leads with a reuse-vs-recompute
    question carrying the existing artifacts' details — so the agent presents the
    choice instead of silently reusing or clobbering."""
    from cppgraph.compdb import load_compdb
    from cppgraph.proto import scip_pb2

    compdb = _write_compdb(tmp_path / "compile_commands.json")
    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    idx = scip_pb2.Index(
        metadata=scip_pb2.Metadata(tool_info=scip_pb2.ToolInfo(name="scip-clang", version="0.4.0")),
        documents=[scip_pb2.Document(relative_path="a.cpp")],
    )
    (cpg / "proj.scip").write_bytes(idx.SerializeToString())

    plan = onboarding_plan(compdb, load_compdb(str(compdb)), tmp_path, "proj")
    assert plan["questions"][0]["key"] == "reuse"
    assert [o["value"] for o in plan["questions"][0]["options"]] == ["reuse", "recompute"]
    assert plan["existing"]["scip"]["tool_version"] == "0.4.0"
    assert "scip-clang" in plan["questions"][0]["info"]


def test_init_plan_json_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    compdb = _write_compdb(tmp_path / "compile_commands.json")
    rc = main(
        ["init", str(compdb), "--project-root", str(tmp_path), "--name", "proj", "--plan-json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "proj"
    assert payload["summary"]["total"] == 3
