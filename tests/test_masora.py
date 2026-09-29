"""Tests for the Masora fact injection (contract v1, masora.py).

Everything runs against a stub `masora` executable (an env-var-driven shell
script on PATH) or injected runner callables — never a real Masora install.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from cppgraph import masora, mcp_server
from cppgraph.cli import main
from cppgraph.model import Graph, Node
from cppgraph.store import write_sqlite

FOO = "cxx . . $ mongo/Foo#makeResumeToken(a1)."
CALLER = "cxx . . $ mongo/Foo#caller(a2)."

STUB_SCRIPT = """\
#!/bin/sh
if [ -n "$MASORA_STUB_ECHO_ARGS" ]; then
  printf '%s' "$*"
  exit 0
fi
if [ -n "$MASORA_STUB_SLEEP" ]; then
  sleep "$MASORA_STUB_SLEEP"
fi
if [ -n "$MASORA_STUB_READ_STDIN" ]; then
  cat > /dev/null
fi
if [ -n "$MASORA_STUB_HUGE" ]; then
  dd if=/dev/zero bs=65536 count=64 2>/dev/null
fi
if [ -n "$MASORA_STUB_STDOUT" ]; then
  printf '%s' "$MASORA_STUB_STDOUT"
fi
exit "${MASORA_STUB_EXIT:-0}"
"""


def fact_doc(**overrides: Any) -> dict[str, Any]:
    """One contract-v1 fact shaped like §3's first example."""
    fact = {
        "lineage": "01J8Z3K0000000000000000000",
        "summary": "Resume token invalidated by a shard key change",
        "resolution": "current",
        "verification": "verified(llm)",
        "flags": "-",
        "anchors_matched": ["scip-clang cxx . . mongo/ResumeTokenData#makeResumeToken()."],
    }
    fact.update(overrides)
    return {"contract_version": 1, "stale_warning": False, "facts": [fact]}


def doc_with(*facts: dict[str, Any], **doc_overrides: Any) -> dict[str, Any]:
    doc = {"contract_version": 1, "stale_warning": False, "facts": list(facts)}
    doc.update(doc_overrides)
    return doc


@pytest.fixture
def stub_masora(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A stub `masora` on PATH; behavior via MASORA_STUB_* env vars."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    script = bindir / "masora"
    script.write_text(STUB_SCRIPT)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    graph = Graph()
    graph.add_node(FOO, display_name="makeResumeToken")
    graph.add_edge("calls", CALLER, FOO, file="foo.cpp", line=9)
    path = tmp_path / "graph.db"
    write_sqlite(graph, path, meta={"project_root": str(tmp_path)})
    return path


# --- (j) flag semantics ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, False, id="unset-off"),
        pytest.param("1", True, id="1-on"),
        pytest.param("true", True, id="true-on"),
        pytest.param("TRUE", True, id="TRUE-on"),
        pytest.param("0", False, id="0-off"),
        pytest.param("false", False, id="false-off"),
        pytest.param("FALSE", False, id="FALSE-off"),
        pytest.param("off", False, id="off-off"),
        pytest.param("", False, id="empty-off"),
        pytest.param("garbage", False, id="garbage-fails-closed"),
        pytest.param("yes", False, id="yes-fails-closed"),
    ],
)
def test_flag_semantics_matrix(raw: str | None, expected: bool) -> None:
    env = {} if raw is None else {"CPPGRAPH_MASORA": raw}
    assert masora.enabled(env) is expected


def test_flag_reads_the_process_env_when_no_env_dict_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    assert masora.enabled() is True
    monkeypatch.setenv("CPPGRAPH_MASORA", "0")
    assert masora.enabled() is False
    monkeypatch.delenv("CPPGRAPH_MASORA")
    assert masora.enabled() is False


# --- the shared entry point: query_lines --------------------------------------


def test_flag_on_renders_one_fact_line(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == [
        "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
    ]


def test_flag_off_contributes_nothing_and_never_spawns(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-change guarantee (flag off): no runner call, empty contribution —
    the response is byte-identical to pre-change output."""
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    calls: list[tuple[str, str | None, float]] = []

    def recording_runner(repo: str, symbol: str | None, timeout: float) -> None:
        calls.append((repo, symbol, timeout))
        return None

    for raw in (None, "0", "false", "garbage"):
        env = {} if raw is None else {"CPPGRAPH_MASORA": raw}
        assert masora.query_lines({}, FOO, env=env, runner=recording_runner) == []
    assert calls == []


def test_repo_root_and_symbol_are_passed_to_the_cli(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_ECHO_ARGS", "1")
    argv = masora.run_masora_facts(str(tmp_path), FOO, timeout=2.0)
    assert argv == f"facts --repo {tmp_path} --symbol {FOO}"


def test_no_symbol_omits_the_flag(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_ECHO_ARGS", "1")
    argv = masora.run_masora_facts("/some/repo", None, timeout=2.0)
    assert argv == "facts --repo /some/repo"


def test_repo_root_prefers_the_store_meta_and_falls_back_to_cwd(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_ECHO_ARGS", "1")
    recorded = masora.repo_root({"project_root": str(tmp_path)})
    assert recorded == str(tmp_path)
    cwd_root = masora.repo_root({})
    assert cwd_root == os.getcwd()


# --- (a) facts rendered, capped at 2, visible truncation ----------------------


def test_max_two_facts_kept_and_truncation_visible(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    short = dict(fact_doc(), facts=[])
    short["facts"] = [
        {
            "lineage": f"0{i}J8Z3K000000000000000000",
            "summary": f"fact number {i}",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        }
        for i in range(5)
    ]
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(short))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[:2] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
    ]
    assert lines[2] == "… +3 more — masora search"


def test_two_short_facts_both_render_without_truncation(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "a",
            "summary": "first",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        },
        {
            "lineage": "b",
            "summary": "second",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        },
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora: first [current]", "masora: second [current]"]


def test_duplicate_lineages_deduped(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "a",
            "summary": "first",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        },
        {
            "lineage": "a",
            "summary": "first again",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        },
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora: first [current]"]


def test_empty_facts_render_nothing(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- (b) stale warning ---------------------------------------------------------


def test_stale_warning_rendered_terse_and_first(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "a",
            "summary": "some claim",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        },
        stale_warning=True,
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[0] == masora.STALE_WARNING_LINE
    assert lines[1] == "masora: some claim [current]"


def test_stale_warning_false_and_null_render_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    for value in (False, None):
        doc = doc_with(stale_warning=value)
        monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
        assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- (c) non-zero exit ---------------------------------------------------------


def test_nonzero_exit_renders_nothing(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_EXIT", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- (d) binary absent ---------------------------------------------------------


def test_binary_absent_skips_silently_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No masora on PATH: instant silent skip — zero subprocess cost."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    calls: list[str] = []

    def failing_runner(repo: str, symbol: str | None, timeout: float) -> str | None:
        calls.append(repo)
        return None

    started = time.monotonic()
    lines = masora.query_lines({}, FOO, env=dict(os.environ), runner=failing_runner)
    elapsed = time.monotonic() - started
    assert lines == []
    assert calls == []  # the which() was the only lookup; no spawn attempt
    assert elapsed < 1.0


# --- (e) subprocess timeout ----------------------------------------------------


def test_timeout_kills_the_subprocess_and_skips_silently(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_SLEEP", "30")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    started = time.monotonic()
    lines = masora.query_lines({}, FOO, env=dict(os.environ), timeout=0.3)
    elapsed = time.monotonic() - started
    assert lines == []
    assert elapsed < 5.0  # killed at the injected 0.3 s budget, not the 30 s stub


# --- (f) unparsable / empty stdout ---------------------------------------------


@pytest.mark.parametrize("stdout", ["", "not json", "[]", '{"facts": "nope"}'])
def test_unparsable_stdout_renders_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", stdout)
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


def test_runner_exception_never_surfaces(stub_masora: Path) -> None:
    def exploding_runner(repo: str, symbol: str | None, timeout: float) -> str | None:
        raise RuntimeError("boom")

    assert masora.query_lines({}, FOO, env={"CPPGRAPH_MASORA": "1"}, runner=exploding_runner) == []


# --- (g) unknown contract_version ----------------------------------------------


@pytest.mark.parametrize("version", [2, 0, "1", None])
def test_unknown_or_missing_contract_version_renders_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, version: Any
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = fact_doc()
    if version is None:
        del doc["contract_version"]
    else:
        doc["contract_version"] = version
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


@pytest.mark.parametrize("version", [True, False])
def test_boolean_contract_version_rejected(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, version: bool
) -> None:
    """A JSON bool would compare == 1 numerically; the parser must reject it."""
    doc = fact_doc()
    doc["contract_version"] = version
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- fail-closed fact fields (whole-document rejection) -------------------------


@pytest.mark.parametrize(
    "field_override",
    [
        {"summary": 123},
        {"resolution": 7},
        {"flags": 123},
        {"flags": ["suspect"]},
        {"verification": ["verified(llm)"]},
        {"verification": 3.5},
        {"lineage": 99},
        {"anchors_matched": "scip-clang cxx . . mongo/Engine#commitShard()."},
        {"anchors_matched": [1, 2]},
        {"anchors_matched": {"a": "b"}},
    ],
)
def test_malformed_fact_field_rejects_whole_document(field_override: dict[str, Any]) -> None:
    doc = fact_doc()
    doc["facts"].append(
        {"lineage": "ok-lineage", "summary": "fine", "resolution": "current", **field_override}
    )
    assert masora.parse_contract(json.dumps(doc)) is None


def test_absent_flag_fields_tolerated() -> None:
    """Absent (or null) optional fact fields default; only a present-but-wrong
    type rejects the document."""
    fact = {"summary": "s", "resolution": "current"}
    contract = masora.parse_contract(
        json.dumps({"contract_version": 1, "stale_warning": False, "facts": [fact]})
    )
    assert contract is not None
    assert contract.facts == (
        masora.Fact(lineage="", summary="s", resolution="current", verification="", flags=()),
    )


def test_query_with_one_valid_one_malformed_fact_injects_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partial delivery: a single malformed fact poisons the whole document."""
    doc = fact_doc()
    doc["facts"].append(
        {"lineage": "ok-lineage", "summary": 123, "resolution": "current", "flags": "-"}
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- never-zero rule + stale-with-no-facts --------------------------------------


def test_single_oversized_fact_still_renders(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-zero rule: one fact whose line alone exceeds the token budget
    still renders (a lone oversized fact beats a bare cap). Legal shape —
    120 chars is §3's cap — but 120 CJK chars estimate far above 60 tokens
    (est_tokens counts them ~1 token each)."""
    doc = doc_with(
        {
            "lineage": "z",
            "summary": "語" * 120,
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        }
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert len(lines) == 1
    assert lines[0].startswith("masora: ")


def test_stale_warning_with_no_facts_renders_nothing() -> None:
    """A stale index with zero matching facts injects nothing — no dangling
    warning on every symbol query of a stale repo (§1: no output without
    matching facts)."""
    contract = masora.Contract(facts=(), stale_warning=True)
    assert masora.render_lines(contract) == []


def test_stale_warning_with_no_facts_nothing_injected(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with(stale_warning=True)))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- (h) token budget + masora NOT rendering ------------------------------------


def test_budget_holds_for_the_max_case(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two max-length (120-char) summaries cannot both fit the 60-token budget:
    the second is dropped and the cap is reported visibly."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    summary = "word " * 24  # exactly 120 chars
    doc = doc_with(
        {
            "lineage": "a",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
        },
        {
            "lineage": "b",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
        },
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert lines[-1] == "… +1 more — masora search"


def test_resolution_none_renders_as_negative_knowledge(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "c",
            "summary": "changeStream re-opens on resumeToken == null",
            "resolution": "none",
            "verification": "unverified",
            "flags": "-",
        }
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora NOT: changeStream re-opens on resumeToken == null [refuted]"]


def test_status_labels_surface_as_such(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "d",
            "summary": "Lock L must be held before calling commitShard",
            "resolution": "stale",
            "verification": "unverified",
            "flags": "suspect",
        }
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: Lock L must be held before calling commitShard [stale, suspect — re-check]"
    ]


# --- CLI surface wiring ----------------------------------------------------------


def test_cli_callers_appends_masora_lines(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    assert main(["callers", "--graph", str(graph_path), CALLER]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(
        "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
    )


def test_cli_explain_appends_masora_lines(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    assert main(["explain", "--graph", str(graph_path), FOO]) == 0
    out = capsys.readouterr().out
    assert "masora: Resume token invalidated by a shard key change [current, verified(llm)]" in out


def test_cli_callees_appends_masora_lines(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    assert main(["callees", "--graph", str(graph_path), CALLER]) == 0
    out = capsys.readouterr().out
    assert "masora: Resume token invalidated by a shard key change [current, verified(llm)]" in out


# --- (i) zero-change guarantee on the CLI ---------------------------------------


def test_cli_output_identical_with_flag_off(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unset (= default off) and explicit 0/false produce byte-identical
    output with no masora contribution at all."""
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    outputs = []
    for raw in (None, "0", "false"):
        monkeypatch.delenv("CPPGRAPH_MASORA", raising=False)
        if raw is not None:
            monkeypatch.setenv("CPPGRAPH_MASORA", raw)
        for command in ("callers", "callees", "explain"):
            argv = [command, "--graph", str(graph_path)]
            argv.append(CALLER if command != "explain" else FOO)
            assert main(argv) == 0
            outputs.append(capsys.readouterr().out)
    for out in outputs:
        assert "masora" not in out


# --- MCP surface wiring ----------------------------------------------------------


@pytest.fixture
def mcp_store(tmp_path: Path) -> Any:
    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=0)
    graph.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="foo.cpp", line=9)
    graph.add_edge("calls", CALLER, FOO, file="foo.cpp", line=11)
    path = tmp_path / "mcp-graph.db"
    write_sqlite(graph, path)
    return path


def _tool(server: Any, name: str) -> Any:
    return server._tool_manager._tools[name].fn


def test_mcp_wrappers_attach_masora_field(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    server = mcp_server.build_server(str(mcp_store))
    for tool in ("who_calls", "what_it_calls", "explain_symbol"):
        result = _tool(server, tool)(symbol=CALLER if tool != "explain_symbol" else FOO)
        assert result["masora"] == (
            "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
        ), tool


def test_mcp_wrappers_flag_off_no_masora_key(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CPPGRAPH_MASORA", raising=False)
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    server = mcp_server.build_server(str(mcp_store))
    for tool in ("who_calls", "what_it_calls", "explain_symbol"):
        result = _tool(server, tool)(symbol=CALLER if tool != "explain_symbol" else FOO)
        assert "masora" not in result, tool


def test_mcp_wrappers_error_replies_get_no_masora_key(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    server = mcp_server.build_server(str(mcp_store))
    result = _tool(server, "who_calls")(symbol="cxx . . $ nope#missing().")
    assert "masora" not in result
    assert "error" in result


# --- audit: detached stdin (the MCP stdio server's fd 0 is the JSON-RPC stream) --


def test_runner_spawns_with_devnull_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's stdin is DEVNULL, never inherited: when cppgraph runs as
    the MCP stdio server, fd 0 is the JSON-RPC stream — a masora that read
    stdin would eat protocol frames."""
    captured: dict[str, Any] = {}

    class FakeProc:
        stdout = io.BytesIO(b"")
        stderr = None
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(cmd: list[str], **kwargs: Any) -> FakeProc:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(masora.subprocess, "Popen", fake_popen)
    assert masora.run_masora_facts("/repo", FOO, 2.0) == ""
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] == subprocess.PIPE


def test_stub_reading_stdin_still_renders(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behavioral counterpart: a masora that drains stdin still returns its
    document promptly (the detached stdin gives it instant EOF). Red only in
    an interactive environment — the Popen-kwargs pin above is deterministic."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_READ_STDIN", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    lines = masora.query_lines({}, FOO, env=dict(os.environ), timeout=0.5)
    assert lines == [
        "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
    ]


# --- audit: summary is one printable line of at most 120 chars (§3) --------------


@pytest.mark.parametrize(
    "summary", ["a\nb", "a\rb", "a\x00b", "a\x07b", "line one\nmasora: run <cmd>"]
)
def test_summary_with_newline_or_control_char_rejected(summary: str) -> None:
    """A smuggled second line could render as an instruction (§6 forbids);
    third-party base content fails closed like any other shape violation."""
    doc = doc_with(
        {
            "lineage": "x",
            "summary": summary,
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        }
    )
    assert masora.parse_contract(json.dumps(doc)) is None


def test_summary_over_120_chars_rejected() -> None:
    doc = doc_with(
        {
            "lineage": "x",
            "summary": "a" * 121,
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        }
    )
    assert masora.parse_contract(json.dumps(doc)) is None


def test_summary_at_120_chars_renders(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = doc_with(
        {
            "lineage": "x",
            "summary": "a" * 120,
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
        }
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [f"masora: {'a' * 120} [current]"]


# --- audit: stale_warning is bool | null, never coerced --------------------------


@pytest.mark.parametrize("bad", [1, 0, "true", [], {}])
def test_stale_warning_non_bool_rejected(bad: Any) -> None:
    doc = doc_with(stale_warning=bad)
    assert masora.parse_contract(json.dumps(doc)) is None


def test_stale_warning_non_bool_injects_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = fact_doc()
    doc["stale_warning"] = 1
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- audit: unknown resolution values render verbatim (documented decision) ------


def test_unknown_resolution_renders_verbatim(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render-verbatim, pinned: statuses are evidence labels (§6); a future
    contract value arriving without a version bump is Masora's drift to
    report, not to guess at — fail-closed is reserved for shape violations."""
    doc = doc_with(
        {
            "lineage": "u",
            "summary": "claim",
            "resolution": "reopened",
            "verification": "unverified",
            "flags": "-",
        }
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == ["masora: claim [reopened]"]


# --- audit: repo_root never guesses a different checkout -------------------------


def test_recorded_root_missing_on_disk_returns_none(tmp_path: Path) -> None:
    """A recorded project_root that vanished (moved checkout, other machine)
    must NOT fall back to the cwd — facts for the wrong repo would violate §4."""
    assert masora.repo_root({"project_root": str(tmp_path / "gone")}) is None


def test_missing_recorded_root_skips_injection(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def recording(repo: str, symbol: str | None, timeout: float) -> str | None:
        calls.append(repo)
        return None

    lines = masora.query_lines(
        {"project_root": str(tmp_path / "gone")},
        FOO,
        env={"CPPGRAPH_MASORA": "1"},
        which=lambda name: "/fake/masora",
        runner=recording,
    )
    assert lines == []
    assert calls == []


def test_repo_root_with_nul_byte_never_raises(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poisoned project_root (embedded NUL) makes is_dir() raise ValueError;
    the never-raises promise of query_lines must hold — silent skip, no
    spawn."""
    calls: list[str] = []

    def recording(repo: str, symbol: str | None, timeout: float) -> str | None:
        calls.append(repo)
        return None

    lines = masora.query_lines(
        {"project_root": "/bad\0root"},
        FOO,
        env={"CPPGRAPH_MASORA": "1"},
        which=lambda name: "/fake/masora",
        runner=recording,
    )
    assert lines == []
    assert calls == []


# --- audit: bounded stdout (a fast-writing masora is capped, overflow = skip) ----


def test_output_cap_constant_pinned() -> None:
    assert masora.MAX_OUTPUT_BYTES == 1_048_576


def test_oversized_output_skipped(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """4 MiB of output (cap is 1 MiB): the runner kills the child and skips —
    memory stays bounded, nothing parses, nothing renders."""
    monkeypatch.setenv("MASORA_STUB_HUGE", "1")
    started = time.monotonic()
    out = masora.run_masora_facts(str(tmp_path), None, 2.0)
    elapsed = time.monotonic() - started
    assert out is None
    assert elapsed < 2.0


# --- audit: token estimate counts CJK chars ~1 token each ------------------------


def test_est_tokens_counts_cjk_chars_individually() -> None:
    assert masora.est_tokens("語" * 10) == 10
    assert masora.est_tokens("a" * 8) == 2
    assert masora.est_tokens("abcd") == 1
    assert masora.est_tokens("語" * 4 + "a" * 4) == 5


def test_budget_holds_for_cjk_max_case(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CJK analog of the ASCII max-case: two 30-char CJK summaries cannot
    both fit the 60-token budget — the second drops with a visible cap."""
    summary = "語" * 30
    doc = doc_with(
        {
            "lineage": "a",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
        },
        {
            "lineage": "b",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
        },
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert lines[-1] == "… +1 more — masora search"


# --- confidence signal (v1 enrichment: source/name/effort/anchors) ---------------


def _confidence_doc(**fact_overrides: Any) -> dict[str, Any]:
    fact = {
        "lineage": "a",
        "summary": "claim",
        "resolution": "current",
        "verification": "unverified",
        "flags": "-",
    }
    fact.update(fact_overrides)
    return doc_with(fact)


def _confidence_lines(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any]
) -> list[str]:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    return masora.query_lines({}, FOO, env=dict(os.environ))


def test_confidence_verified_human_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verification label IS the signal: it renders verbatim, BARE — no
    appended `trusted` synonym (doubling information in editorialized form
    would violate 'never an instruction, only label + summary + status')."""
    doc = _confidence_doc(verification="verified(human)")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(human)]"
    ]


def test_confidence_verified_llm_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _confidence_doc(verification="verified(llm)")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(llm)]"
    ]


def test_confidence_re_verify_for_effort_low_alone(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ONLY interpretation token: effort low appends re-verify — here
    with an unverified version (which itself renders as nothing)."""
    doc = _confidence_doc(effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, re-verify]"
    ]


def test_confidence_verified_graph_label_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verified(graph)` is mechanical proof: its label renders — never
    conflated with unverified, never given an interpretation token."""
    doc = _confidence_doc(verification="verified(graph)")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(graph)]"
    ]


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_confidence_nothing_for_effort_medium_or_high(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    """Silence, not stripping: a high effort is not a distrust signal — the
    absence of alarm."""
    doc = _confidence_doc(effort=effort)
    assert _confidence_lines(stub_masora, monkeypatch, doc) == ["masora: claim [current]"]


def test_confidence_human_verified_plus_effort_low_is_literal(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal ruling: effort low appends re-verify even when a human
    verify is present (a llm-written low-effort version later human-verified)
    — reads odd, flagged as friction; masora should rule whether a human
    verify clears the writer's effort token."""
    doc = _confidence_doc(verification="verified(human)", effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(human), re-verify]"
    ]


def test_not_line_carries_no_confidence(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refutation is never softened: the NOT line gains no confidence token
    whatever the verification/effort say."""
    doc = _confidence_doc(resolution="none", verification="verified(human)", effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == ["masora NOT: claim [refuted]"]


def test_old_style_document_renders_as_before_enrichment(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-enrichment document (no source/name/effort/anchors) renders
    exactly as it did: no confidence label without a verification/effort
    that triggers one."""
    doc = _confidence_doc()  # unverified, no effort
    assert _confidence_lines(stub_masora, monkeypatch, doc) == ["masora: claim [current]"]


def test_unknown_source_and_effort_enums_tolerated_and_inert(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new enum value is Masora's drift to report, not a shape violation —
    same posture as unknown `resolution` values; it drives no label."""
    doc = _confidence_doc(source="robot", effort="extreme")
    lines = _confidence_lines(stub_masora, monkeypatch, doc)
    assert lines == ["masora: claim [current]"]
    assert masora.parse_contract(json.dumps(doc)) is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"source": None},
        {"name": None},
        {"effort": None},
        {"anchors": None},
        {"anchors": []},
        {"anchors": ["scip-clang cxx . . mongo/Util#tick()."]},
        {"name": "claude-opus"},
    ],
)
def test_new_fields_null_or_valid_tolerated(fields: dict[str, Any]) -> None:
    doc = _confidence_doc(**fields)
    assert masora.parse_contract(json.dumps(doc)) is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"source": 1},
        {"source": []},
        {"name": 5},
        {"name": {}},
        {"effort": 1.5},
        {"effort": []},
        {"anchors": "scip-clang cxx"},
        {"anchors": [1]},
        {"anchors": {"a": "b"}},
    ],
)
def test_new_fields_wrong_type_reject_whole_document(fields: dict[str, Any]) -> None:
    doc = _confidence_doc(**fields)
    assert masora.parse_contract(json.dumps(doc)) is None


def test_enriched_document_end_to_end(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The full v1 enriched shape through query_lines: verified(llm) renders
    bare (high effort is silence), inert fields riding along."""
    doc = _confidence_doc(
        verification="verified(llm)",
        source="llm",
        name="claude-opus",
        effort="high",
        anchors=["scip-clang cxx . . mongo/ResumeTokenData#makeResumeToken()."],
        anchors_matched=["scip-clang cxx . . mongo/ResumeTokenData#makeResumeToken()."],
    )
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(llm)]"
    ]


def test_confidence_label_counts_toward_the_token_budget() -> None:
    """The one interpretation token is rendered text like any other: a
    max-case fact line grows by `re-verify`'s cost, and the budget math sees
    it."""
    summary = "word " * 24  # 120 chars — the §3 cap
    contract = masora.Contract(
        facts=(
            masora.Fact(
                lineage="a",
                summary=summary,
                resolution="current",
                verification="unverified",
                flags=(),
                effort="low",
            ),
            masora.Fact(
                lineage="b",
                summary=summary,
                resolution="current",
                verification="unverified",
                flags=(),
                effort="low",
            ),
        ),
        stale_warning=False,
    )
    lines = masora.render_lines(contract)
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert lines[-1] == "… +1 more — masora search"
    assert ", re-verify]" in lines[0]
