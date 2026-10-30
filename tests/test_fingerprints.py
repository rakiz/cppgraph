"""Tests for the `cppgraph fingerprints` contract surface (masora_integration.md §2).

The command is the replacement for masora's direct SQLite reads: one JSON
document on stdout, fail-closed (non-zero exit, nothing on stdout on any
failure). The fingerprint / edge-snapshot algorithm must be byte-for-byte
what masora's `masora/providers.py` computes today — the parity tests below
transcribe that algorithm inline (read-only, cross-repo import forbidden) and
compare.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cppgraph.cli import main
from cppgraph.model import Graph, Node
from cppgraph.store import SCHEMA_VERSION, write_sqlite

F = "cxx . . $ pkg/F#f()."
G = "cxx . . $ pkg/F#g()."
H = "cxx . . $ pkg/F#h()."
MISSING = "cxx . . $ pkg/F#missing()."

# F spans lines 0..3 with a comment line inside (exercises normalize_source);
# G and H are single-line definitions.
SOURCE = (
    "int f(int a) {\n"
    "    // noise comment\n"
    "    return a + 1;\n"
    "}\n"
    "\n"
    "int g() { return 2; }\n"
    "\n"
    "int h() { return 3; }\n"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A tiny git checkout with two commits; returns (repo, commit_a, head)."""
    r = tmp_path / "repo"
    (r / ".cppgraph").mkdir(parents=True)
    (r / "src.cpp").write_text(SOURCE)
    _git(r, "init")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "src.cpp")
    _git(r, "commit", "-m", "first")
    commit_a = _git(r, "rev-parse", "HEAD")
    (r / "later.cpp").write_text("// later\n")
    _git(r, "add", "later.cpp")
    _git(r, "commit", "-m", "second")
    return r, commit_a, _git(r, "rev-parse", "HEAD")


def _graph() -> Graph:
    graph = Graph()
    graph.nodes[F] = Node(symbol=F, display_name="f", file="src.cpp", line=0, end_line=3)
    graph.nodes[G] = Node(symbol=G, display_name="g", file="src.cpp", line=5, end_line=5)
    graph.nodes[H] = Node(symbol=H, display_name="h", file="src.cpp", line=7, end_line=7)
    graph.add_edge("calls", F, G, file="src.cpp", line=2)
    graph.add_edge("calls", F, H, file="src.cpp", line=3)
    graph.add_edge("calls", G, H, file="src.cpp", line=5)
    return graph


def _write_graph(repo: Path, source_commit: str) -> Path:
    db = repo / ".cppgraph" / "test.graph.db"
    write_sqlite(_graph(), db, meta={"source_commit": source_commit})
    return db


# --- masora providers.py semantics, transcribed inline (read-only parity) -----


def _normalize(text: str) -> str:
    """Verbatim transcription of masora/masora/index.py:normalize_source."""
    import re as _re

    without_blocks = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
    lines = []
    for raw in without_blocks.splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith(("//", "#")):
            continue
        lines.append(line)
    return "\n".join(lines)


def _expected_fingerprint(repo: Path, rel: str, line: int, end_line: int) -> str:
    """Verbatim transcription of masora/masora/providers.py:definition_fingerprint."""
    lines = (repo / rel).read_text(encoding="utf-8").splitlines()
    source = "\n".join(lines[line : end_line + 1])
    return hashlib.sha256(_normalize(source).encode("utf-8")).hexdigest()


def _edge_set_hash(callee_symbols: list[str]) -> str:
    """Verbatim transcription of masora/masora/providers.py:_edge_set_hash."""
    return hashlib.sha256("\n".join(sorted(callee_symbols)).encode("utf-8")).hexdigest()


def _run(repo: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    code = main(["fingerprints", "--repo", str(repo), *args])
    assert code == 0, f"expected exit 0, got {code}"
    return json.loads(capsys.readouterr().out)


def test_document_shape_and_values(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, head = repo
    _write_graph(r, head)
    doc = _run(r, "--symbol", F, "--symbol", G, capsys=capsys)
    assert set(doc) == {
        "contract_version",
        "repo_root",
        "graph_source_commit",
        "repo_head",
        "graph_behind_head",
        "symbol_format",
        "symbols",
    }
    assert doc["contract_version"] == 1
    assert doc["repo_root"] == str(r)
    assert doc["graph_source_commit"] == head
    assert doc["repo_head"] == head
    # Pinned letter: ancestor-or-equal. A current graph (commit == HEAD) IS
    # ancestor-or-equal; masora combines this with graph_source_commit (its
    # §5.2 equality gate) to decide refusal.
    assert doc["graph_behind_head"] is True
    assert doc["symbol_format"] == 1
    assert [s["symbol"] for s in doc["symbols"]] == [F, G]
    f_entry, g_entry = doc["symbols"]
    assert set(f_entry) == {"symbol", "found", "definition_fingerprint", "edge_snapshot"}
    assert f_entry["found"] is True
    assert f_entry["definition_fingerprint"] == _expected_fingerprint(r, "src.cpp", 0, 3)
    # F calls G and H: its edge-set hash covers both, sorted; its 1-hop
    # neighbours are G and H (caller∪callee, self excluded), each mapped to
    # that neighbour's OWN edge-set hash.
    assert f_entry["edge_snapshot"] == {
        "edges": _edge_set_hash([G, H]),
        "neighbours": {
            G: _edge_set_hash([H]),
            H: _edge_set_hash([]),
        },
    }
    assert g_entry["edge_snapshot"]["edges"] == _edge_set_hash([H])
    assert g_entry["edge_snapshot"]["neighbours"] == {
        # G's callees: H; G's callers: F — the 1-hop union, self excluded.
        H: _edge_set_hash([]),
        F: _edge_set_hash([G, H]),
    }


def test_fingerprint_normalizes_comments_and_whitespace(
    repo: tuple[Path, str, str], capsys
) -> None:
    """Hand-computed value for the fixture: the comment line and the run of
    spaces collapse, so the hash is over 'int f(int a) {\\nreturn a + 1;\\n}'."""
    r, _a, head = repo
    _write_graph(r, head)
    doc = _run(r, "--symbol", F, capsys=capsys)
    expected = hashlib.sha256(b"int f(int a) {\nreturn a + 1;\n}").hexdigest()
    assert doc["symbols"][0]["definition_fingerprint"] == expected


def test_found_false_is_not_guessed(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, head = repo
    _write_graph(r, head)
    doc = _run(r, "--symbol", MISSING, capsys=capsys)
    assert doc["symbols"] == [{"symbol": MISSING, "found": False}]


def test_repeatable_symbol_batch_keeps_order(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, head = repo
    _write_graph(r, head)
    doc = _run(r, "--symbol", H, "--symbol", F, "--symbol", G, capsys=capsys)
    assert [s["symbol"] for s in doc["symbols"]] == [H, F, G]


def test_stock_store_without_extents_hashes_definition_line_alone(
    repo: tuple[Path, str, str], capsys
) -> None:
    """A store without body extents (end_line NULL) hashes the single
    definition line — providers.py's stock-graph case."""
    r, _a, head = repo
    graph = _graph()
    for node in graph.nodes.values():
        node.end_line = None
    write_sqlite(graph, r / ".cppgraph" / "test.graph.db", meta={"source_commit": head})
    doc = _run(r, "--symbol", F, capsys=capsys)
    assert doc["symbols"][0]["definition_fingerprint"] == _expected_fingerprint(r, "src.cpp", 0, 0)


def test_graph_behind_head_true_for_ancestor_commit(repo: tuple[Path, str, str], capsys) -> None:
    """A store indexed at an ancestor of HEAD reports graph_behind_head: true
    (HEAD has moved past the indexed commit)."""
    r, commit_a, _head = repo
    _write_graph(r, commit_a)
    doc = _run(r, "--symbol", F, capsys=capsys)
    assert doc["graph_behind_head"] is True
    assert doc["graph_source_commit"] == commit_a


def test_graph_behind_head_false_for_ahead_commit(repo: tuple[Path, str, str], capsys) -> None:
    """The indexed commit NOT an ancestor-or-equal of HEAD (the graph is ahead
    of the checkout — indexed on a state HEAD no longer contains) reports
    graph_behind_head: false."""
    r, _a, head = repo
    _write_graph(r, head)
    # Move HEAD back one commit: the indexed commit is now HEAD's child.
    _git(r, "reset", "--hard", "HEAD~1")
    doc = _run(r, "--symbol", F, capsys=capsys)
    assert doc["graph_behind_head"] is False
    assert doc["graph_source_commit"] == head


def test_fail_closed_on_missing_repo(tmp_path: Path, capsys) -> None:
    code = main(["fingerprints", "--repo", str(tmp_path / "nope"), "--symbol", F])
    assert code != 0
    assert capsys.readouterr().out == ""


def test_fail_closed_on_no_graph(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, _head = repo
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code != 0
    assert capsys.readouterr().out == ""


def test_fail_closed_on_graph_behind_without_ancestor(repo: tuple[Path, str, str], capsys) -> None:
    """A source_commit unknown to the checkout's git cannot be proven ancestor:
    fail closed rather than guess."""
    r, _a, _head = repo
    db = r / ".cppgraph" / "test.graph.db"
    write_sqlite(_graph(), db, meta={"source_commit": "0" * 40})
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code != 0
    assert capsys.readouterr().out == ""


def test_strict_argument_rejection(repo: tuple[Path, str, str]) -> None:
    r, _a, _head = repo
    with pytest.raises(SystemExit) as exc:
        main(["fingerprints", "--repo", str(r), "--symbol", F, "--syms", "x"])
    assert exc.value.code == 2


def test_missing_required_repo_argument() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["fingerprints", "--symbol", F])
    assert exc.value.code == 2


def test_help_documents_the_two_second_caller_budget(
    repo: tuple[Path, str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    r, _a, _head = repo
    with pytest.raises(SystemExit):
        main(["fingerprints", "--help"])
    assert "2 s" in capsys.readouterr().out


def test_repo_without_git_checkout_fails_closed(tmp_path: Path, capsys) -> None:
    r = tmp_path / "plain"
    (r / ".cppgraph").mkdir(parents=True)
    write_sqlite(_graph(), r / ".cppgraph" / "test.graph.db", meta={"source_commit": "x"})
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code != 0
    assert capsys.readouterr().out == ""


def test_fail_closed_on_unparsable_symbol_format(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, head = repo
    db = r / ".cppgraph" / "test.graph.db"
    write_sqlite(_graph(), db, meta={"source_commit": head})
    # write_sqlite always stamps its own (valid) version rows; corrupt the
    # symbol_format row afterwards to simulate a drifted store.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE meta SET value = 'banana' WHERE key = 'symbol_format'")
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_fail_closed_on_missing_source_commit(repo: tuple[Path, str, str], capsys) -> None:
    r, _a, _head = repo
    write_sqlite(_graph(), r / ".cppgraph" / "test.graph.db", meta={})
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_fail_closed_on_newer_store_schema(repo: tuple[Path, str, str], capsys) -> None:
    """A store written by a NEWER cppgraph (schema_version beyond this
    module's SCHEMA_VERSION) is refused — an old reader must not misread a
    format it doesn't know (the GraphStore._check_schema_compat policy)."""
    from cppgraph.store import SCHEMA_VERSION

    r, _a, head = repo
    db = r / ".cppgraph" / "test.graph.db"
    write_sqlite(_graph(), db, meta={"source_commit": head})
    # write_sqlite always stamps its own (current) schema_version; bump the
    # row afterwards to simulate a store from a newer cppgraph.
    with sqlite3.connect(db) as con:
        con.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_fail_closed_when_store_is_a_subdirectory_of_a_parent_checkout(
    tmp_path: Path, capsys
) -> None:
    """`git -C repo rev-parse HEAD` succeeds from a SUBDIRECTORY of a parent
    checkout — reporting the parent's HEAD for the wrong repo. A --repo that
    is not itself the checkout root must fail closed."""
    import subprocess as sp

    parent = tmp_path / "parent"
    parent.mkdir()
    sp.run(["git", "init", str(parent)], check=True, capture_output=True)
    sp.run(["git", "-C", str(parent), "config", "user.email", "t@example.com"], check=True)
    sp.run(["git", "-C", str(parent), "config", "user.name", "t"], check=True)
    sp.run(["git", "-C", str(parent), "commit", "--allow-empty", "-m", "root"], check=True)
    nested = parent / "nested" / "pkg"
    (nested / ".cppgraph").mkdir(parents=True)
    write_sqlite(
        _graph(),
        nested / ".cppgraph" / "test.graph.db",
        meta={"source_commit": "0" * 40},
    )
    code = main(["fingerprints", "--repo", str(nested), "--symbol", F])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_store_without_files_table_fails_closed(repo: tuple[Path, str, str], capsys) -> None:
    """A drifted store (no `files` table) must exit 1 with a message — the
    sqlite3.Error is wrapped into the fail-closed contract error, never a
    traceback past the command boundary."""
    import sqlite3 as s3

    r, _a, head = repo
    db = r / ".cppgraph" / "test.graph.db"
    con = s3.connect(db)
    con.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE symbols (id INTEGER PRIMARY KEY, symbol TEXT, file_id INTEGER, line INTEGER);"
        "CREATE TABLE edges (kind TEXT, src_id INTEGER, dst_id INTEGER);"
        f"INSERT INTO meta VALUES ('schema_version', '{SCHEMA_VERSION}');"
        f"INSERT INTO meta VALUES ('source_commit', '{head}');"
        f"INSERT INTO symbols VALUES (1, '{F}', NULL, 0);"
    )
    con.commit()
    con.close()
    code = main(["fingerprints", "--repo", str(r), "--symbol", F])
    assert code == 1
    assert capsys.readouterr().out == ""


def test_definition_outside_checkout_reports_found_false(
    repo: tuple[Path, str, str], capsys
) -> None:
    """A definition file row escaping the checkout (`path.is_relative_to`
    guard) is found: false — no crash, and no path outside the repo leaks
    into the document."""
    r, _a, head = repo
    outside = r.parent / "outside"
    outside.mkdir()
    (outside / "secret.cpp").write_text("int leaked() { return 1; }\n")
    graph = _graph()
    LEAK = "cxx . . $ pkg/L#leak()."
    graph.nodes[LEAK] = Node(
        symbol=LEAK, display_name="leak", file="../outside/secret.cpp", line=0, end_line=0
    )
    write_sqlite(graph, r / ".cppgraph" / "test.graph.db", meta={"source_commit": head})
    doc = _run(r, "--symbol", LEAK, capsys=capsys)
    assert doc["symbols"] == [{"symbol": LEAK, "found": False}]
    # No path outside the checkout leaks into the document.
    assert str(outside) not in json.dumps(doc)
    assert "../outside" not in json.dumps(doc)


# --- MCP parity ---------------------------------------------------------------


def test_mcp_tool_fingerprints_same_document(repo: tuple[Path, str, str], capsys) -> None:
    """The MCP `fingerprints` tool is driven by the same pure function: for the
    same fixture it produces the identical document (no forked logic). Driven
    through the REAL call dispatch (`server.call_tool` — the same public path
    test_mcp_server.py's `_dispatch_payload` takes), so the strict-args gate
    is on the exercised path too."""
    from cppgraph import mcp_server

    r, _a, head = repo
    db = _write_graph(r, head)
    cli_doc = _run(r, "--symbol", F, "--symbol", MISSING, capsys=capsys)
    server = mcp_server.build_server(str(db), root=str(r))

    def _payload(arguments: dict) -> dict:
        result = server.call_tool("fingerprints", arguments)
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    assert _payload({"symbols": [F, MISSING]}) == cli_doc
    # The not-found branch via the same dispatch: found: false, nothing guessed.
    assert _payload({"symbols": [MISSING]})["symbols"] == [{"symbol": MISSING, "found": False}]


def test_unknown_scip_string_shape_is_just_not_found(repo: tuple[Path, str, str], capsys) -> None:
    """v1 is verbatim-only: a plain name never resolves."""
    r, _a, head = repo
    _write_graph(r, head)
    doc = _run(r, "--symbol", "makeResumeToken", capsys=capsys)
    assert doc["symbols"] == [{"symbol": "makeResumeToken", "found": False}]
