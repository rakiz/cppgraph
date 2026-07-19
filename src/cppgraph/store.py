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

import sqlite3
import subprocess
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import TYPE_CHECKING

from cppgraph.builder import build_graph
from cppgraph.model import Edge, Graph, Node, Reference

if TYPE_CHECKING:
    from cppgraph.proto import scip_pb2

_SCHEMA = """
CREATE TABLE files (
    id   INTEGER PRIMARY KEY,
    path TEXT
);
CREATE TABLE symbols (
    id           INTEGER PRIMARY KEY,
    symbol       TEXT NOT NULL,
    display_name TEXT,
    file_id      INTEGER,
    line         INTEGER
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

# On-disk store format version, stamped into `meta.schema_version` at build.
# Bump when the schema changes incompatibly (new/renamed tables or columns);
# then a migration can branch on the stored value. A store with no
# `schema_version` predates versioning (treated as the oldest, still readable).
# `GraphStore` refuses to open a store whose version is *newer* than this — an
# old binary must not silently misread a format it doesn't understand.
SCHEMA_VERSION = 2


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


def changed_files_since(root: str | Path, base_commit: str) -> tuple[list[str], list[str]] | None:
    """Files that differ in `root`'s working tree from `base_commit`.

    Returns `(changed, deleted)` relative paths, or `None` if `root` isn't a
    git checkout / git is unavailable. Diffs the working tree (not just HEAD)
    against the commit, so uncommitted edits count too — this is exactly the
    changed-file set an incremental `cppgraph update` would consume, mirroring
    `reindex.sh --update`.
    """
    root = Path(root)
    changed = _git(root, "diff", "--name-only", "--diff-filter=d", base_commit, "--")
    deleted = _git(root, "diff", "--name-only", "--diff-filter=D", base_commit, "--")
    if changed is None or deleted is None:
        return None
    return (
        [ln for ln in changed.splitlines() if ln.strip()],
        [ln for ln in deleted.splitlines() if ln.strip()],
    )


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
) -> dict[str, str]:
    """Provenance to record in the store's `meta` table: *what* was indexed.

    Copies what the SCIP index already carries (`project_root`, the indexing
    tool + version) so the store is self-describing even after the `.scip` is
    discarded, and captures the **source commit** — the anchor for a future
    incremental `cppgraph update`.

    The commit is best-effort: `source_commit` (e.g. passed by `reindex.sh`,
    captured at *index* time — the accurate moment) wins; otherwise it's
    auto-detected via `git rev-parse HEAD` on `project_root` at build time,
    which is exact when index→build run back-to-back. If `project_root` isn't a
    git checkout, no commit is recorded (no error).
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
            sym_rows.append((i, node.symbol, node.display_name, file_id(node.file), node.line))

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
        if graph.references:
            all_meta.setdefault("has_references", "true")
            all_meta.setdefault("ref_count", str(len(graph.references)))
            if attributed_refs:
                all_meta.setdefault("has_attributed_refs", "true")
                all_meta.setdefault("attributed_ref_count", str(attributed_refs))

        con.executemany(
            "INSERT INTO files VALUES (?, ?)", [(fid, p) for p, fid in file_ids.items()]
        )
        con.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?)", sym_rows)
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

        before = con.total_changes
        # `IS` matches NULL file_id/line the same way the rows were stored.
        con.executemany(
            "UPDATE refs SET enclosing_id = ? WHERE symbol_id = ? AND file_id IS ? AND line IS ?",
            updates,
        )
        attributed = con.total_changes - before

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
                    f"UPDATE symbols SET file_id = NULL, line = NULL WHERE file_id IN ({ph})",
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
                "line = COALESCE(?, line) "
                "WHERE id = ?",
                [
                    (
                        n.display_name,
                        file_id.get(n.file) if n.file else None,
                        n.line,
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

            # (5) refresh meta: provided provenance + recomputed counts.
            node_count = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            edge_count = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            ref_count = con.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            all_meta = dict(meta or {})
            all_meta["node_count"] = str(node_count)
            all_meta["edge_count"] = str(edge_count)
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
