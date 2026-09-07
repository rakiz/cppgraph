from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.model import Graph, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, write_sqlite


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
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return path


def test_find_reports_matches(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["find", "--graph", str(graph_path), "makeResumeToken"]) == 0
    out = capsys.readouterr().out
    assert "makeResumeToken" in out


def test_find_no_match_returns_nonzero(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["find", "--graph", str(graph_path), "nope"]) == 1


def test_callers_lists_caller_with_location(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["callers", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    # By default the CLI prints readable labels (like the MCP tools): the caller
    # row shows the stripped label, not the raw `cxx . . $ …` SCIP string.
    assert "  mongo/Foo#caller(a2).  (foo.cpp:10)" in out


def test_callers_full_symbols_prints_raw_scip(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "callers",
            "--graph",
            str(graph_path),
            "--full-symbols",
            "cxx . . $ mongo/Foo#makeResumeToken(a1).",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "cxx . . $ mongo/Foo#caller(a2)." in out


def test_callees_lists_callee(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["callees", "--graph", str(graph_path), "cxx . . $ mongo/Foo#caller(a2)."])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "makeResumeToken(a1)." in out


def test_callers_unknown_symbol_errors(graph_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["callers", "--graph", str(graph_path), "nonexistent"])


@pytest.fixture
def filter_graph(tmp_path: Path) -> Path:
    """`hub` is called by one real caller and one test caller; it calls a domain
    function and a trivial helper (`uassert`). Enough to exercise every filter."""
    graph = Graph()
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#caller(a1).",
        "cxx . . $ mongo/Foo#hub(h1).",
        file="foo.cpp",
        line=1,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#TestBody(t1).",
        "cxx . . $ mongo/Foo#hub(h1).",
        file="foo_test.cpp",
        line=2,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#hub(h1).",
        "cxx . . $ mongo/Foo#domain(d1).",
        file="foo.cpp",
        line=3,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#hub(h1).",
        "cxx . . $ mongo/Foo#uassert(u1).",
        file="foo.cpp",
        line=4,
    )
    # Definition sites: needed so exclude-tests can resolve the far endpoint's file.
    graph.add_node("cxx . . $ mongo/Foo#caller(a1).").file = "foo.cpp"
    graph.add_node("cxx . . $ mongo/Foo#TestBody(t1).").file = "foo_test.cpp"
    graph.add_node("cxx . . $ mongo/Foo#domain(d1).").file = "foo.cpp"
    graph.add_node("cxx . . $ mongo/Foo#uassert(u1).").file = "foo.cpp"
    path = tmp_path / "f.db"
    write_sqlite(graph, path)
    return path


def test_callers_excludes_tests_by_default(
    filter_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _callers(filter_graph, capsys)
    assert "1 caller(s)" in out and "excluding tests" in out
    assert "caller(a1)." in out
    assert "TestBody" not in out


def test_callers_no_exclude_tests_keeps_them(
    filter_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _callers(filter_graph, capsys, "--no-exclude-tests")
    assert "2 caller(s)" in out
    assert "TestBody(t1)." in out


def test_callees_hide_trivial_drops_helpers(
    filter_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["callees", "--graph", str(filter_graph), "--hide-trivial", "cxx . . $ mongo/Foo#hub(h1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "domain(d1)." in out
    assert "uassert" not in out
    assert "1 trivial callee(s) hidden" in out


def test_callers_limit_caps_and_reports_remainder(
    filter_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _callers(filter_graph, capsys, "--no-exclude-tests", "--limit", "1")
    assert "2 caller(s)" in out  # true total still reported
    assert "and 1 more" in out


def test_callers_exclude_path_drops_vendored_caller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = Graph()
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#projCaller(p1).",
        "cxx . . $ mongo/Foo#target(t1).",
        file="foo.cpp",
        line=1,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#vendorCaller(v1).",
        "cxx . . $ mongo/Foo#target(t1).",
        file="foo.cpp",
        line=2,
    )
    graph.add_node("cxx . . $ mongo/Foo#projCaller(p1).").file = "src/myproject/foo.cpp"
    graph.add_node("cxx . . $ mongo/Foo#vendorCaller(v1).").file = "vendor/somelib/foo.cpp"
    path = tmp_path / "pf.db"
    write_sqlite(graph, path)
    exit_code = main(
        [
            "callers",
            "--graph",
            str(path),
            "--exclude-path",
            "vendor/",
            "cxx . . $ mongo/Foo#target(t1).",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 caller(s)" in out
    assert "projCaller" in out
    assert "vendorCaller" not in out


def test_callers_include_path_keeps_only_project_caller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = Graph()
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#projCaller(p1).",
        "cxx . . $ mongo/Foo#target(t1).",
        file="foo.cpp",
        line=1,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#vendorCaller(v1).",
        "cxx . . $ mongo/Foo#target(t1).",
        file="foo.cpp",
        line=2,
    )
    graph.add_node("cxx . . $ mongo/Foo#projCaller(p1).").file = "src/myproject/foo.cpp"
    graph.add_node("cxx . . $ mongo/Foo#vendorCaller(v1).").file = "vendor/somelib/foo.cpp"
    path = tmp_path / "pf2.db"
    write_sqlite(graph, path)
    exit_code = main(
        [
            "callers",
            "--graph",
            str(path),
            "--include-path",
            "src/myproject/",
            "cxx . . $ mongo/Foo#target(t1).",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 caller(s)" in out
    assert "projCaller" in out
    assert "vendorCaller" not in out


def _callers(graph: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    exit_code = main(["callers", "--graph", str(graph), *extra, "cxx . . $ mongo/Foo#hub(h1)."])
    out = capsys.readouterr().out
    assert exit_code == 0
    return out


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


def _write_attributed_scip(tmp_path: Path) -> Path:
    """A .scip where a definition carries an enclosing_range (its body extent, as
    a #504-built scip-clang emits) and a use of a type sits inside that body — so
    the use is attributed to the definition by containment."""
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///repo"
    doc = index.documents.add(relative_path="render.cpp")
    user = doc.occurrences.add(
        symbol="cxx . . $ pkg/render(r1).", symbol_roles=scip_pb2.SymbolRole.Definition
    )
    user.range.extend([5, 0, 10])
    user.enclosing_range.extend([5, 0, 20, 0])  # render() body spans 5..20
    use = doc.occurrences.add(symbol="cxx . . $ pkg/Widget#")
    use.range.extend([8, 0, 6])  # a use of Widget inside render()
    scip = tmp_path / "index.scip"
    scip.write_bytes(index.SerializeToString())
    return scip


def test_build_attributed_refs_reports_and_status_shows_symbol_granularity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scip = _write_attributed_scip(tmp_path)
    out = tmp_path / "g.db"
    assert main(["build", "--scip", str(scip), "--out", str(out), "--attributed-refs"]) == 0
    assert "attributed to enclosing symbols" in capsys.readouterr().out

    assert main(["status", "--graph", str(out)]) == 0
    status = capsys.readouterr().out
    assert "usage view:    SYMBOL granularity" in status


def test_status_recommends_attribution_when_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scip = _write_attributed_scip(tmp_path)
    out = tmp_path / "g.db"
    # Default build: references on, attribution off.
    assert main(["build", "--scip", str(scip), "--out", str(out)]) == 0
    capsys.readouterr()
    assert main(["status", "--graph", str(out)]) == 0
    status = capsys.readouterr().out
    assert "file granularity" in status
    assert "enrich-refs" in status  # the upgrade path is surfaced


def test_enrich_refs_upgrades_existing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scip = _write_attributed_scip(tmp_path)
    out = tmp_path / "g.db"
    assert main(["build", "--scip", str(scip), "--out", str(out)]) == 0
    capsys.readouterr()

    assert main(["enrich-refs", "--graph", str(out), "--scip", str(scip)]) == 0
    assert "symbol-granularity" in capsys.readouterr().out

    graph = GraphStore(out)
    refs = graph.references_of("cxx . . $ pkg/Widget#")
    assert [r.enclosing_symbol for r in refs] == ["cxx . . $ pkg/render(r1)."]


def test_build_records_source_commit_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Minimal synthetic .scip: a callable definition, so the graph is non-empty.
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///some/repo"
    doc = index.documents.add(relative_path="foo.cpp")
    occ = doc.occurrences.add(
        symbol="cxx . . $ mongo/Foo#bar(a1).", symbol_roles=scip_pb2.SymbolRole.Definition
    )
    occ.range.extend([0, 0, 3])
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    out = tmp_path / "graph.db"

    exit_code = main(
        [
            "build",
            "--scip",
            str(scip_path),
            "--out",
            str(out),
            "--source-commit",
            "cafebabe",
            "--source-dirty",
        ]
    )
    assert exit_code == 0
    assert "cafebabe" in capsys.readouterr().out

    meta = GraphStore(out).meta()
    assert meta["source_commit"] == "cafebabe"
    assert meta["source_dirty"] == "true"
    assert meta["project_root"] == "file:///some/repo"


def test_build_records_and_status_shows_index_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///some/repo"
    doc = index.documents.add(relative_path="foo.cpp")
    occ = doc.occurrences.add(
        symbol="cxx . . $ mongo/Foo#bar(a1).", symbol_roles=scip_pb2.SymbolRole.Definition
    )
    occ.range.extend([0, 0, 3])
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    out = tmp_path / "graph.db"

    assert (
        main(
            [
                "build",
                "--scip",
                str(scip_path),
                "--out",
                str(out),
                "--index-filter",
                "src/mongo",
                "--index-no-tests",
            ]
        )
        == 0
    )
    meta = GraphStore(out).meta()
    assert meta["index_filter"] == "src/mongo"
    assert meta["index_tests"] == "excluded"

    capsys.readouterr()  # drop build output
    assert main(["status", "--graph", str(out)]) == 0
    assert "indexed scope: src/mongo (tests excluded)" in capsys.readouterr().out


def test_status_omits_index_scope_for_legacy_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A graph without recorded scope (legacy) must not crash status and must not
    invent a scope line."""
    graph = Graph()
    graph.add_edge("calls", "a().", "b().", file="foo.cpp", line=1)
    out = tmp_path / "graph.db"
    write_sqlite(graph, out, meta={"source_commit": "abc"})

    assert main(["status", "--graph", str(out)]) == 0
    assert "indexed scope" not in capsys.readouterr().out


def test_update_applies_partial_reindex(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Start from a store where foo.cpp has a() calling old().
    original = Graph()
    original.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#a(a1).",
        "cxx . . $ mongo/Foo#old(o1).",
        file="foo.cpp",
        line=5,
    )
    db = tmp_path / "graph.db"
    write_sqlite(original, db)

    # Partial re-index of foo.cpp: a() now calls new().
    index = scip_pb2.Index()
    index.metadata.project_root = "file:///some/repo"
    doc = index.documents.add(relative_path="foo.cpp")
    d = doc.occurrences.add(
        symbol="cxx . . $ mongo/Foo#a(a1).", symbol_roles=scip_pb2.SymbolRole.Definition
    )
    d.range.extend([2, 0, 3])
    c = doc.occurrences.add(symbol="cxx . . $ mongo/Foo#new(n1).")
    c.range.extend([6, 0, 3])
    scip_path = tmp_path / "partial.scip"
    scip_path.write_bytes(index.SerializeToString())

    exit_code = main(
        ["update", "--graph", str(db), "--scip", str(scip_path), "--source-commit", "newsha"]
    )
    assert exit_code == 0

    store = GraphStore(db)
    assert [e.dst for e in store.callees_of("cxx . . $ mongo/Foo#a(a1).")] == [
        "cxx . . $ mongo/Foo#new(n1)."
    ]
    assert not store.has_symbol("cxx . . $ mongo/Foo#old(o1).")
    assert store.meta()["source_commit"] == "newsha"


def test_update_with_no_args_auto_discovers_and_runs_incremental_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cppgraph update` with no flags is the real entry point: discover the graph
    and compdb from the cwd (like every other query command) and run a full
    incremental update — no `--graph`/`--scip` required."""
    import subprocess as sp

    from cppgraph import pipeline
    from cppgraph.store import build_provenance

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = tmp_path / "a.cpp"
    src.write_text("int a() {}\n")
    (tmp_path / "compile_commands.json").write_text(json.dumps([{"file": str(src)}]))
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    cpg = tmp_path / ".cppgraph"
    cpg.mkdir()
    index = scip_pb2.Index(metadata=scip_pb2.Metadata(project_root=f"file://{tmp_path}"))
    meta = build_provenance(index, source_commit=commit)
    write_sqlite(Graph(), cpg / "proj.graph.db", meta=meta)

    src.write_text("int a() { return 1; }\n")  # a real change since the indexed commit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline.os, "access", lambda *a, **k: True)  # pretend scip-clang exists
    reindexed: list[list[str]] = []

    def _fake_run_scip_clang(project_root, compdb_path, out_scip, *, print_fn=print):
        data = json.loads(compdb_path.read_text())
        reindexed.append([e["file"] for e in data])
        empty = scip_pb2.Index()
        empty.metadata.project_root = f"file://{project_root}"
        out_scip.write_bytes(empty.SerializeToString())

    monkeypatch.setattr(pipeline, "run_scip_clang", _fake_run_scip_clang)

    assert main(["update"]) == 0
    assert reindexed == [[str(src)]]


def test_update_with_explicit_graph_uses_recorded_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `--graph` isn't required to live at the conventional
    `<project_root>/.cppgraph/<name>.graph.db` depth — the store's own recorded
    `project_root` is authoritative, not a `parent.parent` guess from the path."""
    import subprocess as sp

    from cppgraph import pipeline
    from cppgraph.store import build_provenance

    def git(*a: str) -> sp.CompletedProcess:
        return sp.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    src = tmp_path / "a.cpp"
    src.write_text("int a() {}\n")
    (tmp_path / "compile_commands.json").write_text(json.dumps([{"file": str(src)}]))
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    commit = git("rev-parse", "HEAD").stdout.strip()

    # Graph lives somewhere with NO relation to tmp_path two levels up — only
    # `meta.project_root` says where the real checkout/compdb are.
    elsewhere = tmp_path / "elsewhere" / "nested"
    elsewhere.mkdir(parents=True)
    graph_path = elsewhere / "proj.graph.db"
    index = scip_pb2.Index(metadata=scip_pb2.Metadata(project_root=f"file://{tmp_path}"))
    meta = build_provenance(index, source_commit=commit)
    write_sqlite(Graph(), graph_path, meta=meta)

    src.write_text("int a() { return 1; }\n")
    monkeypatch.setattr(pipeline.os, "access", lambda *a, **k: True)
    reindexed: list[list[str]] = []

    def _fake_run_scip_clang(project_root, compdb_path, out_scip, *, print_fn=print):
        data = json.loads(compdb_path.read_text())
        reindexed.append([e["file"] for e in data])
        empty = scip_pb2.Index()
        empty.metadata.project_root = f"file://{project_root}"
        out_scip.write_bytes(empty.SerializeToString())

    monkeypatch.setattr(pipeline, "run_scip_clang", _fake_run_scip_clang)

    assert main(["update", "--graph", str(graph_path)]) == 0
    assert reindexed == [[str(src)]]


@pytest.fixture
def explain_graph(tmp_path: Path) -> Path:
    """A graph whose symbol has a real definition site (file + line)."""
    graph = Graph()
    node = graph.add_node("cxx . . $ mongo/Foo#bar(a1).", display_name="bar")
    node.file = "src/foo.cpp"
    node.line = 3  # 0-indexed -> source line 4
    # one caller and one callee so explain can summarize both directions
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#caller(a2).",
        "cxx . . $ mongo/Foo#bar(a1).",
        file="src/foo.cpp",
        line=20,
    )
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#bar(a1).",
        "cxx . . $ mongo/Foo#callee(a3).",
        file="src/foo.cpp",
        line=5,
    )
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return path


def _write_source(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "foo.cpp").write_text(
        "line0\nline1\nline2\nint Foo::bar() {\n  return callee();\n}\nline6\n"
    )


def test_explain_shows_definition_and_source_snippet(
    explain_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "checkout"
    _write_source(root)
    exit_code = main(
        [
            "explain",
            "--graph",
            str(explain_graph),
            "cxx . . $ mongo/Foo#bar(a1).",
            "--root",
            str(root),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "src/foo.cpp:4" in out  # def location, 1-indexed
    assert "int Foo::bar() {" in out  # the snippet line
    assert "1 caller(s)" in out
    assert "1 callee(s)" in out


def test_explain_missing_source_is_graceful(
    explain_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --root points nowhere useful: still reports location + counts, no crash.
    exit_code = main(
        [
            "explain",
            "--graph",
            str(explain_graph),
            "cxx . . $ mongo/Foo#bar(a1).",
            "--root",
            str(tmp_path / "absent"),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "src/foo.cpp:4" in out
    assert "source not found" in out.lower()


def test_explain_without_root_returns_coordinates_only(
    explain_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No --root => coordinates only, source never read (the single switch).
    exit_code = main(["explain", "--graph", str(explain_graph), "cxx . . $ mongo/Foo#bar(a1)."])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "src/foo.cpp:4" in out  # coordinates still reported
    assert "source:" not in out  # but no snippet section
    assert "int Foo::bar()" not in out  # source text was not read
    assert "1 caller(s)" in out
    assert "1 callee(s)" in out
    assert "tip:" not in out  # non-interactive (captured) => no human hint


def test_explain_tip_shown_only_when_interactive(
    explain_graph: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    exit_code = main(["explain", "--graph", str(explain_graph), "cxx . . $ mongo/Foo#bar(a1)."])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "tip: pass --root" in out


def test_explain_unknown_symbol_errors(explain_graph: Path) -> None:
    with pytest.raises(SystemExit):
        main(["explain", "--graph", str(explain_graph), "nonexistent", "--root", "/tmp"])


def test_explain_shows_signature_with_default_argument(
    explain_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "checkout"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "foo.cpp").write_text(
        "line0\nline1\nline2\nvoid bar(const Document& doc, bool useNullIfMissing = false) {\n}\n"
    )
    exit_code = main(
        [
            "explain",
            "--graph",
            str(explain_graph),
            "cxx . . $ mongo/Foo#bar(a1).",
            "--root",
            str(root),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "signature:  (const Document& doc, bool useNullIfMissing = false)" in out


def test_explain_omits_signature_without_root(
    explain_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["explain", "--graph", str(explain_graph), "cxx . . $ mongo/Foo#bar(a1)."])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "signature:" not in out


def _init_repo(root: Path) -> str:
    """Init a git repo with one committed file; return the HEAD commit hash."""
    root.mkdir(parents=True, exist_ok=True)

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (root / "a.cpp").write_text("int a() { return 0; }\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def _store_at(tmp_path: Path, commit: str | None) -> Path:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#a(a1).", display_name="a")
    db = tmp_path / "graph.db"
    meta = {"source_commit": commit} if commit else {}
    write_sqlite(graph, db, meta=meta)
    return db


def test_status_reports_recorded_commit_without_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _store_at(tmp_path, "deadbeefcafe")
    assert main(["status", "--graph", str(db)]) == 0
    out = capsys.readouterr().out
    assert "deadbeefcafe" in out
    assert "transport:     cli" in out


def test_status_up_to_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    head = _init_repo(root)
    db = _store_at(tmp_path, head)
    assert main(["status", "--graph", str(db), "--root", str(root)]) == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_status_detects_stale_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    head = _init_repo(root)
    db = _store_at(tmp_path, head)
    (root / "a.cpp").write_text("int a() { return 1; }\n")  # uncommitted edit
    exit_code = main(["status", "--graph", str(db), "--root", str(root)])
    out = capsys.readouterr().out
    assert exit_code == 1  # nonzero so `status || reindex` works in a shell
    assert "stale" in out.lower()
    assert "a.cpp" in out


def test_status_ignores_non_source_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    head = _init_repo(root)
    db = _store_at(tmp_path, head)
    # change only a non-C++ file, then commit it
    (root / "README.md").write_text("docs\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "docs"], check=True, capture_output=True
    )
    assert main(["status", "--graph", str(db), "--root", str(root)]) == 0
    assert "up to date" in capsys.readouterr().out.lower()


def _store_with_recorded_root(root: Path, commit: str) -> Path:
    """A store whose `meta.project_root` points at `root` — the provenance
    `_open_store_checked` resolves `root` from for commands with no `--root`
    flag of their own (`callers`, `callees`, `find`, …)."""
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#makeResumeToken(a1).", display_name="makeResumeToken")
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#caller(a2).",
        "cxx . . $ mongo/Foo#makeResumeToken(a1).",
        file="foo.cpp",
        line=9,
    )
    db = root / "graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit, "project_root": f"file://{root}"})
    return db


def test_query_command_warns_on_stderr_when_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_open_store_checked` (used by `callers` and friends) resolves `root` from
    the store's recorded `project_root` when no `--root` flag exists, and warns
    on stderr — but only once the checkout has actually drifted."""
    root = tmp_path / "repo"
    head = _init_repo(root)
    db = _store_with_recorded_root(root, head)

    assert main(["callers", "--graph", str(db), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]) == 0
    assert capsys.readouterr().err == ""

    (root / "a.cpp").write_text("int a() { return 1; }\n")  # uncommitted drift
    assert main(["callers", "--graph", str(db), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]) == 0
    assert "stale" in capsys.readouterr().err.lower()


def test_impact_lists_transitive_callers(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["impact", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 symbol(s)" in out
    assert "caller(a2)." in out


@pytest.fixture
def hotspots_graph(tmp_path: Path) -> Path:
    graph = Graph()
    for i, caller in enumerate(["c1", "c2", "c3", "c4", "c5"]):
        graph.add_edge("calls", caller, "hot", file="f.cpp", line=i)
    for i, caller in enumerate(["c1", "c2", "c3"]):
        graph.add_edge("calls", caller, "warm", file="f.cpp", line=10 + i)
    graph.add_edge("calls", "c1", "cold", file="f.cpp", line=20)
    path = tmp_path / "hotspots.db"
    write_sqlite(graph, path)
    return path


def test_hotspots_ranks_fan_in_descending(
    hotspots_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["hotspots", "--graph", str(hotspots_graph)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 3 of 3 symbol(s) by fan_in" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split()[0] == "5"
    assert "hot" in lines[0]
    assert lines[1].split()[0] == "3"
    assert lines[2].split()[0] == "1"


def test_hotspots_limit_truncates_and_reports_total(
    hotspots_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["hotspots", "--graph", str(hotspots_graph), "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 3 symbol(s)" in out
    assert "... and 2 more" in out


def test_hotspots_fan_out_kind(hotspots_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["hotspots", "--graph", str(hotspots_graph), "--kind", "fan_out"])
    out = capsys.readouterr().out
    assert exit_code == 0
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split()[0] == "3"
    assert "c1" in lines[0]


def test_hotspots_exclude_tests_drops_edges_touching_a_test_defined_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test-ness is decided by the symbol's own definition file (like
    `filters.drop_test_edges`), not the call site — matches `store.hotspots`."""
    graph = Graph()
    graph.add_edge("calls", "prod_caller", "target", file="src/foo.cpp", line=1)
    graph.add_edge("calls", "test_helper_caller", "target", file="src/foo.cpp", line=2)
    graph.nodes["test_helper_caller"].file = "src/foo_test.cpp"
    path = tmp_path / "excl.db"
    write_sqlite(graph, path)
    exit_code = main(["hotspots", "--graph", str(path), "--exclude-tests"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 1 symbol(s)" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split()[0] == "1"  # only prod_caller's edge counted


def test_hotspots_exclude_path_drops_edges_touching_a_vendored_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Path-prefix parity test, mirroring the exclude-tests test above: the same
    SQL-side filtering shape (`cpg_path_ok`), symmetric on both endpoints."""
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "target", file="src/myproject/foo.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "target", file="src/myproject/foo.cpp", line=2)
    graph.nodes["proj_caller"].file = "src/myproject/foo.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/foo.cpp"
    graph.nodes["target"].file = "src/myproject/foo.cpp"
    path = tmp_path / "pf_hotspots.db"
    write_sqlite(graph, path)
    exit_code = main(["hotspots", "--graph", str(path), "--exclude-path", "vendor/"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 1 symbol(s)" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split()[0] == "1"  # only proj_caller's edge counted


@pytest.fixture
def stats_graph(tmp_path: Path) -> Path:
    graph = Graph()
    graph.nodes["big1"] = Node(symbol="big1", file="src/big.cpp", line=1)
    graph.nodes["big2"] = Node(symbol="big2", file="src/big.cpp", line=2)
    graph.add_edge("calls", "big1", "big2", file="src/big.cpp", line=5)
    graph.add_edge("calls", "big1", "helper", file="src/big.cpp", line=6)
    for line in (10, 11, 12):
        graph.add_reference("TYPE", "src/big.cpp", line)
    graph.nodes["small"] = Node(symbol="small", file="src/small.cpp", line=1)
    graph.add_edge("calls", "small", "helper", file="src/small.cpp", line=2)
    graph.nodes["vend"] = Node(symbol="vend", file="vendor/tiny.cpp", line=1)
    path = tmp_path / "stats.db"
    write_sqlite(graph, path)
    return path


def test_stats_counts_per_file(stats_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["stats", "--graph", str(stats_graph)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 3 of 3 file(s)" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split() == ["2", "sym", "2", "edges", "3", "refs", "src/big.cpp"]
    assert any(line.endswith("src/small.cpp") for line in lines)


def test_stats_dir_rollup(stats_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["stats", "--graph", str(stats_graph), "--group-by", "dir"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 2 of 2 dir(s)" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split() == ["3", "sym", "3", "edges", "3", "refs", "src"]
    assert lines[1].split()[-1] == "vendor"


def test_stats_limit_truncates_and_reports_total(
    stats_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["stats", "--graph", str(stats_graph), "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 3 file(s)" in out
    assert "... and 2 more" in out


def test_stats_exclude_path_scopes_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Path-prefix parity test, mirroring the hotspots exclude-path one above:
    the filter applies to the counted file itself, before aggregation."""
    graph = Graph()
    graph.nodes["a"] = Node(symbol="a", file="src/myproject/a.cpp", line=1)
    graph.nodes["v"] = Node(symbol="v", file="vendor/lib/v.cpp", line=1)
    path = tmp_path / "pf_stats.db"
    write_sqlite(graph, path)
    exit_code = main(["stats", "--graph", str(path), "--exclude-path", "vendor/"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 1 file(s)" in out
    assert "src/myproject/a.cpp" in out
    assert "vendor/" not in out


BIG = "cxx . . $ app/big(b1)."
MIDFN = "cxx . . $ app/mid(m1)."
TINY = "cxx . . $ app/tiny(t1)."
NEVER = "cxx . . $ app/never_called(n1)."
WIDGET = "cxx . . $ app/Widget#"


@pytest.fixture
def spans_graph(tmp_path: Path) -> Path:
    """A #504-shaped graph: definitions carry body extents, one real call edge
    (big -> mid), a never-called callable, and a type."""
    graph = Graph()
    graph.nodes[BIG] = Node(symbol=BIG, file="src/app.cpp", line=10, end_line=110)
    graph.nodes[MIDFN] = Node(symbol=MIDFN, file="src/app.cpp", line=200, end_line=250)
    graph.nodes[TINY] = Node(symbol=TINY, file="src/app.cpp", line=300, end_line=302)
    graph.nodes[NEVER] = Node(symbol=NEVER, file="src/lib.cpp", line=5, end_line=8)
    graph.nodes[WIDGET] = Node(symbol=WIDGET, file="src/lib.cpp", line=1)
    graph.add_edge("calls", BIG, MIDFN, file="src/app.cpp", line=15)
    path = tmp_path / "spans.db"
    write_sqlite(graph, path)
    return path


def test_line_span_ranks_descending(spans_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["line_span", "--graph", str(spans_graph)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 4 of 4 definition(s) by body span" in out
    lines = [line for line in out.splitlines() if line.startswith("  ")]
    assert lines[0].split()[0] == "100"
    assert "big" in lines[0]
    assert lines[1].split()[0] == "50"


def test_line_span_limit_truncates_and_reports_total(
    spans_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["line_span", "--graph", str(spans_graph), "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "top 1 of 4 definition(s)" in out
    assert "... and 3 more" in out


def test_line_span_unavailable_returns_nonzero(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stock-binary graph (no body extents) gets an explicit unavailable
    message, like `references` without an index — not an empty list."""
    exit_code = main(["line_span", "--graph", str(graph_path)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "unavailable" in out
    assert "#504" in out


def test_no_incoming_calls_lists_zero_caller_definitions(
    spans_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["no_incoming_calls", "--graph", str(spans_graph)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "3 of 3 defined callable(s) with zero incoming calls" in out
    assert "big" in out
    assert "never_called" in out
    assert "mid" not in out  # mid has a caller; the type Widget is not callable
    # the fact-not-verdict caveat is printed with the answer
    assert "not proof of dead code" in out


def test_no_incoming_calls_limit_truncates_and_reports_total(
    spans_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["no_incoming_calls", "--graph", str(spans_graph), "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 3 defined callable(s)" in out
    assert "... and 2 more" in out


def test_no_incoming_calls_unavailable_returns_nonzero(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused on a stock-binary graph with the why: a phantom caller from a
    mis-attributed declaration site turns a real 0 into a false 1."""
    exit_code = main(["no_incoming_calls", "--graph", str(graph_path)])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "not reliable" in out
    assert "phantom caller" in out


@pytest.fixture
def boundary_graph(tmp_path: Path) -> Path:
    """One legal downward edge (`platform/` may call `common/`) and one that
    crosses `common/ -> platform/`."""
    graph = Graph()
    graph.add_edge("calls", "platform_fn", "common_fn", file="platform/io.cpp", line=1)
    graph.add_edge("calls", "common_fn", "platform_secret", file="common/util.cpp", line=9)
    graph.nodes["common_fn"].file = "common/util.cpp"
    graph.nodes["platform_fn"].file = "platform/io.cpp"
    graph.nodes["platform_secret"].file = "platform/hidden.cpp"
    path = tmp_path / "boundary.db"
    write_sqlite(graph, path)
    return path


def test_boundary_violations_prints_crossing_edge(
    boundary_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["boundary-violations", "--graph", str(boundary_graph), "--rule", "common/:platform/"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 1 violation(s) across 1 rule(s)" in out
    assert "[common/ -> platform/] calls  common_fn -> platform_secret" in out
    assert "(common/util.cpp:10)" in out
    # the facts-not-judgments caveat prints with the answer
    assert "zero false positives" in out


def test_boundary_violations_clean_layering_exits_zero(
    boundary_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["boundary-violations", "--graph", str(boundary_graph), "--rule", "platform/:projects/"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 of 0 violation(s)" in out
    # 0 is a lower bound, never proof of conformance
    assert "statically indexed" in out


def test_boundary_violations_limit_truncates_and_reports_total(
    boundary_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "boundary-violations",
            "--graph",
            str(boundary_graph),
            "--rule",
            "common/:platform/",
            "--limit",
            "0",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 of 1 violation(s)" in out
    assert "... and 1 more" in out


def test_boundary_violations_rule_flag_is_repeatable(
    boundary_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "boundary-violations",
            "--graph",
            str(boundary_graph),
            "--rule",
            "common/:platform/",
            "--rule",
            "platform/:projects/",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "across 2 rule(s)" in out
    assert "[common/ -> platform/]" in out
    assert "[platform/ -> projects/]" not in out


def test_boundary_violations_kind_restricts_edge_kinds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = Graph()
    graph.add_edge("inherits", "CommonWidget", "PlatformBase", file="common/widget.h", line=3)
    graph.nodes["CommonWidget"].file = "common/widget.h"
    graph.nodes["PlatformBase"].file = "platform/base.h"
    path = tmp_path / "inh.db"
    write_sqlite(graph, path)
    exit_code = main(
        [
            "boundary-violations",
            "--graph",
            str(path),
            "--rule",
            "common/:platform/",
            "--kind",
            "calls",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 of 0 violation(s)" in out
    exit_code = main(
        [
            "boundary-violations",
            "--graph",
            str(path),
            "--rule",
            "common/:platform/",
            "--kind",
            "inherits",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 1 violation(s)" in out
    assert "inherits  CommonWidget -> PlatformBase" in out


def test_boundary_violations_rejects_malformed_rule_flag(boundary_graph: Path) -> None:
    with pytest.raises(SystemExit):  # no FROM:FORBIDDEN colon
        main(["boundary-violations", "--graph", str(boundary_graph), "--rule", "common"])
    with pytest.raises(SystemExit):  # from == forbidden (a layer vs itself)
        main(["boundary-violations", "--graph", str(boundary_graph), "--rule", "common/:common/"])


def test_boundary_violations_requires_a_rule(boundary_graph: Path) -> None:
    with pytest.raises(SystemExit):
        main(["boundary-violations", "--graph", str(boundary_graph)])


# --- outline / class-members -------------------------------------------------

FOO = "cxx . . $ mongo/Foo#"
FOO_PARSE = "cxx . . $ mongo/Foo#parse(a1)."
FOO_COUNT = "cxx . . $ mongo/Foo#count."
FOO_INNER = "cxx . . $ mongo/Foo#Inner#"
MAKE_FOO = "cxx . . $ mongo/makeFoo(a2)."
FOOBAR = "cxx . . $ mongo/FooBar#"
FOOBAR_PARSE = "cxx . . $ mongo/FooBar#parse(a3)."


@pytest.fixture
def container_graph(tmp_path: Path) -> Path:
    """One file (`mongo/foo.h`) holding class `Foo` (a method, a field, a
    nested type), a free function, and the sibling class `FooBar` whose
    members must not leak into `Foo`'s."""
    graph = Graph()
    for symbol, line in (
        (MAKE_FOO, 30),
        (FOOBAR_PARSE, 41),
        (FOO, 10),
        (FOO_INNER, 13),
        (FOOBAR, 40),
        (FOO_COUNT, 12),
        (FOO_PARSE, 11),
    ):
        graph.add_node(symbol)
        graph.nodes[symbol].file = "mongo/foo.h"
        graph.nodes[symbol].line = line
    path = tmp_path / "container.db"
    write_sqlite(graph, path)
    return path


def test_outline_prints_definitions_in_line_order(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["outline", "--graph", str(container_graph), "mongo/foo.h"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "7 of 7 definition(s) in mongo/foo.h, by line" in out
    assert "mongo/Foo#" in out
    assert "(mongo/foo.h:11)" in out  # first definition, 0-indexed 10 -> 1-indexed


def test_outline_unknown_file_prints_note_not_bare_zero(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["outline", "--graph", str(container_graph), "mongo/foo"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 of 0 definition(s)" in out
    assert "exactly" in out


def test_outline_limit_truncates_and_reports_total(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["outline", "--graph", str(container_graph), "mongo/foo.h", "--limit", "2"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "2 of 7 definition(s)" in out
    assert "... and 5 more" in out


def test_class_members_prints_members(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["class-members", "--graph", str(container_graph), FOO])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "3 of 3 member(s) of cxx . . $ mongo/Foo#" in out
    assert "mongo/Foo#parse(a1)." in out  # readable label by default
    assert "mongo/FooBar#parse(a3)." not in out  # FooBar's members don't leak


def test_class_members_limit_truncates_and_reports_total(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["class-members", "--graph", str(container_graph), FOO, "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 3 member(s)" in out
    assert "... and 2 more" in out


def test_class_members_unknown_symbol_errors(container_graph: Path) -> None:
    with pytest.raises(SystemExit):
        main(["class-members", "--graph", str(container_graph), "cxx . . $ mongo/Nope#"])


def test_class_members_ambiguous_name_lists_candidates_and_errors(
    container_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Foo` matches the class and its own members: candidates are listed,
    none is guessed (the shared `_resolve_symbol` behaviour)."""
    with pytest.raises(SystemExit):
        main(["class-members", "--graph", str(container_graph), "Foo"])
    err = capsys.readouterr().err
    assert "ambiguous" in err


def test_class_members_non_type_symbol_errors(container_graph: Path) -> None:
    """A known non-type symbol is bad input: parser.error, not an empty list."""
    with pytest.raises(SystemExit):
        main(["class-members", "--graph", str(container_graph), MAKE_FOO])


def test_class_members_zero_members_prints_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A memberless (but known) type prints an explanatory note, not a bare
    zero — mirrors the MCP `class_members` tool's note and `outline`'s CLI
    zero-result note."""
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Empty#")
    graph.nodes["cxx . . $ mongo/Empty#"].file = "mongo/empty.h"
    graph.nodes["cxx . . $ mongo/Empty#"].line = 5
    path = tmp_path / "empty.db"
    write_sqlite(graph, path)
    exit_code = main(["class-members", "--graph", str(path), "cxx . . $ mongo/Empty#"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 of 0 member(s)" in out
    assert "no members recorded on this type" in out


# --- strongly-connected-components -------------------------------------------


@pytest.fixture
def scc_graph(tmp_path: Path) -> Path:
    """A 3-cycle (x->y->z->x) and a 2-cycle (a<->b); `spur` calls into a cycle
    without joining it, `selfrec` is direct self-recursion (out of scope)."""
    graph = Graph()
    graph.add_edge("calls", "a", "b", file="src/a.cpp", line=1)
    graph.add_edge("calls", "b", "a", file="src/a.cpp", line=2)
    graph.add_edge("calls", "x", "y", file="src/x.cpp", line=1)
    graph.add_edge("calls", "y", "z", file="src/x.cpp", line=2)
    graph.add_edge("calls", "z", "x", file="src/x.cpp", line=3)
    graph.add_edge("calls", "spur", "x", file="src/a.cpp", line=3)
    graph.add_edge("calls", "selfrec", "selfrec", file="src/s.cpp", line=1)
    for symbol, file, line in (
        ("a", "src/a.cpp", 10),
        ("b", "src/a.cpp", 11),
        ("x", "src/x.cpp", 20),
        ("y", "src/x.cpp", 21),
        ("z", "src/x.cpp", 22),
        ("spur", "src/a.cpp", 5),
        ("selfrec", "src/s.cpp", 30),
    ):
        graph.nodes[symbol].file = file
        graph.nodes[symbol].line = line
    path = tmp_path / "scc.db"
    write_sqlite(graph, path)
    return path


def test_strongly_connected_components_prints_cycles(
    scc_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["strongly-connected-components", "--graph", str(scc_graph)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "2 of 2 cyclic group(s) of 2+ symbols" in out
    assert "component of 3 symbol(s)" in out
    assert "component of 2 symbol(s)" in out
    assert "x  (src/x.cpp:21)" in out
    # neither the acyclic caller nor the self-loop surfaces as a component
    assert "spur" not in out
    assert "selfrec" not in out
    # the facts-not-judgments caveat prints with the answer
    assert "mutual recursion" in out


def test_strongly_connected_components_limit_truncates_and_reports_total(
    scc_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["strongly-connected-components", "--graph", str(scc_graph), "--limit", "1"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 2 cyclic group(s)" in out
    assert "... and 1 more" in out


def test_strongly_connected_components_exclude_tests_drops_test_only_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Filters drop a component only when every member is test-defined: the
    production cycle survives `--exclude-tests`, the test-only one doesn't."""
    graph = Graph()
    graph.add_edge("calls", "prod1", "prod2", file="src/p.cpp", line=1)
    graph.add_edge("calls", "prod2", "prod1", file="src/p.cpp", line=2)
    graph.add_edge("calls", "t1", "t2", file="src/t.cpp", line=1)
    graph.add_edge("calls", "t2", "t1", file="src/t.cpp", line=2)
    for symbol, file, line in (
        ("prod1", "src/p.cpp", 10),
        ("prod2", "src/p.cpp", 11),
        ("t1", "src/handler_test.cpp", 1),
        ("t2", "src/handler_test.cpp", 2),
    ):
        graph.nodes[symbol].file = file
        graph.nodes[symbol].line = line
    path = tmp_path / "mix.db"
    write_sqlite(graph, path)
    exit_code = main(["strongly-connected-components", "--graph", str(path), "--exclude-tests"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 of 1 cyclic group(s)" in out
    assert "t1" not in out


def test_strongly_connected_components_rejects_negative_limit(scc_graph: Path) -> None:
    with pytest.raises(SystemExit):
        main(["strongly-connected-components", "--graph", str(scc_graph), "--limit", "-1"])


@pytest.fixture
def refs_graph(tmp_path: Path) -> Path:
    graph = Graph()
    graph.add_reference("cxx . . $ mongo/ResumeTokenData#", "a.cpp", 10)
    graph.add_reference("cxx . . $ mongo/ResumeTokenData#", "b.cpp", 41)
    path = tmp_path / "r.db"
    write_sqlite(graph, path)
    return path


def test_references_lists_use_sites(refs_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["references", "--graph", str(refs_graph), "cxx . . $ mongo/ResumeTokenData#"]) == 0
    out = capsys.readouterr().out
    assert "2 use site(s)" in out
    assert "a.cpp:11" in out  # 0-indexed 10 -> 1-indexed 11
    assert "b.cpp:42" in out


def test_references_without_index_returns_nonzero(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # graph_path has no reference index; the symbol exists as a node
    code = main(
        ["references", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "--no-references" in out


def test_references_with_root_shows_snippet(
    refs_graph: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "co"
    root.mkdir()
    (root / "a.cpp").write_text("\n".join(f"line {i}" for i in range(50)))
    (root / "b.cpp").write_text("\n".join(f"line {i}" for i in range(50)))
    assert (
        main(
            [
                "references",
                "--graph",
                str(refs_graph),
                "--root",
                str(root),
                "cxx . . $ mongo/ResumeTokenData#",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "line 10" in out  # the source at a.cpp:10 (0-indexed)


@pytest.fixture
def hierarchy_graph(tmp_path: Path) -> Path:
    graph = Graph()
    graph.add_edge(
        "inherits", "cxx . . $ mongo/Derived#", "cxx . . $ mongo/Base#", file="d.h", line=2
    )
    graph.add_edge(
        "inherits", "cxx . . $ mongo/Leaf#", "cxx . . $ mongo/Derived#", file="l.h", line=3
    )
    path = tmp_path / "h.db"
    write_sqlite(graph, path)
    return path


def test_bases_lists_direct_supertypes(
    hierarchy_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["bases", "--graph", str(hierarchy_graph), "cxx . . $ mongo/Derived#"]) == 0
    out = capsys.readouterr().out
    assert "1 base class(es)" in out
    assert "mongo/Base#" in out


def test_subtypes_lists_direct_subclasses(
    hierarchy_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["subtypes", "--graph", str(hierarchy_graph), "cxx . . $ mongo/Base#"]) == 0
    out = capsys.readouterr().out
    assert "1 subclass(es)" in out
    assert "mongo/Derived#" in out


def test_impact_kind_inherits_walks_hierarchy(
    hierarchy_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "impact",
                "--graph",
                str(hierarchy_graph),
                "--kind",
                "inherits",
                "cxx . . $ mongo/Base#",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "2 symbol(s) transitively inherit from" in out
    assert "mongo/Derived#" in out
    assert "mongo/Leaf#" in out


def test_export_writes_graphify_json(
    graph_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    out = tmp_path / "g.json"
    rc = main(
        [
            "export",
            "--graph",
            str(graph_path),
            "cxx . . $ mongo/Foo#makeResumeToken(a1).",
            "--depth",
            "1",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    data = _json.loads(out.read_text())
    ids = {n["id"] for n in data["nodes"]}
    assert "cxx . . $ mongo/Foo#makeResumeToken(a1)." in ids
    assert "cxx . . $ mongo/Foo#caller(a2)." in ids  # depth-1 in-neighbour
    assert any(lk["relation"] == "calls" for lk in data["links"])
    assert "exported" in capsys.readouterr().out


def test_export_unknown_symbol_errors(graph_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["export", "--graph", str(graph_path), "nope", "--out", "/tmp/x.json"])


def test_export_usage_mode_emits_file_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    from cppgraph.model import Graph

    sym = "cxx . . $ mongo/ResumeTokenData#"
    graph = Graph()
    graph.add_node(sym, display_name="ResumeTokenData")
    graph.add_reference(sym, "a/foo.cpp", 1)
    graph.add_reference(sym, "a/foo.cpp", 8)
    graph.add_reference(sym, "b/bar.h", 3)
    db = tmp_path / "refs.db"
    write_sqlite(graph, db)

    out = tmp_path / "usage.json"
    rc = main(["export", "--graph", str(db), sym, "--mode", "usage", "--out", str(out)])
    assert rc == 0
    data = _json.loads(out.read_text())
    files = {lk["target"] for lk in data["links"]}
    assert files == {"file:a/foo.cpp", "file:b/bar.h"}
    assert "usage graph" in capsys.readouterr().out


def test_view_no_open_writes_standalone_html(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "view",
            "--graph",
            str(graph_path),
            "cxx . . $ mongo/Foo#makeResumeToken(a1).",
            "--depth",
            "1",
            "--no-open",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "open it with" in out and ".html" in out
    # the printed path should be a real self-contained file
    html_path = Path(out.split("open it with: open ")[1].strip())
    assert html_path.exists()
    assert "window.GRAPH" in html_path.read_text(encoding="utf-8")


# --- symbol resolution: accept a plain name, not just the exact SCIP string ---


def test_callers_resolves_plain_name(graph_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # "makeResumeToken" is not an exact SCIP symbol, but resolves to the one match.
    exit_code = main(["callers", "--graph", str(graph_path), "makeResumeToken"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "mongo/Foo#caller(a2)." in out


def test_callers_ambiguous_name_errors(tmp_path: Path) -> None:
    graph = Graph()
    graph.add_node("cxx . . $ mongo/A#run(a1).", display_name="run")
    graph.add_node("cxx . . $ mongo/B#run(a2).", display_name="run")
    path = tmp_path / "g.db"
    write_sqlite(graph, path)
    with pytest.raises(SystemExit):
        main(["callers", "--graph", str(path), "run"])


def test_exact_scip_symbol_still_accepted(
    graph_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        ["callers", "--graph", str(graph_path), "cxx . . $ mongo/Foo#makeResumeToken(a1)."]
    )
    assert exit_code == 0


# --- graph auto-discovery: --graph optional when run from inside a project ---


def test_graph_auto_discovered_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = tmp_path / "proj"
    (proj / ".cppgraph").mkdir(parents=True)
    graph = Graph()
    graph.add_node("cxx . . $ mongo/Foo#makeResumeToken(a1).", display_name="makeResumeToken")
    graph.add_edge(
        "calls",
        "cxx . . $ mongo/Foo#caller(a2).",
        "cxx . . $ mongo/Foo#makeResumeToken(a1).",
        file="foo.cpp",
        line=9,
    )
    write_sqlite(graph, proj / ".cppgraph" / "proj.graph.db")
    monkeypatch.chdir(proj)
    # No --graph: discovered from the cwd's .cppgraph/. Combined with name resolution.
    exit_code = main(["callers", "makeResumeToken"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "mongo/Foo#caller(a2)." in out


def test_no_graph_and_none_discovered_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .cppgraph/ anywhere above
    with pytest.raises(SystemExit):
        main(["callers", "makeResumeToken"])
