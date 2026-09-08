"""Tests for the index pipeline — especially the never-overwrite guards."""

from __future__ import annotations

import json
import subprocess as sp
import sys
from pathlib import Path

from cppgraph import pipeline
from cppgraph.model import Graph
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, build_provenance, write_sqlite


def _compdb(path: Path) -> Path:
    path.write_text(json.dumps([{"file": "/repo/src/a.cpp"}, {"file": "/repo/src/a_test.cpp"}]))
    return path


def test_filter_compdb_substring_and_no_tests(tmp_path: Path) -> None:
    compdb = _compdb(tmp_path / "cc.json")
    out = tmp_path / "out.json"
    kept, total, dropped = pipeline.filter_compdb(compdb, out, "src", no_tests=True)
    assert total == 2
    assert dropped == 1  # a_test.cpp dropped
    assert kept == 1
    assert json.loads(out.read_text()) == [{"file": "/repo/src/a.cpp"}]


def test_filter_compdb_empty_result_raises(tmp_path: Path) -> None:
    compdb = _compdb(tmp_path / "cc.json")
    try:
        pipeline.filter_compdb(compdb, tmp_path / "o.json", "nomatch", no_tests=False)
    except pipeline.PipelineError as e:
        assert "0 entries" in str(e)
    else:
        raise AssertionError("expected PipelineError on empty filter")


def test_full_build_reuses_scip_and_graph_untouched(tmp_path: Path, monkeypatch) -> None:
    """With recompute_scip=False and rebuild_graph=False, an existing .scip and
    .graph.db must be left byte-for-byte untouched (the core safety guard)."""
    compdb = _compdb(tmp_path / "cc.json")
    out_dir = tmp_path / ".cppgraph"
    out_dir.mkdir()
    scip = out_dir / "proj.scip"
    graph = out_dir / "proj.graph.db"
    scip.write_bytes(b"PRECIOUS-INDEX-4H")
    graph.write_bytes(b"PRECIOUS-GRAPH")

    # scip-clang must never be invoked on the reuse path.
    def _boom(*a, **k):
        raise AssertionError("scip-clang must not run when reusing the index")

    monkeypatch.setattr(pipeline, "run_scip_clang", _boom)

    rc = pipeline.full_build(
        compdb=compdb,
        project_root=tmp_path,
        name="proj",
        src_filter="",
        no_tests=False,
        attributed_refs=False,
        recompute_scip=False,
        rebuild_graph=False,
        print_fn=lambda *a: None,
    )
    assert rc == 0
    assert scip.read_bytes() == b"PRECIOUS-INDEX-4H"
    assert graph.read_bytes() == b"PRECIOUS-GRAPH"


def test_full_build_reuses_scip_then_builds_real_graph(tmp_path: Path) -> None:
    """End-to-end: an existing .scip is reused (no scip-clang run) and a real,
    openable graph.db is produced with the index scope recorded in meta."""
    compdb = _compdb(tmp_path / "cc.json")
    out_dir = tmp_path / ".cppgraph"
    out_dir.mkdir()
    index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(
            project_root="file:///repo",
            tool_info=scip_pb2.ToolInfo(name="scip-clang", version="0.4.0"),
        ),
        documents=[scip_pb2.Document(relative_path="src/a.cpp")],
    )
    (out_dir / "proj.scip").write_bytes(index.SerializeToString())

    rc = pipeline.full_build(
        compdb=compdb,
        project_root=tmp_path,
        name="proj",
        src_filter="",
        no_tests=False,
        attributed_refs=False,
        recompute_scip=False,  # reuse the .scip above
        rebuild_graph=True,
        print_fn=lambda *a: None,
    )
    assert rc == 0
    graph = out_dir / "proj.graph.db"
    assert graph.is_file()
    store = GraphStore(graph)
    try:
        assert store.meta().get("index_filter") == ""  # whole-tree scope recorded
    finally:
        store.close()


def test_incremental_update_matches_status_on_dirty_fingerprints(
    tmp_path: Path, monkeypatch
) -> None:
    """`incremental_update` must agree with `status` on what "changed" means: a
    file dirty *at build time* and still holding that exact content is NOT
    re-indexed (the false-stale `changed_files_since` kills); reverted back to the
    committed version, it IS re-indexed (the index still holds the dirty content).
    Mirrors `test_dirty_fingerprints_prevent_false_stale` in test_store.py."""

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = tmp_path / "a.cpp"
    src.write_text("int a() { return 0; }\n")
    git("add", "a.cpp")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    # The state actually indexed: an uncommitted edit.
    src.write_text("int a() { return 1; }\n")
    index = scip_pb2.Index(metadata=scip_pb2.Metadata(project_root=f"file://{tmp_path}"))
    meta = build_provenance(index, source_commit=commit, source_dirty=True)

    db = tmp_path / "proj.graph.db"
    write_sqlite(Graph(), db, meta=meta)
    compdb = tmp_path / "compile_commands.json"
    compdb.write_text(json.dumps([{"file": str(src)}]))

    monkeypatch.setattr(pipeline.os, "access", lambda *a, **k: True)  # pretend scip-clang exists
    reindexed: list[list[str]] = []

    def _fake_run_scip_clang(
        project_root, compdb_path, out_scip, *, total_tus=None, print_fn=print
    ):
        data = json.loads(compdb_path.read_text())
        reindexed.append([e["file"] for e in data])
        empty = scip_pb2.Index()
        empty.metadata.project_root = f"file://{project_root}"
        out_scip.write_bytes(empty.SerializeToString())

    monkeypatch.setattr(pipeline, "run_scip_clang", _fake_run_scip_clang)

    # Still dirty, unchanged since build -> nothing to do, scip-clang never runs.
    rc = pipeline.incremental_update(
        graph_db=db, compdb=compdb, project_root=tmp_path, print_fn=lambda *a: None
    )
    assert rc == 0
    assert reindexed == []

    # Reverted to the committed version -> the index holds different (dirty)
    # content than the tree now does -> stale, must be re-indexed.
    src.write_text("int a() { return 0; }\n")
    rc = pipeline.incremental_update(
        graph_db=db, compdb=compdb, project_root=tmp_path, print_fn=lambda *a: None
    )
    assert rc == 0
    assert reindexed == [[str(src)]]


# ---- run_scip_clang: consuming scip-clang's per-TU report --------------------
#
# The real binary (v0.4.0, verified) writes "[N/total] Indexed <file>" to stdout
# (flushed as it goes), one "[N/total] Merged partial index" line per TU as well,
# and a final "Finished indexing ..." summary. The fakes below reproduce that
# shape with a real /bin/sh stub so the actual Popen streaming path is exercised.


class _FakeClock:
    """Deterministic stand-in for the `time` module: every `monotonic()` call
    advances the clock by `step` seconds."""

    def __init__(self, step: float) -> None:
        self.t = 1000.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t


def _fake_scip_clang_bin(tmp_path: Path, lines: list[str], *, exit_code: int = 0) -> Path:
    payload = tmp_path / "fake-report.txt"
    payload.write_text("".join(f"{line}\n" for line in lines))
    script = tmp_path / "fake-scip-clang"
    script.write_text(f'#!/bin/sh\ncat "{payload}"\nexit {exit_code}\n')
    script.chmod(0o755)
    return script


def _tu_report(n: int, total: int, *, pad: bool = False) -> list[str]:
    """The per-TU report shape for `n` TUs of `total` (n == total on success)."""
    width = len(str(total))

    def num(i: int) -> str:
        return f"{i:>{width}}" if pad else str(i)  # the real binary pads N

    return (
        [f"[{num(i)}/{total}] Indexed src/t{i}.cpp" for i in range(1, n + 1)]
        + [f"[{num(i)}/{total}] Merged partial index for src/t{i}.cpp" for i in range(1, n + 1)]
        + [f"Finished indexing {n} translation units in 1.0s (num errored TUs: 0)."]
    )


def test_run_scip_clang_pipe_progress_bounded(tmp_path: Path, monkeypatch, capsys) -> None:
    """Under a pipe, 1000 TUs must not stream 1000+ raw lines: progress is
    re-emitted once per 5% bucket crossed (~21 lines whatever the TU count) and
    the raw per-TU flood is consumed, never forwarded."""
    monkeypatch.setattr(
        pipeline, "scip_clang_path", lambda: _fake_scip_clang_bin(tmp_path, _tu_report(1000, 1000))
    )
    monkeypatch.setattr(pipeline, "time", _FakeClock(step=0.01))  # 10s total: no time trigger

    pipeline.run_scip_clang(
        tmp_path, tmp_path / "cc.json", tmp_path / "out.scip", total_tus=1000, print_fn=print
    )

    out = capsys.readouterr().out
    assert "running scip-clang" in out  # the pre-existing announcements still happen
    progress = [ln for ln in out.splitlines() if "indexed" in ln]
    assert len(progress) == 21  # TU 1, then one per 5%: 50, 100, ..., 1000
    assert "1000/1000 TU(s) (100%)" in progress[-1]
    assert "ETA" in progress[1]
    assert "Finished indexing 1000 translation units" in out  # the summary line survives
    assert "Indexed src/" not in out  # the ~2000 raw report lines are swallowed


def test_run_scip_clang_pipe_progress_time_cadence_without_total(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`total_tus=None` (a future caller without the denominator) degrades to a
    plain indexed count — no percentage, no ETA, no division — and still reports
    on the every-N-seconds cadence."""
    monkeypatch.setattr(
        pipeline, "scip_clang_path", lambda: _fake_scip_clang_bin(tmp_path, _tu_report(3, 7))
    )
    monkeypatch.setattr(pipeline, "time", _FakeClock(step=100.0))  # >60s apart: time trigger

    pipeline.run_scip_clang(tmp_path, tmp_path / "cc.json", tmp_path / "out.scip", print_fn=print)

    out = capsys.readouterr().out
    progress = [ln for ln in out.splitlines() if "indexed" in ln]
    assert len(progress) == 3  # one per TU: each arrives >60s after the last on this clock
    assert "indexed 1 TU(s)" in progress[0]
    assert "indexed 3 TU(s)" in progress[-1]
    assert all("%" not in ln and "ETA" not in ln for ln in progress)


def test_run_scip_clang_tty_progress_is_one_live_throttled_line(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """On a TTY, progress is a single in-place line (carriage return, no newline
    until the end), redrawn at most once per _TTY_REDRAW_INTERVAL — not once per
    TU — and ends at N/total with an ETA while the run is going."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)  # same pattern as test_cli.py
    monkeypatch.setattr(
        pipeline,
        "scip_clang_path",
        lambda: _fake_scip_clang_bin(tmp_path, _tu_report(10, 10, pad=True)),
    )
    monkeypatch.setattr(pipeline, "time", _FakeClock(step=0.1))

    pipeline.run_scip_clang(
        tmp_path, tmp_path / "cc.json", tmp_path / "out.scip", total_tus=10, print_fn=print
    )

    out = capsys.readouterr().out
    # Redraws at TUs 1, 4, 7, 10 (>=0.3s apart on this clock) + the final state
    # line: 5 carriage returns, and only 3 newlines (announcement, final line,
    # summary) — the updates overwrite in place instead of scrolling.
    assert out.count("\r") == 5
    assert out.count("\n") == 3
    assert "10/10 TU(s) (100%)" in out
    assert "ETA" in out
    assert "Finished indexing 10 translation units" in out


def test_run_scip_clang_nonzero_exit_still_raises_pipeline_error(
    tmp_path: Path, monkeypatch
) -> None:
    """The streaming refactor must keep the contract: nonzero exit -> PipelineError
    (and no deadlock: the single piped stream is drained to EOF before waiting)."""
    monkeypatch.setattr(
        pipeline,
        "scip_clang_path",
        lambda: _fake_scip_clang_bin(
            tmp_path, ["[1/2] Indexed a.cpp", "clang: error: broken TU"], exit_code=3
        ),
    )
    try:
        pipeline.run_scip_clang(tmp_path, tmp_path / "cc.json", tmp_path / "out.scip")
    except pipeline.PipelineError as e:
        assert "status 3" in str(e)
    else:
        raise AssertionError("expected PipelineError on nonzero scip-clang exit")
