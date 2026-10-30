"""Tests for the redwood `log_id_lookup` surface (redwood_integration.md §1,
contract v1) — the id→symbol table extracted at index time plus its query
surface (CLI `log-id-lookup`, MCP `log_id_lookup`), all in
`cppgraph.redwood` (the connector-file convention, NEXT-SESSION.md).

The extraction pattern is deliberately narrow (see redwood.py's docstring):
the id argument of a `LOG(`/`MONGO_LOGV2(`/`LOGV2(`-shaped call (`LOGV2` is
mongo's canonical caller-facing macro — log.h defines it as the form the
codebase uses everywhere; its id is the 1st argument, while legacy `LOG`
keeps the id 2nd), when it is a plain
integer literal, mapped to the enclosing definition symbol by the SAME
containment machinery the builder uses for calls (`builder._attribute_containment`
over the document's `enclosing_range` intervals).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cppgraph.builder import build_graph
from cppgraph.cli import main
from cppgraph.model import Graph, Node
from cppgraph.redwood import (
    CALLERS_LIMIT,
    LogIdLookupError,
    extract_log_occurrences,
    log_id_lookup_document,
)
from cppgraph.store import SCHEMA_VERSION, write_sqlite

F = "cxx . . $ pkg/F#f()."
G = "cxx . . $ pkg/F#g()."
H = "cxx . . $ pkg/F#h()."

SOURCE = (
    "int f(int a) {\n"  # line 0
    '    LOG(2, 12983100, "boom");\n'  # line 1
    '    MONGO_LOGV2(12983101, "boom");\n'  # line 2 (mongo id-first shape)
    "    g();\n"  # line 3
    "}\n"  # line 4
    "\n"  # line 5
    'int g() { LOGV2(23408, "Starting Watchdog Monitor"); h(); }\n'  # line 6 (mongo shape)
    "\n"  # line 7
    "int h() {\n"  # line 8
    "    // LOG(1, 999, commented out);\n"  # line 9 (commented -> no row)
    "    LOG(a, b);\n"  # line 10 (non-literal LOG id (2nd arg) -> no row)
    "    LOGV2(watchdog, 12983198);\n"  # line 11 (non-literal LOGV2 id (1st arg) -> no row)
    "    return 12983199;  // a bare integer, not a LOG call -> no row\n"  # line 12
    "}\n"  # line 13
)

# Only the real LOG-shaped calls with a plain-integer 2nd argument.
EXPECTED_IDS = {
    12983100: (F, "src.cpp", 1),
    12983101: (F, "src.cpp", 2),
    23408: (G, "src.cpp", 6),
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A tiny git checkout (one commit) with the synthetic source; returns
    (repo, indexed_commit, head)."""
    r = tmp_path / "repo"
    (r / ".cppgraph").mkdir(parents=True)
    (r / "src.cpp").write_text(SOURCE)
    _git(r, "init")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "src.cpp")
    _git(r, "commit", "-m", "first")
    return r, _git(r, "rev-parse", "HEAD"), _git(r, "rev-parse", "HEAD")


def _index() -> Any:
    """A synthetic SCIP index for SOURCE: F, G, H defined, F calls G."""
    from cppgraph.proto import scip_pb2

    def _occ(sym: str, line: int, end: int | None = None, *, definition: bool = False) -> Any:
        occ = scip_pb2.Occurrence(
            symbol=sym, range=[line, 0, line, 10], symbol_roles=1 if definition else 0
        )
        if definition:
            # #504-style packed enclosing_range [startLine, startCol, endLine, endCol].
            occ.enclosing_range.extend([line, 0, line if end is None else end, 10])
        return occ

    index = scip_pb2.Index()
    index.metadata.project_root = "file:///nope"  # overridden per-test
    doc = index.documents.add()
    doc.relative_path = "src.cpp"
    for sym, line, end in ((F, 0, 4), (G, 6, 6), (H, 8, 13)):
        si = doc.symbols.add()
        si.symbol = sym
        si.display_name = sym.split("#")[-1].rstrip(".")
        doc.occurrences.append(_occ(sym, line, end, definition=True))
    doc.occurrences.append(_occ(G, 3))  # F calls G at line 3
    return index


def _build(repo: Path, **kwargs: Any) -> Graph:
    index = _index()
    index.metadata.project_root = f"file://{repo}"
    return build_graph(index, **kwargs)


# --- extraction pattern (pure scanner) ----------------------------------------


def test_extract_log_mongo_logv2_and_logv2_integer_second_args() -> None:
    occs = dict((line, lid) for line, lid in extract_log_occurrences(SOURCE))
    assert occs == {1: 12983100, 2: 12983101, 6: 23408}


def test_extract_skips_non_log_integers_comments_and_non_literals() -> None:
    lines = {line for line, _ in extract_log_occurrences(SOURCE)}
    # The commented LOG (line 9), the macro-level LOG (line 10), the
    # non-literal LOGV2 id (line 11) and the bare return literal (line 12)
    # produce NO row.
    assert 9 not in lines and 10 not in lines and 11 not in lines and 12 not in lines


def test_extract_ignores_prefixed_names_and_non_macro_uses() -> None:
    text = "XLOG(1, 7); myLOG(3, 9); _LOG(4, 10); LOGV2_OPTIONS(5, 11);\n"
    assert extract_log_occurrences(text) == []


def test_extract_logv2_mongo_real_line_shape() -> None:
    """The real mongo caller-facing shape (watchdog.cpp:313): `LOGV2(ID, msg)`
    with a plain-integer id and a string-literal message round-trips."""
    text = (
        "int f() {\n"
        '    LOGV2(23408, "Starting Watchdog Monitor");\n'
        '    MONGO_LOGV2(23409, "Other");\n'
        "}\n"
    )
    assert extract_log_occurrences(text) == [(1, 23408), (2, 23409)]


def test_extract_logv2_non_integer_id_skipped() -> None:
    """The id is the 1st argument for LOGV2-family macros (mongo canonical):
    a non-integer id is skipped even when a later argument is a plain int."""
    text = "LOGV2(watchdog, 1); LOGV2(opts, foo); LOGV2(7, 2);\n"
    assert extract_log_occurrences(text) == [(0, 7)]


def test_extract_handles_nested_parens_and_multiline_args() -> None:
    text = "int f() {\n    LOG(\n        1,\n        42,\n        foo(bar(1))\n    );\n}\n"
    assert extract_log_occurrences(text) == [(1, 42)]


def test_extract_masks_string_literals() -> None:
    text = 'int f() { puts("LOG(1, 55)"); LOG(2, 56); }\n'
    assert extract_log_occurrences(text) == [(0, 56)]


def test_extract_rejects_hex_negative_and_float_second_args() -> None:
    text = "LOG(1, 0x10); LOG(2, -1); LOG(3, 1.5); LOG(4, 7);\n"
    assert extract_log_occurrences(text) == [(0, 7)]


# --- index-time extraction into the graph -------------------------------------


def test_build_extracts_attributed_log_ids(repo) -> None:
    graph = _build(repo[0], extract_log_ids=True)
    rows = {(e.log_id, e.symbol, e.file, e.line) for e in graph.log_ids}
    assert rows == {(lid, sym, f, line) for lid, (sym, f, line) in EXPECTED_IDS.items()}


def test_build_without_flag_extracts_nothing(repo) -> None:
    graph = _build(repo[0])
    assert graph.log_ids == []


def test_build_skips_unattributed_occurrences(repo) -> None:
    """Degradation choice (documented): a LOG occurrence outside every
    enclosing_range interval is SKIPPED, not attributed to a guessed
    file-level entry."""
    r = repo[0]
    # Appended AFTER the definitions: a line no enclosing_range interval
    # covers (file scope).
    (r / "src.cpp").write_text(SOURCE + "\nLOG(1, 777, top_level);\n")
    index = _index()
    index.metadata.project_root = f"file://{r}"
    graph = build_graph(index, extract_log_ids=True)
    assert all(e.log_id != 777 for e in graph.log_ids)


# --- store: schema v6 table, byte-stable, optional at read time ---------------


def test_store_writes_log_ids_table(repo: Path) -> None:
    r, commit, _head = repo
    graph = _build(r, extract_log_ids=True)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    con = sqlite3.connect(db)
    rows = con.execute("SELECT log_id, line FROM log_ids ORDER BY log_id").fetchall()
    assert [lid for lid, _ in rows] == [23408, 12983100, 12983101]
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "6"
    con.close()


def test_store_log_ids_byte_stable_across_rebuilds(repo: Path) -> None:
    r, commit, _head = repo
    dumps = []
    for _ in range(2):
        graph = _build(r, extract_log_ids=True)
        db = r / ".cppgraph" / "t.graph.db"
        write_sqlite(graph, db, meta={"source_commit": commit})
        con = sqlite3.connect(db)
        dumps.append(con.execute("SELECT * FROM log_ids ORDER BY log_id, file_id, line").fetchall())
        con.close()
    assert dumps[0] == dumps[1]


def test_prebump_store_degrades_to_reason_not_crash(repo: Path) -> None:
    """A pre-v6 store simply has no log_ids table: lookups answer found:false
    with a stated reason, never a crash (mirrors attributed-refs degradation)."""
    r, commit, _head = repo
    graph = _build(r)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    con = sqlite3.connect(db)
    con.execute("DROP TABLE log_ids")
    con.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    con.commit()
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is False
    assert doc["ids"][0]["reason"]


def _make_v5_store(repo, *, with_refs: bool) -> Path:
    """A store as a PRE-v6 binary would have written it: schema_version 5, no
    log_ids table, no marker — the upgrade-path fixture for the enriched/
    updated degradation tests."""
    r, commit, _head = repo
    graph = Graph()
    graph.nodes[F] = Node(symbol=F, display_name="f", file="src.cpp", line=0, end_line=4)
    graph.add_edge("calls", G, F, file="src.cpp", line=3)
    if with_refs:
        graph.add_reference(G, "src.cpp", 3)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    con = sqlite3.connect(db)
    con.execute("DROP TABLE log_ids")
    con.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    con.commit()
    con.close()
    return db


def test_enrich_on_v5_store_never_sets_the_marker(repo) -> None:
    """`enrich-refs` creates the (empty) log_ids table for shape consistency
    but must NOT set `log_ids_extracted` — it never backfills, so lookups
    keep degrading to a reasoned found:false instead of a confident false
    negative for every id from untouched files."""
    from cppgraph.store import enrich_references

    r, _c, _h = repo
    db = _make_v5_store(repo, with_refs=True)
    enrich_references(db, _index())
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone() is None
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is False
    assert doc["ids"][0]["reason"]


def test_update_on_v5_store_without_marker_still_degrades(repo) -> None:
    """A v5-upgraded store (no marker) + a VALID-root incremental update that
    re-extracts its changed files completely: the marker must still be absent
    — the partial's true claim cannot certify rows for the UNCHANGED files,
    which were never extracted (the two-true-conjuncts invariant). The store
    degrades honestly: found rows are real evidence and answer normally, but
    a MISS stays unverified (uncertified reason, log_id_table False) instead
    of a confident false negative."""
    from cppgraph.pipeline import update_store

    r, _c, _h = repo
    db = _make_v5_store(repo, with_refs=False)
    update_store(db, _partial_index(r), deleted_files=[])
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone() is None
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["log_id_table"] is False
    # The partial re-extracted src.cpp's rows: found = evidence.
    assert doc["ids"][0]["found"] is True
    miss = log_id_lookup_document(db, r, [9999999], None)["ids"][0]
    assert miss["found"] is False
    assert "not certified complete" in miss["reason"]


def test_full_build_marker_preserved_through_update(repo) -> None:
    """A full extraction build sets the marker; an incremental update over
    that store PRESERVES it (it re-extracts the changed files): lookups stay
    live, changed-file rows are refreshed, untouched rows intact."""
    from cppgraph.pipeline import update_store

    r, commit, _h = repo
    graph = _build(r, extract_log_ids=True)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone()[0] == "true"
    )
    con.close()
    # Change the source: one id replaced (12983100 -> 4242), 12983101 intact.
    (r / "src.cpp").write_text(SOURCE.replace("12983100", "4242"))
    update_store(db, _partial_index(r), deleted_files=[])
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone()[0] == "true"
    )
    rows = {row[0] for row in con.execute("SELECT log_id FROM log_ids")}
    con.close()
    assert rows == {4242, 12983101, 23408}  # refreshed + intact
    doc = log_id_lookup_document(db, r, [4242], None)
    assert doc["log_id_table"] is True
    assert doc["ids"][0]["found"] is True


def test_update_with_missing_project_root_clears_the_marker(repo) -> None:
    """(a) prior-true + partial-false → CLEARED. When the partial index's
    project_root is not on disk, NOTHING can
    be re-extracted — the changed files' rows are gone, so the update must
    CLEAR the completeness marker instead of keeping it true (which would
    answer the changed files' ids a confident false found:false)."""
    from cppgraph.pipeline import update_store

    r, commit, _h = repo
    graph = _build(r, extract_log_ids=True)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    # A partial index claiming a project_root that doesn't exist on disk.
    index = _index()
    index.metadata.project_root = f"file://{r / 'gone'}"
    update_store(db, index, deleted_files=[])
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone() is None
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is False
    assert doc["ids"][0]["reason"]


def test_full_build_with_unreadable_source_withholds_marker(repo) -> None:
    """(b) One scanned document whose source is unreadable = rows missing for
    it = the completeness claim is false. The full build must NOT set the
    marker (extraction is never silently best-effort); lookups degrade
    honestly even though other documents' rows were extracted."""
    r, commit, _head = repo
    index = _index()
    ghost = index.documents.add()
    ghost.relative_path = "ghost.cpp"  # not on disk
    gs = ghost.symbols.add()
    gs.symbol = H
    gs.display_name = "h"
    gocc = ghost.occurrences.add()
    gocc.symbol = H
    gocc.symbol_roles = 1
    gocc.range.extend([0, 0, 2, 1])
    gocc.enclosing_range.extend([0, 0, 2, 1])
    index.metadata.project_root = f"file://{r}"
    graph = build_graph(index, extract_log_ids=True)
    assert graph.log_ids  # the readable document still extracted its rows
    assert graph.log_ids_extracted is False
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone() is None
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    # The table is uncertified (log_id_table stays False), but the readable
    # document's rows are real evidence: a present id answers found:True
    # normally; only misses degrade to the unverified reason.
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is True
    miss = log_id_lookup_document(db, r, [9999999], None)["ids"][0]
    assert miss["found"] is False
    assert "1" in miss["reason"] and "unverified" in miss["reason"]


# --- uncertified-but-populated tables: found = evidence, miss = unverified -----


def _store_with_ghost_document(repo) -> Path:
    """A FULL extraction build where one scanned document's source is
    unreadable (the real-mongo finding: bazel-generated files absent from the
    checkout): the table IS populated with the readable documents' rows, but
    the `log_ids_extracted` marker is withheld and the skip count is 1."""
    r, commit, _head = repo
    index = _index()
    ghost = index.documents.add()
    ghost.relative_path = "ghost.cpp"  # not on disk
    gs = ghost.symbols.add()
    gs.symbol = H
    gs.display_name = "h"
    gocc = ghost.occurrences.add()
    gocc.symbol = H
    gocc.symbol_roles = 1
    gocc.range.extend([0, 0, 2, 1])
    gocc.enclosing_range.extend([0, 0, 2, 1])
    index.metadata.project_root = f"file://{r}"
    graph = build_graph(index, extract_log_ids=True)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    return db


def test_store_records_log_id_skip_count(repo) -> None:
    """A build with skipped (unreadable) documents records the count in the
    store meta (`log_id_skips`) alongside the withheld marker; a clean build
    records 0."""
    r, _c, _h = repo
    db = _store_with_ghost_document(repo)
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='log_id_skips'").fetchone()[0] == "1"
    assert con.execute("SELECT value FROM meta WHERE key='log_ids_extracted'").fetchone() is None
    con.close()
    clean = _store_with(repo)
    con = sqlite3.connect(clean)
    assert con.execute("SELECT value FROM meta WHERE key='log_id_skips'").fetchone()[0] == "0"
    con.close()


def test_miss_on_uncertified_table_names_skip_count(repo) -> None:
    """A miss on a populated-but-uncertified table is precise: not a
    definitive absence — the reason carries the skip count N from meta and
    says the miss is unverified. `log_id_table` stays False (no semantics
    change on the flag)."""
    r, _c, _h = repo
    doc = log_id_lookup_document(_store_with_ghost_document(repo), r, [9999999], None)
    assert doc["log_id_table"] is False
    miss = doc["ids"][0]
    assert miss["found"] is False
    assert "1" in miss["reason"]
    assert "unverified" in miss["reason"]
    assert "not certified complete" in miss["reason"]


def test_found_row_on_uncertified_table_answers_normally(repo) -> None:
    """A found row is always real evidence: the same uncertified store answers
    a present id with the normal found:True shape (symbol, scip, location,
    callers)."""
    r, _c, _h = repo
    doc = log_id_lookup_document(_store_with_ghost_document(repo), r, [12983100], None)
    assert doc["log_id_table"] is False  # uncertified: the flag stays False
    hit = doc["ids"][0]
    assert hit["found"] is True
    assert hit["scip"] == F
    assert hit["location"] == "src.cpp:2"


def test_miss_on_certified_table_keeps_plain_reason(repo) -> None:
    """On a marker-set store a miss stays the plain definitive reason."""
    r, _c, _h = repo
    doc = log_id_lookup_document(_store_with(repo), r, [9999999], None)
    assert doc["log_id_table"] is True
    assert doc["ids"][0]["reason"] == "no log-id entry for this id in this graph"


def test_no_table_store_keeps_old_reason(repo) -> None:
    """A store without the table keeps the v1 reason, unchanged."""
    r, _c, _h = repo
    graph = _build(r)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": repo[1]})
    con = sqlite3.connect(db)
    con.execute("DROP TABLE log_ids")
    con.commit()
    con.close()
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["ids"][0]["reason"] == ("graph store predates the log-id table (rebuild to get it)")


def test_uncertified_table_without_skip_meta_keeps_generic_reason(repo) -> None:
    """A table whose marker is unset but which carries no skip count (a
    v5-upgraded store,     never extracted) keeps the generic uncertified reason —
    no fabricated N."""
    from cppgraph.store import enrich_references

    r, _c, _h = repo
    db = _make_v5_store(repo, with_refs=True)

    enrich_references(db, _index())  # creates the (empty) table, no marker, no count
    doc = log_id_lookup_document(db, r, [12983100], None)
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is False
    assert "not certified complete" in doc["ids"][0]["reason"]
    assert "unverified" not in doc["ids"][0]["reason"]


def test_zero_skip_meta_falls_back_to_generic_reason(repo) -> None:
    """Belt-and-braces: a hypothetic `log_id_skips='0'` under a WITHHELD
    marker (an inconsistent store — 0 skips would have set the marker) falls
    back to the generic reason instead of the nonsensical "0 source
    document(s) unreadable"."""
    r, _c, _h = repo
    db = _store_with(repo)  # log_id_skips='0', marker set
    con = sqlite3.connect(db)
    con.execute("DELETE FROM meta WHERE key='log_ids_extracted'")
    con.commit()
    con.close()
    doc = log_id_lookup_document(db, r, [9999999], None)
    assert doc["log_id_table"] is False
    assert doc["ids"][0]["found"] is False
    assert "not certified complete" in doc["ids"][0]["reason"]
    assert "unverified" not in doc["ids"][0]["reason"]


# --- the query surface (pure function) ----------------------------------------


def _store_with(repo) -> Path:
    """Build + write the store for the `repo` fixture (the tuple it yields)."""
    r, commit, _head = repo
    graph = _build(r, extract_log_ids=True)
    graph.add_edge("calls", G, F, file="src.cpp", line=3)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db, meta={"source_commit": commit})
    return db


def test_document_shape_and_revision_guard(repo: Path) -> None:
    r, commit, head = repo
    doc = log_id_lookup_document(_store_with(repo), r, [12983100, 9999999], "9b8165b1")
    assert doc["schema"] == 1 and doc["command"] == "log-id-lookup"
    assert doc["contract_version"] == 2
    ra = doc["resolved_against"]
    assert ra["graph_source_commit"] == commit
    assert ra["repo_head"] == head
    assert ra["head_matches_graph"] is True
    assert ra["revision"] == "9b8165b1"
    (hit, miss) = doc["ids"]
    assert hit["id"] == 12983100 and hit["found"] is True
    assert hit["scip"] == F
    assert hit["location"] == "src.cpp:2"
    # _store_with added one caller (G -> F).
    assert hit["callers"]["total"] == 1
    assert hit["callers"]["items"][0]["symbol"] == G
    assert hit["callers"]["truncated"] is False
    assert hit["symbol"] == "f()"
    assert miss == {
        "id": 9999999,
        "found": False,
        "reason": "no log-id entry for this id in this graph",
    }


def test_callers_bounded_and_visible(repo: Path) -> None:
    r, _c, _h = repo
    db = r / ".cppgraph" / "t.graph.db"
    graph = _build(r, extract_log_ids=True)
    for i in range(CALLERS_LIMIT + 3):  # 13 distinct callers of F
        sym = f"cxx . . $ pkg/C{i}#m()."
        graph.nodes[sym] = Node(symbol=sym, display_name=f"c{i}", file="src.cpp", line=20 + i)
        graph.add_edge("calls", sym, F, file="src.cpp", line=20 + i)
    write_sqlite(graph, db, meta={"source_commit": _git(r, "rev-parse", "HEAD")})
    doc = log_id_lookup_document(db, r, [12983100], None)
    entry = doc["ids"][0]
    assert entry["callers"]["total"] == CALLERS_LIMIT + 3
    assert len(entry["callers"]["items"]) == CALLERS_LIMIT
    assert entry["callers"]["truncated"] is True


# --- the `scip` field's exact form (the masora-anchor seam, pinned) -----------


def test_scip_field_is_the_scheme_prefixed_scip_identity(repo: Path) -> None:
    """redwood_integration.md §1, pinned: `scip` is the SCIP identity string
    EXACTLY as the store carries it — scheme-prefixed (`cxx …`) — NOT
    masora's `scip-clang …` ANCHOR form. The store carries no such prefix
    (verified against real stores), and a fabricated one would be a guess
    AND would fail `cppgraph fingerprints`' verbatim matching."""
    r, _c, _h = repo
    doc = log_id_lookup_document(_store_with(repo), r, [12983100], None)
    hit = doc["ids"][0]
    assert hit["scip"] == F
    assert hit["scip"].startswith("cxx ")  # SCIP's scheme prefix — the store's form
    assert not hit["scip"].startswith("scip-clang")


def test_masora_anchor_is_a_prefix_derivation_of_the_scip_field(repo: Path) -> None:
    """redwood_integration.md §1's anchor round-trip, pinned to the shipped
    truth: masora's anchor identity is `"scip-clang " + <scip>` — a
    documented, deterministic serialization (space-separated tokens starting
    `scip-clang cxx …`) — while the store string itself is what
    `cppgraph fingerprints` matches VERBATIM (the anchor-prefixed form
    answers `found: false`; the store never carries the prefix)."""
    from cppgraph.fingerprints import fingerprints_document

    r, _c, _h = repo
    db = _store_with(repo)
    hit = log_id_lookup_document(db, r, [12983100], None)["ids"][0]
    anchor = f"scip-clang {hit['scip']}"
    tokens = anchor.split(" ")
    assert tokens[0] == "scip-clang" and tokens[1] == "cxx" and len(tokens) >= 5
    (store_form, anchor_form) = fingerprints_document(db, r, [hit["scip"], anchor])["symbols"]
    assert store_form["found"] is True
    assert anchor_form["found"] is False  # the anchor prefix is never store data


def test_symbol_field_stays_the_display_name(repo: Path) -> None:
    """`symbol` is the human display name from the .scip — unchanged by the
    scip-form pinning; the two fields never swap roles."""
    r, _c, _h = repo
    doc = log_id_lookup_document(_store_with(repo), r, [12983100], None)
    hit = doc["ids"][0]
    assert hit["symbol"] == "f()"
    assert hit["symbol"] != hit["scip"]


def test_head_mismatch_reported_not_resolved(repo: Path) -> None:
    """The guard is a reported fact: a HEAD ahead of the graph still answers,
    visibly mismatched — cppgraph never silently re-resolves."""
    r, _c, _h = repo
    (r / "later.cpp").write_text("// later\n")
    _git(r, "add", "later.cpp")
    # Amend: HEAD becomes a DIFFERENT commit that does not contain the graph's
    # source commit — the merge-base guard must report the mismatch (an
    # ordinary commit ahead would still leave the graph commit an ancestor).
    _git(r, "commit", "--amend", "-m", "second")
    doc = log_id_lookup_document(_store_with(repo), r, [12983100], None)
    assert doc["resolved_against"]["head_matches_graph"] is False


def test_more_than_two_ids_is_an_error(repo: Path) -> None:
    r, _c, _h = repo
    with pytest.raises(LogIdLookupError, match="2"):
        log_id_lookup_document(_store_with(repo), r, [1, 2, 3], None)


def test_no_source_commit_fails_closed(repo: Path) -> None:
    r, _c, _h = repo
    graph = _build(r, extract_log_ids=True)
    db = r / ".cppgraph" / "t.graph.db"
    write_sqlite(graph, db)  # no source_commit in meta
    with pytest.raises(LogIdLookupError):
        log_id_lookup_document(db, r, [12983100], None)


def test_mid_query_database_error_becomes_log_id_lookup_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sqlite error raised AFTER the open (mid-query, e.g. a corrupt store's
    DatabaseError during the log_ids / callers SELECTs) must not escape raw —
    the contract boundary wraps it into `LogIdLookupError` (fail-closed)."""
    import sqlite3

    r, _c, _h = repo
    db = _store_with(repo)

    def _corrupt(conn: Any, scip: str) -> dict[str, Any]:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("cppgraph.redwood._callers_of", _corrupt)
    with pytest.raises(LogIdLookupError, match="malformed"):
        log_id_lookup_document(db, r, [12983100], None)


# --- CLI surface ---------------------------------------------------------------


def _cli(repo: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["log-id-lookup", "--repo", str(repo), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_golden_document(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r, _c, _h = repo
    _store_with(repo)
    code, out, err = _cli(r, "--ids", "12983100", "--revision", "deadbeef", capsys=capsys)
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc["schema"] == 1 and doc["command"] == "log-id-lookup"
    assert doc["ids"][0]["found"] is True
    assert doc["resolved_against"]["revision"] == "deadbeef"


def test_cli_cap_is_an_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r, _c, _h = repo
    _store_with(repo)
    code, out, err = _cli(r, "--ids", "1", "2", "3", capsys=capsys)
    assert code == 1 and out == "" and "2" in err


def test_cli_no_graph_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r = tmp_path / "empty"
    r.mkdir()
    code, out, err = _cli(r, "--ids", "12983100", capsys=capsys)
    assert code == 1 and out == "" and err


def test_cli_missing_repo_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _cli(tmp_path / "nope", "--ids", "12983100", capsys=capsys)
    assert code == 1 and out == ""


# --- MCP parity ----------------------------------------------------------------


def test_mcp_parity_same_function(repo: Path) -> None:
    """The MCP tool is registered and delegates to the SAME pure function as
    the CLI (`cppgraph.redwood.log_id_lookup_document`) — parity by routing,
    not by forked logic. Dispatched through the server's PUBLIC `call_tool`
    (the tests/test_json_docs.py MCP-parity pattern), no internals reached."""

    from cppgraph import mcp_server

    r, _c, _h = repo
    db = _store_with(repo)
    server = mcp_server.build_server(str(db), root=str(r))

    def _call(arguments: dict[str, Any]) -> dict[str, Any]:
        result = server.call_tool("log_id_lookup", arguments)
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    doc = _call({"ids": [12983100], "revision": "deadbeef"})
    expected = log_id_lookup_document(db, r, [12983100], "deadbeef")
    assert doc == expected
    assert doc["ids"][0]["scip"] == F
    assert doc["resolved_against"]["revision"] == "deadbeef"
    # The 2-id hard cap on the MCP surface too.
    over = _call({"ids": [1, 2, 3]})
    assert "error" in over


def test_update_replaces_changed_files_rows(repo: Path) -> None:
    """An incremental update re-extracts the changed TUs' rows and drops the
    deleted ones' rows — the table never keeps stale entries."""
    r, commit, _h = repo
    db = _store_with(repo)
    (r / "src.cpp").write_text(SOURCE.replace("12983100", "4242"))
    from cppgraph.pipeline import update_store

    update_store(db, _partial_index(r), deleted_files=[])
    con = sqlite3.connect(db)
    ids = {row[0] for row in con.execute("SELECT log_id FROM log_ids")}
    con.close()
    assert 4242 in ids and 12983100 not in ids


def _partial_index(repo: Path) -> Any:
    index = _index()
    index.metadata.project_root = f"file://{repo}"
    return index


def test_schema_version_bumped(repo: Path) -> None:
    assert SCHEMA_VERSION == 6
