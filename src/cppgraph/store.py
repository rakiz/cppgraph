"""SQLite-backed graph store: interned symbols, indexed topology.

Phase 2 replaces the flat `graph.json` (whole 1.19 GB file parsed into RAM on
*every* query) with a SQLite database that answers callers/callees off a
B-tree index and walks paths/impact through indexed neighbour lookups, never
materialising the full graph. See DESIGN.md § Store for the measured rationale
(interning → 3.7× smaller, `callers_of` in 0.08 ms vs ~3.4 s per-query load).

Two halves:

- `write_sqlite(graph, path)` — one-shot writer. The in-memory `Graph` is built
  once from the `.scip` (transient), then interned to disk: each distinct symbol
  and file path gets an integer id; edges reference ids, not the 127-char
  symbol strings. This is what both shrinks the store and speeds up traversal
  (integer joins beat string ops).
- `GraphStore` — query handle over the file. Resolves a symbol to its id once
  (via `ix_sym`), then everything downstream is id-space until results are
  materialised back to `Node`/`Edge` for display.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import TYPE_CHECKING

from cppgraph.builder import (
    _gc_disabled,
    _is_direct_member,
    build_graph,
    is_callable_symbol,
    is_term_symbol,
    is_type_symbol,
)
from cppgraph.export import is_test_file
from cppgraph.filters import matches_path_prefix
from cppgraph.model import Edge, Graph, Node, Reference

if TYPE_CHECKING:
    from cppgraph.proto import scip_pb2

_SCHEMA = """
CREATE TABLE files (
    id   INTEGER PRIMARY KEY,
    path TEXT
);
-- `end_line` = the definition's `enclosing_range` body extent, present only
-- when the binary emits it (#504); NULL on stock. Powers `line_span`, gates
-- `no_incoming_calls` (flagged `has_enclosing_ranges` in meta).
CREATE TABLE symbols (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL,
    display_name TEXT,
    file_id      INTEGER,
    line         INTEGER,
    end_line     INTEGER
);
CREATE TABLE edges (
    kind    TEXT NOT NULL,
    src_id  INTEGER NOT NULL,
    dst_id  INTEGER NOT NULL,
    file_id INTEGER,
    line    INTEGER
);
-- Exact reference-location index (opt-in, `cppgraph build --references`): each
-- non-local use of a symbol as symbol_id -> file:line. `enclosing_id` is the
-- definition symbol that contains the use site (opt-in `--attributed-refs`,
-- needs an enclosing_range-emitting binary), or NULL when unattributed — then
-- the reference is a pure location (file granularity). See DESIGN.md § Graph
-- model. Empty unless the graph was built with references.
CREATE TABLE refs (
    symbol_id    INTEGER NOT NULL,
    file_id      INTEGER,
    line         INTEGER,
    enclosing_id INTEGER
);
-- Provenance: what was indexed. `source_commit` is the anchor for an
-- incremental `cppgraph update` (git-diff the stored commit against HEAD to
-- learn exactly which files changed). See DESIGN.md § "Keeping the graph up
-- to date".
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Built after bulk insert (faster than maintaining them per-row).
_INDEXES = """
CREATE INDEX ix_sym ON symbols(symbol);   -- exact symbol -> id resolution
CREATE INDEX ix_src ON edges(src_id);     -- callees_of / forward traversal
CREATE INDEX ix_dst ON edges(dst_id);     -- callers_of / reverse traversal
CREATE INDEX ix_refs ON refs(symbol_id);  -- references_of a symbol
"""

# SQLite caps host-variable count per statement (default 999 historically).
# Chunk `IN (...)` id lists well under that.
_ID_CHUNK = 900

# A `file:line` query: the LAST `:` splits off a 1-indexed line, so the file
# part may itself contain `:` (a Windows drive letter). No valid input to the
# name-resolution path has this shape — a SCIP symbol string always ends in a
# descriptor (`.`, `#` or `)`), and a C++ name never contains a lone `:` — so
# recognizing it first is unambiguous. Line 0 or a leading zero is not the
# form (1-indexed lines start at 1) and falls through to name matching.
_FILE_LINE_RE = re.compile(r"^(.+):([1-9][0-9]*)$")

# On-disk store format version, stamped into `meta.schema_version` at build.
# Bump when the schema changes incompatibly (new/renamed tables or columns);
# then a migration can branch on the stored value. A store with no
# `schema_version` predates versioning (treated as the oldest, still readable).
# `GraphStore` refuses to open a store whose version is *newer* than this — an
# old binary must not silently misread a format it doesn't understand.
# v3: `symbols.end_line` (definition body extents, #504 binaries only).
SCHEMA_VERSION = 3


class IncompatibleStoreError(RuntimeError):
    """Raised opening a store written by a newer cppgraph than this one."""


def _git(root: Path, *args: str) -> str | None:
    """Best-effort `git -C root <args>`; None if git is missing, times out, or
    the command fails (e.g. root isn't a repo). Never raises — provenance is
    optional and must not break a build."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def dirty_fingerprints(root: str | Path, base_commit: str) -> dict[str, str]:
    """`{path: git-blob-hash}` for the tracked files that differ from `base_commit`
    in `root`'s working tree — i.e. the uncommitted edits *present at this moment*.

    Recorded at build time (when the indexed tree was dirty) so a later staleness
    check can tell "this file was already indexed in exactly this state" from "this
    file changed since indexing". Content hash (`git hash-object`), not
    `(mtime,size)`: a checkout/touch/reformat changes mtime without changing
    content, which would otherwise resurrect the false-stale it's meant to kill."""
    root = Path(root)
    diff = _git(root, "diff", "--name-only", "--diff-filter=d", base_commit, "--")
    if not diff:
        return {}
    fingerprints: dict[str, str] = {}
    for path in (ln.strip() for ln in diff.splitlines()):
        if not path:
            continue
        h = _git(root, "hash-object", "--", path)
        if h:
            fingerprints[path] = h
    return fingerprints


def changed_files_since(
    root: str | Path,
    base_commit: str,
    dirty_fingerprints: dict[str, str] | None = None,
) -> tuple[list[str], list[str]] | None:
    """Files that differ in `root`'s working tree from `base_commit`.

    Returns `(changed, deleted)` relative paths, or `None` if `root` isn't a
    git checkout / git is unavailable. Diffs the working tree (not just HEAD)
    against the commit, so uncommitted edits count too — this is exactly the
    changed-file set an incremental `cppgraph update` would consume, mirroring
    `the index wizard`.

    `dirty_fingerprints` (from the graph's provenance) are the blob hashes of files
    that were uncommitted *when the graph was built* — indexed in that exact state.
    For those files the fingerprint is authoritative, so the diff-vs-commit result
    is corrected against it:
    - content still equal to the fingerprint -> indexed as-is -> **not** stale,
      dropped from `changed` (kills the false "N files changed" for a dirty build);
    - content differs (edited further, **or reverted** to the committed version) ->
      the index holds a different content than the tree -> **stale**, forced into
      `changed` even when `git diff` no longer flags it (the revert case).
    Deletions are left to the `deleted` set (a fingerprinted file now gone shows up
    there via the commit diff).
    """
    root = Path(root)
    changed = _git(root, "diff", "--name-only", "--diff-filter=d", base_commit, "--")
    deleted = _git(root, "diff", "--name-only", "--diff-filter=D", base_commit, "--")
    if changed is None or deleted is None:
        return None
    deleted_list = [ln for ln in deleted.splitlines() if ln.strip()]
    changed_set = {ln for ln in changed.splitlines() if ln.strip()}
    if dirty_fingerprints:
        deleted_set = set(deleted_list)
        for path, recorded in dirty_fingerprints.items():
            if path in deleted_set:
                continue  # gone now — handled as a deletion, not a change
            current = _git(root, "hash-object", "--", path)
            if current is None:
                continue  # unreadable/removed — the deletion path covers it
            if current == recorded:
                changed_set.discard(path)  # indexed exactly as it is now
            else:
                changed_set.add(path)  # index holds a different content -> stale
    return (sorted(changed_set), deleted_list)


def read_dirty_fingerprints(meta: dict[str, str]) -> dict[str, str] | None:
    """Parse the `dirty_fingerprints` provenance (JSON `{path: blob-hash}`) from a
    store's meta, or None if absent/malformed. Pass the result to
    `changed_files_since` so a dirty-at-build graph isn't reported stale."""
    raw = meta.get("dirty_fingerprints")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def commits_behind(root: str | Path, base_commit: str) -> int | None:
    """How many commits `root`'s HEAD is ahead of `base_commit` (the indexed
    commit), i.e. `git rev-list --count base_commit..HEAD`. None if unknown."""
    out = _git(Path(root), "rev-list", "--count", f"{base_commit}..HEAD")
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


# At/above this share of indexed files changed, an incremental update stops
# paying off (it re-indexes each changed TU) — recommend a full rebuild instead.
REBUILD_FILE_FRACTION = 0.25


def staleness_verdict(
    changed: int,
    deleted: int,
    indexed_files: int,
    commits_behind: int | None = None,
) -> dict:
    """Turn drift counts into a magnitude + a recommendation (pure/testable).

    `changed`/`deleted` are the C++ file counts since the indexed commit;
    `indexed_files` is the store's file count (the denominator). Recommends a
    full `rebuild` once the changed fraction reaches `REBUILD_FILE_FRACTION`,
    else an incremental `update`. `up_to_date` when nothing changed.
    """
    n = changed + deleted
    verdict: dict = {
        "up_to_date": n == 0,
        "changed": changed,
        "deleted": deleted,
        "commits_behind": commits_behind,
    }
    if n == 0:
        return verdict
    fraction = n / indexed_files if indexed_files else None
    verdict["indexed_files"] = indexed_files or None
    verdict["changed_fraction"] = round(fraction, 3) if fraction is not None else None
    verdict["recommend"] = (
        "rebuild" if fraction is not None and fraction >= REBUILD_FILE_FRACTION else "update"
    )
    return verdict


def is_stale(store: GraphStore, root: str | Path, source_exts: tuple[str, ...]) -> bool | None:
    """Cheap per-query drift flag: has any indexed C++ file changed since the
    graph's source commit? A single `git diff --name-only`, no `commits_behind`
    subprocess and no fraction/recommendation — just enough to flag `stale` on
    every query response without the cost of a full `status` drift report.
    `None` when unknown (no recorded commit, or `root` isn't a git checkout)."""
    m = store.meta()
    commit = m.get("source_commit")
    if not commit:
        return None
    changes = changed_files_since(root, commit, dirty_fingerprints=read_dirty_fingerprints(m))
    if changes is None:
        return None
    changed, deleted = changes
    return any(f.endswith(source_exts) for f in changed) or any(
        f.endswith(source_exts) for f in deleted
    )


def project_root_path(project_root_uri: str) -> Path | None:
    """The local filesystem path behind a SCIP `Metadata.project_root`, which is
    a `file://` URI."""
    if project_root_uri.startswith("file://"):
        return Path(project_root_uri[len("file://") :])
    if project_root_uri:
        return Path(project_root_uri)
    return None


def discover_graph(start: str | Path | None = None) -> tuple[Path, Path] | None:
    """Find the graph for the current project, Serena-style (`--project-from-cwd`).

    Walk up from `start` (default: cwd) looking for a `.cppgraph/` holding at
    least one `*.graph.db`; return `(graph, project_root)` — the most recently
    built graph there and the directory that owns the `.cppgraph/`. `None` if no
    indexed project is found above the cwd. Shared by the MCP server (one global
    registration serves every project) and the CLI (so `--graph` is optional when
    run from inside an indexed project).
    """
    d = Path(start or Path.cwd()).resolve()
    for cur in (d, *d.parents):
        cpg = cur / ".cppgraph"
        if cpg.is_dir():
            graphs = sorted(cpg.glob("*.graph.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            if graphs:
                return graphs[0], cur
    return None


def build_provenance(
    index: scip_pb2.Index,
    *,
    source_commit: str | None = None,
    source_dirty: bool | None = None,
    scip_variant: str | None = None,
    index_filter: str | None = None,
    index_excludes_tests: bool | None = None,
) -> dict[str, str]:
    """Provenance to record in the store's `meta` table: *what* was indexed.

    Copies what the SCIP index already carries (`project_root`, the indexing
    tool + version) so the store is self-describing even after the `.scip` is
    discarded, and captures the **source commit** — the anchor for a future
    incremental `cppgraph update`.

    The commit is best-effort: `source_commit` (e.g. passed by the index wizard,
    captured at *index* time — the accurate moment) wins; otherwise it's
    auto-detected via `git rev-parse HEAD` on `project_root` at build time,
    which is exact when index→build run back-to-back. If `project_root` isn't a
    git checkout, no commit is recorded (no error).

    `index_filter` / `index_excludes_tests` record the **index scope** — which
    subtree was indexed and whether test TUs were dropped. Stamped by the caller
    (the index wizard knows both), they make the graph self-describing (`status`
    shows the scope) and let an incremental `--update` reuse the exact same scope
    instead of guessing. `index_filter` is recorded even when empty (whole tree),
    so "whole tree" is explicit rather than indistinguishable from a legacy graph
    that never recorded a scope at all. Left unset here -> no key written.
    """
    meta: dict[str, str] = {}
    md = index.metadata
    if md.project_root:
        meta["project_root"] = md.project_root
    if md.tool_info.name:
        meta["index_tool"] = md.tool_info.name
    if md.tool_info.version:
        meta["index_tool_version"] = md.tool_info.version
    # The SCIP metadata carries the tool's *version* but not which patch variant
    # it was — "stock" vs a patched build (e.g. enclosing_range-504) emit
    # different indexes, so the caller stamps it (from the binary's provenance
    # sidecar). Lets `cppgraph status` tell when a graph is stale for the pin.
    if scip_variant:
        meta["index_tool_variant"] = scip_variant

    # Index scope: recorded even when the filter is empty (whole tree), so a
    # scoped graph is distinguishable from a legacy one that never stored a scope.
    if index_filter is not None:
        meta["index_filter"] = index_filter
    if index_excludes_tests is not None:
        meta["index_tests"] = "excluded" if index_excludes_tests else "included"

    commit = source_commit
    dirty = source_dirty
    root = project_root_path(md.project_root)
    if commit is None and root is not None:
        commit = _git(root, "rev-parse", "HEAD")
        if commit is not None and dirty is None:
            # Non-empty porcelain output => uncommitted changes in the checkout.
            status = _git(root, "status", "--porcelain")
            dirty = bool(status)
    if commit:
        meta["source_commit"] = commit
    if dirty is not None:
        meta["source_dirty"] = "true" if dirty else "false"

    # When the indexed tree was dirty, fingerprint the uncommitted files so a later
    # staleness check doesn't flag them as "changed" (they were indexed as-is).
    # See dirty_fingerprints / changed_files_since.
    if dirty and commit and root is not None:
        fingerprints = dirty_fingerprints(root, commit)
        if fingerprints:
            meta["dirty_fingerprints"] = json.dumps(fingerprints)

    meta["built_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        meta["cppgraph_version"] = importlib_metadata.version("cppgraph")
    except importlib_metadata.PackageNotFoundError:
        pass
    return meta


def write_sqlite(graph: Graph, path: str | Path, *, meta: dict[str, str] | None = None) -> None:
    """Serialise an in-memory `Graph` to an interned SQLite store, overwriting
    any existing file at `path`.

    `meta` is provenance (see `build_provenance`) stored in the `meta` table;
    `node_count`/`edge_count` are always recorded from the graph itself.
    """
    path = Path(path)
    if path.exists():
        path.unlink()

    con = sqlite3.connect(path)
    try:
        # Throwaway bulk build: durability doesn't matter, speed does.
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        con.executescript(_SCHEMA)

        file_ids: dict[str, int] = {}

        def file_id(p: str | None) -> int | None:
            if p is None:
                return None
            fid = file_ids.get(p)
            if fid is None:
                fid = len(file_ids)
                file_ids[p] = fid
            return fid

        sym_ids: dict[str, int] = {}
        sym_rows = []
        for i, node in enumerate(graph.nodes.values()):
            sym_ids[node.symbol] = i
            sym_rows.append(
                (i, node.symbol, node.display_name, file_id(node.file), node.line, node.end_line)
            )

        edge_rows = [
            (e.kind, sym_ids[e.src], sym_ids[e.dst], file_id(e.file), e.line) for e in graph.edges
        ]
        # add_reference interns the symbol as a node, so sym_ids covers it. The
        # enclosing symbol is a definition (also interned); guard with .get in
        # case attribution named a symbol outside the indexed set.
        ref_rows = [
            (
                sym_ids[r.symbol],
                file_id(r.file),
                r.line,
                sym_ids.get(r.enclosing_symbol) if r.enclosing_symbol else None,
            )
            for r in graph.references
        ]
        attributed_refs = sum(1 for r in graph.references if r.enclosing_symbol)

        all_meta = dict(meta or {})
        all_meta["schema_version"] = str(SCHEMA_VERSION)
        all_meta.setdefault("node_count", str(len(graph.nodes)))
        all_meta.setdefault("edge_count", str(len(graph.edges)))
        # Definition body extents present => the binary emitted enclosing_range
        # (#504). The data-driven gate for `line_span`/`no_incoming_calls`,
        # mirroring `has_references`/`has_attributed_refs` (absent on stock).
        if any(n.end_line is not None for n in graph.nodes.values()):
            all_meta.setdefault("has_enclosing_ranges", "true")
        if graph.references:
            all_meta.setdefault("has_references", "true")
            all_meta.setdefault("ref_count", str(len(graph.references)))
            if attributed_refs:
                all_meta.setdefault("has_attributed_refs", "true")
                all_meta.setdefault("attributed_ref_count", str(attributed_refs))

        con.executemany(
            "INSERT INTO files VALUES (?, ?)", [(fid, p) for p, fid in file_ids.items()]
        )
        con.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?)", sym_rows)
        con.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", edge_rows)
        con.executemany("INSERT INTO refs VALUES (?, ?, ?, ?)", ref_rows)
        con.executemany("INSERT INTO meta VALUES (?, ?)", all_meta.items())
        con.executescript(_INDEXES)
        con.commit()
    finally:
        con.close()


@dataclass
class UpdateStats:
    """What an incremental update touched — for `cppgraph update`'s summary."""

    files_changed: int
    edges_removed: int
    edges_added: int
    symbols_removed: int
    node_count: int
    edge_count: int


def update_store(
    path: str | Path,
    partial_index: scip_pb2.Index,
    *,
    deleted_files: Iterable[str] = (),
    meta: dict[str, str] | None = None,
) -> UpdateStats:
    """Apply a partial re-index to an existing store in place.

    `partial_index` is the SCIP index of *only the changed translation units*
    (re-indexed after a `git diff`); `deleted_files` are source paths removed
    from the tree entirely (no Document in the partial index). The set of files
    whose old contributions to invalidate is taken from the partial index's
    Documents — not the rebuilt graph — so a file that changed to produce *no*
    edges still gets its stale edges cleared (see DESIGN.md § "Keeping the graph
    up to date").

    Correctness rests on the builder being document-local: every edge's `file`
    is exactly the Document that produced it, so replacing a file's edges never
    needs cross-file analysis.
    """
    changed_files = {doc.relative_path for doc in partial_index.documents}
    changed_files.update(deleted_files)
    store = GraphStore(path)
    try:
        # Match the store: if it carries a reference-location index, rebuild
        # references for the changed files too, else they'd be silently dropped.
        include_references = store.meta().get("has_references") == "true"
        # Preserve the store's attribution level across incremental updates, so a
        # `--attributed-refs` store keeps its enclosing attribution for rebuilt
        # files instead of silently downgrading them to file granularity.
        attribute_references = store.meta().get("has_attributed_refs") == "true"
        partial_graph = build_graph(
            partial_index,
            include_references=include_references,
            attribute_references=attribute_references,
        )
        return store.apply_update(partial_graph, changed_files, meta=meta)
    finally:
        store.close()


@_gc_disabled
def enrich_references(path: str | Path, index: scip_pb2.Index) -> tuple[int, int]:
    """Add symbol-granularity reference attribution to an existing store in place.

    Reads enclosing ranges from `index` (a #504-built .scip for the same sources)
    and back-fills each stored reference's enclosing definition — no full rebuild.
    Returns `(attributed, total_refs)`. Raises `ValueError` if the store carries
    no reference index to enrich (build it with `--references` first).

    Matching is by (referenced symbol, file, line): the same key the store already
    interns, so a reference the .scip attributes is updated exactly where it lives.
    A reference whose occurrence has no enclosing range (or whose enclosing symbol
    isn't in the store) is left untouched — degrading, never wrong.
    """
    graph = build_graph(index, include_references=True, attribute_references=True)
    con = sqlite3.connect(Path(path))
    try:
        total = con.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        has_refs = con.execute("SELECT value FROM meta WHERE key = 'has_references'").fetchone()
        if not has_refs or total == 0:
            raise ValueError(
                "store has no reference index to enrich — rebuild it with "
                "`cppgraph build --references --attributed-refs` instead"
            )
        # Old (v1) stores lack the column; add it so enrichment can write.
        cols = {row[1] for row in con.execute("PRAGMA table_info(refs)")}
        if "enclosing_id" not in cols:
            con.execute("ALTER TABLE refs ADD COLUMN enclosing_id INTEGER")
        # Old (v2) stores lack `symbols.end_line` too. This function stamps
        # schema_version=3 below, so a store it touches must actually be
        # v3-shaped — otherwise a later `line_span` query ("no such column:
        # s.end_line") would crash on a store that only ever ran enrichment.
        sym_cols = {row[1] for row in con.execute("PRAGMA table_info(symbols)")}
        if "end_line" not in sym_cols:
            con.execute("ALTER TABLE symbols ADD COLUMN end_line INTEGER")

        sym_ids = dict(con.execute("SELECT symbol, id FROM symbols"))
        file_ids = dict(con.execute("SELECT path, id FROM files"))
        updates = []
        for r in graph.references:
            if not r.enclosing_symbol:
                continue
            sid = sym_ids.get(r.symbol)
            eid = sym_ids.get(r.enclosing_symbol)
            if sid is None or eid is None:
                continue
            updates.append((eid, sid, file_ids.get(r.file) if r.file else None, r.line))

        # Without a composite index the UPDATE below can only seek on symbol_id
        # (the sole index, ix_refs), then scans every row of that symbol to match
        # file_id/line. On hub symbols (thousands of use sites) that is a full scan
        # per UPDATE, x millions of updates -> the enrich never finishes. A
        # (symbol_id, file_id, line) index turns each UPDATE into an O(log n) seek
        # (SQLite uses it for the `IS` NULL comparisons too). Drop it afterwards:
        # it exists only to speed up the write, not to serve queries.
        con.execute("CREATE INDEX IF NOT EXISTS ix_refs_enrich ON refs(symbol_id, file_id, line)")
        try:
            before = con.total_changes
            # `IS` matches NULL file_id/line the same way the rows were stored.
            con.executemany(
                "UPDATE refs SET enclosing_id = ? "
                "WHERE symbol_id = ? AND file_id IS ? AND line IS ?",
                updates,
            )
            attributed = con.total_changes - before
        finally:
            con.execute("DROP INDEX IF EXISTS ix_refs_enrich")

        # Only claim symbol granularity when at least one reference was actually
        # attributed. A run that attributes 0 (stock .scip, or a mismatched index)
        # must leave the view at file granularity — not flip the flag on and make
        # `status` advertise "SYMBOL granularity (0 refs attributed)".
        for key, value in (
            ("has_attributed_refs", "true" if attributed else "false"),
            ("attributed_ref_count", str(attributed)),
            ("schema_version", str(SCHEMA_VERSION)),
        ):
            con.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        con.commit()
        return attributed, total
    finally:
        con.close()


def _tarjan_sccs(adjacency: dict[int, list[int]]) -> list[list[int]]:
    """Tarjan's strongly-connected components over an id-space adjacency
    (`src_id -> [dst_id, …]`), **iterative** on purpose: a call graph has
    thousands of nodes and the textbook recursive form would overflow Python's
    ~1000-frame default recursion limit on a deep DFS chain (the same
    scale-first reasoning as builder.py's iterative containment sweep). Each
    explicit frame is `(node, iterator over its successors)` — resuming the
    iterator is the recursive call's return point.

    Returns every component, singletons included; filtering to the reportable
    ones is the caller's job. A self-loop leaves its node a singleton (its own
    index never lowers its lowlink). Only nodes with outgoing edges are used
    as DFS roots — a member of a bigger component always has one, so nothing
    reportable is missed.
    """
    index: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0

    for root in adjacency:
        if root in index:
            continue
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        frames: list[tuple[int, Iterator[int]]] = [(root, iter(adjacency[root]))]
        while frames:
            v, successors = frames[-1]
            pushed = False
            for w in successors:
                if w not in index:
                    index[w] = lowlink[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    frames.append((w, iter(adjacency.get(w, ()))))
                    pushed = True
                    break
                if w in on_stack and index[w] < lowlink[v]:
                    lowlink[v] = index[w]
            if pushed:
                continue
            frames.pop()
            if frames:
                parent = frames[-1][0]
                if lowlink[v] < lowlink[parent]:
                    lowlink[parent] = lowlink[v]
            if lowlink[v] == index[v]:
                component: list[int] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == v:
                        break
                components.append(component)
    return components


class GraphStore:
    """Query + incremental-update handle over a SQLite store written by
    `write_sqlite`.

    Mirrors the query surface of the in-memory `Graph` (callers_of, callees_of,
    find, shortest_call_path, impact) so the CLI is agnostic to the backend, and
    adds `apply_update` for in-place partial re-indexing.
    """

    def __init__(self, path: str | Path) -> None:
        self._con = sqlite3.connect(Path(path))
        self._check_schema_compat()

    def _check_schema_compat(self) -> None:
        """Refuse a store whose format is newer than this binary understands.

        An older/unversioned store is fine (backward compatible — missing tables
        are handled by the individual queries). A *newer* one is not: reading it
        with an old schema could silently return wrong results.
        """
        raw = self.meta().get("schema_version")
        if raw is None:
            return  # predates versioning; readable as legacy
        try:
            version = int(raw)
        except ValueError:
            return  # unparseable; treat as legacy rather than hard-fail
        if version > SCHEMA_VERSION:
            self._con.close()
            raise IncompatibleStoreError(
                f"graph store schema v{version} is newer than this cppgraph "
                f"(supports v{SCHEMA_VERSION}); upgrade cppgraph or rebuild the graph"
            )

    def schema_version(self) -> int | None:
        """The store's on-disk format version, or None if it predates versioning."""
        raw = self.meta().get("schema_version")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- id resolution -----------------------------------------------------

    def _symbol_id(self, symbol: str) -> int | None:
        row = self._con.execute("SELECT id FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        return row[0] if row else None

    def has_symbol(self, symbol: str) -> bool:
        return self._symbol_id(symbol) is not None

    def meta(self) -> dict[str, str]:
        """Provenance recorded at build time (project_root, source_commit,
        index tool + version, counts, timestamp). Empty dict for a store
        written before the `meta` table existed."""
        try:
            rows = self._con.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.OperationalError:
            return {}
        return dict(rows)

    def indexed_file_count(self) -> int:
        """Number of distinct files that contributed to the index — the
        denominator for the staleness magnitude (what share changed)."""
        (n,) = self._con.execute("SELECT COUNT(*) FROM files").fetchone()
        return n

    def _symbols_for_ids(self, ids: set[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids_list = list(ids)
        for start in range(0, len(ids_list), _ID_CHUNK):
            chunk = ids_list[start : start + _ID_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for sid, symbol in self._con.execute(
                f"SELECT id, symbol FROM symbols WHERE id IN ({placeholders})", chunk
            ):
                out[sid] = symbol
        return out

    # --- point queries -----------------------------------------------------

    def get_node(self, symbol: str) -> Node | None:
        try:
            row = self._con.execute(
                """
                SELECT s.symbol, s.display_name, f.path, s.line, s.end_line
                FROM symbols s LEFT JOIN files f ON f.id = s.file_id
                WHERE s.symbol = ?
                """,
                (symbol,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Store predates `symbols.end_line` (schema v2, never migrated by
            # apply_update/enrich_references) — degrade to the v2 shape rather
            # than crash a plain read-only query.
            row = self._con.execute(
                """
                SELECT s.symbol, s.display_name, f.path, s.line
                FROM symbols s LEFT JOIN files f ON f.id = s.file_id
                WHERE s.symbol = ?
                """,
                (symbol,),
            ).fetchone()
            if row is None:
                return None
            return Node(symbol=row[0], display_name=row[1] or "", file=row[2], line=row[3])
        if row is None:
            return None
        return Node(
            symbol=row[0], display_name=row[1] or "", file=row[2], line=row[3], end_line=row[4]
        )

    def find(self, query: str, fuzzy: bool = False) -> list[Node]:
        """Nodes matching `query`.

        A single-token query is a substring test (`instr(col, ?) > 0`, matching
        the in-memory `Graph.find`'s Python `in` — unlike `LIKE`, which SQLite
        runs case-insensitively for ASCII). A multi-token query (whitespace-
        separated) is an order-free **AND**: every token must appear as a
        substring in the symbol *or* the display name (tokens may match either,
        and different tokens may match different columns), so
        `find "buildPipeline changeStream"` matches a symbol containing both
        rather than the literal phrase.

        With `fuzzy=True`, matching is case- *and* separator-insensitive: both
        sides are lowercased and underscores stripped before the substring test,
        so `changestream` matches `change_stream` and `changeStream`. This is the
        fallback the MCP layer uses when an exact query returns nothing (the
        `change_stream` vs `changeStream` naming trap), never the default —
        default stays exact and predictable.

        This scans the symbols table (a leading wildcard can't use `ix_sym`),
        which is fine: `find` is a rare interactive lookup, not a hot-path
        traversal.
        """
        tokens = query.split()
        if not tokens:
            return []
        if fuzzy:
            # Normalise both sides: lower-case + drop underscores. `instr` over
            # the folded columns makes the match case/separator-insensitive.
            col_sym = "replace(lower(s.symbol), '_', '')"
            col_name = "replace(lower(s.display_name), '_', '')"
            clause = " AND ".join(
                [f"(instr({col_sym}, ?) > 0 OR instr({col_name}, ?) > 0)"] * len(tokens)
            )
            params: list[str] = []
            for t in tokens:
                norm = t.lower().replace("_", "")
                params.extend((norm, norm))
        else:
            # Each token: present in symbol OR display_name. AND across tokens.
            clause = " AND ".join(
                ["(instr(s.symbol, ?) > 0 OR instr(s.display_name, ?) > 0)"] * len(tokens)
            )
            params = []
            for t in tokens:
                params.extend((t, t))
        rows = self._con.execute(
            f"""
            SELECT s.symbol, s.display_name, f.path, s.line
            FROM symbols s LEFT JOIN files f ON f.id = s.file_id
            WHERE {clause}
            """,
            params,
        ).fetchall()
        return [Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in rows]

    def resolve(self, query: str) -> tuple[str | None, list[Node]]:
        """Resolve a caller-supplied `query` to one exact symbol — the shared
        name->symbol step behind both the CLI and the MCP tools (so they stay
        equivalent).

        A `file:line` query is recognized first (trailing `:<positive
        integer>`, a shape neither a SCIP symbol string nor a C++ name can
        have): the file is matched exactly against the recorded (relative)
        path — `outline`'s convention, backslashes normalized, no fuzzy or
        suffix matching, so the caller must pass the path form the graph
        recorded — and the line is **1-indexed** (editor convention; the store
        keeps 0-indexed lines, converted once on the way in). Lookup is
        two-tier: a definition whose own start line is exactly that line;
        then, only on a store with body extents (`has_enclosing_ranges`,
        #504-built binaries), the innermost (narrowest-span) definition whose
        `[line, end_line]` contains the requested line — a line inside a
        function body, not on its first line. On a stock store a body line is
        an honest no-match, never a nearest-definition guess.

        Otherwise an exact symbol string is returned as-is, and `query` is a
        name: a plain substring match, then — if that misses —
        `Class::method` normalized to SCIP's `Class#method`, then a
        case/separator-insensitive fuzzy match. The three outcomes are encoded
        in the return:

        - `(symbol, [])`     — exact hit, or a name/location matching exactly
                                 one symbol;
        - `(None, [n1, n2])` — ambiguous: the distinct candidate nodes to pick
                                 from (several symbols sharing the exact line,
                                 or the same narrowest containing span);
        - `(None, [])`       — no match.

        It never picks one of several candidates: a wrong guess would be a
        confidently-wrong answer, the failure mode cppgraph exists to avoid.
        """
        loc = _FILE_LINE_RE.match(query)
        if loc:
            return self._resolve_location(loc.group(1), int(loc.group(2)))
        if self.has_symbol(query):
            return query, []
        matches = self.find(query)
        if not matches and "::" in query:
            matches = self.find(query.replace("::", "#"))
        if not matches:
            matches = self.find(query, fuzzy=True)
        distinct: list[Node] = []
        seen: set[str] = set()
        for n in matches:
            if n.symbol not in seen:
                seen.add(n.symbol)
                distinct.append(n)
        if len(distinct) == 1:
            return distinct[0].symbol, []
        return None, distinct

    def _resolve_location(self, file: str, line1: int) -> tuple[str | None, list[Node]]:
        """The `file:line` arm of `resolve`. `line1` is the 1-indexed line the
        user means; the store records 0-indexed lines (what every display path
        adds 1 back onto), so convert once here — an off-by-one would resolve
        a confidently-wrong symbol."""
        file = file.replace("\\", "/")
        line0 = line1 - 1
        rows = self._con.execute(
            """
            SELECT s.symbol, s.display_name, f.path, s.line
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE f.path = ? AND s.line = ?
            """,
            (file, line0),
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0], []
        if len(rows) > 1:
            nodes = [Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in rows]
            return None, nodes
        # No definition *starts* on that line. With body extents (#504) the
        # line may sit inside one: the innermost containing definition, i.e.
        # the narrowest `[line, end_line]` span (a method beats its class).
        # Ordered narrowest-first, all rows sharing the narrowest span are
        # kept — a tie is ambiguous, never a pick.
        if self._has_enclosing_ranges():
            spans = self._con.execute(
                """
                SELECT s.symbol, s.display_name, f.path, s.line,
                       (s.end_line - s.line) AS span
                FROM symbols s JOIN files f ON f.id = s.file_id
                WHERE f.path = ? AND s.line <= ? AND s.end_line >= ?
                ORDER BY span ASC, s.symbol ASC
                """,
                (file, line0, line0),
            ).fetchall()
            if spans:
                narrowest = spans[0][4]
                tied = [r for r in spans if r[4] == narrowest]
                if len(tied) == 1:
                    return tied[0][0], []
                return None, [
                    Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in tied
                ]
        return None, []

    def callers_of(self, symbol: str) -> list[Edge]:
        dst_id = self._symbol_id(symbol)
        if dst_id is None:
            return []
        rows = self._con.execute(
            """
            SELECT src.symbol, f.path, e.line
            FROM edges e
            JOIN symbols src ON src.id = e.src_id
            LEFT JOIN files f ON f.id = e.file_id
            WHERE e.kind = 'calls' AND e.dst_id = ?
            """,
            (dst_id,),
        ).fetchall()
        return [Edge(kind="calls", src=r[0], dst=symbol, file=r[1], line=r[2]) for r in rows]

    def callees_of(self, symbol: str) -> list[Edge]:
        src_id = self._symbol_id(symbol)
        if src_id is None:
            return []
        rows = self._con.execute(
            """
            SELECT dst.symbol, f.path, e.line
            FROM edges e
            JOIN symbols dst ON dst.id = e.dst_id
            LEFT JOIN files f ON f.id = e.file_id
            WHERE e.kind = 'calls' AND e.src_id = ?
            """,
            (src_id,),
        ).fetchall()
        return [Edge(kind="calls", src=symbol, dst=r[0], file=r[1], line=r[2]) for r in rows]

    def bases_of(self, symbol: str) -> list[Node]:
        """Direct base classes of `symbol` (one `inherits` hop forward).

        `inherits` edges point derived -> base, so the bases are the `dst`s of
        edges where `symbol` is the `src`. Returns the base *types* with their
        own definition sites — an inheritance edge carries no meaningful line,
        so what's useful is where each base class is defined.
        """
        src_id = self._symbol_id(symbol)
        if src_id is None:
            return []
        rows = self._con.execute(
            """
            SELECT dst.symbol, dst.display_name, f.path, dst.line
            FROM edges e
            JOIN symbols dst ON dst.id = e.dst_id
            LEFT JOIN files f ON f.id = dst.file_id
            WHERE e.kind = 'inherits' AND e.src_id = ?
            """,
            (src_id,),
        ).fetchall()
        return [Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in rows]

    def subtypes_of(self, symbol: str) -> list[Node]:
        """Direct subclasses of `symbol` (one `inherits` hop backward).

        The `src`s of `inherits` edges whose `dst` is `symbol`, returned as the
        derived *types* with their own definition sites (see `bases_of`).
        """
        dst_id = self._symbol_id(symbol)
        if dst_id is None:
            return []
        rows = self._con.execute(
            """
            SELECT src.symbol, src.display_name, f.path, src.line
            FROM edges e
            JOIN symbols src ON src.id = e.src_id
            LEFT JOIN files f ON f.id = src.file_id
            WHERE e.kind = 'inherits' AND e.dst_id = ?
            """,
            (dst_id,),
        ).fetchall()
        return [Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in rows]

    def references_of(self, symbol: str) -> list[Reference]:
        """Exact use sites of `symbol` (the `--references` location index).

        Each carries its `enclosing_symbol` when the graph was built with
        `--attributed-refs` (else None). Empty if built without `--references`
        (or the store predates the `refs` table).
        """
        sym_id = self._symbol_id(symbol)
        if sym_id is None:
            return []
        try:
            rows = self._con.execute(
                """
                SELECT f.path, r.line, e.symbol
                FROM refs r
                LEFT JOIN files f ON f.id = r.file_id
                LEFT JOIN symbols e ON e.id = r.enclosing_id
                WHERE r.symbol_id = ?
                ORDER BY f.path, r.line
                """,
                (sym_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Store predates the `enclosing_id` column (schema v1) or the refs
            # table entirely; retry without the enclosing join, else give up.
            try:
                rows = [
                    (r[0], r[1], None)
                    for r in self._con.execute(
                        """
                        SELECT f.path, r.line
                        FROM refs r LEFT JOIN files f ON f.id = r.file_id
                        WHERE r.symbol_id = ?
                        ORDER BY f.path, r.line
                        """,
                        (sym_id,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                return []
        return [Reference(symbol=symbol, file=r[0], line=r[1], enclosing_symbol=r[2]) for r in rows]

    # --- traversals (indexed neighbour lookups, never a full load) ---------

    def shortest_call_path(self, src: str, dst: str) -> list[Edge] | None:
        """Shortest chain of `calls` edges from `src` to `dst`, BFS in id-space.

        Returns `[]` if src == dst, `None` if no path exists or either symbol is
        unknown. Only the visited frontier touches the DB — one indexed
        `ix_src` lookup per expanded node.
        """
        src_id = self._symbol_id(src)
        dst_id = self._symbol_id(dst)
        if src_id is None or dst_id is None:
            return None
        if src_id == dst_id:
            return []

        visited = {src_id}
        # queue carries (node_id, node_symbol, path_of_edges)
        queue: deque[tuple[int, str, list[Edge]]] = deque([(src_id, src, [])])
        while queue:
            node_id, node_symbol, path = queue.popleft()
            for e_dst_id, e_dst_symbol, path_str, line in self._con.execute(
                """
                SELECT e.dst_id, d.symbol, f.path, e.line
                FROM edges e
                JOIN symbols d ON d.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id
                WHERE e.kind = 'calls' AND e.src_id = ?
                """,
                (node_id,),
            ).fetchall():
                edge = Edge(
                    kind="calls", src=node_symbol, dst=e_dst_symbol, file=path_str, line=line
                )
                if e_dst_id == dst_id:
                    return path + [edge]
                if e_dst_id not in visited:
                    visited.add(e_dst_id)
                    queue.append((e_dst_id, e_dst_symbol, path + [edge]))
        return None

    def impact(self, symbol: str, max_depth: int | None = None, kind: str = "calls") -> set[str]:
        """Symbols that transitively reach `symbol` backward along `kind` edges.

        Reverse BFS over `ix_dst`; `max_depth` bounds the backward hops
        (`None` = unbounded). `kind="calls"` is the call blast-radius (who
        transitively calls it); `kind="inherits"` is the type hierarchy below a
        base (all transitive subclasses). Walks in id-space, resolving to symbol
        strings only for the final result set.
        """
        start_id = self._symbol_id(symbol)
        if start_id is None:
            return set()

        visited = {start_id}
        frontier = [start_id]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            next_frontier: list[int] = []
            for node_id in frontier:
                for (caller_id,) in self._con.execute(
                    "SELECT src_id FROM edges WHERE kind = ? AND dst_id = ?",
                    (kind, node_id),
                ).fetchall():
                    if caller_id not in visited:
                        visited.add(caller_id)
                        next_frontier.append(caller_id)
            frontier = next_frontier
            depth += 1

        visited.discard(start_id)
        return set(self._symbols_for_ids(visited).values())

    def reachable_from(
        self, symbol: str, max_depth: int | None = None, kind: str = "calls"
    ) -> set[str]:
        """Symbols `symbol` transitively reaches forward along `kind` edges.

        The exact mirror of `impact`: forward BFS over `ix_src` instead of
        backward over `ix_dst`; `max_depth` bounds the forward hops (`None` =
        unbounded). `kind="calls"` is forward call reachability (what an entry
        point can reach); `kind="inherits"` walks a derived type up its base
        hierarchy (all transitive ancestors). Walks in id-space, resolving to
        symbol strings only for the final result set.
        """
        start_id = self._symbol_id(symbol)
        if start_id is None:
            return set()

        visited = {start_id}
        frontier = [start_id]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            next_frontier: list[int] = []
            for node_id in frontier:
                for (callee_id,) in self._con.execute(
                    "SELECT dst_id FROM edges WHERE kind = ? AND src_id = ?",
                    (kind, node_id),
                ).fetchall():
                    if callee_id not in visited:
                        visited.add(callee_id)
                        next_frontier.append(callee_id)
            frontier = next_frontier
            depth += 1

        visited.discard(start_id)
        return set(self._symbols_for_ids(visited).values())

    def hotspots(
        self,
        limit: int | None = 20,
        kind: str = "fan_in",
        exclude_tests: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        target_paths: list[str] | None = None,
    ) -> tuple[list[tuple[str, int]], int]:
        """Rank symbols by call-edge volume across the whole graph.

        `kind="fan_in"` (default) ranks by incoming `calls` edges (most-called);
        `"fan_out"` by outgoing edges (most-calling); `"edges"` sums both per
        symbol — including a self-recursive symbol's own edge to itself *twice*
        (once as the caller, once as the callee), the usual graph-degree
        convention for a self-loop, not a double-counting bug. `limit=None`
        returns the full ranking uncapped (for callers that aggregate over it,
        e.g. `dependency_cost`'s total); the default 20 slices it.

        Aggregated entirely in SQL (`GROUP BY` + `ORDER BY` + `LIMIT`) over the
        `edges` index, in id-space until the final `LIMIT` rows are resolved to
        symbol strings — consistent with this store's "hot topology stays
        all-integer, cold payload materialized only for results actually shown"
        design (see DESIGN.md § Store); a global ranking still has to scan every
        `calls` edge (there's no way around that for a whole-graph aggregate),
        but never materializes a symbol string or a Python `Counter` over it.

        `exclude_tests` drops a `calls` edge if *either* endpoint symbol is
        itself defined in a test file (via its own definition site, like
        `cppgraph.filters.drop_test_edges` — not the call site, which is a
        different, unrelated file). Symmetric because this is a global ranking
        with no single "far endpoint" the way a per-symbol query has one — and
        it keeps that symmetric either-endpoint shape in `target_paths` mode
        too. `include_paths`/`exclude_paths` apply the same way, via
        `cppgraph.filters.matches_path_prefix` registered as a SQL function
        (`cpg_path_ok`) — an edge counts only if *both* endpoints' definition
        files pass the prefix filter.

        `target_paths` switches that path filtering to an **asymmetric** mode:
        the ranked (callee) side is pinned to symbols *defined* under one of
        the `target_paths` prefixes (a `cpg_target_ok` match on the destination
        endpoint's definition file), while `include_paths`/`exclude_paths`
        apply to the **caller** side *only* instead of both sides. That answers
        "how many call sites point into this library?" — counting
        cross-boundary edges (callers anywhere calling INTO the prefix), which
        the symmetric mode cannot express (it would require the caller to be
        inside the prefix too, i.e. library-internal fan-in — a different
        question). E.g. `target_paths=["spirv_cross/"],
        exclude_paths=["vendor/"]` = "call sites into `spirv_cross/` from my
        code, not from other vendored users of it". `kind` must be `"fan_in"`
        there — the mode *is* an incoming-call-site count, so `"fan_out"`/
        `"edges"` are a nonsensical combination and raise `ValueError` (same
        defensive-validation style as the unknown-kind one). An empty list is
        treated as not given, matching `matches_path_prefix`'s convention.
        With `target_paths=None` (the default) the symmetric semantics above
        apply exactly as before — a fully backward-compatible additive mode.

        NOTE for readers arriving from TODO.md's original wording ("likely
        falls out of hotspots + path-prefix filtering for free"): it did not —
        the symmetric `cpg_path_ok`-on-both-endpoints semantics cannot restrict
        the callee side alone, so this mode is a real extension of the filter
        model, not just wiring a prefix into the existing parameters.

        Returns `(ranked, total)`: `ranked` is the top `limit` `(symbol, count)`
        pairs in descending order, `total` is how many distinct symbols have at
        least one edge of the requested kind (so the caller knows if the list
        was truncated).
        """
        if kind not in ("fan_in", "fan_out", "edges"):
            raise ValueError(f"unknown hotspots kind: {kind!r}")
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be >= 0 or None, got {limit}")
        if target_paths and kind != "fan_in":
            raise ValueError(
                "target_paths counts incoming call sites into the target prefix(es), "
                f"so it requires kind='fan_in', got {kind!r}"
            )

        self._con.create_function("cpg_is_test_file", 1, is_test_file, deterministic=True)
        test_clause = (
            "AND NOT cpg_is_test_file(f_src.path) AND NOT cpg_is_test_file(f_dst.path)"
            if exclude_tests
            else ""
        )
        path_clause = ""
        if target_paths:

            def _target_ok(path: str | None) -> bool:
                return matches_path_prefix(path, include=target_paths, exclude=None)

            self._con.create_function("cpg_target_ok", 1, _target_ok, deterministic=True)
            # Asymmetric mode: the callee side is pinned by cpg_target_ok, and
            # include/exclude (if given) constrain the caller side ONLY.
            path_clause = "AND cpg_target_ok(f_dst.path)"
            if include_paths or exclude_paths:

                def _path_ok(path: str | None) -> bool:
                    return matches_path_prefix(path, include=include_paths, exclude=exclude_paths)

                self._con.create_function("cpg_path_ok", 1, _path_ok, deterministic=True)
                path_clause += " AND cpg_path_ok(f_src.path)"
        elif include_paths or exclude_paths:

            def _path_ok(path: str | None) -> bool:
                return matches_path_prefix(path, include=include_paths, exclude=exclude_paths)

            self._con.create_function("cpg_path_ok", 1, _path_ok, deterministic=True)
            path_clause = "AND cpg_path_ok(f_src.path) AND cpg_path_ok(f_dst.path)"
        # UNION ALL one subquery per role so a self-loop naturally yields two
        # rows (one per role) — the degree-convention doubling from the
        # docstring falls out of the query shape rather than a special case.
        role_columns: list[str] = []
        if kind in ("fan_in", "edges"):
            role_columns.append("dst_id")
        if kind in ("fan_out", "edges"):
            role_columns.append("src_id")
        per_role = "\n            UNION ALL\n            ".join(
            f"""
            SELECT e.{column} AS ranked_id
            FROM edges e
            JOIN symbols s_src ON s_src.id = e.src_id
            JOIN symbols s_dst ON s_dst.id = e.dst_id
            LEFT JOIN files f_src ON f_src.id = s_src.file_id
            LEFT JOIN files f_dst ON f_dst.id = s_dst.file_id
            WHERE e.kind = 'calls' {test_clause} {path_clause}
            """
            for column in role_columns
        )
        rows = self._con.execute(
            f"""
            SELECT s.symbol, COUNT(*) AS n
            FROM ({per_role}) ranked
            JOIN symbols s ON s.id = ranked.ranked_id
            GROUP BY ranked.ranked_id
            ORDER BY n DESC
            """
        ).fetchall()
        ranked = [(sym, n) for sym, n in rows]
        return ranked[:limit], len(ranked)

    def stats(
        self,
        group_by: str = "file",
        limit: int = 20,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> tuple[list[dict[str, str | int]], int]:
        """Aggregate counts per file or per directory — a module-level
        "how big / how dense is this part of the codebase" view.

        Per file (the group's own path, before any rollup): `symbols` = symbols
        *defined* there (`symbols.file_id`), `edges` = `calls` edges whose call
        site is there (`edges.file_id` — the caller's call-site file, not either
        endpoint's definition file), `refs` = references whose use site is there
        (`refs.file_id`). Three SQL `GROUP BY file_id` aggregations merged into
        one dict per file; a file with zero of everything never appears (there
        is nothing to count, no LEFT JOIN needed).

        `group_by="dir"` rolls the per-file counts up by `dirname(path)` in
        Python (top-level files land in `"."`) — the file-level counts are the
        aggregation unit, so the rollup is over grouped rows, not raw ones.
        `include_paths`/`exclude_paths` filter by the group's own file path via
        `cppgraph.filters.matches_path_prefix` (registered as `cpg_path_ok`,
        like `hotspots`), applied *before* aggregation — a vendored file's
        counts drop out entirely, including its rollup contribution.

        Sorted by `symbols + edges + refs` (combined size) descending. Returns
        `(groups, total)`: `groups` is the top `limit` dicts (`{"file"|"dir":
        path, "symbols": n, "edges": n, "refs": n}` — the key matches
        `group_by`), `total` the full group count, so `truncated =
        total > len(groups)` works like `hotspots`' `(ranked, total)`.
        """
        if group_by not in ("file", "dir"):
            raise ValueError(f"unknown stats group_by: {group_by!r}")
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")

        path_clause = ""
        if include_paths or exclude_paths:

            def _path_ok(path: str | None) -> bool:
                return matches_path_prefix(path, include=include_paths, exclude=exclude_paths)

            self._con.create_function("cpg_path_ok", 1, _path_ok, deterministic=True)
            path_clause = "AND cpg_path_ok(f.path)"
        counts: dict[str, dict[str, int]] = {}

        def _bucket(path: str) -> dict[str, int]:
            c = counts.get(path)
            if c is None:
                c = {"symbols": 0, "edges": 0, "refs": 0}
                counts[path] = c
            return c

        for key, sql in (
            (
                "symbols",
                "SELECT f.path AS path, COUNT(*) AS n FROM symbols s "
                "JOIN files f ON f.id = s.file_id WHERE 1=1 {pc} GROUP BY s.file_id",
            ),
            (
                "edges",
                "SELECT f.path AS path, COUNT(*) AS n FROM edges e "
                "JOIN files f ON f.id = e.file_id WHERE e.kind = 'calls' {pc} "
                "GROUP BY e.file_id",
            ),
            (
                "refs",
                "SELECT f.path AS path, COUNT(*) AS n FROM refs r "
                "JOIN files f ON f.id = r.file_id WHERE 1=1 {pc} GROUP BY r.file_id",
            ),
        ):
            for path, n in self._con.execute(sql.format(pc=path_clause)):
                _bucket(path)[key] = n
        if group_by == "dir":
            rolled: dict[str, dict[str, int]] = {}
            for path, c in counts.items():
                p = path.replace("\\", "/")
                d = p.rsplit("/", 1)[0] if "/" in p else "."
                b = rolled.setdefault(d, {"symbols": 0, "edges": 0, "refs": 0})
                for k in ("symbols", "edges", "refs"):
                    b[k] += c[k]
            grouped: list[tuple[str, dict[str, int]]] = list(rolled.items())
        else:
            grouped = list(counts.items())
        grouped.sort(key=lambda pc: sum(pc[1].values()), reverse=True)
        top = grouped[:limit]
        name = group_by
        return [{name: path, **c} for path, c in top], len(grouped)

    def _own_file_filters(
        self,
        *,
        alias: str,
        exclude_tests: bool,
        include_paths: list[str] | None,
        exclude_paths: list[str] | None,
    ) -> str:
        """SQL `AND ...` clauses filtering a symbols row by its *own* definition
        file (joined as `alias`) — the same test/path-prefix filters the other
        ranking tools apply, on the row's own file like `stats` (a global
        ranking over definitions has no far endpoint to filter on)."""
        clauses: list[str] = []
        if exclude_tests:
            self._con.create_function("cpg_is_test_file", 1, is_test_file, deterministic=True)
            clauses.append(f"AND NOT cpg_is_test_file({alias}.path)")
        if include_paths or exclude_paths:

            def _path_ok(path: str | None) -> bool:
                return matches_path_prefix(path, include=include_paths, exclude=exclude_paths)

            self._con.create_function("cpg_path_ok", 1, _path_ok, deterministic=True)
            clauses.append(f"AND cpg_path_ok({alias}.path)")
        return " ".join(clauses)

    def _has_enclosing_ranges(self) -> bool:
        """The data-driven capability gate: did any indexed definition carry a
        body extent (`end_line`, from `enclosing_range` — a #504-built binary)?
        Mirrors `has_references`/`has_attributed_refs`: a build-time meta flag,
        not a live COUNT per call."""
        return self.meta().get("has_enclosing_ranges") == "true"

    def line_span(
        self,
        limit: int = 20,
        exclude_tests: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> tuple[list[tuple[str, int]], int] | None:
        """Rank definitions by body extent — `end_line - line`, largest first.

        The extents are each definition's own `enclosing_range` (#504-built
        binary), persisted at build time — the exact span, not a
        def→next-symbol heuristic. On a store without them (a stock binary) the
        meta gate `has_enclosing_ranges` is unset, so this returns **None** —
        "unavailable", never an empty list that would read as "no definitions"
        (the same degrade-cleanly contract as the reference-attribution
        features).

        Aggregated in SQL like `hotspots` (ORDER BY + LIMIT after the filters,
        symbol strings materialized only for the shown rows). `exclude_tests`
        and `include_paths`/`exclude_paths` filter by the definition's own
        file. Returns `(ranked, total)`: the top `limit` `(symbol, span)` pairs
        descending, and the full filtered count so the caller can flag
        truncation.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        if not self._has_enclosing_ranges():
            return None
        filters = self._own_file_filters(
            alias="f",
            exclude_tests=exclude_tests,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )
        rows = self._con.execute(
            f"""
            SELECT s.symbol, (s.end_line - s.line) AS span
            FROM symbols s LEFT JOIN files f ON f.id = s.file_id
            WHERE s.end_line IS NOT NULL AND s.file_id IS NOT NULL AND s.line IS NOT NULL {filters}
            ORDER BY span DESC, s.symbol ASC
            """
        ).fetchall()
        ranked = [(symbol, span) for symbol, span in rows]
        return ranked[:limit], len(ranked)

    def no_incoming_calls(
        self,
        limit: int = 20,
        exclude_tests: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> tuple[list[str], int] | None:
        """Callable definitions with zero incoming `calls` edges — the graph
        fact behind the "dead code" question, stated as a fact, never a verdict
        (vtable dispatch, exported API, templates, entry points can all have
        no static caller and still be live).

        Gated on `has_enclosing_ranges` like `line_span`, because the *answer*
        is only trustworthy there: on a stock-binary graph, caller attribution
        falls back to nearest-preceding-definition, which can fabricate a
        phantom caller from a bodyless declaration site and turn a real 0 into
        a false 1 (DESIGN.md "Known limitation") — so this returns None
        (refuses) instead of answering unreliably.

        Callable is the SCIP descriptor suffix (`is_callable_symbol`,
        registered as a SQL function — one source of truth), and only symbols
        with a recorded definition site count (`file_id IS NOT NULL`): a
        callable merely mentioned (e.g. address-taken) but not defined in the
        index isn't a definition. `exclude_tests`/path filters scope which
        definitions are listed; every `calls` edge counts as a caller
        regardless of where it comes from (a test-only caller still means
        "called"). Returns `(symbols, total)`, ordered by definition site.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        if not self._has_enclosing_ranges():
            return None
        self._con.create_function("cpg_is_callable", 1, is_callable_symbol, deterministic=True)
        filters = self._own_file_filters(
            alias="f",
            exclude_tests=exclude_tests,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )
        rows = self._con.execute(
            f"""
            SELECT s.symbol
            FROM symbols s LEFT JOIN files f ON f.id = s.file_id
            WHERE s.file_id IS NOT NULL
              AND cpg_is_callable(s.symbol)
              AND NOT EXISTS (
                  SELECT 1 FROM edges e WHERE e.kind = 'calls' AND e.dst_id = s.id
              )
              {filters}
            ORDER BY f.path, s.line, s.symbol
            """
        ).fetchall()
        symbols = [row[0] for row in rows]
        return symbols[:limit], len(symbols)

    def global_init_references(
        self, symbol: str, limit: int = 40
    ) -> tuple[list[Reference], int] | None:
        """The term (global/field) symbols referenced within `symbol`'s
        initializer region — the graph fact behind the "static initialization
        order fiasco" question: global A's initializer references global B
        (across translation units, initialization order is unspecified, so the
        read may see an uninitialized B). A fact, never a verdict: a
        `constexpr`/`constinit` initializer is constant-initialized and safe,
        and a read inside a lambda body in the region may run lazily rather
        than at initialization (scip-clang emits no separate interval for such
        a lambda, so it cannot be split out — measured). The tool reports the
        reference; the hazard judgment is the reader's.

        The region is the definition's `enclosing_range` (spanning the
        declaration including the initializer, #504 `saveVarDecl`), and each
        use it contains was attributed to `symbol` at build time by the
        containment sweep — so this is a lookup over the attributed refs:
        `refs.enclosing_id = symbol` where the referenced symbol is itself a
        term. One row per referenced global (a global read twice is one row,
        at its first use site), ordered by first use site. Non-term uses in
        the region (a call to a function) are not global reads and are
        excluded.

        Returns **None** when the store carries no attributed references
        (stock binary, or built without `--attributed-refs`/`enrich-refs`) —
        explicitly unavailable, never an empty list that would read as
        "references nothing"; the gate dominates the input checks below (on
        such a store nothing is answerable). Raises ValueError on a negative
        `limit` (checked first, like `line_span`), or — on a gated store — an
        unknown symbol or a known non-term symbol (a callable has no
        initializer region): bad input, distinct from both None and empty.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        if self.meta().get("has_attributed_refs") != "true":
            return None
        if not is_term_symbol(symbol):
            raise ValueError(
                f"{symbol} is not a global/term symbol (its SCIP descriptor does "
                "not end in '.'): global_init_references reports what a global's "
                "initializer region references"
            )
        sym_id = self._symbol_id(symbol)
        if sym_id is None:
            raise ValueError(f"unknown symbol {symbol!r}")
        self._con.create_function("cpg_is_term", 1, is_term_symbol, deterministic=True)
        rows = self._con.execute(
            """
            SELECT dst.symbol, f.path, MIN(r.line) AS first_line
            FROM refs r
            JOIN symbols dst ON dst.id = r.symbol_id
            LEFT JOIN files f ON f.id = r.file_id
            WHERE r.enclosing_id = ? AND cpg_is_term(dst.symbol)
            GROUP BY dst.id
            ORDER BY first_line, dst.symbol
            """,
            (sym_id,),
        ).fetchall()
        refs = [Reference(symbol=r[0], file=r[1], line=r[2], enclosing_symbol=symbol) for r in rows]
        return refs[:limit], len(refs)

    def boundary_violations(
        self,
        rules: list[tuple[str, str]],
        edge_kinds: tuple[str, ...] = ("calls", "inherits"),
        limit: int = 40,
    ) -> tuple[list[dict[str, str | int | None]], int]:
        """`calls`/`inherits` edges that cross a caller-declared layering rule.

        A rule is `(from_prefix, forbidden_prefix)`: "no edge whose *source*
        symbol is defined under `from_prefix` may point at a symbol defined
        under `forbidden_prefix`" — e.g. `("common/", "platform/")` means
        `common/` must not call `platform/`. The rules come from the caller
        (the project's declared architecture, which the graph doesn't know),
        and each reported violation *is* a real compiler-traced edge, so there
        are no false positives by construction. The converse direction is a
        lower bound: an empty result means no *statically indexed* edge
        crosses the rules (runtime dispatch — virtual calls, function
        pointers — can cross a boundary with no static edge), never a proof of
        conformance.

        Directory membership is the endpoint's own definition file, matched on
        a path-segment boundary via `cppgraph.filters.matches_path_prefix`
        (registered as the SQL function `cpg_under_prefix`, the same pattern
        as `hotspots`' `cpg_path_ok`): `"common"` matches `common/util.cpp`
        and `common/` itself, never `commons/util.cpp`. A symbol with no
        recorded definition site belongs to no layer and can match neither
        side of a rule.

        Each rule is one SQL query (edge kinds + both prefixes bound per
        rule); an edge matching several rules yields one record per rule, each
        naming the rule it broke, ordered rule-major (the caller's rule
        order), then by call site. Returns `(violations, total)`: `violations`
        is at most `limit` dicts (`{"kind", "src", "dst", "file", "line",
        "rule"}` — `file`/`line` is the call/inheritance site, `rule` is
        `"from -> forbidden"`), `total` the full record count across all
        rules.

        Raises ValueError on an empty `rules` list, a malformed rule (not a
        pair of strings, an empty prefix — which would match nothing —, or
        `from == forbidden`, a layer forbidden to itself), an empty
        `edge_kinds`, an unknown edge kind (a typo must not silently read as
        "layering holds"), or a negative `limit`.
        """
        if not rules:
            raise ValueError(
                "boundary_violations needs at least one (from_prefix, forbidden_prefix) rule"
            )
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        if not edge_kinds:
            raise ValueError("edge_kinds must not be empty (it would match nothing)")
        unknown = sorted(set(edge_kinds) - {"calls", "inherits", "implements"})
        if unknown:
            raise ValueError(f"unknown edge kind(s): {', '.join(unknown)}")

        def _norm(prefix: str) -> str:
            # The same normalization `matches_path_prefix` applies: forward
            # slashes, trailing separator trimmed. Empty after trimming (""
            # or "/") matches no path, so it is rejected as a rule below.
            return prefix.replace("\\", "/").rstrip("/")

        for rule in rules:
            if not isinstance(rule, (list, tuple)) or len(rule) != 2:
                raise ValueError(
                    f"malformed rule {rule!r}: expected (from_prefix, forbidden_prefix)"
                )
            from_prefix, forbidden_prefix = rule
            if not isinstance(from_prefix, str) or not isinstance(forbidden_prefix, str):
                raise ValueError(f"malformed rule {rule!r}: prefixes must be strings")
            if not _norm(from_prefix) or not _norm(forbidden_prefix):
                raise ValueError(
                    f"malformed rule {from_prefix!r} -> {forbidden_prefix!r}: "
                    "a prefix must be non-empty (an empty one matches nothing)"
                )
            if _norm(from_prefix) == _norm(forbidden_prefix):
                raise ValueError(
                    f"malformed rule {from_prefix!r} -> {forbidden_prefix!r}: "
                    "from and forbidden prefixes are the same layer"
                )

        def _under_prefix(path: str | None, prefix: str) -> bool:
            return matches_path_prefix(path, include=[prefix], exclude=None)

        self._con.create_function("cpg_under_prefix", 2, _under_prefix, deterministic=True)
        kind_ph = ",".join("?" * len(edge_kinds))
        violations: list[dict[str, str | int | None]] = []
        for from_prefix, forbidden_prefix in rules:
            rows = self._con.execute(
                f"""
                SELECT e.kind, s_src.symbol, s_dst.symbol, f_edge.path, e.line
                FROM edges e
                JOIN symbols s_src ON s_src.id = e.src_id
                JOIN symbols s_dst ON s_dst.id = e.dst_id
                JOIN files f_src ON f_src.id = s_src.file_id
                JOIN files f_dst ON f_dst.id = s_dst.file_id
                LEFT JOIN files f_edge ON f_edge.id = e.file_id
                WHERE e.kind IN ({kind_ph})
                  AND cpg_under_prefix(f_src.path, ?)
                  AND cpg_under_prefix(f_dst.path, ?)
                ORDER BY f_edge.path, e.line, s_src.symbol
                """,
                (*edge_kinds, from_prefix, forbidden_prefix),
            ).fetchall()
            rule_label = f"{from_prefix} -> {forbidden_prefix}"
            violations.extend(
                {
                    "kind": kind,
                    "src": src,
                    "dst": dst,
                    "file": site_path,
                    "line": site_line,
                    "rule": rule_label,
                }
                for kind, src, dst, site_path, site_line in rows
            )
        return violations[:limit], len(violations)

    def api_surface(
        self,
        module_prefix: str,
        limit: int = 40,
        exclude_tests: bool = False,
    ) -> tuple[list[dict[str, str | int | None]], int, bool]:
        """Definitions under `module_prefix` that are used from *outside* it —
        the **actually-used** external surface of a module, as opposed to what
        is merely declared with some visibility keyword (SCIP encodes no C++
        visibility, so "used from outside" is the observable fact; the
        facts-not-judgments rule makes it the right one to report).

        Two use sources, one boundary predicate per use: the *used* symbol's
        own definition file (`symbols.file_id`) must be under `module_prefix`,
        and the *use site* file must NOT be — for `calls` edges that site is
        `edges.file_id` (where the call happened), for references `refs.file_id`
        (where the occurrence lives). Counts are kept **separate** per symbol
        (`external_calls` / `external_refs`, the same per-column detail `stats`
        gives for symbols/edges/refs) and ranked by their **sum** descending —
        so the headline ordering is one number while the detail never collapses
        into a blob.

        References are the `--references` location index (`references_of`'s
        data): when the store was built without it, the surface is call sites
        only — the third return element is then `False` so callers can say so
        (a bare `external_refs == 0` everywhere would read as "never referenced
        outside", which is not known). Reference *attribution* is not needed:
        `refs.file_id` is already the exact use-site file.

        `exclude_tests` drops a use when *either* side of the boundary is a
        test file — the use site, or the used symbol's own definition file
        (a test-defined helper inside the module is not production surface;
        `hotspots`' either-endpoint shape). Prefix membership is
        `matches_path_prefix` registered as `cpg_under_prefix` (the
        `boundary_violations` pattern), i.e. a path-segment boundary: `"mod"`
        matches `mod/util.cpp`, never `mods/util.cpp`. A symbol with no
        recorded definition file belongs to no module and can never be listed.

        Aggregated entirely in SQL (`GROUP BY` + `ORDER BY` + slice) over the
        `edges`/`refs` indexes — the `hotspots` discipline: id-space until the
        final rows are resolved to symbol strings. Returns `(ranked, total,
        has_refs_data)`: `ranked` is at most `limit` dicts (`{"symbol",
        "file", "line", "external_calls", "external_refs"}`), `total` the
        count of distinct symbols with at least one external use, and
        `has_refs_data` whether reference uses were counted at all.

        Raises ValueError on an empty `module_prefix` (which would match
        nothing, reading as "empty surface") or a negative `limit`.
        """
        prefix = module_prefix.replace("\\", "/").rstrip("/")
        if not prefix:
            raise ValueError(
                "api_surface needs a non-empty module prefix (an empty one matches nothing)"
            )
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")

        has_refs = self.meta().get("has_references") == "true"

        def _under_prefix(path: str | None, prefix: str) -> bool:
            return matches_path_prefix(path, include=[prefix], exclude=None)

        self._con.create_function("cpg_under_prefix", 2, _under_prefix, deterministic=True)
        test_clause = ""
        if exclude_tests:
            self._con.create_function("cpg_is_test_file", 1, is_test_file, deterministic=True)
            test_clause = (
                "AND NOT cpg_is_test_file(f_own.path) AND NOT cpg_is_test_file(f_use.path)"
            )
        # One UNION ALL row per use (call site or reference site), tagged with
        # its kind so a single GROUP BY splits the counts per symbol — the
        # same per-role UNION ALL shape `hotspots` uses for its edges ranking.
        refs_union = (
            "UNION ALL SELECT r.symbol_id, r.file_id, 'ref' FROM refs r" if has_refs else ""
        )
        rows = self._con.execute(
            f"""
            SELECT s.symbol, f_own.path, s.line,
                   SUM(use_kind = 'call') AS external_calls,
                   SUM(use_kind = 'ref') AS external_refs
            FROM (
                SELECT e.dst_id AS sym_id, e.file_id AS use_file_id, 'call' AS use_kind
                FROM edges e WHERE e.kind = 'calls'
                {refs_union}
            ) uses
            JOIN symbols s ON s.id = uses.sym_id
            JOIN files f_own ON f_own.id = s.file_id
            LEFT JOIN files f_use ON f_use.id = uses.use_file_id
            WHERE cpg_under_prefix(f_own.path, ?)
              AND NOT cpg_under_prefix(f_use.path, ?)
              {test_clause}
            GROUP BY uses.sym_id
            ORDER BY (external_calls + external_refs) DESC, s.symbol ASC
            """,
            (prefix, prefix),
        ).fetchall()
        ranked = [
            {
                "symbol": symbol,
                "file": file,
                "line": line,
                "external_calls": calls,
                "external_refs": refs,
            }
            for symbol, file, line, calls, refs in rows
        ]
        return ranked[:limit], len(ranked), has_refs

    def outline(self, file: str, limit: int = 200) -> tuple[list[Node], int]:
        """Every symbol *defined* in `file`, sorted by definition line — the
        file's outline, "what's in this file?" answered without reading it.

        `file` is matched exactly against the store's recorded (relative)
        path — never a prefix — so an unindexed or misspelled path returns
        `([], 0)` (the surfaces above this add a note; a bare zero would read
        as "empty file"). Backslashes are normalized to `/` first (mirroring
        `filters.matches_path_prefix`), so a path given either separator
        convention matches a graph indexed with the other. Returns
        `(nodes, total)` in the usual bounded-output shape: `nodes` is at most
        `limit`, `total` the full count, so an outline of a huge generated file
        can't flood a reply.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        file = file.replace("\\", "/")
        rows = self._con.execute(
            """
            SELECT s.symbol, s.display_name, f.path, s.line
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE f.path = ?
            ORDER BY s.line, s.symbol
            """,
            (file,),
        ).fetchall()
        nodes = [Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3]) for r in rows]
        return nodes[:limit], len(nodes)

    def class_members(self, class_symbol: str, limit: int = 200) -> tuple[list[Node], int] | None:
        """Every *direct* member declared on `class_symbol` (a class/struct/enum).

        A member's SCIP symbol string starts with the class's own symbol
        string, which ends in `#` (the type descriptor, `is_type_symbol`):
        `mongo/Foo#` prefixes the method `mongo/Foo#parse(a1).` and the field
        `mongo/Foo#count.` but never `mongo/FooBar#x.` (after `Foo` comes `B`,
        not `#`) — the container nesting the SCIP grammar already encodes, so
        no new symbol parsing. The class itself is excluded (it equals the
        prefix, doesn't extend it).

        "Direct" matters because a NESTED class's members also share the outer
        class's symbol as a *string* prefix: `mongo/Foo#Inner#method().` starts
        with `mongo/Foo#` too, even though it's declared on `Foo::Inner`, not on
        `Foo`. `_is_direct_member` (see `builder.py`) tells the two apart using
        the SCIP descriptor grammar's own terminators, so `class_members(Foo)`
        excludes `Foo::Inner`'s (and any deeper nesting's) members. A nested
        type's own symbol — `mongo/Foo#Inner#` — IS one of `Foo`'s direct
        members (one descriptor, `Inner#`); only what's declared *inside* it is
        excluded. So for `class Foo { class Inner { class Innermost { void m();
        }; }; };`: `class_members(Foo)` lists `Foo::Inner`'s own type symbol,
        but neither `Inner`'s nor `Innermost`'s members; `class_members(Foo::Inner)`
        lists `Innermost`'s own type symbol and `Inner`'s direct members, but not
        `Innermost`'s members.

        Returns `None` when `class_symbol` is unknown or not a type — bad
        input, distinct from the `([], 0)` that is a valid answer for a
        memberless type. `substr(symbol, 1, ?) = ?` scans `symbols` (a
        computed prefix can't use `ix_sym`), the same trade `find`'s substring
        search makes for a rare interactive lookup; the direct-vs-nested
        filter runs after, in Python, since it isn't expressible as a clean
        SQLite string comparison.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")
        if not is_type_symbol(class_symbol) or self._symbol_id(class_symbol) is None:
            return None
        prefix_len = len(class_symbol)
        rows = self._con.execute(
            """
            SELECT s.symbol, s.display_name, f.path, s.line
            FROM symbols s LEFT JOIN files f ON f.id = s.file_id
            WHERE substr(s.symbol, 1, ?) = ? AND s.symbol <> ?
            ORDER BY f.path, s.line, s.symbol
            """,
            (prefix_len, class_symbol, class_symbol),
        ).fetchall()
        members = [
            Node(symbol=r[0], display_name=r[1] or "", file=r[2], line=r[3])
            for r in rows
            if _is_direct_member(r[0][prefix_len:])
        ]
        return members[:limit], len(members)

    def strongly_connected_components(
        self,
        limit: int = 40,
        exclude_tests: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> tuple[list[list[str]], int]:
        """The cycles of the call graph: strongly-connected components of the
        `calls` subgraph with more than one member — maximal sets of symbols
        that can all reach each other, the exact primitive behind "circular
        dependencies", stated as a graph fact, never a verdict (mutual
        recursion is often completely legitimate — a visitor pattern, a
        recursive-descent parser's mutually-recursive rules; the surfaces
        above this carry that caveat as a standing note).

        Unlike the SQL-aggregated ranking tools this is a whole-graph
        traversal: Tarjan's algorithm (`_tarjan_sccs`) over the `calls`
        edges' id-space — all `(src_id, dst_id)` pairs fetched in one scan,
        adjacency walked as integers, symbol strings resolved only for the
        components actually returned (the same "hot topology all-integer,
        cold payload materialized late" discipline as `hotspots`).

        `exclude_tests`/`include_paths`/`exclude_paths` filter the *output*,
        not the edges Tarjan sees: a cycle that genuinely involves test-only
        or excluded-path symbols is still a real cycle in the compiled binary,
        and dropping its edges first could split or hide it. A component is
        dropped only when *every* member's own definition file is filtered
        out, and a reported component always lists all its members —
        redacting the filtered ones would misrepresent the actual compiled
        dependency. Singletons (size 1) are never reported: direct
        self-recursion is a degenerate one-node cycle, out of this tool's
        scope by spec (components of size > 1).

        Components are sorted biggest first (ties by the first member's
        symbol, for determinism), members by definition `file:line` then
        symbol. Returns `(components, total)`: at most `limit` components
        (never a partial one), `total` the full post-filter count. Raises
        ValueError on a negative `limit`.
        """
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")

        adjacency: dict[int, list[int]] = {}
        for src_id, dst_id in self._con.execute(
            "SELECT src_id, dst_id FROM edges WHERE kind = 'calls'"
        ).fetchall():
            adjacency.setdefault(src_id, []).append(dst_id)
        big = [comp for comp in _tarjan_sccs(adjacency) if len(comp) > 1]
        if not big:
            return [], 0

        # Resolve late: only the members of size>1 components touch the cold
        # payload tables — symbol strings + definition sites, needed for the
        # output filter and the result itself. (Edge ids always have a
        # symbols row: write_sqlite interns both from the same graph.)
        ids = sorted({sid for comp in big for sid in comp})
        info: dict[int, tuple[str, str | None, int | None]] = {}
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for sid, symbol, path, line in self._con.execute(
                f"""
                SELECT s.id, s.symbol, f.path, s.line
                FROM symbols s LEFT JOIN files f ON f.id = s.file_id
                WHERE s.id IN ({placeholders})
                """,
                chunk,
            ):
                info[sid] = (symbol, path, line)

        def _kept(path: str | None) -> bool:
            return not (exclude_tests and is_test_file(path)) and matches_path_prefix(
                path, include=include_paths, exclude=exclude_paths
            )

        reported: list[list[str]] = []
        for comp in big:
            members = [info[sid] for sid in comp]
            if not any(_kept(path) for _symbol, path, _line in members):
                continue
            members.sort(key=lambda m: (m[1] or "", m[2] if m[2] is not None else -1, m[0]))
            reported.append([symbol for symbol, _path, _line in members])
        reported.sort(key=lambda ms: (-len(ms), ms))
        return reported[:limit], len(reported)

    def subgraph(
        self, symbol: str, depth: int = 2, direction: str = "both"
    ) -> tuple[list[Node], list[Edge]]:
        """A viewable neighbourhood around `symbol`, for export/visualization.

        BFS up to `depth` hops over *all* edge kinds (calls/inherits/implements)
        in id-space; `direction` picks which way to walk: ``"out"`` (things the
        node reaches), ``"in"`` (things that reach it), or ``"both"``. Returns
        the visited nodes and the edges *induced* on them (both endpoints
        visited), resolved to `Node`/`Edge`. Unknown symbol → `([], [])`.

        The full graph is far too large to render; a bounded neighbourhood is
        the unit a human or an LLM actually wants to look at.
        """
        start_id = self._symbol_id(symbol)
        if start_id is None:
            return [], []

        visited = {start_id}
        frontier = [start_id]
        d = 0
        while frontier and d < depth:
            next_frontier: list[int] = []
            for node_id in frontier:
                neighbours: list[int] = []
                if direction in ("out", "both"):
                    neighbours += [
                        r[0]
                        for r in self._con.execute(
                            "SELECT dst_id FROM edges WHERE src_id = ?", (node_id,)
                        ).fetchall()
                    ]
                if direction in ("in", "both"):
                    neighbours += [
                        r[0]
                        for r in self._con.execute(
                            "SELECT src_id FROM edges WHERE dst_id = ?", (node_id,)
                        ).fetchall()
                    ]
                for m in neighbours:
                    if m not in visited:
                        visited.add(m)
                        next_frontier.append(m)
            frontier = next_frontier
            d += 1

        placeholders = ",".join("?" * len(visited))
        ids = list(visited)
        nodes = [
            Node(symbol=sym, display_name=name or "", file=path, line=line)
            for sym, name, path, line in self._con.execute(
                f"""
                SELECT s.symbol, s.display_name, f.path, s.line
                FROM symbols s
                LEFT JOIN files f ON f.id = s.file_id
                WHERE s.id IN ({placeholders})
                """,
                ids,
            ).fetchall()
        ]
        edges = [
            Edge(kind=kind, src=src, dst=dst, file=path, line=line)
            for kind, src, dst, path, line in self._con.execute(
                f"""
                SELECT e.kind, s.symbol, d.symbol, f.path, e.line
                FROM edges e
                JOIN symbols s ON s.id = e.src_id
                JOIN symbols d ON d.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id
                WHERE e.src_id IN ({placeholders}) AND e.dst_id IN ({placeholders})
                """,
                ids + ids,
            ).fetchall()
        ]
        return nodes, edges

    # --- incremental update ------------------------------------------------

    def _bulk_intern(self, table: str, col: str, values: Iterable[str]) -> dict[str, int]:
        """Map each value in `values` to an integer id in `table.col`, inserting
        rows for values not already present. Resolves existing ids with chunked
        `IN (...)` lookups and assigns new ids in one `executemany` — so a large
        partial re-index is a handful of bulk statements, not a per-row probe.

        `table`/`col` are internal literals, never user input.
        """
        values = list(dict.fromkeys(values))  # dedup, keep order
        mapping: dict[str, int] = {}
        for start in range(0, len(values), _ID_CHUNK):
            chunk = values[start : start + _ID_CHUNK]
            ph = ",".join("?" * len(chunk))
            for vid, val in self._con.execute(
                f"SELECT id, {col} FROM {table} WHERE {col} IN ({ph})", chunk
            ):
                mapping[val] = vid
        missing = [v for v in values if v not in mapping]
        if missing:
            (max_id,) = self._con.execute(f"SELECT MAX(id) FROM {table}").fetchone()
            next_id = 0 if max_id is None else max_id + 1
            new_rows = []
            for v in missing:
                mapping[v] = next_id
                new_rows.append((next_id, v))
                next_id += 1
            self._con.executemany(f"INSERT INTO {table}(id, {col}) VALUES (?, ?)", new_rows)
        return mapping

    def _file_ids(self, paths: Iterable[str]) -> list[int]:
        ids: list[int] = []
        for p in paths:
            row = self._con.execute("SELECT id FROM files WHERE path = ?", (p,)).fetchone()
            if row is not None:
                ids.append(row[0])
        return ids

    def apply_update(
        self,
        partial: Graph,
        changed_files: Iterable[str],
        *,
        meta: dict[str, str] | None = None,
    ) -> UpdateStats:
        """Replace the contributions of `changed_files` with those in `partial`.

        Steps, all in one transaction: (1) collect the symbols touched by the
        changed files as GC candidates, (2) delete those files' edges and clear
        the definition site of symbols defined there, (3) re-insert `partial`'s
        nodes + edges (interning any new symbols/files), (4) drop candidate
        symbols now orphaned (no defining site *and* no edge references them) so
        `find` doesn't surface stale symbols, (5) refresh `meta` counts +
        provided provenance.
        """
        con = self._con
        changed_files = list(changed_files)
        with con:  # atomic: commit on success, rollback on error
            # (0) an older (v2) store lacks `end_line`; add it on demand — the
            # same ALTER pattern `enrich_references` uses for `refs.enclosing_id`
            # — so the re-insert below can write body extents. The store is now
            # v3-shaped, so stamp it.
            cols = {row[1] for row in con.execute("PRAGMA table_info(symbols)")}
            if "end_line" not in cols:
                con.execute("ALTER TABLE symbols ADD COLUMN end_line INTEGER")
                con.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            changed_ids = self._file_ids(changed_files)

            # (1) candidate symbols for GC: endpoints of the edges we're about
            # to delete, plus symbols whose definition lives in a changed file.
            gc_candidates: set[int] = set()
            edges_removed = 0
            for start in range(0, len(changed_ids), _ID_CHUNK):
                chunk = changed_ids[start : start + _ID_CHUNK]
                ph = ",".join("?" * len(chunk))
                for src_id, dst_id in con.execute(
                    f"SELECT src_id, dst_id FROM edges WHERE file_id IN ({ph})", chunk
                ):
                    gc_candidates.add(src_id)
                    gc_candidates.add(dst_id)
                for (sid,) in con.execute(f"SELECT id FROM symbols WHERE file_id IN ({ph})", chunk):
                    gc_candidates.add(sid)
                for (sid,) in con.execute(
                    f"SELECT symbol_id FROM refs WHERE file_id IN ({ph})", chunk
                ):
                    gc_candidates.add(sid)

                # (2) delete the changed files' edges + refs; clear defs sited there.
                cur = con.execute(f"DELETE FROM edges WHERE file_id IN ({ph})", chunk)
                edges_removed += cur.rowcount
                con.execute(f"DELETE FROM refs WHERE file_id IN ({ph})", chunk)
                con.execute(
                    f"UPDATE symbols SET file_id = NULL, line = NULL, end_line = NULL "
                    f"WHERE file_id IN ({ph})",
                    chunk,
                )

            # (3) re-insert the partial graph's nodes + edges, in bulk. Every
            # edge endpoint is also a node (Graph.add_edge adds both), so
            # interning the nodes covers all symbols the edges reference.
            sym_id = self._bulk_intern("symbols", "symbol", partial.nodes)
            partial_files = [n.file for n in partial.nodes.values() if n.file]
            partial_files += [e.file for e in partial.edges if e.file]
            partial_files += [r.file for r in partial.references if r.file]
            file_id = self._bulk_intern("files", "path", partial_files)

            con.executemany(
                "UPDATE symbols SET "
                "display_name = COALESCE(NULLIF(?, ''), display_name), "
                "file_id = COALESCE(?, file_id), "
                "line = COALESCE(?, line), "
                # `end_line` must travel with file_id/line, not COALESCE
                # independently: a fresh definition site (file_id is not NULL
                # here) always replaces end_line too — even with NULL, when
                # this occurrence turned out bodyless — instead of leaking a
                # stale extent from a previous, different definition site.
                # Only when the partial has *no* fresh site (file_id param is
                # NULL, e.g. a symbol only seen as an edge endpoint) does the
                # old end_line survive untouched.
                "end_line = CASE WHEN ? IS NOT NULL THEN ? ELSE end_line END "
                "WHERE id = ?",
                [
                    (
                        n.display_name,
                        file_id.get(n.file) if n.file else None,
                        n.line,
                        file_id.get(n.file) if n.file else None,
                        n.end_line,
                        sym_id[n.symbol],
                    )
                    for n in partial.nodes.values()
                ],
            )
            con.executemany(
                "INSERT INTO edges(kind, src_id, dst_id, file_id, line) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        e.kind,
                        sym_id[e.src],
                        sym_id[e.dst],
                        file_id.get(e.file) if e.file else None,
                        e.line,
                    )
                    for e in partial.edges
                ],
            )
            con.executemany(
                "INSERT INTO refs(symbol_id, file_id, line, enclosing_id) VALUES (?, ?, ?, ?)",
                [
                    (
                        sym_id[r.symbol],
                        file_id.get(r.file) if r.file else None,
                        r.line,
                        sym_id.get(r.enclosing_symbol) if r.enclosing_symbol else None,
                    )
                    for r in partial.references
                ],
            )

            # (4) GC candidates now orphaned (undefined, and unreferenced by any
            # edge or ref location) so `find` doesn't surface stale symbols.
            symbols_removed = 0
            for sid in gc_candidates:
                row = con.execute("SELECT file_id FROM symbols WHERE id = ?", (sid,)).fetchone()
                if row is None or row[0] is not None:
                    continue  # already gone, or still defined somewhere
                referenced = con.execute(
                    "SELECT 1 FROM edges WHERE src_id = ? OR dst_id = ? LIMIT 1",
                    (sid, sid),
                ).fetchone()
                if referenced is None:
                    referenced = con.execute(
                        "SELECT 1 FROM refs WHERE symbol_id = ? LIMIT 1", (sid,)
                    ).fetchone()
                if referenced is None:
                    con.execute("DELETE FROM symbols WHERE id = ?", (sid,))
                    symbols_removed += 1

            # (5) refresh meta: provided provenance + recomputed counts. A
            # partial carrying body extents (#504 re-index) flips the
            # enclosing-range gate on; nothing ever flips it off.
            node_count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            edge_count = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            ref_count = con.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            all_meta = dict(meta or {})
            all_meta["node_count"] = str(node_count)
            all_meta["edge_count"] = str(edge_count)
            if any(n.end_line is not None for n in partial.nodes.values()):
                all_meta.setdefault("has_enclosing_ranges", "true")
            if ref_count:
                all_meta["ref_count"] = str(ref_count)
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", all_meta.items())

        return UpdateStats(
            files_changed=len(changed_files),
            edges_removed=edges_removed,
            edges_added=len(partial.edges),
            symbols_removed=symbols_removed,
            node_count=node_count,
            edge_count=edge_count,
        )
