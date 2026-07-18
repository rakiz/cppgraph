from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cppgraph.cli import main
from cppgraph.model import Graph
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
    assert "deadbeefcafe" in capsys.readouterr().out


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
