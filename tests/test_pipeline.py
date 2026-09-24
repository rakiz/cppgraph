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


# ---- rescope_update: widening the recorded scope without a full rebuild --------
#
# The fakes follow test_incremental_update_matches_status_on_dirty_fingerprints:
# scip-clang is "present" (os.access patched) and the partial index it produces
# carries one definition per compdb entry, named after the file stem, so tests
# can assert exactly which TUs entered the graph.


def _scoped_store(db: Path, *, src_filter: str, tests: str, commit: str | None = None) -> None:
    """An empty store whose recorded scope is `src_filter` / tests `tests`."""
    index = scip_pb2.Index(metadata=scip_pb2.Metadata(project_root="file:///repo"))
    meta = build_provenance(
        index,
        source_commit=commit or "a" * 40,
        index_filter=src_filter,
        index_excludes_tests=tests == "excluded",
    )
    write_sqlite(Graph(), db, meta=meta)


def _fake_rescope_run_scip_clang(reindexed: list[list[str]]):
    """A run_scip_clang stand-in recording each run's compdb files and emitting a
    partial index defining `<stem>_sym()` per entry."""

    def _run(project_root, compdb_path, out_scip, *, total_tus=None, print_fn=print):
        data = json.loads(compdb_path.read_text())
        reindexed.append([e["file"] for e in data])
        idx = scip_pb2.Index(metadata=scip_pb2.Metadata(project_root=f"file://{project_root}"))
        for e in data:
            stem = Path(e["file"]).stem
            doc = idx.documents.add(relative_path=e["file"])
            occ = doc.occurrences.add(
                symbol=f"cxx . . $ {stem}_sym()",
                symbol_roles=scip_pb2.SymbolRole.Definition,
            )
            occ.range.extend([0, 0, 5])
        out_scip.write_bytes(idx.SerializeToString())

    return _run


def _rescope_env(tmp_path: Path, monkeypatch, entries: list[dict]) -> tuple[Path, list[list[str]]]:
    """scip-clang patched to exist + the fake partial-index producer wired in.
    Writes `entries` to a compdb in `tmp_path`; returns `(compdb_path, reindexed)`."""
    monkeypatch.setattr(pipeline.os, "access", lambda *a, **k: True)
    reindexed: list[list[str]] = []
    monkeypatch.setattr(pipeline, "run_scip_clang", _fake_rescope_run_scip_clang(reindexed))
    compdb = tmp_path / "cc.json"
    compdb.write_text(json.dumps(entries))
    return compdb, reindexed


def test_rescope_widens_filter_and_indexes_only_new_tus(tmp_path: Path, monkeypatch) -> None:
    """A valid filter widening (the new filter is a substring of the recorded one)
    re-indexes exactly the TUs the wider scope adds — not the already-in-scope
    ones, not the still-out-of-scope ones — and records the new scope in meta."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src/foo/bar", tests="excluded")
    compdb, reindexed = _rescope_env(
        tmp_path,
        monkeypatch,
        [
            {"file": "/repo/src/foo/bar/old.cpp"},  # already in scope
            {"file": "/repo/src/foo/new.cpp"},  # newly in scope
            {"file": "/repo/src/other/never.cpp"},  # still out of scope
        ],
    )

    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src/foo",
        print_fn=lambda *a: None,
    )
    assert rc == 0
    assert reindexed == [["/repo/src/foo/new.cpp"]]
    store = GraphStore(db)
    try:
        meta = store.meta()
        assert meta["index_filter"] == "src/foo"
        assert store.has_symbol("cxx . . $ new_sym()")
        assert not store.has_symbol("cxx . . $ never_sym()")
    finally:
        store.close()


def test_rescope_includes_previously_excluded_tests(tmp_path: Path, monkeypatch) -> None:
    """Tests excluded -> included widens the scope: a test TU that already matched
    the recorded filter but was dropped by the tests state is the only newly
    in-scope TU, and meta.index_tests flips to \"included\"."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src", tests="excluded")
    compdb, reindexed = _rescope_env(
        tmp_path,
        monkeypatch,
        [
            {"file": "/repo/src/a.cpp"},  # already in scope
            {"file": "/repo/src/a_test.cpp"},  # filter-matched, tests-excluded until now
        ],
    )

    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        include_tests=True,
        print_fn=lambda *a: None,
    )
    assert rc == 0
    assert reindexed == [["/repo/src/a_test.cpp"]]
    store = GraphStore(db)
    try:
        meta = store.meta()
        assert meta["index_filter"] == "src"  # untouched
        assert meta["index_tests"] == "included"
        assert store.has_symbol("cxx . . $ a_test_sym()")
    finally:
        store.close()


def test_rescope_widens_filter_and_tests_together(tmp_path: Path, monkeypatch) -> None:
    """Both widenings in one call: the newly in-scope set is the union (wider
    filter OR tests no longer dropped), one partial index covers it, and both
    meta keys move."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src/foo/bar", tests="excluded")
    compdb, reindexed = _rescope_env(
        tmp_path,
        monkeypatch,
        [
            {"file": "/repo/src/foo/bar/old.cpp"},  # already in scope
            {"file": "/repo/src/foo/new.cpp"},  # filter widening
            {"file": "/repo/src/foo/b_test.cpp"},  # test TU: filter-widened AND tests
            {"file": "/repo/src/x_test.cpp"},  # test TU outside the new filter
        ],
    )

    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src/foo",
        include_tests=True,
        print_fn=lambda *a: None,
    )
    assert rc == 0
    assert sorted(reindexed[0]) == ["/repo/src/foo/b_test.cpp", "/repo/src/foo/new.cpp"]
    store = GraphStore(db)
    try:
        meta = store.meta()
        assert meta["index_filter"] == "src/foo"
        assert meta["index_tests"] == "included"
        assert store.has_symbol("cxx . . $ new_sym()")
        assert store.has_symbol("cxx . . $ b_test_sym()")
        assert not store.has_symbol("cxx . . $ x_test_sym()")
    finally:
        store.close()


def test_rescope_records_scope_even_with_no_new_tus(tmp_path: Path, monkeypatch) -> None:
    """A real widening the compdb can't fill (no file outside the old scope):
    nothing is re-indexed, but the recorded scope must still move — a later
    plain `update` filters by meta, and would keep filtering with the old one."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src/foo/bar", tests="excluded")
    compdb, reindexed = _rescope_env(
        tmp_path,
        monkeypatch,
        [{"file": "/repo/src/foo/bar/old.cpp"}],  # nothing new under src/foo
    )

    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src/foo",
        print_fn=lambda *a: None,
    )
    assert rc == 0
    assert reindexed == []  # no scip-clang run at all
    store = GraphStore(db)
    try:
        assert store.meta()["index_filter"] == "src/foo"
    finally:
        store.close()


def test_rescope_rejects_filter_narrowing(tmp_path: Path, monkeypatch) -> None:
    """A new filter that is not a substring of the recorded one (a narrowing, or
    an orthogonal change) is refused with a clear error — no re-index, meta
    untouched — pointing at `init --from-scratch`."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src/foo", tests="excluded")
    compdb, reindexed = _rescope_env(
        tmp_path,
        monkeypatch,
        [{"file": "/repo/src/foo/a.cpp"}, {"file": "/repo/src/foo/bar/b.cpp"}],
    )

    def quiet(*a: object) -> None:
        pass

    # narrowing: a deeper subtree
    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src/foo/bar",
        print_fn=quiet,
    )
    assert rc == 1
    # orthogonal: a different subtree
    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src/other",
        print_fn=quiet,
    )
    assert rc == 1
    # narrowing from the whole tree: any subtree filter shrinks it
    _scoped_store(tmp_path / "whole.graph.db", src_filter="", tests="excluded")
    rc = pipeline.rescope_update(
        graph_db=tmp_path / "whole.graph.db",
        compdb=compdb,
        project_root=tmp_path,
        new_filter="src",
        print_fn=quiet,
    )
    assert rc == 1

    assert reindexed == []  # never re-indexed on a refused rescope
    store = GraphStore(db)
    try:
        assert store.meta()["index_filter"] == "src/foo"  # untouched
    finally:
        store.close()


def test_rescope_rejects_tests_included_to_excluded(tmp_path: Path, monkeypatch) -> None:
    """Turning tests OFF is a narrowing (already-indexed test TUs would have to be
    removed) — refused, not attempted."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src", tests="included")
    compdb, reindexed = _rescope_env(tmp_path, monkeypatch, [{"file": "/repo/src/a_test.cpp"}])

    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        include_tests=False,
        print_fn=lambda *a: None,
    )
    assert rc == 1
    assert reindexed == []
    store = GraphStore(db)
    try:
        assert store.meta()["index_tests"] == "included"  # untouched
    finally:
        store.close()


def test_rescope_rejects_when_nothing_to_widen(tmp_path: Path, monkeypatch) -> None:
    """A rescope that wouldn't change the scope is refused with a clear \"nothing
    to widen\" error: no request at all, a filter identical to the recorded one,
    or --include-tests when tests are already included."""
    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src", tests="excluded")
    compdb, reindexed = _rescope_env(tmp_path, monkeypatch, [{"file": "/repo/src/a.cpp"}])

    def quiet(*a: object) -> None:
        pass

    # no widening requested at all
    assert (
        pipeline.rescope_update(graph_db=db, compdb=compdb, project_root=tmp_path, print_fn=quiet)
        == 1
    )
    # a filter identical to the recorded one is not a widening
    assert (
        pipeline.rescope_update(
            graph_db=db,
            compdb=compdb,
            project_root=tmp_path,
            new_filter="src",
            print_fn=quiet,
        )
        == 1
    )
    # tests already included: asking again is not a widening
    _scoped_store(tmp_path / "tests_in.graph.db", src_filter="src", tests="included")
    assert (
        pipeline.rescope_update(
            graph_db=tmp_path / "tests_in.graph.db",
            compdb=compdb,
            project_root=tmp_path,
            include_tests=True,
            print_fn=quiet,
        )
        == 1
    )

    assert reindexed == []
    store = GraphStore(db)
    try:
        meta = store.meta()
        assert meta["index_filter"] == "src"
        assert meta["index_tests"] == "excluded"
    finally:
        store.close()


def test_rescope_lets_plain_update_see_new_scope(tmp_path: Path, monkeypatch) -> None:
    """The point of the meta flip: before a rescope, plain `update` filters a
    drifted test TU right back out; after it, the same drift is picked up."""

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = tmp_path / "src"
    src.mkdir()
    main_cpp = src / "a.cpp"
    test_cpp = src / "a_test.cpp"
    main_cpp.write_text("int a() { return 0; }\n")
    test_cpp.write_text("int t() { return 0; }\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    db = tmp_path / "proj.graph.db"
    _scoped_store(db, src_filter="src", tests="excluded", commit=commit)
    compdb, reindexed = _rescope_env(
        tmp_path, monkeypatch, [{"file": str(main_cpp)}, {"file": str(test_cpp)}]
    )

    def quiet(*a: object) -> None:
        pass

    # Before the rescope: the test TU drifted, but the recorded scope (tests
    # excluded) filters it out — plain `update` re-indexes nothing.
    test_cpp.write_text("int t() { return 1; }\n")
    rc = pipeline.incremental_update(
        graph_db=db, compdb=compdb, project_root=tmp_path, print_fn=quiet
    )
    assert rc == 0
    assert reindexed == []

    # The rescope widens to tests-included; the drifted test TU is newly in
    # scope and gets indexed (at its current content).
    rc = pipeline.rescope_update(
        graph_db=db,
        compdb=compdb,
        project_root=tmp_path,
        include_tests=True,
        print_fn=quiet,
    )
    assert rc == 0
    assert reindexed == [[str(test_cpp)]]

    # After the rescope: the same kind of drift is no longer filtered — plain
    # `update` (no rescope flags) picks the test TU up.
    test_cpp.write_text("int t() { return 2; }\n")
    rc = pipeline.incremental_update(
        graph_db=db, compdb=compdb, project_root=tmp_path, print_fn=quiet
    )
    assert rc == 0
    assert reindexed[-1] == [str(test_cpp)]


# --- pre-index check: stale compdb include directories ------------------------


def _include_compdb(path: Path, dirs: list[str]) -> Path:
    """A two-entry compdb whose entries include the given directories (one flag
    per entry via `command`, one via `arguments` — both compdb spellings)."""
    entries = [
        {
            "directory": "/repo",
            "file": "/repo/src/a.cpp",
            "command": f"clang++ -I{dirs[0]} -c a.cpp",
        },
        {
            "directory": "/repo",
            "file": "/repo/src/b.cpp",
            "arguments": ["clang++", "-isystem", dirs[1], "-c", "b.cpp"],
        },
    ]
    path.write_text(json.dumps(entries))
    return path


def test_missing_include_dirs_counts_sampled_dirs(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    compdb = _include_compdb(tmp_path / "cc.json", [str(good), "/gone-a"])
    checked, missing, examples = pipeline.missing_include_dirs(compdb)
    assert checked == 2  # deduped across entries and spellings
    assert missing == 1
    assert examples == ["/gone-a"]


def test_missing_include_dirs_samples_not_all(tmp_path: Path) -> None:
    """Only the first `sample` entries are read — the check stays cheap on a
    40k-entry compdb."""
    entries = [
        {
            "directory": "/repo",
            "file": f"/repo/src/t{i}.cpp",
            "command": f"clang++ -I/gone-{i} -c t{i}.cpp",
        }
        for i in range(10)
    ]
    compdb = tmp_path / "cc.json"
    compdb.write_text(json.dumps(entries))
    checked, missing, _examples = pipeline.missing_include_dirs(compdb, sample=3)
    assert checked == 3
    assert missing == 3


def test_missing_include_dirs_no_flags_or_empty(tmp_path: Path) -> None:
    """No include flags (or no entries) -> nothing checked, no examples —
    never a crash and never a warning."""
    compdb = tmp_path / "cc.json"
    compdb.write_text(
        json.dumps([{"directory": "/repo", "file": "a.cpp", "command": "clang++ -c a.cpp"}])
    )
    assert pipeline.missing_include_dirs(compdb) == (0, 0, [])
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert pipeline.missing_include_dirs(empty) == (0, 0, [])


def test_warn_missing_include_dirs_fires_above_threshold(tmp_path: Path) -> None:
    """Most sampled include dirs gone (a deleted build tree, e.g. a stale bazel
    output_base) -> a WARNING naming the fraction, examples and the likely
    cause — but never an exception: the check warns, it does not fail."""
    compdb = _include_compdb(tmp_path / "cc.json", ["/gone-a", "/gone-b"])
    lines: list[str] = []
    pipeline.warn_missing_include_dirs(compdb, print_fn=lines.append)
    warning = "\n".join(lines)
    assert "WARNING" in warning
    assert "2 of 2" in warning
    assert "/gone-a" in warning
    assert "stale" in warning


def test_warn_missing_include_dirs_silent_below_threshold(tmp_path: Path) -> None:
    compdb = _include_compdb(tmp_path / "cc.json", ["/gone-a", "/gone-b"])
    compdb.write_text(
        json.dumps(
            [
                {"directory": "/repo", "file": "/repo/src/a.cpp", "command": "clang++ -c a.cpp"},
                {"directory": "/repo", "file": "/repo/src/b.cpp", "command": "clang++ -c b.cpp"},
            ]
        )
    )
    lines: list[str] = []
    pipeline.warn_missing_include_dirs(compdb, print_fn=lines.append)
    assert lines == []  # nothing missing at all -> no output


def test_warn_missing_include_dirs_silent_on_minor_missing(tmp_path: Path) -> None:
    """One stale dir among many is noise (an optional/generated path) — below
    the warn fraction the check stays quiet. Distinct dirs per entry: the
    sample dedups, so a shared dir would collapse the denominator."""
    compdb = tmp_path / "cc.json"
    good_roots = [tmp_path / f"good{i}" for i in range(19)]
    for g in good_roots:
        g.mkdir()
    compdb.write_text(
        json.dumps(
            [
                {
                    "directory": "/repo",
                    "file": f"/repo/src/t{i}.cpp",
                    "command": f"clang++ -I{good_roots[i]} -c t{i}.cpp",
                }
                for i in range(19)
            ]
            + [
                {
                    "directory": "/repo",
                    "file": "/repo/src/gone.cpp",
                    "command": "clang++ -I/gone -c gone.cpp",
                }
            ]
        )
    )
    lines: list[str] = []
    pipeline.warn_missing_include_dirs(compdb, print_fn=lines.append)
    assert lines == []


def test_full_build_warns_when_include_dirs_missing(tmp_path: Path, monkeypatch) -> None:
    """full_build runs the check before invoking scip-clang, on its own output
    channel (print_fn) — the wizard/CLI path shows the warning."""
    compdb = _include_compdb(tmp_path / "cc.json", ["/gone-a", "/gone-b"])

    def _boom(*_a: object, **_k: object) -> None:
        raise pipeline.PipelineError("stop here — the check must run BEFORE indexing")

    monkeypatch.setattr(pipeline, "run_scip_clang", _boom)
    lines: list[str] = []
    rc = pipeline.full_build(
        compdb=compdb,
        project_root=tmp_path,
        name="proj",
        src_filter="",
        no_tests=False,
        attributed_refs=False,
        recompute_scip=True,
        rebuild_graph=True,
        print_fn=lines.append,
    )
    assert rc == 1
    assert any("WARNING" in ln and "include director" in ln for ln in lines)


def test_missing_include_dirs_resolve_relative_to_entry_directory(
    tmp_path: Path,
) -> None:
    """Relative include dirs (`-I../include`, `-I.`) are the norm in CMake
    compdbs: they resolve against the entry's `directory` field (where the
    compiler runs), never against the process cwd — testing them raw made
    every normal compdb with relative includes warn spuriously. Missing
    examples come back resolved (absolute), for the warning text."""
    proj = tmp_path / "proj"
    (proj / "inc").mkdir(parents=True)  # exists, relative to the build dir
    (proj / "build").mkdir()  # the entry directory must exist for `..` traversal
    compdb = tmp_path / "cc.json"
    compdb.write_text(
        json.dumps(
            [
                {
                    # CMake records the BUILD dir as the entry's directory; the
                    # source's own `inc/` is then `-I../inc` from it.
                    "directory": str(proj / "build"),
                    "file": str(proj / "src/a.cpp"),
                    "command": "clang++ -I../inc -I./gone_rel -c a.cpp",
                }
            ]
        )
    )
    checked, missing, examples = pipeline.missing_include_dirs(compdb)
    assert checked == 2
    assert missing == 1
    assert examples == [str(proj / "build" / "gone_rel")]


def test_missing_include_dirs_relative_falls_back_to_compdb_parent(
    tmp_path: Path,
) -> None:
    """An entry without a `directory` field resolves relative dirs against the
    compdb's own directory — the compdb lives with the build tree it describes."""
    (tmp_path / "inc").mkdir()
    compdb = tmp_path / "cc.json"
    compdb.write_text(json.dumps([{"file": "a.cpp", "command": "clang++ -Iinc -c a.cpp"}]))
    assert pipeline.missing_include_dirs(compdb) == (1, 0, [])


def test_warn_missing_include_dirs_relative_not_a_false_positive(tmp_path: Path) -> None:
    """End to end: a healthy CMake-style compdb (relative includes, all present
    relative to their entries' directories) must not warn — no matter what the
    process cwd is."""
    proj = tmp_path / "proj"
    (proj / "inc").mkdir(parents=True)
    (proj / "build").mkdir()
    compdb = tmp_path / "cc.json"
    compdb.write_text(
        json.dumps(
            [
                {
                    "directory": str(proj / "build"),
                    "file": str(proj / "src/a.cpp"),
                    "command": "clang++ -I../inc -I. -c a.cpp",
                }
            ]
        )
    )
    lines: list[str] = []
    pipeline.warn_missing_include_dirs(compdb, print_fn=lines.append)
    assert lines == []
