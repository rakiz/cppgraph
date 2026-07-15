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
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import TYPE_CHECKING

from cppgraph.model import Edge, Graph, Node

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
"""

# SQLite caps host-variable count per statement (default 999 historically).
# Chunk `IN (...)` id lists well under that.
_ID_CHUNK = 900


def _git(root: Path, *args: str) -> str | None:
    """Best-effort `git -C root <args>`; None if git is missing, times out, or
    the command fails (e.g. root isn't a repo). Never raises — provenance is
    optional and must not break a build."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _project_root_path(project_root_uri: str) -> Path | None:
    """The local filesystem path behind a SCIP `Metadata.project_root`, which is
    a `file://` URI."""
    if project_root_uri.startswith("file://"):
        return Path(project_root_uri[len("file://"):])
    if project_root_uri:
        return Path(project_root_uri)
    return None


def build_provenance(
    index: scip_pb2.Index,
    *,
    source_commit: str | None = None,
    source_dirty: bool | None = None,
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

    commit = source_commit
    dirty = source_dirty
    root = _project_root_path(md.project_root)
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

    meta["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        meta["cppgraph_version"] = importlib_metadata.version("cppgraph")
    except importlib_metadata.PackageNotFoundError:
        pass
    return meta


def write_sqlite(
    graph: Graph, path: str | Path, *, meta: dict[str, str] | None = None
) -> None:
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
            (e.kind, sym_ids[e.src], sym_ids[e.dst], file_id(e.file), e.line)
            for e in graph.edges
        ]

        all_meta = dict(meta or {})
        all_meta.setdefault("node_count", str(len(graph.nodes)))
        all_meta.setdefault("edge_count", str(len(graph.edges)))

        con.executemany("INSERT INTO files VALUES (?, ?)",
                        [(fid, p) for p, fid in file_ids.items()])
        con.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?)", sym_rows)
        con.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", edge_rows)
        con.executemany("INSERT INTO meta VALUES (?, ?)", all_meta.items())
        con.executescript(_INDEXES)
        con.commit()
    finally:
        con.close()


class GraphStore:
    """Read-only query handle over a SQLite store written by `write_sqlite`.

    Mirrors the query surface of the in-memory `Graph` (callers_of, callees_of,
    find, shortest_call_path, impact) so the CLI is agnostic to the backend.
    """

    def __init__(self, path: str | Path) -> None:
        self._con = sqlite3.connect(Path(path))

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- id resolution -----------------------------------------------------

    def _symbol_id(self, symbol: str) -> int | None:
        row = self._con.execute(
            "SELECT id FROM symbols WHERE symbol = ?", (symbol,)
        ).fetchone()
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

    def _symbols_for_ids(self, ids: set[int]) -> dict[int, str]:
        out: dict[int, str] = {}
        ids_list = list(ids)
        for start in range(0, len(ids_list), _ID_CHUNK):
            chunk = ids_list[start:start + _ID_CHUNK]
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

    def find(self, query: str) -> list[Node]:
        """Nodes whose symbol or display name *contains* `query`, case-sensitive.

        `instr(col, ?) > 0` is a case-sensitive substring test matching the
        in-memory `Graph.find`'s Python `in` — unlike `LIKE`, which SQLite runs
        case-insensitively for ASCII. This scans the symbols table (a leading
        wildcard can't use `ix_sym`), which is fine: `find` is a rare
        interactive lookup, not a hot-path traversal.
        """
        rows = self._con.execute(
            """
            SELECT s.symbol, s.display_name, f.path, s.line
            FROM symbols s LEFT JOIN files f ON f.id = s.file_id
            WHERE instr(s.symbol, ?) > 0 OR instr(s.display_name, ?) > 0
            """,
            (query, query),
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
                edge = Edge(kind="calls", src=node_symbol, dst=e_dst_symbol,
                            file=path_str, line=line)
                if e_dst_id == dst_id:
                    return path + [edge]
                if e_dst_id not in visited:
                    visited.add(e_dst_id)
                    queue.append((e_dst_id, e_dst_symbol, path + [edge]))
        return None

    def impact(self, symbol: str, max_depth: int | None = None) -> set[str]:
        """Symbols that transitively call `symbol` (reverse blast-radius).

        Reverse BFS over `ix_dst`; `max_depth` bounds the backward hops
        (`None` = unbounded). Walks in id-space, resolving to symbol strings
        only for the final result set.
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
                    "SELECT src_id FROM edges WHERE kind = 'calls' AND dst_id = ?",
                    (node_id,),
                ).fetchall():
                    if caller_id not in visited:
                        visited.add(caller_id)
                        next_frontier.append(caller_id)
            frontier = next_frontier
            depth += 1

        visited.discard(start_id)
        return set(self._symbols_for_ids(visited).values())
