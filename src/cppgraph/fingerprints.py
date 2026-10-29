"""The `cppgraph fingerprints` contract surface (masora_integration.md §2).

One spawned command, one JSON document on stdout — the replacement for
masora's direct reads of this project's SQLite store (`.cppgraph/*.graph.db`),
which is a declared-hard coupling (masora_integration.md §1): the store schema
is NOT a public contract, so masora must consume this surface instead.

Contract (§2, versioned by `contract_version`; changes land as a new major,
never in place):

    cppgraph fingerprints --repo <path> [--symbol <scip-string> ...]

The document carries the store's provenance (`graph_source_commit`,
`symbol_format`), the checkout's HEAD and whether the indexed commit is an
ancestor-or-equal of it (`graph_behind_head` — masora keeps the refusal
policy, cppgraph supplies the fact), and per queried symbol: `found`, the
sha256 of the normalized definition-range source (`definition_fingerprint`)
and the `edge_snapshot` (own edge-set hash plus each 1-hop `calls`
neighbour's). Matching is VERBATIM on exact SCIP identity strings — no fuzzy
resolution in v1; an identity that matches nothing is `found: false`, never a
guessed fingerprint.

Fail-closed: no usable graph under `<repo>/.cppgraph/`, a missing/non-git
repo, an unknown `source_commit`, or bad arguments all exit non-zero with
NOTHING on stdout — a caller treats any non-zero exit or timeout as
provider-unavailable. Timeouts are the caller's budget: masora's expected
budget is 2 s wall-clock (documented in the CLI help).

Algorithm parity: `definition_fingerprint` and `edge_snapshot` are
byte-for-byte what masora's `masora/providers.py` computes today over the
same store schema (v5 at parity time; now v6 — the `log_ids` table does not
touch `symbols`) (`definition_fingerprint` / `edge_snapshot_of` /
`_edge_set_hash`, and `masora/index.py:normalize_source`); the parity is
pinned test-side (tests/test_fingerprints.py transcribes those functions).
Read-only throughout: this module only ever SELECTs and spawns
`git rev-parse` / `git merge-base`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from cppgraph.store import SCHEMA_VERSION

CONTRACT_VERSION = 1
# Legacy stores predate the `symbol_format` meta row — always format 1 (the
# same reading masora's gate applies).
_LEGACY_SYMBOL_FORMAT = 1
_GIT_TIMEOUT_S = 5


class FingerprintsError(RuntimeError):
    """The document cannot be produced honestly (§3 fail-closed): no usable
    graph, missing/non-git repo, unknown source commit, an unusable meta row.
    The CLI turns it into a stderr message + non-zero exit; nothing goes to
    stdout."""


def normalize_source(text: str) -> str:
    """Whitespace/comment-normalized content — byte-for-byte
    `masora/masora/index.py:normalize_source` (the parity the §2 contract
    pins). Terminated `/* … */` block comments (inline or spanning lines) are
    removed, then blank lines and full-line `//`/`#` comments are dropped and
    whitespace runs collapsed; an unterminated `/*` is content, not a
    comment."""
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    lines = []
    for raw in without_blocks.splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith(("//", "#")):
            continue
        lines.append(line)
    return "\n".join(lines)


def find_repo_graph(repo: Path) -> Path | None:
    """Newest `*.graph.db` under `<repo>/.cppgraph/` — the store masora's
    `providers.py:discover_graph_db` reads today (the repo directory itself is
    the anchor, never a parent walk, so a foreign graph cannot answer for this
    checkout)."""
    cpg = repo / ".cppgraph"
    if not cpg.is_dir():
        return None
    graphs = sorted(cpg.glob("*.graph.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return graphs[0] if graphs else None


def _git(repo: Path, *args: str) -> str | None:
    """Best-effort `git -C repo <args>`; None when git is missing, fails, or
    times out (the provenance discipline `store._git` applies to builds)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_head(repo: Path) -> str | None:
    """`git rev-parse HEAD`; None when `repo` is not a checkout. A plain
    rev-parse also succeeds for a SUBDIRECTORY of a parent checkout — which
    would report the parent's HEAD for the wrong repo — so the caller must
    additionally require `git_toplevel(repo) == repo` (see
    `fingerprints_document`)."""
    return _git(repo, "rev-parse", "HEAD")


def git_toplevel(repo: Path) -> str | None:
    """`git rev-parse --show-toplevel`: the checkout `repo` actually belongs
    to (None when git fails or `repo` is not inside one)."""
    return _git(repo, "rev-parse", "--show-toplevel")


def _is_ancestor_or_equal(repo: Path, commit: str, head: str) -> bool | None:
    """`git merge-base --is-ancestor commit HEAD`: exit 0 = ancestor-or-equal,
    1 = not, anything else = the fact cannot be established (e.g. the commit
    is unknown to this checkout's git — a graph from elsewhere). None means
    unknown: the caller fails closed rather than guessing."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, head],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _open_store(db: Path) -> tuple[sqlite3.Connection, dict[str, str]]:
    """Read-only connection + meta, gated like the rest of the read surface:
    a store newer than this binary understands is refused (the same policy as
    `GraphStore._check_schema_compat` — an old reader must not misread a
    format it doesn't know). Tables the queries below need must exist."""
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise FingerprintsError(f"{db} cannot be opened read-only: {e}") from e
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        raw = meta.get("schema_version")
        if raw is not None:
            version = int(raw)
            if version > SCHEMA_VERSION:
                raise FingerprintsError(
                    f"{db} store schema v{version} is newer than this cppgraph "
                    f"(supports v{SCHEMA_VERSION}); upgrade cppgraph or rebuild the graph"
                )
        columns = frozenset(row[1] for row in conn.execute("PRAGMA table_info(symbols)"))
        if not {"symbol", "file_id", "line"} <= columns:
            raise FingerprintsError(f"{db} is not a usable cppgraph store (symbols table)")
        conn.execute("SELECT kind, src_id, dst_id FROM edges LIMIT 1").fetchone()
    except FingerprintsError:
        conn.close()
        raise
    except (sqlite3.Error, ValueError) as e:
        conn.close()
        raise FingerprintsError(f"{db} is not a usable cppgraph store: {e}") from e
    return conn, meta


def _definition_fingerprint(conn: sqlite3.Connection, root: Path, identity: str) -> str | None:
    """sha256 of the normalized definition-range source — byte-for-byte
    `masora/masora/providers.py:definition_fingerprint`: the range is
    `symbols.line`..`symbols.end_line` inclusive (0-indexed); a store without
    body extents hashes the definition line alone. None (= `found: false`,
    never a guess) when the symbol is absent, its file is missing/escapes the
    checkout, or the range is out of bounds."""
    columns = frozenset(row[1] for row in conn.execute("PRAGMA table_info(symbols)"))
    end_sel = "s.end_line" if "end_line" in columns else "NULL"
    row = conn.execute(
        f"SELECT s.line, {end_sel}, f.path"
        " FROM symbols s LEFT JOIN files f ON f.id = s.file_id"
        " WHERE s.symbol = ?",
        (identity,),
    ).fetchone()
    if row is None:
        return None
    line, end_line, rel = row
    if line is None or rel is None:
        return None
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    end = end_line if end_line is not None else line
    if line < 0 or line >= len(lines) or end < line or end >= len(lines):
        return None
    source = "\n".join(lines[line : end + 1])
    return hashlib.sha256(normalize_source(source).encode("utf-8")).hexdigest()


def _edge_set_hash(conn: sqlite3.Connection, symbol_id: int) -> str:
    """sha256 of the sorted distinct callee SCIP strings from `calls` edges —
    byte-for-byte `masora/masora/providers.py:_edge_set_hash`."""
    rows = conn.execute(
        "SELECT DISTINCT s.symbol FROM edges e JOIN symbols s ON s.id = e.dst_id"
        " WHERE e.kind = 'calls' AND e.src_id = ? ORDER BY s.symbol",
        (symbol_id,),
    ).fetchall()
    return hashlib.sha256("\n".join(r[0] for r in rows).encode("utf-8")).hexdigest()


def _edge_snapshot(conn: sqlite3.Connection, identity: str) -> dict | None:
    """`{edges, neighbours}` — byte-for-byte
    `masora/masora/providers.py:edge_snapshot_of`: the symbol's own edge-set
    hash plus each 1-hop `calls` neighbour (callers ∪ callees, self excluded)
    mapped to that neighbour's edge-set hash. None when the identity matches
    nothing."""
    row = conn.execute("SELECT id FROM symbols WHERE symbol = ?", (identity,)).fetchone()
    if row is None:
        return None
    symbol_id = row[0]
    neighbours: dict[str, str] = {}
    for direction in ("src_id", "dst_id"):
        other = "dst_id" if direction == "src_id" else "src_id"
        for (nid,) in conn.execute(
            f"SELECT DISTINCT {other} FROM edges WHERE kind = 'calls' AND {direction} = ?",
            (symbol_id,),
        ):
            if nid == symbol_id:
                continue
            symbol = conn.execute("SELECT symbol FROM symbols WHERE id = ?", (nid,)).fetchone()
            if symbol is not None and symbol[0] not in neighbours:
                neighbours[symbol[0]] = _edge_set_hash(conn, nid)
    return {
        "edges": _edge_set_hash(conn, symbol_id),
        "neighbours": dict(sorted(neighbours.items())),
    }


def fingerprints_document(db: Path, repo: Path, symbols: list[str] | None) -> dict:
    """The §2 document, pure given a store path + checkout. Raises
    `FingerprintsError` (fail-closed) whenever a required fact cannot be
    established: the repo must be an existing directory AND the root of a git
    checkout (HEAD, an ancestor test and the toplevel identity are pinned
    bool/string fields — no null escape hatch in v1), a store must exist under
    `<repo>/.cppgraph/`, and the store must record a `source_commit`. Every
    sqlite3 failure on the way is wrapped too — nothing escapes as a
    traceback. Symbol matching is verbatim on exact SCIP identity strings."""
    repo = repo.resolve()
    if not repo.is_dir():
        raise FingerprintsError(f"--repo path is not an existing directory: {repo}")
    if not db.is_file():
        raise FingerprintsError(f"no usable graph store: {db}")
    conn, meta = _open_store(db)
    try:
        commit = meta.get("source_commit") or None
        head = git_head(repo)
        toplevel = git_toplevel(repo)
        # A plain rev-parse succeeds from a SUBDIRECTORY of a parent checkout:
        # fail closed rather than report the parent's HEAD (and parent's
        # ancestry) for a repo that is not the checkout root.
        if head is None or toplevel is None or toplevel != str(repo):
            raise FingerprintsError(
                f"{repo} is not a git checkout root (toplevel: {toplevel or 'none'})"
                " — repo_head cannot be established"
            )
        if commit is None:
            raise FingerprintsError(
                f"{db} records no source_commit — graph_source_commit cannot be established"
            )
        behind = _is_ancestor_or_equal(repo, commit, head)
        if behind is None:
            raise FingerprintsError(
                f"cannot establish whether {commit} is an ancestor of HEAD {head} in this checkout"
            )
        raw_format = meta.get("symbol_format")
        if raw_format is None:
            symbol_format = _LEGACY_SYMBOL_FORMAT
        else:
            try:
                symbol_format = int(raw_format)
            except ValueError:
                raise FingerprintsError(
                    f"{db} has an unparsable symbol_format {raw_format!r}"
                ) from None
        entries = []
        for identity in symbols or []:
            fingerprint = _definition_fingerprint(conn, repo, identity)
            if fingerprint is None:
                # Not found (or its definition is unreadable) — no fingerprint
                # is ever guessed; the snapshot would be equally meaningless.
                entries.append({"symbol": identity, "found": False})
                continue
            entries.append(
                {
                    "symbol": identity,
                    "found": True,
                    "definition_fingerprint": fingerprint,
                    "edge_snapshot": _edge_snapshot(conn, identity),
                }
            )
        return {
            "contract_version": CONTRACT_VERSION,
            "repo_root": str(repo),
            "graph_source_commit": commit,
            "repo_head": head,
            "graph_behind_head": behind,
            "symbol_format": symbol_format,
            "symbols": entries,
        }
    except sqlite3.Error as e:
        # Fail-closed means EVERY failure: a store whose shape drifted under
        # the queries (a missing `files` table, a locked/corrupt db) must exit
        # 1 with a message — never a traceback past the contract boundary.
        raise FingerprintsError(f"{db} is not a usable cppgraph store: {e}") from e
    finally:
        conn.close()


def run_cli(repo: str, symbols: list[str] | None) -> int:
    """The `cppgraph fingerprints` entry: the §2 document as the ONLY thing on
    stdout, or a stderr message + exit 1 (fail-closed — a caller treats any
    non-zero exit as provider-unavailable, never a guessed fingerprint)."""
    try:
        resolved = Path(repo)
        db = find_repo_graph(resolved)
        if db is None:
            raise FingerprintsError(
                f"no .cppgraph/*.graph.db found under {resolved} — index the "
                "project with cppgraph first"
            )
        document = fingerprints_document(db, resolved, symbols)
    except FingerprintsError as e:
        print(f"[cppgraph] fingerprints: {e}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2))
    return 0
