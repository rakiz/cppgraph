"""Tests for the Masora fact injection (contract v2, masora.py).

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
if [ -n "$MASORA_STUB_COUNT_FILE" ]; then
  echo x >> "$MASORA_STUB_COUNT_FILE"
fi
if [ -n "$MASORA_STUB_ARGS_FILE" ]; then
  printf '%s' "$*" >> "$MASORA_STUB_ARGS_FILE"
fi
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


CTX_SILENT: dict[str, Any] = {
    "established_relation": "in_line",
    "established_commit": "1a1a8e1f4e5a",
    "off_version": False,
    "context_ordering": "exact",
}
"""The silent v2 context stamp (no trigger fires) for hand-built facts.

Every hand-built fact JSON must carry the four v2 context fields — they are
required per fact — and this combination renders byte-identically to v1.
Missing-field tests deliberately do NOT use this spread."""


def fact_doc(**overrides: Any) -> dict[str, Any]:
    """One contract-v2 fact shaped like §3's first example (silent stamp)."""
    fact = {
        "lineage": "01J8Z3K0000000000000000000",
        "summary": "Resume token invalidated by a shard key change",
        "resolution": "current",
        "verification": "verified(llm)",
        "flags": "-",
        "anchors_matched": ["scip-clang cxx . . mongo/ResumeTokenData#makeResumeToken()."],
        "established_relation": "in_line",
        "established_commit": "1a1a8e1f4e5a",
        "off_version": False,
        "context_ordering": "exact",
    }
    fact.update(overrides)
    return {"contract_version": 2, "stale_warning": False, "facts": [fact]}


def doc_with(*facts: dict[str, Any], **doc_overrides: Any) -> dict[str, Any]:
    doc = {"contract_version": 2, "stale_warning": False, "facts": list(facts)}
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


@pytest.fixture
def outline_graph_path(tmp_path: Path) -> Path:
    """A graph whose nodes carry foo.cpp positions — `outline`-addressable
    (the `graph_path` nodes have no file, so its outline is empty)."""
    graph = Graph()
    graph.nodes[FOO] = Node(symbol=FOO, display_name="makeResumeToken", file="foo.cpp", line=0)
    graph.nodes[CALLER] = Node(symbol=CALLER, display_name="caller", file="foo.cpp", line=9)
    path = tmp_path / "outline-graph.db"
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
            **CTX_SILENT,
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
            **CTX_SILENT,
        },
        {
            "lineage": "b",
            "summary": "second",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
            **CTX_SILENT,
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
            **CTX_SILENT,
        },
        {
            "lineage": "a",
            "summary": "first again",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
            **CTX_SILENT,
        },
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora: first [current]"]


def test_empty_facts_render_the_presence_hint(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.11: masora present (flag on, binary found, root resolved, contract
    parsed) and zero facts rendered — exactly ONE capability hint line."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [masora.HINT_LINE]


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
            **CTX_SILENT,
        },
        stale_warning=True,
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[0] == masora.STALE_WARNING_LINE
    assert lines[1] == "masora: some claim [current]"


def test_stale_warning_false_and_null_render_the_presence_hint(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero facts + a non-firing stale value: the presence hint is the one
    line (the stale note renders only alongside facts)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    for value in (False, None):
        doc = doc_with(stale_warning=value)
        monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
        assert masora.query_lines({}, FOO, env=dict(os.environ)) == [masora.HINT_LINE]


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


@pytest.mark.parametrize("version", ["1", None])
def test_non_integer_or_missing_contract_version_renders_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, version: Any
) -> None:
    """A non-integer or missing version stays SILENT (zero-change). Strict
    integer mismatches (v0/v1/v3+) now return the one-line advisory instead
    (see the advisory tests), and version 2 renders."""
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


# --- version-mismatch advisory (the one-line carve-out from silent skip) ---------


def test_mismatch_advisory_matrix() -> None:
    """A strict-int `contract_version` != 2 → the one-line advisory (newer:
    update cppgraph; older: update masora). Everything else — unparsable
    JSON, non-dict document, missing/bool/str version, and 2 itself — is
    silence."""
    assert masora.mismatch_advisory(json.dumps({"contract_version": 3})) == (
        "masora: [facts contract v3 unsupported — update cppgraph]"
    )
    assert masora.mismatch_advisory(json.dumps({"contract_version": 99})) == (
        "masora: [facts contract v99 unsupported — update cppgraph]"
    )
    assert masora.mismatch_advisory(json.dumps({"contract_version": 1})) == (
        "masora: [facts contract v1 — update masora]"
    )
    assert masora.mismatch_advisory(json.dumps({"contract_version": 0})) == (
        "masora: [facts contract v0 — update masora]"
    )
    assert masora.mismatch_advisory(json.dumps({"contract_version": 2})) is None
    for silent in (
        "",
        "not json",
        "[]",
        '"a string"',
        "{}",
        '{"contract_version": true}',
        '{"contract_version": false}',
        '{"contract_version": "3"}',
        '{"contract_version": null}',
        '{"no_version": 3}',
    ):
        assert masora.mismatch_advisory(silent) is None, silent


def test_advisory_newer_version_renders_one_line_no_facts(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = fact_doc()
    doc["contract_version"] = 3
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: [facts contract v3 unsupported — update cppgraph]"
    ]


def test_advisory_older_version_renders_one_line(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = fact_doc()
    doc["contract_version"] = 1
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: [facts contract v1 — update masora]"
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        '{"contract_version": true, "facts": []}',
        '{"stale_warning": false}',
        "garbage{",
    ],
)
def test_version_shapes_that_stay_silent(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """No advisory for unparsable output, a bool version, or a missing one —
    those stay with the zero-change silence."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", stdout)
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
        {
            "lineage": "ok-lineage",
            "summary": "fine",
            "resolution": "current",
            **CTX_SILENT,
            **field_override,
        }
    )
    assert masora.parse_contract(json.dumps(doc)) is None


def test_absent_flag_fields_tolerated() -> None:
    """Absent (or null) optional fact fields default; only a present-but-wrong
    type rejects the document. The four v2 context fields are REQUIRED — they
    ride along explicitly here."""
    fact = {"summary": "s", "resolution": "current", **CTX_SILENT}
    contract = masora.parse_contract(
        json.dumps({"contract_version": 2, "stale_warning": False, "facts": [fact]})
    )
    assert contract is not None
    assert contract.facts == (
        masora.Fact(
            lineage="",
            summary="s",
            resolution="current",
            verification="",
            flags=(),
            established_relation="in_line",
            established_commit="1a1a8e1f4e5a",
            off_version=False,
            context_ordering="exact",
        ),
    )


def test_query_with_one_valid_one_malformed_fact_injects_nothing(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partial delivery: a single malformed fact poisons the whole document."""
    doc = fact_doc()
    doc["facts"].append(
        {
            "lineage": "ok-lineage",
            "summary": 123,
            "resolution": "current",
            "flags": "-",
            **CTX_SILENT,
        }
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


# --- contract v2: the four context fields (parser) -------------------------------


def test_v2_document_parses_with_context_fields() -> None:
    contract = masora.parse_contract(json.dumps(fact_doc()))
    assert contract is not None
    fact = contract.facts[0]
    assert fact.established_relation == "in_line"
    assert fact.established_commit == "1a1a8e1f4e5a"
    assert fact.off_version is False
    assert fact.context_ordering == "exact"


def test_v1_document_rejected() -> None:
    """No dual-version window: the contract is v2 or nothing — a v1 document
    is an unknown major, rejected like any other."""
    doc = fact_doc()
    doc["contract_version"] = 1
    assert masora.parse_contract(json.dumps(doc)) is None


@pytest.mark.parametrize("version", [True, False])
def test_v2_boolean_version_rejected_at_parse(version: bool) -> None:
    """A JSON bool would compare == 1 numerically; the parser rejects it
    before any version comparison."""
    doc = fact_doc()
    doc["contract_version"] = version
    assert masora.parse_contract(json.dumps(doc)) is None


@pytest.mark.parametrize(
    "field", ["established_relation", "established_commit", "off_version", "context_ordering"]
)
def test_missing_v2_field_rejects_whole_document(field: str) -> None:
    """The four context stamps are REQUIRED per fact in v2: a fact missing
    one is a shape violation → the whole document rejects (fail-closed, no
    partial delivery)."""
    doc = fact_doc()
    del doc["facts"][0][field]
    assert masora.parse_contract(json.dumps(doc)) is None


@pytest.mark.parametrize(
    "field_override",
    [
        {"established_relation": 5},
        {"established_relation": ["ahead"]},
        {"established_commit": 12},
        {"established_commit": ["9f8e7d6c5b4a"]},
        {"off_version": 1},
        {"off_version": "true"},
        {"off_version": None},
        {"context_ordering": 7},
        {"context_ordering": ["exact"]},
        {"context_ordering": None},
    ],
)
def test_v2_field_wrong_type_rejects_whole_document(field_override: dict[str, Any]) -> None:
    """`off_version` is a STRICT bool (a JSON int/str/null is a violation);
    the other three must be str-or-null (`context_ordering` may not even be
    null)."""
    doc = fact_doc()
    doc["facts"][0].update(field_override)
    assert masora.parse_contract(json.dumps(doc)) is None


@pytest.mark.parametrize("fields", [{"established_relation": None}, {"established_commit": None}])
def test_v2_nullable_stamps_explicit_null_accepted(fields: dict[str, Any]) -> None:
    doc = fact_doc()
    doc["facts"][0].update(fields)
    assert masora.parse_contract(json.dumps(doc)) is not None


def test_unknown_relation_value_inert_to_parser() -> None:
    """An unrecognized `established_relation` is parser-inert (the document
    is accepted) — enum VALUES are not shape; it still triggers the context
    label (§6 forward compatibility)."""
    doc = fact_doc(established_relation="rebased")
    contract = masora.parse_contract(json.dumps(doc))
    assert contract is not None
    assert contract.facts[0].established_relation == "rebased"


def test_unknown_ordering_value_inert_to_parser() -> None:
    """Same inert posture for an unrecognized `context_ordering` value —
    but it does NOT trigger the context label (only `degraded` does)."""
    doc = fact_doc(context_ordering="fuzzy")
    contract = masora.parse_contract(json.dumps(doc))
    assert contract is not None
    assert contract.facts[0].context_ordering == "fuzzy"


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
            **CTX_SILENT,
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


def test_stale_warning_with_no_facts_hint_only(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale index with zero matching facts renders no stale note (§6: only
    alongside facts) — but masora IS present and the contract parsed, so the
    one-line presence hint renders (§9.11)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with(stale_warning=True)))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [masora.HINT_LINE]


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
            **CTX_SILENT,
        },
        {
            "lineage": "b",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
            **CTX_SILENT,
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
            **CTX_SILENT,
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
            **CTX_SILENT,
        }
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: Lock L must be held before calling commitShard [stale, suspect — re-check]"
    ]


# --- contract v2: the context line (rendering matrix) -----------------------------


def _fact(**overrides: Any) -> masora.Fact:
    """A v2 Fact at the silent stamp (no trigger fires); override to trigger."""
    fields: dict[str, Any] = {
        "lineage": "a",
        "summary": "claim",
        "resolution": "current",
        "verification": "unverified",
        "flags": (),
        "established_relation": "in_line",
        "established_commit": "1a1a8e1f4e5a",
        "off_version": False,
        "context_ordering": "exact",
    }
    fields.update(overrides)
    return masora.Fact(**fields)


def _render(fact: masora.Fact) -> list[str]:
    return masora.render_lines(masora.Contract(facts=(fact,), stale_warning=False))


def test_silent_stamp_is_byte_identical_to_v1_rendering() -> None:
    """`in_line` + `exact` + `off_version: false` renders NO context line —
    the common case costs exactly what it cost under v1."""
    assert _render(_fact()) == ["masora: claim [current]"]


@pytest.mark.parametrize(
    ("fact", "context"),
    [
        pytest.param(_fact(off_version=True), "  context: off-version", id="off_version"),
        pytest.param(
            _fact(established_relation="ahead"),
            "  context: established ahead of this checkout (1a1a8e1f4e5a)",
            id="ahead",
        ),
        pytest.param(
            _fact(established_relation="out_of_line"),
            "  context: established on another line (1a1a8e1f4e5a)",
            id="out_of_line",
        ),
        pytest.param(
            _fact(established_relation="unknown"),
            "  context: context unproven (1a1a8e1f4e5a)",
            id="unknown-relation",
        ),
        pytest.param(
            _fact(established_relation="rebased"),
            "  context: established relation rebased (1a1a8e1f4e5a)",
            id="unrecognized-relation-verbatim",
        ),
        pytest.param(
            _fact(context_ordering="degraded"),
            "  context: selection ordering degraded",
            id="degraded",
        ),
    ],
)
def test_context_line_triggers(fact: masora.Fact, context: str) -> None:
    """One context line per fired trigger, assembled in order: off-version,
    then the relation phrase with the commit beside it, then the degraded
    note — joined with ' — '. An unrecognized relation renders verbatim in
    the neutral template, never guessed; with no relation phrase (in_line)
    there is nothing for the commit to point at, so it does not render."""
    assert _render(fact) == ["masora: claim [current]", context]


def test_resolution_none_never_renders_context() -> None:
    """The ONE exception, short-circuited before the trigger test: a
    `resolution: "none"` fact renders no context line even with EVERY
    trigger set — the NOT: envelope already carries the negative knowledge."""
    fact = _fact(
        resolution="none",
        established_relation=None,
        established_commit=None,
        off_version=True,
        context_ordering="degraded",
    )
    assert _render(fact) == ["masora NOT: claim [refuted]"]


def test_null_relation_exact_renders_no_context() -> None:
    """A null relation never triggers alone: a stale fact with unproven
    context and exact ordering renders exactly the v1 line."""
    fact = _fact(resolution="stale", established_relation=None, established_commit=None)
    assert _render(fact) == ["masora: claim [stale — re-check]"]


def test_null_relation_degraded_renders_unproven_and_degraded() -> None:
    """Null relation + degraded: the line renders (degraded fired) with
    'context unproven' (the null relation's phrase) and the degraded note."""
    fact = _fact(
        established_relation=None,
        established_commit=None,
        context_ordering="degraded",
    )
    assert _render(fact) == [
        "masora: claim [current]",
        "  context: context unproven — selection ordering degraded",
    ]


# --- contract v2: the worked example (§6), byte-exact -----------------------------


WORKED_EXAMPLE_FACT: dict[str, Any] = {
    "lineage": "01J8ZP20000000000000000000",
    "summary": "Lock L must be held before calling commitShard",
    "resolution": "stale",
    "verification": "unverified",
    "flags": "suspect",
    "established_relation": "out_of_line",
    "established_commit": "9f8e7d6c5b4a",
    "off_version": True,
    "context_ordering": "exact",
}


def test_contract_worked_example_renders_exactly(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6's worked example, byte for byte, through the full query_lines
    pipeline — the anchor for the assembly (off-version, then the relation
    phrase with the commit beside it, ' — '-joined)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with(WORKED_EXAMPLE_FACT)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == [
        "masora: Lock L must be held before calling commitShard [stale, suspect — re-check]",
        "  context: off-version — established on another line (9f8e7d6c5b4a)",
    ]


# --- contract v2: atomicity — a fact line and its context drop together -----------


def test_context_line_counts_in_budget_and_drops_atomically(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fact 2's context line is what tips the block over the budget: without
    it both fact lines fit. The block (fact + context) drops TOGETHER and
    the cap is reported — the context never renders orphaned and never
    hides from the estimate (est_tokens runs over the joined text)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    triggering = {
        "lineage": "b",
        "summary": "word " * 24,  # 120 chars — the §3 cap
        "resolution": "current",
        "verification": "verified(llm)",
        "flags": "-",
        "established_relation": "out_of_line",
        "established_commit": "1a1a8e1f4e5a",
        "off_version": True,
        "context_ordering": "exact",
    }
    silent = {
        **triggering,
        "established_relation": "in_line",
        "off_version": False,
    }
    first = {
        "lineage": "a",
        "summary": "fact number 0",
        "resolution": "current",
        "verification": "unverified",
        "flags": "-",
        **CTX_SILENT,
    }
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with(first, triggering)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora: fact number 0 [current]", "… +1 more — masora search"]
    # The control: the same two facts at a silent stamp fit together —
    # the context line is what pushed fact 2 out.
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with(first, silent)))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: fact number 0 [current]",
        f"masora: {'word ' * 24} [current, verified(llm)]",
    ]


def test_single_oversized_block_with_context_still_renders(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-zero rule covers the whole block: one fact whose fact line
    AND context line together exceed the budget still renders — both lines
    (a lone oversized fact beats a bare cap, context included)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = doc_with(
        {
            "lineage": "z",
            "summary": "語" * 120,
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
            "established_relation": "out_of_line",
            "established_commit": "9f8e7d6c5b4a",
            "off_version": True,
            "context_ordering": "exact",
        }
    )
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert len(lines) == 2
    assert lines[0].startswith("masora: ")
    assert lines[1] == "  context: off-version — established on another line (9f8e7d6c5b4a)"


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
            **CTX_SILENT,
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
            **CTX_SILENT,
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
            **CTX_SILENT,
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
            **CTX_SILENT,
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
            **CTX_SILENT,
        },
        {
            "lineage": "b",
            "summary": summary,
            "resolution": "current",
            "verification": "verified(llm)",
            "flags": "-",
            **CTX_SILENT,
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
        **CTX_SILENT,
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


def test_confidence_llm_low_renders_the_low_effort_token(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ONE interpretive token, per the pinned state-form matrix — exact
    string `low-effort`, pinned against the contract's own example line:
    a low-effort LLM verification is the weakest trust state a verified
    label can carry."""
    doc = _confidence_doc(
        summary="Bring-up order: start before stop",
        verification="verified(llm)",
        effort="low",
    )
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: Bring-up order: start before stop [current, verified(llm), low-effort]"
    ]


def test_confidence_unverified_effort_low_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowed semantics: effort qualifies the LLM's VERIFICATION quality —
    an unverified fact (contract-wise effort always null) gets NO token."""
    doc = _confidence_doc(effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == ["masora: claim [current]"]


def test_confidence_verified_graph_label_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`verified(graph)` is mechanical proof: its label renders — never
    conflated with unverified, never given an interpretation token, whatever
    the effort (the verify's currency is the resolution axis's job)."""
    doc = _confidence_doc(verification="verified(graph)", effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(graph)]"
    ]


@pytest.mark.parametrize("effort", ["medium", "high"])
def test_confidence_nothing_for_effort_medium_or_high(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, effort: str
) -> None:
    """Silence, not stripping: a high effort is not a distrust signal — the
    absence of alarm. On a verified(llm) fact too: medium/high/null render
    bare."""
    doc = _confidence_doc(verification="verified(llm)", effort=effort)
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(llm)]"
    ]


def test_confidence_verified_human_effort_low_renders_bare(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned matrix resolved the former friction the other way: the
    interpretive token NEVER renders under verified(human) whatever the
    writer's effort — a human verification answers for the content."""
    doc = _confidence_doc(verification="verified(human)", effort="low")
    assert _confidence_lines(stub_masora, monkeypatch, doc) == [
        "masora: claim [current, verified(human)]"
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
    """The one interpretive token is rendered text like any other: a
    max-case fact line grows by `low-effort`'s cost, and the budget math
    sees it."""
    summary = "word " * 24  # 120 chars — the §3 cap
    contract = masora.Contract(
        facts=(
            masora.Fact(
                lineage="a",
                summary=summary,
                resolution="current",
                verification="verified(llm)",
                flags=(),
                established_relation="in_line",
                established_commit="1a1a8e1f4e5a",
                off_version=False,
                context_ordering="exact",
                effort="low",
            ),
            masora.Fact(
                lineage="b",
                summary=summary,
                resolution="current",
                verification="verified(llm)",
                flags=(),
                established_relation="in_line",
                established_commit="1a1a8e1f4e5a",
                off_version=False,
                context_ordering="exact",
                effort="low",
            ),
        ),
        stale_warning=False,
    )
    lines = masora.render_lines(contract)
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert lines[-1] == "… +1 more — masora search"
    assert ", low-effort]" in lines[0]


# --- (k) configurable fact count: CPPGRAPH_MASORA_MAX_FACTS (§9.8b) --------------


def _many_fact_contract(n: int, *, summary: str = "") -> masora.Contract:
    """A contract with `n` distinct short facts (or `n` copies of `summary`)."""
    return masora.Contract(
        facts=tuple(
            masora.Fact(
                lineage=f"l{i}",
                summary=summary or f"fact number {i}",
                resolution="current",
                verification="unverified",
                flags=(),
                established_relation="in_line",
                established_commit="1a1a8e1f4e5a",
                off_version=False,
                context_ordering="exact",
            )
            for i in range(n)
        ),
        stale_warning=False,
    )


def _many_facts_doc(n: int) -> dict[str, Any]:
    """The JSON shape of `_many_fact_contract`, for the stub-masora e2e tests."""
    return doc_with(
        *[
            {
                "lineage": f"l{i}",
                "summary": f"fact number {i}",
                "resolution": "current",
                "verification": "unverified",
                "flags": "-",
                **CTX_SILENT,
            }
            for i in range(n)
        ]
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 2, id="unset-default"),
        pytest.param("3", 3, id="three"),
        pytest.param("1", 1, id="one"),
        pytest.param("10", 10, id="ten-higher-allowed"),
        pytest.param("  4  ", 4, id="surrounding-whitespace-tolerated"),
        pytest.param("0", 2, id="zero-below-one-default"),
        pytest.param("-1", 2, id="negative-default"),
        pytest.param("abc", 2, id="non-integer-default"),
        pytest.param("2.5", 2, id="float-string-default"),
        pytest.param("", 2, id="empty-default"),
        pytest.param("   ", 2, id="whitespace-only-default"),
    ],
)
def test_max_facts_matrix(raw: str | None, expected: int) -> None:
    """Pure-unit matrix for `max_facts`: unset / non-integer / < 1 silently
    uses the default (a config typo is a zero-change case, never an error);
    any integer >= 1 is accepted."""
    env = {} if raw is None else {masora.MAX_FACTS_ENV_VAR: raw}
    assert masora.max_facts(env) == expected


def test_max_facts_reads_the_process_env_when_no_env_dict_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", "3")
    assert masora.max_facts() == 3
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", "junk")
    assert masora.max_facts() == 2
    monkeypatch.delenv("CPPGRAPH_MASORA_MAX_FACTS")
    assert masora.max_facts() == 2


def test_render_lines_max_facts_parameter_extends_and_shrinks() -> None:
    """Pure-unit pin on `render_lines(max_facts=…)`: 3 renders three facts and
    reports the remaining one visibly; 1 renders one and reports the other
    three. The default (param omitted) stays MAX_FACTS."""
    contract = _many_fact_contract(4)
    three = masora.render_lines(contract, max_facts=3)
    assert three[:3] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
        "masora: fact number 2 [current]",
    ]
    assert three[3] == "… +1 more — masora search"
    one = masora.render_lines(contract, max_facts=1)
    assert one == ["masora: fact number 0 [current]", "… +3 more — masora search"]
    assert masora.render_lines(contract)[:2] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
    ]


def test_render_lines_budget_wins_over_max_facts() -> None:
    """The §6 budget always wins over the configured count: with max_facts=3,
    two max-length summaries still drop to one rendered fact plus the visible
    cap — a higher fact count never buys its way past the token budget."""
    contract = _many_fact_contract(3, summary="word " * 24)  # 3 × 120-char summaries
    lines = masora.render_lines(contract, max_facts=3)
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert len(lines) == 2
    assert lines[-1] == "… +2 more — masora search"


def test_max_facts_env_renders_three_with_visible_cap(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end (§9.8b): CPPGRAPH_MASORA_MAX_FACTS=3 renders three matching
    facts and reports the remaining two with the visible truncation line."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", "3")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_many_facts_doc(5)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[:3] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
        "masora: fact number 2 [current]",
    ]
    assert lines[3] == "… +2 more — masora search"


def test_max_facts_env_one_renders_one(stub_masora: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_many_facts_doc(5)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == ["masora: fact number 0 [current]", "… +4 more — masora search"]


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "2.5", ""])
def test_max_facts_env_invalid_values_use_the_default(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo'd fact count never errors and never zeroes the injection: it
    silently renders the default 2 (the zero-change guarantee covers config)."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", raw)
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_many_facts_doc(5)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[:2] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
    ]
    assert lines[2] == "… +3 more — masora search"


def test_max_facts_env_unset_defaults_to_two(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.delenv("CPPGRAPH_MASORA_MAX_FACTS", raising=False)
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_many_facts_doc(5)))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines[:2] == [
        "masora: fact number 0 [current]",
        "masora: fact number 1 [current]",
    ]
    assert lines[2] == "… +3 more — masora search"


def test_budget_wins_over_max_facts_three_cjk(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CJK case at the higher count: three 30-char CJK summaries cannot all
    fit the 60-token budget even with CPPGRAPH_MASORA_MAX_FACTS=3 — the second
    drops and the cap is reported visibly (budget > fact count, always)."""
    summary = "語" * 30
    doc = doc_with(
        *[
            {
                "lineage": f"c{i}",
                "summary": summary,
                "resolution": "current",
                "verification": "verified(llm)",
                "flags": "-",
                **CTX_SILENT,
            }
            for i in range(3)
        ]
    )
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("CPPGRAPH_MASORA_MAX_FACTS", "3")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    total = sum(masora.est_tokens(line) for line in lines)
    assert total <= masora.TOKEN_BUDGET
    assert len(lines) == 2
    assert lines[-1] == "… +2 more — masora search"


# --- §9.10: multi-symbol batched spawn (find / outline) --------------------------


def _two_lineage_doc() -> dict[str, Any]:
    """One batched-response document: two facts from different lineages."""
    return doc_with(
        {
            "lineage": "a",
            "summary": "first claim",
            "resolution": "current",
            "verification": "unverified",
            "flags": "-",
            **CTX_SILENT,
        },
        {
            "lineage": "b",
            "summary": "second claim",
            "resolution": "stale",
            "verification": "unverified",
            "flags": "-",
            **CTX_SILENT,
        },
    )


def test_run_masora_facts_repeats_the_symbol_flag(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2/§9.10: `--symbol` is repeatable — one spawn carries one flag per
    result symbol, in order."""
    monkeypatch.setenv("MASORA_STUB_ECHO_ARGS", "1")
    argv = masora.run_masora_facts(str(tmp_path), [FOO, CALLER], timeout=2.0)
    assert argv == f"facts --repo {tmp_path} --symbol {FOO} --symbol {CALLER}"


def test_query_lines_dedups_repeated_symbols_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[Any] = []

    def recording(repo: str, symbols: Any, timeout: float) -> str | None:
        received.append(symbols)
        return json.dumps(fact_doc())

    lines = masora.query_lines(
        {"project_root": str(tmp_path)},
        [FOO, CALLER, FOO],
        env={"CPPGRAPH_MASORA": "1"},
        which=lambda name: "/fake/masora",
        runner=recording,
    )
    assert received == [[FOO, CALLER]]  # order-preserving distinct, ONE batch
    assert lines == [
        "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
    ]


def test_query_lines_empty_symbol_list_skips_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response with zero result symbols asks nothing: no spawn at all —
    spawning flag-less would return every lineage of the matching base(s),
    not what an empty find/outline means."""
    calls: list[str] = []

    def recording(repo: str, symbols: Any, timeout: float) -> str | None:
        calls.append(repo)
        return json.dumps(fact_doc())

    lines = masora.query_lines(
        {"project_root": str(tmp_path)},
        [],
        env={"CPPGRAPH_MASORA": "1"},
        which=lambda name: "/fake/masora",
        runner=recording,
    )
    assert lines == []
    assert calls == []


def test_response_symbols_collects_entries_and_overload_arms() -> None:
    from cppgraph.queries import response_symbols

    result = {
        "results": [
            {"symbol": FOO, "name": "makeResumeToken"},
            {
                "symbol": CALLER,
                "overloads": 2,
                "signatures": [{"symbol": CALLER}, {"symbol": FOO}],
            },
        ]
    }
    assert response_symbols(result) == [FOO, CALLER]  # ordered-distinct, arms included


def test_response_symbols_on_error_or_empty_results() -> None:
    from cppgraph.queries import response_symbols

    for result in ({"error": "no graph"}, {"results": []}, {}):
        assert response_symbols(result) == []


def test_cli_find_batches_all_result_symbols_in_one_spawn(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """§9.10: find injects with ONE batched spawn carrying every result
    symbol (`--symbol` repeated); facts from different lineages of the
    batched symbols all render."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_two_lineage_doc()))
    count = tmp_path / "spawns"
    args_file = tmp_path / "args"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    monkeypatch.setenv("MASORA_STUB_ARGS_FILE", str(args_file))
    assert main(["find", "--graph", str(graph_path), "Foo"]) == 0
    out = capsys.readouterr().out
    assert "masora: first claim [current]" in out
    assert "masora: second claim [stale — re-check]" in out
    assert count.read_text() == "x\n"  # exactly one spawn for the whole response
    argv = args_file.read_text()
    assert f"--symbol {FOO}" in argv
    assert f"--symbol {CALLER}" in argv


def test_cli_find_budget_is_per_response(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """§9.10: the ≤ 2-fact / ≤ 60-token budget is per RESPONSE across all
    batched symbols, and the truncation line stays visible."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_many_facts_doc(5)))
    count = tmp_path / "spawns"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    assert main(["find", "--graph", str(graph_path), "Foo"]) == 0
    out = capsys.readouterr().out
    assert "masora: fact number 0 [current]" in out
    assert "masora: fact number 1 [current]" in out
    assert "… +3 more — masora search" in out
    assert count.read_text() == "x\n"


def test_cli_find_two_symbols_same_lineage_render_one_fact(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Two batched symbols anchoring the SAME lineage render that one fact
    once (lineage dedup over the one batched document)."""
    doc = fact_doc()
    doc["facts"].append({**doc["facts"][0], "summary": "first again"})
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    count = tmp_path / "spawns"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    assert main(["find", "--graph", str(graph_path), "Foo"]) == 0
    out = capsys.readouterr().out
    assert out.count("masora: Resume token invalidated") == 1
    assert count.read_text() == "x\n"


def test_cli_outline_batches_all_defined_symbols(
    outline_graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """§9.10: outline injects too — ONE batched spawn with every defined
    symbol of the outlined file."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_two_lineage_doc()))
    count = tmp_path / "spawns"
    args_file = tmp_path / "args"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    monkeypatch.setenv("MASORA_STUB_ARGS_FILE", str(args_file))
    assert main(["outline", "--graph", str(outline_graph_path), "foo.cpp"]) == 0
    out = capsys.readouterr().out
    assert "masora: first claim [current]" in out
    assert "masora: second claim [stale — re-check]" in out
    assert count.read_text() == "x\n"
    argv = args_file.read_text()
    assert f"--symbol {FOO}" in argv
    assert f"--symbol {CALLER}" in argv


def test_cli_find_zero_results_spawns_nothing(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No result symbols → no spawn (and no hint): nothing to anchor on."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    count = tmp_path / "spawns"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    assert main(["find", "--graph", str(graph_path), "zzzzqqqq"]) == 1
    assert not count.exists()


def test_mcp_find_batches_all_result_symbols(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_two_lineage_doc()))
    count = tmp_path / "spawns"
    args_file = tmp_path / "args"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    monkeypatch.setenv("MASORA_STUB_ARGS_FILE", str(args_file))
    server = mcp_server.build_server(str(mcp_store))
    result = _tool(server, "find")(query="Foo")
    assert result["masora"] == (
        "masora: first claim [current]\nmasora: second claim [stale — re-check]"
    )
    assert count.read_text() == "x\n"
    argv = args_file.read_text()
    assert f"--symbol {FOO}" in argv
    assert f"--symbol {CALLER}" in argv


def test_mcp_outline_batches_all_definitions(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The compact outline (full_symbols=False — definitions carry no raw
    symbol strings) still injects: the batched symbols come straight from
    the store."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(_two_lineage_doc()))
    count = tmp_path / "spawns"
    args_file = tmp_path / "args"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    monkeypatch.setenv("MASORA_STUB_ARGS_FILE", str(args_file))
    server = mcp_server.build_server(str(mcp_store))
    result = _tool(server, "outline")(file="foo.cpp")
    assert result["masora"] == (
        "masora: first claim [current]\nmasora: second claim [stale — re-check]"
    )
    assert count.read_text() == "x\n"
    argv = args_file.read_text()
    assert f"--symbol {FOO}" in argv
    assert f"--symbol {CALLER}" in argv


def test_mcp_find_outline_empty_results_spawn_nothing(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    count = tmp_path / "spawns"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    server = mcp_server.build_server(str(mcp_store))
    find_result = _tool(server, "find")(query="zzzzqqqq")
    assert "masora" not in find_result
    outline_result = _tool(server, "outline")(file="no-such-file.cpp")
    assert "masora" not in outline_result
    assert not count.exists()


# --- §9.11: the presence hint ----------------------------------------------------


def test_presence_hint_once_per_response_for_all_symbols(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most once per response, whatever the number of batched symbols."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    assert masora.query_lines({}, [FOO, CALLER], env=dict(os.environ)) == [masora.HINT_LINE]


def test_presence_hint_absent_without_the_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent-mode (no binary): nothing renders — not even the hint."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


def test_presence_hint_absent_with_the_flag_off(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CPPGRAPH_MASORA", raising=False)
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


def test_presence_hint_absent_with_unresolvable_base(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def recording(repo: str, symbols: Any, timeout: float) -> str | None:
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


@pytest.mark.parametrize("stdout", ["", "not json", '{"contract_version": true}'])
def test_presence_hint_not_on_unparsable_output(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """The hint needs a PARSED contract; an unparsable one stays silent."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", stdout)
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


def test_presence_hint_not_on_spawn_failure(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_EXIT", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == []


def test_presence_hint_not_on_version_mismatch(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisory is the response's one line — the hint does not join it."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    doc = fact_doc()
    doc["contract_version"] = 3
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc))
    assert masora.query_lines({}, FOO, env=dict(os.environ)) == [
        "masora: [facts contract v3 unsupported — update cppgraph]"
    ]


def test_presence_hint_never_alongside_facts(
    stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(fact_doc()))
    lines = masora.query_lines({}, FOO, env=dict(os.environ))
    assert lines == [
        "masora: Resume token invalidated by a shard key change [current, verified(llm)]"
    ]
    assert masora.HINT_LINE not in lines


def test_cli_single_symbol_tools_render_the_hint_on_zero_facts(
    graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§9.11 covers every injected tool, single-symbol ones included."""
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    for command, sym in (("callers", CALLER), ("callees", CALLER), ("explain", FOO)):
        assert main([command, "--graph", str(graph_path), sym]) == 0
        out = capsys.readouterr().out
        assert out.count(masora.HINT_LINE) == 1, command


def test_cli_find_and_outline_render_the_hint_on_zero_facts(
    graph_path: Path,
    outline_graph_path: Path,
    stub_masora: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    count = tmp_path / "spawns"
    monkeypatch.setenv("MASORA_STUB_COUNT_FILE", str(count))
    assert main(["find", "--graph", str(graph_path), "Foo"]) == 0
    assert capsys.readouterr().out.count(masora.HINT_LINE) == 1
    assert main(["outline", "--graph", str(outline_graph_path), "foo.cpp"]) == 0
    assert capsys.readouterr().out.count(masora.HINT_LINE) == 1
    assert count.read_text() == "x\nx\n"  # one batched spawn per response


def test_mcp_find_and_outline_attach_the_hint_on_zero_facts(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    server = mcp_server.build_server(str(mcp_store))
    for tool, kwargs in (("find", {"query": "Foo"}), ("outline", {"file": "foo.cpp"})):
        result = _tool(server, tool)(**kwargs)
        assert result["masora"] == masora.HINT_LINE, tool


def test_mcp_single_symbol_tools_attach_the_hint_on_zero_facts(
    mcp_store: Path, stub_masora: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPPGRAPH_MASORA", "1")
    monkeypatch.setenv("MASORA_STUB_STDOUT", json.dumps(doc_with()))
    server = mcp_server.build_server(str(mcp_store))
    for tool in ("who_calls", "what_it_calls", "explain_symbol"):
        result = _tool(server, tool)(symbol=CALLER if tool != "explain_symbol" else FOO)
        assert result["masora"] == masora.HINT_LINE, tool
