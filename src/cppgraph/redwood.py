"""The redwood `log_id_lookup` surface (redwood_integration.md §1, contract v1)
— the cppgraph side of the log→code hop redwood's triage loop needs.

Redwood owns the "texte" hop (log id → file:line over the checkout); cppgraph
owns the "symbole" hop (file:line → enclosing symbol → callers). The missing
graph-side twin is id → symbol, which only cppgraph can answer because it holds
the index: the id→symbol table is extracted at INDEX time (a numeric-literal
pass over the already-scanned compilation, wired through `builder.build_graph`
/ `pipeline.build_store`) into a store table, and this module is the connector
file for everything redwood-facing (the NEXT-SESSION.md connector-file
convention, same shape as `masora.py`): the extraction scanner, the ONE pure
query function both surfaces share, and the CLI entry. `cli.py` /
`mcp_server.py` only wire it.

Extraction pattern (deliberately NARROW — this is a contract, so the pattern is
pinned here and tested):

- The masked source (comments and string/char literals blanked — a `LOG(`
  inside a comment or string is NOT a call, and false positives are only
  acceptable for real `LOG(`-shaped calls) is scanned for
  `\\b(?:MONGO_LOGV2|LOG)\\s*\\(` — word-bounded, so `XLOG(`/`myLOG(`/`_LOG(`
  never match, and the legacy `LOGV2` alias is NOT in the pattern (it is not
  in the contract; widening it is a contract change, not a fix).
- From the open paren, arguments are split at top-level commas (a paren-depth
  scan; nested calls survive), and the 2nd argument must be a PLAIN integer
  literal: `^[0-9]+$` after stripping whitespace. Hex, floats, negatives,
  macro names, expressions — all excluded. A multi-line call is fine (the
  scan crosses newlines); the id is attributed to the line of `LOG(` itself.
- Known limits, stated: C++ raw strings (`R"(...)`) are not modelled — their
  bodies are masked like ordinary literals here, which masks a `LOG(` inside
  them (the safe direction); macro invocations that expand to `LOG(...)` are
  invisible (the scanner sees the source, not the preprocessor); a `#define
  LOG` shim is likewise unmodelled. Occurrences whose enclosing symbol cannot
  be attributed are SKIPPED, never guessed (see below), so the symbol side of
  the table over-reports nothing.

Enclosing-symbol attribution: the same machinery the builder already uses for
call sites — `_attribute_containment` over the document's `enclosing_range`
intervals (#504-built binaries), the innermost definition whose body contains
the call. Degradation (stated and tested): when an occurrence falls in no
interval (or the document has none — a stock binary, or an unattributable
site), the row is SKIPPED, not attributed to a file-level or nearest-preceding
guessed symbol; a skipped occurrence is simply `found: false` at query time.
A source file that cannot be read at build time contributes no rows AND —
together with any other skip — withholds the store's `log_ids_extracted`
completeness marker (see below): extraction is never a build failure, but it
is never silently best-effort either.

Contract v1 (pinned — redwood codes against THIS, golden-file tested like
`masora facts`):

    cppgraph log-id-lookup --ids <int> [<int> ...] --repo <checkout> [--revision <githash>]

    → {"schema": 1, "command": "log-id-lookup", "contract_version": 1,
       "resolved_against": {"graph_source_commit": "...", "repo_head": "...",
                             "head_matches_graph": true, "revision": "...|null"},
       "log_id_table": true,
       "ids": [{"id": 12983100, "found": true, "symbol": "<display name>",
                "scip": "<exact SCIP identity string, as the store carries it>",
                "location": "src/foo.cpp:140",
                "matches": 1,               # present only when > 1 row
                "callers": {"items": [{"symbol", "name", "file", "line"}, ...],
                             "truncated": false, "total": 0}}]}

Deviations from the §1 sketch, pinned here as v1: `schema`/`command` (the
workspace --json envelope), `contract_version` (the contract governance key),
per-id `scip` carries the SCIP identity string EXACTLY as the store carries
it — scheme-prefixed (`cxx …`, `symbols.symbol` verbatim), NOT masora's
`"scip-clang cxx . . …"` ANCHOR format: the store carries no such prefix
(the anchor prefix names the indexer, not SCIP's scheme), cppgraph does not
fabricate identity data it does not store, and a `scip-clang`-prefixed
string fed back to `cppgraph fingerprints` would answer `found: false`
(verbatim matching). A consumer needing a masora anchor derives it
deterministically: `"scip-clang " + <scip>` — see the handoff note in
`masora_integration.md` on masora's exact-identity predicate accepting the
bare `cxx …` form.

`callers` is a bounded OBJECT (items/truncated/total) rather than a flat
list — bounding must be visible (the §1 sketch's "…bounded list…" leaves
the shape to this document) — and `location` is 1-indexed (editor
convention, same as `resolve`).

- Revision guard first-class: `head_matches_graph` is the SAME merge-base
  computation `fingerprints` uses (`git merge-base --is-ancestor
  graph_source_commit HEAD`). `--revision` is ECHOED back, never resolved
  against anything cppgraph doesn't have (no network, no fetch); what redwood
  does with a mismatch is redwood's policy.
- Hard cap: MORE THAN 2 ids per call is an ERROR (exit 1 / an MCP error dict),
  not a truncation — redwood's own budget rule, enforced server-side.
- Fail-closed: no `.cppgraph/*.graph.db` under the repo, a non-git checkout
  root, a store with no recorded `source_commit`, or an unusable store all
  raise `LogIdLookupError` → CLI exit 1 with a stderr message and NOTHING on
  stdout. An unknown id is a per-id `found: false`, never a guessed symbol.
  A pre-v6 store simply has no `log_ids` table → every id answers `found:
  false` with the stated reason and `log_id_table: false` (the same
  optional-table degradation as attributed-refs). The capability gate is
  table presence AND the `log_ids_extracted` meta marker, which means
  "extraction ran to completion over all scanned sources" — every row is
  complete: a FULL build withholds it when any document's source was
  unreadable, `apply_update` clears it when the partial re-extraction could
  not run (e.g. the index's project_root missing on disk — the changed
  files' rows would otherwise be silently lost under a true marker), and
  `enrich_references` never sets it (it never backfills). Either absence
  degrades to the reasoned `found: false`, never a confident false
  negative.
- Bounded: `callers` is capped at `CALLERS_LIMIT` entries with visible
  `truncated`/`total`; the whole answer stays within redwood's ≤1500-token
  budget for ≤ 2 ids.

Read-only throughout: this module only ever SELECTs from the store and spawns
`git rev-parse` / `git merge-base`; it never writes anywhere.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from cppgraph.json_docs import envelope
from cppgraph.store import (
    IncompatibleStoreError,
    SQLiteConnection,
    SqliteError,
    open_log_id_capability,
)

CONTRACT_VERSION = 1
# Redwood's own budget rule (redwood_integration.md §2.5), enforced here:
# more ids per call is an error, not a truncation.
MAX_IDS = 2
# The `callers` cap: bounded with visible truncation, keeping the whole
# ≤2-id answer inside redwood's ≤~1500-token budget.
CALLERS_LIMIT = 10
_GIT_TIMEOUT_S = 5
# The narrow LOG-call pattern (see module docstring). `MONGO_LOGV2` first so
# alternation prefers it; word boundaries keep `XLOG`/`myLOG`/`_LOG` out.
_LOG_CALL_RE = re.compile(r"\b(?:MONGO_LOGV2|LOG)\s*\(")
# The 2nd argument must be a plain decimal integer literal, nothing else.
_PLAIN_INT_RE = re.compile(r"^[0-9]+$")

_NO_TABLE_REASON = "graph store predates the log-id table (rebuild to get it)"
# An upgraded store, or one whose extraction did not run to completion: the
# table exists but the `log_ids_extracted` marker is unset — its rows were
# never (fully) backfilled, so answering would be a confident false negative.
_NOT_EXTRACTED_REASON = (
    "graph store's log-id table is not certified complete "
    "(log_ids_extracted unset — rebuild with cppgraph build to extract it)"
)
_NO_ROW_REASON = "no log-id entry for this id in this graph"


class LogIdLookupError(RuntimeError):
    """The document cannot be produced honestly (fail-closed, §1): no usable
    graph under the repo, a missing/non-git repo, a store with no recorded
    source_commit, too many ids, or an unusable store. The CLI turns it into a
    stderr message + exit 1; nothing goes to stdout. The MCP tool turns it
    into an `{"error": …}` dict."""


def mask_comments_and_strings(text: str) -> str:
    """`text` with the contents of comments (`//…`, `/*…*/`) and string/char
    literals replaced by spaces — same length, newlines preserved — so a
    `LOG(` inside a comment or literal never matches the call pattern.

    Escape sequences (`\\"`) are honoured inside literals. NOT modelled (and
    stated in the module docstring): C++ raw strings `R"(...)"` — their
    contents are masked like a normal literal's, which is right for the
    quote-less form and harmless for the parenthesized one (either way a
    `LOG(` inside a raw string is masked, never matched)."""
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
        elif ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    if i + 1 < n and text[i + 1] != "\n":
                        out[i] = out[i + 1] = " "
                        i += 2
                        continue
                    out[i] = " "
                    i += 1
                elif text[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                else:
                    if text[i] != "\n":
                        out[i] = " "
                    i += 1
        else:
            i += 1
    return "".join(out)


def extract_log_occurrences(text: str) -> list[tuple[int, int]]:
    """The `(line0, log_id)` pairs in `text`: every `LOG(`/`MONGO_LOGV2(`
    occurrence whose 2nd argument is a plain integer literal (see the module
    docstring for the pattern and its limits). Lines are 0-indexed; the
    result is deduped per (line, id) and sorted by line."""
    masked = mask_comments_and_strings(text)
    results: set[tuple[int, int]] = set()
    for m in _LOG_CALL_RE.finditer(masked):
        line0 = masked.count("\n", 0, m.start())
        arg2 = _second_argument(masked, m.end() - 1)
        if arg2 is None or not _PLAIN_INT_RE.match(arg2.strip()):
            continue
        results.add((line0, int(arg2.strip())))
    return sorted(results)


def _second_argument(masked: str, open_paren: int) -> str | None:
    """The 2nd top-level argument of the call opening at `open_paren` (the
    index of `(`), or None when the parens never close or there is no 2nd
    argument. A paren-depth scan over the MASKED text: nested calls survive,
    commas inside them don't split arguments."""
    depth = 0
    arg_starts: list[int] = []
    for i in range(open_paren, min(len(masked), open_paren + 8192)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                arg_starts.append(i)
                break
        elif ch == "," and depth == 1:
            arg_starts.append(i)
    if len(arg_starts) < 2:
        return None
    start = arg_starts[0] + 1
    end = arg_starts[1]
    return masked[start:end]


# --- index-time helpers (called by builder.build_graph) -----------------------


def read_source(root: Path, relative_path: str) -> str | None:
    """The source text of `root / relative_path`, or None when it cannot be
    read (missing file, non-UTF-8, escaping the root). Extraction is
    best-effort: a skip is never a build failure."""
    path = (root / relative_path).resolve()
    try:
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# --- the query surface (ONE pure function, both surfaces) ---------------------


def _git(repo: Path, *args: str) -> str | None:
    """Best-effort `git -C repo <args>`; None when git is missing, fails, or
    times out — the same provenance discipline `fingerprints` applies."""
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


def find_repo_graph(repo: Path) -> Path | None:
    """Newest `*.graph.db` under `<repo>/.cppgraph/` (the fingerprints
    convention: the repo directory itself is the anchor, never a parent
    walk — a foreign graph cannot answer for this checkout)."""
    cpg = repo / ".cppgraph"
    if not cpg.is_dir():
        return None
    graphs = sorted(cpg.glob("*.graph.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return graphs[0] if graphs else None


def _is_ancestor_or_equal(repo: Path, commit: str, head: str) -> bool | None:
    """`git merge-base --is-ancestor commit HEAD` — the SAME computation
    `fingerprints` uses for `graph_behind_head`; None = unknown (fail closed
    rather than guess)."""
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


def _open_store(db: Path) -> tuple[SQLiteConnection, dict[str, str], bool, bool]:
    """The store-side opener (`store.open_log_id_capability`) wrapped into
    this contract's fail-closed error. The capability gate semantics — table
    presence AND the `log_ids_extracted` marker ("extraction ran to
    completion over all scanned sources"; set only by a full extraction
    build, preserved by `apply_update` only when its partial re-extracted
    completely, cleared when it could not, never set by `enrich_references`)
    — live with the SQL, in `store.py`."""
    try:
        return open_log_id_capability(db)
    except IncompatibleStoreError as e:
        raise LogIdLookupError(str(e)) from e


def _callers_of(conn: SQLiteConnection, scip: str) -> dict[str, Any]:
    """The bounded `callers` object for one symbol: at most CALLERS_LIMIT
    direct `calls` edges into it, `total` the full count, `truncated` visible."""
    rows = conn.execute(
        """
        SELECT s.symbol, s.display_name, f.path, e.line
        FROM edges e
        JOIN symbols s ON s.id = e.src_id
        LEFT JOIN files f ON f.id = e.file_id
        WHERE e.kind = 'calls' AND e.dst_id = (SELECT id FROM symbols WHERE symbol = ?)
        ORDER BY s.symbol
        """,
        (scip,),
    ).fetchall()
    return {
        "items": [
            {"symbol": sym, "name": name, "file": path, "line": line}
            for sym, name, path, line in rows[:CALLERS_LIMIT]
        ],
        "truncated": len(rows) > CALLERS_LIMIT,
        "total": len(rows),
    }


def log_id_lookup_document(
    db: Path, repo: Path, ids: list[int], revision: str | None
) -> dict[str, Any]:
    """The contract v1 document — the ONE pure function behind the CLI
    `log-id-lookup` command and the MCP `log_id_lookup` tool. Raises
    `LogIdLookupError` (fail-closed) whenever a required fact cannot be
    established; unknown ids are per-id `found: false`, never errors."""
    if len(ids) > MAX_IDS:
        raise LogIdLookupError(
            f"at most {MAX_IDS} ids per call (got {len(ids)}) — redwood's own "
            "budget rule, enforced server-side (more is an error, not a truncation)"
        )
    repo = repo.resolve()
    if not repo.is_dir():
        raise LogIdLookupError(f"--repo path is not an existing directory: {repo}")
    if not db.is_file():
        raise LogIdLookupError(f"no usable graph store: {db}")
    conn, meta, table_present, extracted = _open_store(db)
    try:
        commit = meta.get("source_commit") or None
        head = _git(repo, "rev-parse", "HEAD")
        toplevel = _git(repo, "rev-parse", "--show-toplevel")
        if head is None or toplevel is None or toplevel != str(repo):
            raise LogIdLookupError(
                f"{repo} is not a git checkout root (toplevel: {toplevel or 'none'})"
                " — repo_head cannot be established"
            )
        if commit is None:
            raise LogIdLookupError(
                f"{db} records no source_commit — graph_source_commit cannot be established"
            )
        matches = _is_ancestor_or_equal(repo, commit, head)
        if matches is None:
            raise LogIdLookupError(
                f"cannot establish whether {commit} is an ancestor of HEAD {head} in this checkout"
            )
        entries: list[dict[str, Any]] = []
        has_table = table_present and extracted
        for log_id in ids:
            entry: dict[str, Any] = {"id": log_id}
            if not has_table:
                reason = _NO_TABLE_REASON if not table_present else _NOT_EXTRACTED_REASON
                entries.append({**entry, "found": False, "reason": reason})
                continue
            rows = conn.execute(
                """
                SELECT s.symbol, s.display_name, f.path, l.line
                FROM log_ids l
                JOIN symbols s ON s.id = l.symbol_id
                LEFT JOIN files f ON f.id = l.file_id
                WHERE l.log_id = ?
                ORDER BY f.path, l.line
                """,
                (log_id,),
            ).fetchall()
            if not rows:
                entries.append({**entry, "found": False, "reason": _NO_ROW_REASON})
                continue
            scip, display, path, line = rows[0]
            hit = {
                **entry,
                "found": True,
                "symbol": display or scip,
                "scip": scip,
                "location": f"{path}:{line + 1}",  # 1-indexed, editor convention
                "callers": _callers_of(conn, scip),
            }
            if len(rows) > 1:
                hit["matches"] = len(rows)
            entries.append(hit)
        return {
            **envelope("log-id-lookup", {"ids": entries}),
            "contract_version": CONTRACT_VERSION,
            "resolved_against": {
                "graph_source_commit": commit,
                "repo_head": head,
                "head_matches_graph": matches,
                "revision": revision,
            },
            "log_id_table": has_table,
        }
    except SqliteError as e:
        raise LogIdLookupError(f"{db} is not a usable cppgraph store: {e}") from e
    finally:
        conn.close()


def run_cli(repo: str, ids: list[int], revision: str | None) -> int:
    """The `cppgraph log-id-lookup` entry: the contract v1 document as the
    ONLY thing on stdout, or a stderr message + exit 1 (fail-closed — a
    caller treats any non-zero exit as provider-unavailable, never a guessed
    symbol)."""
    try:
        resolved = Path(repo)
        db = find_repo_graph(resolved)
        if db is None:
            raise LogIdLookupError(
                f"no .cppgraph/*.graph.db found under {resolved} — index the "
                "project with cppgraph first"
            )
        document = log_id_lookup_document(db, resolved, ids, revision)
    except LogIdLookupError as e:
        print(f"[cppgraph] log-id-lookup: {e}", file=sys.stderr)
        return 1
    print(json.dumps(document, indent=2))
    return 0
