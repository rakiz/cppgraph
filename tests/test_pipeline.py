"""Tests for the index pipeline — especially the never-overwrite guards."""

from __future__ import annotations

import json
from pathlib import Path

from cppgraph import pipeline
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore


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
