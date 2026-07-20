"""Tests for `describe_scip` — the reuse/recompute introspection panel source."""

from __future__ import annotations

from pathlib import Path

from cppgraph.proto import scip_pb2
from cppgraph.scip_introspect import describe_scip


def _write_scip(path: Path, *, tool="scip-clang", version="0.4.0", root="/repo", docs=2) -> Path:
    index = scip_pb2.Index(
        metadata=scip_pb2.Metadata(
            project_root=root,
            tool_info=scip_pb2.ToolInfo(name=tool, version=version),
        ),
        documents=[scip_pb2.Document(relative_path=f"f{i}.cpp") for i in range(docs)],
    )
    path.write_bytes(index.SerializeToString())
    return path


def test_describe_absent(tmp_path: Path) -> None:
    st = describe_scip(tmp_path / "nope.scip")
    assert st == {"exists": False, "path": str(tmp_path / "nope.scip")}


def test_describe_reports_tool_root_and_document_count(tmp_path: Path) -> None:
    scip = _write_scip(tmp_path / "proj.scip", version="0.4.0", root="/repo/src", docs=3)
    st = describe_scip(scip)
    assert st["exists"] is True
    assert st["tool_name"] == "scip-clang"
    assert st["tool_version"] == "0.4.0"
    assert st["project_root"] == "/repo/src"
    assert st["document_count"] == 3
    assert st["size_bytes"] > 0
    assert "mtime" in st and "mtime_iso" in st


def test_describe_corrupt_scip_does_not_raise(tmp_path: Path) -> None:
    bad = tmp_path / "bad.scip"
    bad.write_bytes(b"\xff\xff not a real protobuf \x00\x01")
    st = describe_scip(bad)
    # A corrupt file still yields a description (so the wizard can lean recompute),
    # with the SCIP-derived fields absent.
    assert st["exists"] is True
    assert "error" in st or "document_count" not in st
