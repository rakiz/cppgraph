"""Tests for the pre-index compile_commands.json summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.compdb import load_compdb, summarize_compdb

_ENTRIES = [
    {"file": "/repo/src/mongo/db/query.cpp"},
    {"file": "/repo/src/mongo/db/query_test.cpp"},
    {"file": "/repo/src/mongo/client/conn.cpp"},
    {"file": "/repo/src/third_party/grpc/x.cpp"},
    {"file": "/repo/src/third_party/boost/y.cpp"},
]


def test_summary_counts_total_and_tests() -> None:
    s = summarize_compdb(_ENTRIES)
    assert s.total == 5
    assert s.tests == 1  # only *_test.cpp


def test_summary_strips_common_prefix_and_groups_by_subtree() -> None:
    s = summarize_compdb(_ENTRIES)
    assert s.common_prefix == "/repo/src"
    groups = {k: (n, t) for k, n, t in s.groups}
    assert groups["mongo/db"] == (2, 1)  # 2 TUs, 1 test
    assert groups["mongo/client"] == (1, 0)
    assert groups["third_party/grpc"] == (1, 0)


def test_summary_filter_preview() -> None:
    s = summarize_compdb(_ENTRIES, filter="src/mongo")
    assert (s.matched, s.matched_tests) == (3, 1)


def test_summary_handles_single_entry() -> None:
    s = summarize_compdb([{"file": "/a/b/c.cpp"}])
    assert s.total == 1 and s.tests == 0


def test_load_compdb_rejects_non_list(tmp_path: Path) -> None:
    p = tmp_path / "cc.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="expected a JSON array"):
        load_compdb(str(p))


def test_cli_compdb_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "cc.json"
    p.write_text(json.dumps(_ENTRIES))
    assert main(["compdb-summary", str(p), "--filter", "src/mongo"]) == 0
    out = capsys.readouterr().out
    assert "5 translation unit(s), 1 test(s)" in out
    assert "mongo/db" in out
    assert "keeps 3 of 5" in out
