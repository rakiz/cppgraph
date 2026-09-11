"""Surface-agnostic symbol queries shared by the CLI and the MCP server.

The whole `find` behaviour — exact match, then the relaxation cascade
(`Class::method` -> SCIP `Class#method`, then case/separator-insensitive, then
the bare leaf), noise hiding, path scoping, overload grouping, budget capping —
lives ONCE here: `cppgraph find` (CLI) and the MCP `find` tool both call
`find_symbols`, so a given query gives the same answer on either surface
(AGENTS.md: never fork query logic into one surface only). The source readers
it needs (`read_source_snippet`, `extract_signature`) live here too —
checkout-rooted lookup helpers, no transport in sight.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cppgraph.filters import (
    is_noise_symbol,
    matches_path_prefix,
    qualified_name,
    short_label,
)

if TYPE_CHECKING:
    from cppgraph.model import Node
    from cppgraph.store import GraphStore


def read_source_snippet(
    root: str | Path, rel_path: str, line0: int, *, context: int = 3
) -> list[tuple[int, str]] | None:
    """Read `line0` (0-indexed) ± `context` lines from `root/rel_path`.

    Returns a list of `(0-indexed line number, text)`, or `None` if the file
    can't be read — the checkout root is a runtime argument, so a missing file
    is an expected, recoverable condition, not an error.
    """
    try:
        text = (Path(root) / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(0, line0 - context)
    end = min(len(lines), line0 + context + 1)
    return [(i, lines[i]) for i in range(start, end)]


def extract_signature(root: str | None, file: str | None, line0: int | None) -> str | None:
    """A best-effort readable parameter signature — including any default
    argument values, verbatim as written — read from the source at a
    definition site.

    `scip-clang` disambiguates overloads by an opaque hash, not by argument
    types, so grouped overloads (`find`) are otherwise indistinguishable. A
    patched scip-clang can carry a stored `signature_documentation` on the
    graph itself (`explain`/`explain_symbol`'s "signature (stored):"), but it
    is a pretty-printed declaration, not this verbatim-source extraction.
    Since cppgraph has the checkout (`root`), it reads the def line and captures
    the text from the first `(` to its matching `)` verbatim — so a defaulted
    parameter (`bool useNullIfMissing = false`) is visible without opening the
    header. Display-only, so templates / macros / multi-line params are
    tolerated (whitespace collapsed). `None` if there's no root, the file can't
    be read, no parameter list is found, or the declaration at this line ends
    (`;`/`{`) before any `(` — a field/variable has no parameter list of its
    own, and without this check a bare `int x;` followed a few lines later by
    an unrelated function would misattribute that function's parameter list
    to the field (observed in practice on a field declared just above a
    `friend bool operator==(...)`)."""
    if root is None or file is None or line0 is None:
        return None
    snippet = read_source_snippet(root, file, line0, context=8)
    if not snippet:
        return None
    text = " ".join(t for i, t in snippet if i >= line0)
    start = text.find("(")
    if start < 0:
        return None
    # A `;` or `{` before the first `(` ends the current declaration/statement
    # without ever opening a parameter list — this line/lookahead window
    # belongs to a DIFFERENT, later declaration (e.g. a field with no `(` of
    # its own, followed within the lookahead window by an unrelated function).
    stop = min((i for i in (text.find(";"), text.find("{")) if i >= 0), default=-1)
    if stop >= 0 and stop < start:
        return None
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return " ".join(text[start : j + 1].split())
    return None


def line1(line0: int | None) -> int | None:
    """0-indexed store line -> 1-indexed for display; None stays None."""
    return None if line0 is None else line0 + 1


def loosen_to_leaf(query: str) -> str | None:
    """The trailing name segment of a qualified query, or None if there's nothing
    to loosen.

    `find` is an exact substring match, so a guessed *qualifier* that's wrong
    (`Class#method` when `method` is actually a free function, or a wrong
    namespace) returns nothing even though the bare name exists. Dropping
    everything up to the last `#`, `::`, or `/` gives the leaf name to retry on.
    Returns None when the query has no such separator (it's already a leaf, so
    there's nothing to relax)."""
    leaf = re.split(r"#|::|/", query.rstrip(".#")).pop().strip()
    if not leaf or leaf == query:
        return None
    return leaf


def label(symbol: str, node: Node | None) -> str:
    """Preferred human label: the indexed display name if present (other indexers
    may fill it), else one derived from the SCIP string."""
    return (node.display_name if node is not None else "") or short_label(symbol)


def node_dict(node: Node, full_symbols: bool = False) -> dict[str, Any]:
    """Compact node identity. The full SCIP `symbol` string is 150-250 chars of
    near-noise repeated per hit; by default we emit a readable `name` +
    `file:line` (a substring `find` can re-resolve) and only carry the raw SCIP
    string when explicitly asked (`full_symbols`)."""
    d: dict[str, Any] = {"name": label(node.symbol, node)}
    if full_symbols:
        d["symbol"] = node.symbol
    d["file"] = node.file
    d["line"] = line1(node.line)
    return d


def capped(items: list[Any], limit: int | None) -> tuple[list[Any], bool]:
    """The first `limit` items and whether any were cut. `limit=None` is
    uncapped — the CLI's `--limit` default (show all, print the true total);
    the MCP tools always pass their budget explicitly."""
    if limit is None:
        return items, False
    return items[:limit], len(items) > limit


def find_symbols(
    store: GraphStore,
    query: str,
    limit: int | None = None,
    hide_trivial: bool = False,
    root: str | None = None,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Symbols whose SCIP string or display name contains `query`.

    The entry point to every other tool: SCIP symbol strings aren't memorable,
    so an LLM resolves a human name here first, then feeds the exact string on.
    With `hide_trivial=True`, compiler-generated / boilerplate hits (unnamed-type
    lambdas, operators, `*assert`/`makeStatus`, …) are dropped and counted as
    `trivial_hidden`, so a broad query isn't buried in noise. `include_paths`/
    `exclude_paths` filter matches by their definition file's path prefix (e.g.
    scope out vendored deps). `limit` caps the number of result *groups*
    (`None` = uncapped).

    On an exact-zero result, `find` relaxes and flags the response `relaxed`:
    first a C++-spelled qualified guess (`Class::method`) is retried with
    SCIP's `Class#method` separator (the same step `GraphStore.resolve` tries
    before anything fuzzier), then case/separator-insensitively (the
    `change_stream` vs `changeStream` vs `changestream` trap), then, for a
    *qualified* query (`Class#method`, a wrong guess), on the bare leaf name.
    So a naming miss degrades to a hint instead of a silent empty answer.

    Grouped overloads carry a best-effort `signature` read from source (when
    `root` is available), since scip-clang distinguishes them only by hash.
    When the graph was built from a kind-patched binary (`has_symbol_kind`),
    the symbol's fine-grained SCIP kind is returned as `scip_kind` (e.g.
    "StaticMethod") on the entry and on each `signatures[i]` arm — also absent
    when the graph carries none. When the graph carries external-package
    symbol metadata (`has_external_symbols`), `is_out_of_project` is returned
    on the entry and on each arm — true (defined in an un-indexed external
    package), false (project-native), or null (no `SymbolInformation` from
    either source); for a grouped entry the top-level value is the arms'
    shared value, or null when the arms disagree. Absent when the graph
    predates the feature.
    """
    has_external_symbols = store.meta().get("has_external_symbols") == "true"
    matches = store.find(query)
    relaxation: str | None = None
    relaxed_query: str | None = None
    if not matches and "::" in query:
        # `Class::method` in C++ spelling is `Class#method` in SCIP's — the same
        # first relaxation `GraphStore.resolve` tries (before fuzzy), so a
        # qualified guess resolves exactly instead of loosening to the bare
        # leaf and dragging in same-named methods on unrelated classes.
        scip_query = query.replace("::", "#")
        matches = store.find(scip_query)
        if matches:
            relaxation = "colon"
            relaxed_query = scip_query
    if not matches:
        fuzzy = store.find(query, fuzzy=True)
        if fuzzy:
            matches = fuzzy
            relaxation = "fuzzy"
        else:
            leaf = loosen_to_leaf(query)
            if leaf:
                loosened = store.find(leaf) or store.find(leaf, fuzzy=True)
                if loosened:
                    matches = loosened
                    relaxation = "leaf"
                    relaxed_query = leaf
    trivial_hidden = 0
    if hide_trivial:
        kept = [n for n in matches if not is_noise_symbol(n.symbol)]
        trivial_hidden = len(matches) - len(kept)
        matches = kept
    if include_paths or exclude_paths:
        matches = [
            n
            for n in matches
            if matches_path_prefix(n.file, include=include_paths, exclude=exclude_paths)
        ]

    # Group overloads: signatures sharing a qualified name (distinct SCIP hashes
    # for the same `Class::method`) collapse into one entry, so querying doesn't
    # silently surface only one arm of an overload set. Order-preserving.
    groups: dict[str, list[Node]] = {}
    for n in matches:
        groups.setdefault(qualified_name(n.symbol), []).append(n)

    shown_keys, truncated = capped(list(groups), limit)
    results: list[dict[str, Any]] = []
    for key in shown_keys:
        members = groups[key]
        entry = node_dict(members[0], full_symbols=True)
        if members[0].scip_kind is not None:
            # Fine-grained SCIP kind (kind-patched binary, `has_symbol_kind`);
            # absent when the graph carries none — never null.
            entry["scip_kind"] = members[0].scip_kind
        if has_external_symbols:
            # External-package classification (`has_external_symbols`):
            # true/false/null on every result — null meaning "no
            # SymbolInformation from either source", never omitted. A grouped
            # entry carries the arms' shared value, null when they disagree
            # (never one arm's value picked silently).
            classifications = {m.is_out_of_project for m in members}
            entry["is_out_of_project"] = (
                next(iter(classifications)) if len(classifications) == 1 else None
            )
        if len(members) > 1:
            # An overload set: keep every signature's exact symbol + site, plus a
            # source-derived parameter signature so the arms are distinguishable.
            entry["overloads"] = len(members)
            sigs: list[dict[str, Any]] = []
            for m in members:
                d = node_dict(m, full_symbols=True)
                if m.scip_kind is not None:
                    d["scip_kind"] = m.scip_kind
                if has_external_symbols:
                    d["is_out_of_project"] = m.is_out_of_project
                sig = extract_signature(root, m.file, m.line)
                if sig:
                    d["signature"] = sig
                sigs.append(d)
            entry["signatures"] = sigs
        results.append(entry)

    result = {
        "query": query,
        "total": len(matches),
        "groups": len(groups),
        "truncated": truncated,
        "results": results,
    }
    if relaxation == "fuzzy":
        result["relaxed"] = True
        result["note"] = (
            f"no exact match for {query!r}; matched case/separator-insensitively "
            "(e.g. `changestream` ~ `change_stream` / `changeStream`)"
        )
    elif relaxation == "colon":
        result["relaxed"] = True
        result["relaxed_query"] = relaxed_query
        result["note"] = (
            f"no exact match for {query!r}; showing results for "
            f"{relaxed_query!r} (C++ `::` normalized to SCIP's `#` member "
            "separator)"
        )
    elif relaxation == "leaf":
        result["relaxed"] = True
        result["relaxed_query"] = relaxed_query
        result["note"] = (
            f"no exact match for {query!r}; showing results for the loosened "
            f"name {relaxed_query!r} (the qualifier may be wrong — e.g. a free "
            "function, not a method)"
        )
    if hide_trivial:
        result["trivial_hidden"] = trivial_hidden
    if include_paths or exclude_paths:
        result["include_paths"] = include_paths
        result["exclude_paths"] = exclude_paths
    return result
