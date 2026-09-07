"""MCP server exposing the cppgraph query surface to an LLM.

Phase 3. The graph store is a large, semantically-exact C++ call graph; an LLM
reasoning about a change wants to ask it questions ("what calls this?", "am I
even looking at a current graph?", "show me the definition") without shelling
out or loading anything. This module wraps `GraphStore` as MCP tools.

Two layers, deliberately split so the substance is testable without a transport:

- **Pure query functions** (`find_symbols`, `callers`, `callees`, `call_path`,
  `impact`, `explain`, `status_report`): take a `GraphStore` and return
  JSON-serialisable, *token-budgeted* dicts. Lists are capped with an explicit
  `truncated` flag and a `total` count, so a fan-out query (a symbol with
  hundreds of callers) never dumps the whole set into the model's context.
- **Transport wiring** (`build_server` / `main`): a thin FastMCP layer that
  binds one long-lived `GraphStore` (resolved at launch — from `--graph` or
  auto-discovered from the cwd's `.cppgraph/`) to those functions.

Source snippets: by default these tools return **coordinates** (`file:line`),
which are cheap. When you actually want to see the code, pass
`include_source=True` — cppgraph reads the file and returns the **snippet
inline**, so the caller does *not* need a separate file-read step. This works
out of the box: the checkout root is auto-discovered (the project that owns the
`.cppgraph/`), so no `--root` flag is required. The same root drives the
`status` drift check.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cppgraph.cli import SOURCE_EXTS, build_export_json, extract_signature, read_source_snippet
from cppgraph.export import is_test_file
from cppgraph.filters import drop_test_edges as _drop_test_edges
from cppgraph.filters import filter_by_path as _filter_by_path
from cppgraph.filters import is_noise_symbol as _is_noise_symbol
from cppgraph.filters import is_trivial_callee as _is_trivial_callee
from cppgraph.filters import matches_path_prefix as _matches_path_prefix
from cppgraph.filters import qualified_name as _qualified_name
from cppgraph.filters import short_label as _short_label
from cppgraph.store import (
    GraphStore,
    changed_files_since,
    commits_behind,
    discover_graph,
    is_stale,
    read_dirty_fingerprints,
    staleness_verdict,
)
from cppgraph.updates import scip_update_advice, update_advice

if TYPE_CHECKING:
    from cppgraph.model import Edge, Node

# Default cap on any list a tool returns. Big enough to be useful for reasoning,
# small enough that a hub symbol's callers don't blow the context budget. The
# caller can tune it per-query either way — lower to spend fewer tokens, raise to
# see more — and always learns the true `total`. Set to 40
# (not 25): a real function with ~30 stage/callee edges had genuine edges pushed
# past a cap of 25, so the default now clears a typical fan-out.
DEFAULT_LIMIT = 40
# `explain` bundles callers + callees in one payload, so it caps each side lower.
EXPLAIN_LIMIT = 10

_UNKNOWN = "unknown symbol {symbol!r} — use the `find` tool to look up its exact SCIP symbol string"

# Degrade-cleanly responses for the enclosing-range-gated (#504) tools, the
# same `available: false` + `reason` convention `references` uses when the
# graph was built `--no-references`: say why, point at the upgrade, never a
# silently empty/wrong list.
_NO_ENCLOSING_RANGES = (
    "this graph carries no definition body extents: `enclosing_range` is emitted "
    "only by a #504-built scip-clang, and a stock-binary graph cannot answer "
    "this (an empty list would read as 'none', which is wrong). Index with a "
    "#504-built scip-clang and rebuild the store (`cppgraph build --scip "
    "<index.scip>`) to enable it"
)
_STOCK_ATTRIBUTION_UNRELIABLE = (
    "not reliable on this graph: it was indexed with a stock scip-clang, whose "
    "caller attribution (nearest-preceding definition, no enclosing ranges) can "
    "mis-attribute a bodyless member declaration to the preceding definition — "
    "a phantom caller that turns a real 0 into a false 1, exactly the answer "
    "this tool must get right. Index with a #504-built scip-clang and rebuild "
    "the store to enable it"
)
# Standing caveat on every boundary_violations response, both directions of the
# facts-not-judgments rule: a listed violation is exact (an edge that exists),
# while an empty list is a lower bound (the static graph under-reports runtime
# reachability), never a conformance verdict.
_BOUNDARY_NOTE = (
    "each violation is a real compiler-traced edge — zero false positives. The "
    "converse is a lower bound: 0 violations means no *statically indexed* edge "
    "crosses these rules — runtime dispatch (virtual calls, function pointers, "
    "registered factories) can cross a boundary with no static edge, and only "
    "the given rules and edge kinds were checked. The rules are supplied by "
    "you (the project's declared layering); the graph stores no intended "
    "architecture of its own."
)


def _line1(line0: int | None) -> int | None:
    """0-indexed store line -> 1-indexed for display; None stays None."""
    return None if line0 is None else line0 + 1


def _is_type_symbol(symbol: str) -> bool:
    """True if the SCIP string denotes a *type* (class/struct/enum), not a
    callable. SCIP suffixes a type descriptor with `#` and a method/function
    with `().`; a bare type reference therefore ends in `#`. Types have no
    call-graph callers, so `impact_of(kind="calls")` on one is meaningless —
    the blast radius lives in `find_references` instead."""
    return symbol.rstrip().endswith("#")


def _loosen_to_leaf(query: str) -> str | None:
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


def _label(symbol: str, node: Node | None) -> str:
    """Preferred human label: the indexed display name if present (other indexers
    may fill it), else one derived from the SCIP string."""
    return (node.display_name if node is not None else "") or _short_label(symbol)


def _node_dict(node: Node, full_symbols: bool = False) -> dict[str, Any]:
    """Compact node identity. The full SCIP `symbol` string is 150-250 chars of
    near-noise repeated per hit; by default we emit a readable `name` +
    `file:line` (a substring `find` can re-resolve) and only carry the raw SCIP
    string when explicitly asked (`full_symbols`)."""
    d: dict[str, Any] = {"name": _label(node.symbol, node)}
    if full_symbols:
        d["symbol"] = node.symbol
    d["file"] = node.file
    d["line"] = _line1(node.line)
    return d


def _edge_dict(
    edge: Edge, other: str, store: GraphStore | None = None, full_symbols: bool = False
) -> dict[str, Any]:
    """An edge as seen from one endpoint: `other` is the symbol at the far end
    (the caller for a callers query, the callee for a callees query). Same
    compaction as `_node_dict`: a readable label by default, the raw SCIP string
    only when `full_symbols`."""
    node = store.get_node(other) if store is not None else None
    d: dict[str, Any] = {"name": _label(other, node)}
    if full_symbols:
        d["symbol"] = other
    d["file"] = edge.file
    d["line"] = _line1(edge.line)
    return d


def _capped(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    return items[:limit], len(items) > limit


def _merged_source(
    root: str, rel_path: str, hit_lines0: list[int], context: int
) -> list[dict[str, Any]] | None:
    """Read one file once and return the union of the `± context` windows around
    every hit line, deduplicated.

    Overlapping windows (hits within `2·context` lines of each other) are
    collapsed into a single contiguous run instead of re-sending the shared
    lines per hit — the "include_source duplicates overlapping lines" cost.
    Non-adjacent runs stay in the same flat list; the gap shows up as a jump in
    the emitted line numbers. Each line is 1-indexed and flagged `is_use` when it
    is itself a reference site. `None` if the file can't be read.
    """
    try:
        lines = (Path(root) / rel_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    hits = set(hit_lines0)
    wanted: set[int] = set()
    for h in hits:
        wanted.update(range(max(0, h - context), min(len(lines), h + context + 1)))
    return [{"line": i + 1, "text": lines[i], "is_use": i in hits} for i in sorted(wanted)]


def find_symbols(
    store: GraphStore,
    query: str,
    limit: int = DEFAULT_LIMIT,
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
    scope out vendored deps).

    On an exact-zero result, `find` relaxes once and flags the response
    `relaxed`: first case/separator-insensitively (the `change_stream` vs
    `changeStream` vs `changestream` trap), then, for a *qualified* query
    (`Class#method`, a wrong guess), on the bare leaf name. So a naming miss
    degrades to a hint instead of a silent empty answer.

    Grouped overloads carry a best-effort `signature` read from source (when
    `root` is available), since scip-clang distinguishes them only by hash.
    """
    matches = store.find(query)
    relaxation: str | None = None
    relaxed_query: str | None = None
    if not matches:
        fuzzy = store.find(query, fuzzy=True)
        if fuzzy:
            matches = fuzzy
            relaxation = "fuzzy"
        else:
            leaf = _loosen_to_leaf(query)
            if leaf:
                loosened = store.find(leaf) or store.find(leaf, fuzzy=True)
                if loosened:
                    matches = loosened
                    relaxation = "leaf"
                    relaxed_query = leaf
    trivial_hidden = 0
    if hide_trivial:
        kept = [n for n in matches if not _is_noise_symbol(n.symbol)]
        trivial_hidden = len(matches) - len(kept)
        matches = kept
    if include_paths or exclude_paths:
        matches = [
            n
            for n in matches
            if _matches_path_prefix(n.file, include=include_paths, exclude=exclude_paths)
        ]

    # Group overloads: signatures sharing a qualified name (distinct SCIP hashes
    # for the same `Class::method`) collapse into one entry, so querying doesn't
    # silently surface only one arm of an overload set. Order-preserving.
    groups: dict[str, list[Node]] = {}
    for n in matches:
        groups.setdefault(_qualified_name(n.symbol), []).append(n)

    shown_keys, truncated = _capped(list(groups), limit)
    results: list[dict[str, Any]] = []
    for key in shown_keys:
        members = groups[key]
        entry = _node_dict(members[0], full_symbols=True)
        if len(members) > 1:
            # An overload set: keep every signature's exact symbol + site, plus a
            # source-derived parameter signature so the arms are distinguishable.
            entry["overloads"] = len(members)
            sigs: list[dict[str, Any]] = []
            for m in members:
                d = _node_dict(m, full_symbols=True)
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


def _resolve(store: GraphStore, symbol: str) -> tuple[str | None, dict[str, Any] | None]:
    """The MCP wrapper over `GraphStore.resolve` (shared with the CLI): map a name
    or exact SCIP string to one symbol for a tool to act on.

    Returns `(exact_symbol, None)` when it resolves, else `(None, payload)` where
    `payload` is the tool's JSON reply — an `ambiguous` candidate list when several
    match (the caller re-picks; it never guesses), or an error when none do.
    """
    resolved, candidates = store.resolve(symbol)
    if resolved is not None:
        return resolved, None
    if not candidates:
        return None, {"error": _UNKNOWN.format(symbol=symbol)}
    shown = candidates[:DEFAULT_LIMIT]
    return None, {
        "ambiguous": symbol,
        "total": len(candidates),
        "truncated": len(candidates) > len(shown),
        "candidates": [_node_dict(n, full_symbols=True) for n in shown],
        "hint": (
            f"{len(candidates)} symbols match {symbol!r}; re-call with the exact "
            "`symbol` from candidates (or use `find` to narrow a broad name)."
        ),
    }


def callers(
    store: GraphStore,
    symbol: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
    exclude_tests: bool = True,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Direct callers of `symbol` (one `calls` hop). Error dict if unknown.

    Test callers (and destructor teardown sites) are dropped by default
    (`exclude_tests`); `include_paths`/`exclude_paths` further filter callers by
    their definition file's path prefix (e.g. scope out vendored deps); pass
    `full_symbols=True` for the raw SCIP strings."""
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    edges = store.callers_of(symbol)
    if exclude_tests:
        edges = _drop_test_edges(store, edges, on="src")
    edges = _filter_by_path(
        store, edges, on="src", include_paths=include_paths, exclude_paths=exclude_paths
    )
    shown, truncated = _capped(edges, limit)
    return {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "callers": [_edge_dict(e, e.src, store, full_symbols) for e in shown],
    }


def callees(
    store: GraphStore,
    symbol: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
    exclude_tests: bool = True,
    hide_trivial: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Direct callees of `symbol` (one `calls` hop). Error dict if unknown.

    Callees defined in test files are dropped by default (`exclude_tests`);
    `include_paths`/`exclude_paths` further filter callees by their definition
    file's path prefix (e.g. scope out vendored deps); pass `full_symbols=True`
    for the raw SCIP strings. With `hide_trivial=True`, ubiquitous helpers
    (`operator==`, `tassert`/`uassert`, `makeStatus`, `source_location`, …) are
    dropped so the domain edges stand out; the count of hidden edges is
    reported as `trivial_hidden`."""
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    edges = store.callees_of(symbol)
    if exclude_tests:
        edges = _drop_test_edges(store, edges, on="dst")
    edges = _filter_by_path(
        store, edges, on="dst", include_paths=include_paths, exclude_paths=exclude_paths
    )
    trivial_hidden = 0
    if hide_trivial:
        kept = [e for e in edges if not _is_trivial_callee(e.dst)]
        trivial_hidden = len(edges) - len(kept)
        edges = kept
    shown, truncated = _capped(edges, limit)
    result = {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "callees": [_edge_dict(e, e.dst, store, full_symbols) for e in shown],
    }
    if hide_trivial:
        result["trivial_hidden"] = trivial_hidden
    return result


def bases(
    store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
) -> dict[str, Any]:
    """Direct base classes `symbol` inherits from (one `inherits` hop).

    Each base is returned with its own definition site (an inheritance edge has
    no meaningful line).
    """
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    nodes = store.bases_of(symbol)
    shown, truncated = _capped(nodes, limit)
    result = {
        "symbol": symbol,
        "total": len(nodes),
        "truncated": truncated,
        "bases": [_node_dict(n, full_symbols) for n in shown],
    }
    if not nodes:
        # A bare 0 reads as "no hierarchy", which is often wrong: it may be a
        # root class, or a holder type (e.g. a factory holder like
        # DocumentSourceChangeStream) that participates in no `inherits` edge.
        result["note"] = (
            "no base classes recorded. This may be a root/standalone type, or a "
            "holder whose relationships aren't inheritance — check `subclasses`, "
            "or `find_references` for where the type is used."
        )
    return result


def subtypes(
    store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
) -> dict[str, Any]:
    """Direct subclasses of `symbol` (one `inherits` hop backward), each with
    its own definition site."""
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    nodes = store.subtypes_of(symbol)
    shown, truncated = _capped(nodes, limit)
    result = {
        "symbol": symbol,
        "total": len(nodes),
        "truncated": truncated,
        "subtypes": [_node_dict(n, full_symbols) for n in shown],
    }
    if not nodes:
        # A bare 0 misleads: it may be a leaf class, or a holder type (e.g. a
        # factory holder like DocumentSourceChangeStream) that isn't a real base
        # — its actual hierarchy is reached via its own bases.
        result["note"] = (
            "no subclasses recorded. This may be a leaf class, or a holder type "
            "that isn't itself a base — check `base_classes` (it may inherit "
            "rather than be inherited from), or `find_references` for its uses."
        )
    return result


def references(
    store: GraphStore,
    symbol: str,
    root: str | None = None,
    include_source: bool = False,
    context: int = 0,
    limit: int = DEFAULT_LIMIT,
    exclude_tests: bool = True,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Exact use sites of `symbol` (the `--references` location index).

    Answers "where is this type/symbol used?" — the dependency the call graph
    can't express (a plain struct has no callers). Positions are always exact.
    When the graph was built with attributed references (`--attributed-refs`, a
    #504 binary), each use also carries `used_by`: the definition that uses it,
    so the answer names the *functions*, not just the locations. Test-file uses
    are dropped by default (`exclude_tests`); `include_paths`/`exclude_paths`
    further filter uses by their file's path prefix (e.g. scope out vendored
    deps). Coordinates only by default; with `include_source=True` *and* a
    `root`, sites are grouped by file and each file carries one merged snippet
    (overlapping `± context` windows are collapsed). `available` is False (not
    an error) when the graph was built with `--no-references`, so the caller
    knows to rebuild.
    """
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    refs = store.references_of(symbol)
    if not refs and store.meta().get("has_references") != "true":
        return {
            "symbol": symbol,
            "available": False,
            "reason": "graph built with --no-references (no location index)",
        }
    if exclude_tests:
        refs = [r for r in refs if not is_test_file(r.file)]
    if include_paths or exclude_paths:
        refs = [
            r
            for r in refs
            if _matches_path_prefix(r.file, include=include_paths, exclude=exclude_paths)
        ]
    shown, truncated = _capped(refs, limit)

    with_src = include_source and root is not None
    if with_src:
        # Group by file so a file's overlapping windows are read and merged once.
        by_file: dict[str, list[int]] = {}
        order: list[str] = []
        for ref in shown:
            if ref.file is None or ref.line is None:
                continue
            if ref.file not in by_file:
                order.append(ref.file)
            by_file.setdefault(ref.file, []).append(ref.line)
        items: list[dict[str, Any]] = [
            {
                "file": f,
                "lines": sorted(_line1(ln) for ln in by_file[f]),
                "source": _merged_source(root, f, by_file[f], context),
            }
            for f in order
        ]
    else:
        items = []
        for ref in shown:
            item: dict[str, Any] = {"file": ref.file, "line": _line1(ref.line)}
            if ref.enclosing_symbol:
                item["used_by"] = _short_label(ref.enclosing_symbol)
            items.append(item)

    return {
        "symbol": symbol,
        "available": True,
        "total": len(refs),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "uses": items,
    }


def call_path(store: GraphStore, src: str, dst: str) -> dict[str, Any]:
    """Shortest `calls` chain from `src` to `dst`, as an ordered node list.

    A bounded answer by construction (one shortest path), so it isn't capped.
    """
    src, _alt = _resolve(store, src)
    if _alt is not None:
        return _alt
    dst, _alt = _resolve(store, dst)
    if _alt is not None:
        return _alt
    chain = store.shortest_call_path(src, dst)
    if chain is None:
        return {
            "src": src,
            "dst": dst,
            "found": False,
            "path": None,
            "hint": (
                "No *static* call chain — this does not prove the two are unrelated. "
                "The flow may cross a runtime-dispatch boundary the static graph can't "
                "link: a virtual call, or a registered-factory hop (e.g. a "
                "DocumentSource built by a pipeline parser and later run via "
                "doGetNext), where the edge exists only at runtime. Try `path` "
                "against the concrete override/implementation, or bridge the boundary "
                "with `find_references` / `subclasses`."
            ),
        }
    # chain is a list of edges src->...->dst; render as the node sequence.
    nodes = [{"symbol": src, "file": None, "line": None}]
    for edge in chain:
        nodes.append({"symbol": edge.dst, "file": edge.file, "line": _line1(edge.line)})
    return {"src": src, "dst": dst, "found": True, "hops": len(chain), "path": nodes}


def impact(
    store: GraphStore,
    symbol: str,
    depth: int | None = None,
    limit: int = DEFAULT_LIMIT,
    kind: str = "calls",
    full_symbols: bool = False,
    exclude_tests: bool = True,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Reverse blast-radius: everything that transitively reaches `symbol`.

    `kind="calls"` (default) = transitive callers ("what breaks if I change this
    function?"); `kind="inherits"` = all transitive subclasses of a base type.
    `depth` bounds the backward hops (None = unbounded). Results are symbols
    (with their definition site); capped like the other fan-out tools. Symbols
    defined in test files are dropped by default (`exclude_tests`);
    `include_paths`/`exclude_paths` further filter by definition-file path
    prefix (e.g. scope out vendored deps).
    """
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt

    # A type has no call-graph callers: `kind="calls"` on one would return a bare
    # `total: 0` that reads as "nothing depends on this", which is misleading —
    # the blast radius of a type lives in its reference sites. Redirect explicitly
    # instead of silently returning 0.
    if kind == "calls" and _is_type_symbol(symbol):
        ref_count = len(store.references_of(symbol))
        return {
            "symbol": symbol,
            "kind": kind,
            "is_type": True,
            "reached_by": [],
            "total": 0,
            "notice": (
                f"{symbol} is a type, which has no call-graph callers. Its blast "
                f"radius is its {ref_count} reference site(s) — use `find_references` "
                '(or impact_of with kind="inherits" for the subclass tree).'
                if ref_count
                else f"{symbol} is a type (no call-graph callers). Use `find_references` "
                'for its use sites, or impact_of with kind="inherits" for subclasses. '
                "(0 reference sites recorded — the graph may have been built with "
                "--no-references.)"
            ),
            "reference_sites": ref_count,
        }

    affected = sorted(store.impact(symbol, max_depth=depth, kind=kind))
    nodes = [(sym, store.get_node(sym)) for sym in affected]
    if exclude_tests:
        nodes = [(sym, n) for sym, n in nodes if n is None or not is_test_file(n.file)]
    if include_paths or exclude_paths:
        nodes = [
            (sym, n)
            for sym, n in nodes
            if _matches_path_prefix(
                n.file if n is not None else None, include=include_paths, exclude=exclude_paths
            )
        ]
    shown, truncated = _capped(nodes, limit)
    out: list[dict[str, Any]] = [
        _node_dict(n, full_symbols)
        if n is not None
        else {"symbol": sym, "file": None, "line": None}
        for sym, n in shown
    ]
    return {
        "symbol": symbol,
        "kind": kind,
        "depth": depth,
        "total": len(nodes),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "reached_by": out,
    }


def hotspot_ranking(
    store: GraphStore,
    limit: int = DEFAULT_LIMIT,
    kind: str = "fan_in",
    exclude_tests: bool = False,
    full_symbols: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Global ranking of symbols by call-edge volume — "what's most-called /
    most-calling across the whole project?", the question `who_calls`/
    `what_it_calls` can only answer one symbol at a time.

    `kind="fan_in"` (default) ranks by incoming `calls` edges (most-called);
    `"fan_out"` by outgoing edges (most-calling); `"edges"` sums both per
    symbol. `exclude_tests` drops an edge if either endpoint symbol is itself
    defined in a test file (its own definition site, like `find_references`'
    test filtering — not the call site). `include_paths`/`exclude_paths`
    further drop an edge unless both endpoints' definition files pass the path
    prefix filter (same symmetric shape as `exclude_tests`).
    `limit` caps the list (default 40): lower it to spend fewer tokens, raise
    it when `truncated` is true — `total` always reports the full ranked count.
    """
    ranked, total = store.hotspots(
        limit=limit,
        kind=kind,
        exclude_tests=exclude_tests,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    truncated = total > len(ranked)
    items: list[dict[str, Any]] = []
    for symbol, count in ranked:
        node = store.get_node(symbol)
        item = (
            _node_dict(node, full_symbols)
            if node is not None
            else {"name": _short_label(symbol), "file": None, "line": None}
        )
        if full_symbols:
            item["symbol"] = symbol
        item["count"] = count
        items.append(item)
    return {
        "kind": kind,
        "total": total,
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "hotspots": items,
    }


def stats_summary(
    store: GraphStore,
    group_by: str = "file",
    limit: int = DEFAULT_LIMIT,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Module-level aggregate counts — "how big / how connected is this part of
    the codebase?" without reading a single file, the size/density view next to
    `hotspots`' "what's most-called".

    `group_by="file"` (default) counts per file; `"dir"` rolls up per directory
    (`dirname`, top-level files in `"."`). Each row: `symbols` defined there,
    `edges` = `calls` edges whose call site is there, `refs` = reference use
    sites there, sorted by the three summed, descending. `include_paths`/
    `exclude_paths` drop a file's counts entirely on path-prefix mismatch
    (e.g. scope out vendored deps). `limit` caps the list (default 40): lower it
    to spend fewer tokens, raise it when `truncated` is true — `total` always
    reports the full group count.
    """
    groups, total = store.stats(
        group_by=group_by,
        limit=limit,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    return {
        "group_by": group_by,
        "total": total,
        "truncated": total > len(groups),
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "stats": groups,
    }


def line_span_ranking(
    store: GraphStore,
    limit: int = DEFAULT_LIMIT,
    exclude_tests: bool = False,
    full_symbols: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Definitions ranked by body extent — `end_line - start line`, largest
    first. Where the biggest bodies live, from the exact `enclosing_range`
    extents (#504), not a def→next-symbol heuristic.

    On a store without body extents (a stock-binary graph) the response is
    `{"available": false, "reason": …}` — the same convention `references`
    uses — not a silently empty list. `exclude_tests` drops definitions in
    test files; `include_paths`/`exclude_paths` filter by definition-file path
    prefix. `limit` caps the list (default 40); `total` always reports the
    full filtered count.
    """
    result = store.line_span(
        limit=limit,
        exclude_tests=exclude_tests,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    if result is None:
        return {"available": False, "reason": _NO_ENCLOSING_RANGES}
    ranked, total = result
    items: list[dict[str, Any]] = []
    for symbol, span in ranked:
        node = store.get_node(symbol)
        item = (
            _node_dict(node, full_symbols)
            if node is not None
            else {"name": _short_label(symbol), "file": None, "line": None}
        )
        if full_symbols:
            item["symbol"] = symbol
        item["span"] = span
        items.append(item)
    return {
        "total": total,
        "truncated": total > len(ranked),
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "definitions": items,
    }


def no_incoming_calls_report(
    store: GraphStore,
    limit: int = DEFAULT_LIMIT,
    exclude_tests: bool = False,
    full_symbols: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Callable definitions with zero incoming `calls` edges — the exact
    primitive behind the "dead code" question, stated as a graph fact
    (`callers_of(sym) == []`), never a verdict. Unreliable as "dead": vtable
    dispatch, exported API, templates, entry points all have no static caller
    yet are live — the tool states the fact (a standing `note` on every
    response) and the LLM judges.

    Refuses (same `available: false` + `reason` convention as `line_span`) on
    a stock-binary graph, where a phantom caller from a mis-attributed
    declaration site can turn a real 0 into a false 1. `exclude_tests` drops
    definitions in test files (a test caller still counts as a caller);
    `include_paths`/`exclude_paths` filter by definition-file path prefix.
    `limit` caps the list (default 40); `total` always reports the full count.
    """
    result = store.no_incoming_calls(
        limit=limit,
        exclude_tests=exclude_tests,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )
    if result is None:
        return {"available": False, "reason": _STOCK_ATTRIBUTION_UNRELIABLE}
    symbols, total = result
    items: list[dict[str, Any]] = []
    for symbol in symbols:
        node = store.get_node(symbol)
        item = (
            _node_dict(node, full_symbols)
            if node is not None
            else {"name": _short_label(symbol), "file": None, "line": None}
        )
        if full_symbols:
            item["symbol"] = symbol
        items.append(item)
    return {
        "total": total,
        "truncated": total > len(symbols),
        "excluded_tests": exclude_tests,
        "include_paths": include_paths,
        "exclude_paths": exclude_paths,
        "note": (
            "0 static callers is a graph fact, not proof of dead code — vtable "
            "dispatch, exported API, templates, and entry points (e.g. main, "
            "setup/loop) all have no static caller yet are live. The tool "
            "states the fact; the judgment is yours."
        ),
        "definitions": items,
    }


def boundary_violation_report(
    store: GraphStore,
    rules: list[list[str]],
    edge_kinds: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
) -> dict[str, Any]:
    """Declared-layering conformance check: the `calls`/`inherits` edges that
    cross rules the CALLER supplies — e.g. `rules=[["common/", "platform/"]]`
    means "no symbol defined under `common/` may call one defined under
    `platform/`". The rules are yours (the project's SPEC); the graph stores no
    intended architecture, it only confronts exact edges with the given
    constraint — so every reported violation is a real compiler-traced edge
    (zero false positives), while 0 violations is a lower bound, not proof of
    conformance (see the standing `note`).

    `edge_kinds` defaults to `["calls", "inherits"]` (`"implements"` = override
    relationships, also accepted). `limit` caps the list (default 40); `total`
    always reports the full count. An edge matching several rules appears once
    per rule, each record naming the rule it broke. A malformed rule comes
    back as an `{"error", "hint"}` dict showing the expected shape, not an
    exception.
    """
    kinds = tuple(edge_kinds) if edge_kinds else ("calls", "inherits")
    try:
        violations, total = store.boundary_violations(
            [tuple(r) for r in rules], edge_kinds=kinds, limit=limit
        )
    except ValueError as e:
        return {
            "error": f"invalid rules: {e}",
            "hint": (
                "each rule is a [from_prefix, forbidden_prefix] pair, e.g. "
                '[["common/", "platform/"]] = "common/ must not call platform/"'
            ),
        }

    def _name(symbol: str) -> str:
        return symbol if full_symbols else _label(symbol, store.get_node(symbol))

    items: list[dict[str, Any]] = [
        {
            "rule": v["rule"],
            "kind": v["kind"],
            "src": _name(v["src"]),
            "dst": _name(v["dst"]),
            "file": v["file"],
            "line": _line1(v["line"]),
        }
        for v in violations
    ]
    return {
        "rules": [
            f"{from_prefix} -> {forbidden_prefix}" for from_prefix, forbidden_prefix in rules
        ],
        "edge_kinds": list(kinds),
        "total": total,
        "truncated": total > len(violations),
        "note": _BOUNDARY_NOTE,
        "violations": items,
    }


def file_outline_report(
    store: GraphStore,
    file: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
) -> dict[str, Any]:
    """The outline of one file: every symbol *defined* in it, sorted by line —
    "what's in this file?" without reading it. A compact symbol list that
    replaces a `Read` of the file (the same reflex-replacement
    `explain_symbol` is for one definition), exact and available on any graph
    (no #504 needed).

    `file` is the exact path as recorded in the index (relative, e.g.
    `src/app.cpp`) — not a prefix. `limit` caps the list (default 40); `total`
    always reports the full count. An empty result carries a `note` (a wrong
    path is the usual cause — `stats` lists the indexed files), never a bare
    zero that would read as "empty file".
    """
    nodes, total = store.outline(file, limit=limit)
    result: dict[str, Any] = {
        "file": file,
        "total": total,
        "truncated": total > len(nodes),
        "definitions": [_node_dict(n, full_symbols) for n in nodes],
    }
    if total == 0:
        result["note"] = (
            "no symbols defined in this file in the index — the path must match "
            "the index's recorded relative path exactly (e.g. `src/app.cpp`, not "
            "absolute, not a prefix); `stats` lists the indexed files"
        )
    return result


def class_members_report(
    store: GraphStore,
    symbol: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
) -> dict[str, Any]:
    """Every member declared on a class/struct — methods, fields, nested
    types — the class's real member list, replacing a header `grep`. Exact by
    construction: a member's SCIP symbol string starts with its class's own
    (container nesting), so the list is the compiler's, not a text match.

    Named `class_members`, not `public_api`, because SCIP doesn't encode C++
    visibility — this lists members that *exist* (a fact), not a
    public/private claim. `symbol` is a name or an exact SCIP string (a unique
    name resolves automatically; note a bare class name typically also matches
    its own members, so the exact `#`-terminated string is the reliable input).
    Unknown symbol → the shared error/candidates convention; a known non-type
    symbol → an error dict — bad input, never an empty list that would read
    as "memberless class". `limit` caps the list (default 40); `total` always
    reports the full count.
    """
    symbol, _alt = _resolve(store, symbol)
    if _alt is not None:
        return _alt
    result = store.class_members(symbol, limit=limit)
    if result is None:
        return {
            "error": (
                f"{symbol} is not a type (class/struct/enum): `class_members` lists "
                "the members declared on a class — use `find` to locate the class "
                "symbol (it ends in `#`)"
            ),
        }
    members, total = result
    out: dict[str, Any] = {
        "symbol": symbol,
        "total": total,
        "truncated": total > len(members),
        "members": [_node_dict(n, full_symbols) for n in members],
    }
    if total == 0:
        out["note"] = (
            "no members recorded on this type — it may be genuinely memberless, "
            "defined outside the indexed scope, or only forward-declared; "
            "`find_references` shows where it is used"
        )
    return out


def explain(
    store: GraphStore,
    symbol: str,
    root: str | None = None,
    include_source: bool = False,
    context: int = 3,
    limit: int = EXPLAIN_LIMIT,
    full_symbols: bool = False,
    exclude_tests: bool = True,
    hide_trivial: bool = False,
) -> dict[str, Any]:
    """Definition site + caller/callee summary for `symbol`.

    Coordinates only by default (token-cheap). `include_source=True` adds a
    source snippet, but only if `root` (a checkout) is given — otherwise there's
    no file to read. `source` is `None` (not omitted) when a snippet was asked
    for but the file couldn't be read, so the caller can tell "not requested"
    from "requested, unavailable". Test-file callers/callees are dropped by
    default (`exclude_tests`); `full_symbols=True` keeps the raw SCIP strings in
    the caller/callee lists. With `hide_trivial=True`, ubiquitous helpers
    (operators, `*assert`, `makeStatus`, `source_location`, …) are dropped from
    the caller/callee lists, each side reporting its `trivial_hidden` count.
    With `root` given, `signature` is a best-effort parameter list read from the
    definition site — including any default argument value, verbatim (e.g.
    `(bool useNullIfMissing = false)`) — since the graph itself never carries a
    parsed signature and a defaulted param is otherwise invisible without
    opening the header."""
    node = store.get_node(symbol)
    if node is None:
        return {"error": _UNKNOWN.format(symbol=symbol)}
    caller_edges = store.callers_of(symbol)
    callee_edges = store.callees_of(symbol)
    if exclude_tests:
        caller_edges = _drop_test_edges(store, caller_edges, on="src")
        callee_edges = _drop_test_edges(store, callee_edges, on="dst")
    callers_trivial = callees_trivial = 0
    if hide_trivial:
        kept_callers = [e for e in caller_edges if not _is_trivial_callee(e.src)]
        kept_callees = [e for e in callee_edges if not _is_trivial_callee(e.dst)]
        callers_trivial = len(caller_edges) - len(kept_callers)
        callees_trivial = len(callee_edges) - len(kept_callees)
        caller_edges, callee_edges = kept_callers, kept_callees
    shown_callers, callers_trunc = _capped(caller_edges, limit)
    shown_callees, callees_trunc = _capped(callee_edges, limit)

    callers_block: dict[str, Any] = {
        "total": len(caller_edges),
        "truncated": callers_trunc,
        "items": [_edge_dict(e, e.src, store, full_symbols) for e in shown_callers],
    }
    callees_block: dict[str, Any] = {
        "total": len(callee_edges),
        "truncated": callees_trunc,
        "items": [_edge_dict(e, e.dst, store, full_symbols) for e in shown_callees],
    }
    if hide_trivial:
        callers_block["trivial_hidden"] = callers_trivial
        callees_block["trivial_hidden"] = callees_trivial

    result: dict[str, Any] = {
        "symbol": node.symbol,
        "name": node.display_name or None,
        "defined_at": {"file": node.file, "line": _line1(node.line)},
        "excluded_tests": exclude_tests,
        "callers": callers_block,
        "callees": callees_block,
    }

    if root is not None:
        # Always set the key, even when extraction failed (None) — mirrors
        # `source`'s "None means requested but unavailable, absent means not
        # requested" convention, rather than silently omitting the key and
        # letting the caller wonder if signature extraction was even attempted.
        result["signature"] = extract_signature(root, node.file, node.line)

    if include_source and root is not None and node.file is not None and node.line is not None:
        snippet = read_source_snippet(root, node.file, node.line, context=context)
        if snippet is None:
            result["source"] = None  # requested but unreadable
        else:
            result["source"] = [
                {"line": lineno + 1, "text": text, "is_def": lineno == node.line}
                for lineno, text in snippet
            ]
    return result


def status_report(
    store: GraphStore, root: str | None = None, check_updates: bool = True, force: bool = False
) -> dict[str, Any]:
    """The graph's provenance and, with `root`, whether the checkout has drifted.

    The "should I trust this graph?" check an LLM runs first: `drift.up_to_date`
    False means re-index before relying on the topology. Only C++ source changes
    count as drift (docs/build-config edits don't change the call graph).

    Also reports `tool` advice (unless `check_updates=False`): whether a newer
    cppgraph is published and — crucially — whether adopting it, or the version
    already installed, needs a full graph rebuild. Best-effort and cached; `force`
    bypasses the cache. See `cppgraph.updates`.
    """
    m = store.meta()
    commit = m.get("source_commit")
    result: dict[str, Any] = {
        "transport": "mcp",
        "graph_meta": {
            "source_commit": commit,
            "source_dirty": m.get("source_dirty") == "true",
            "project_root": m.get("project_root"),
            "built_at": m.get("built_at"),
            "indexed_with": " ".join(
                v for v in (m.get("index_tool"), m.get("index_tool_version")) if v
            )
            or None,
            "index_filter": m.get("index_filter"),
            "index_scope": (
                None if m.get("index_filter") is None else (m.get("index_filter") or "whole tree")
            ),
            "index_tests": m.get("index_tests"),
            "schema_version": m.get("schema_version"),
            "cppgraph_version": m.get("cppgraph_version"),
            "has_references": m.get("has_references") == "true",
            "has_attributed_refs": m.get("has_attributed_refs") == "true",
            "has_enclosing_ranges": m.get("has_enclosing_ranges") == "true",
            "node_count": m.get("node_count"),
            "edge_count": m.get("edge_count"),
            "ref_count": m.get("ref_count"),
            "attributed_ref_count": m.get("attributed_ref_count"),
        },
        "source_commit": commit,
        "drift": {"checked": False},
    }
    # Make the usage-view granularity — and how to upgrade it — explicit, since
    # it materially changes what "where is this used?" answers (functions vs files).
    if m.get("has_references") == "true":
        if m.get("has_attributed_refs") == "true":
            result["usage_view"] = {
                "granularity": "symbol",
                "note": "references are attributed to their enclosing definition "
                "('where is X used?' returns the functions/types that use it).",
            }
        else:
            result["usage_view"] = {
                "granularity": "file",
                "note": "references are exact but unattributed — 'where is X used?' "
                "answers at file granularity only.",
                "upgrade": "For symbol granularity (the using functions, not just "
                "files), index with a #504-built scip-clang, then rebuild with "
                "`cppgraph build --attributed-refs` or enrich in place with "
                "`cppgraph enrich-refs --graph <db> --scip <index.scip>`. Costs extra "
                "store space; worth it when you want symbol-level usage.",
            }
    if check_updates:
        result["tool"] = update_advice(m.get("cppgraph_version"), force=force)
        result["scip_clang"] = scip_update_advice(
            {"version": m.get("index_tool_version"), "variant": m.get("index_tool_variant")},
            force=force,
        )
    if root is None or not commit:
        if root is not None and not commit:
            result["drift"] = {"checked": False, "reason": "no source commit recorded in the graph"}
        return result

    changes = changed_files_since(root, commit, dirty_fingerprints=read_dirty_fingerprints(m))
    if changes is None:
        result["drift"] = {
            "checked": False,
            "reason": f"{root} is not a git checkout (or git unavailable)",
        }
        return result
    changed = [f for f in changes[0] if f.endswith(SOURCE_EXTS)]
    deleted = [f for f in changes[1] if f.endswith(SOURCE_EXTS)]
    behind = commits_behind(root, commit)
    verdict = staleness_verdict(
        len(changed), len(deleted), store.indexed_file_count(), commits_behind=behind
    )
    drift: dict[str, Any] = {
        "checked": True,
        "up_to_date": verdict["up_to_date"],
        "changed": changed[:DEFAULT_LIMIT],
        "deleted": deleted[:DEFAULT_LIMIT],
        "changed_total": len(changed),
        "deleted_total": len(deleted),
        "commits_behind": behind,
    }
    if not verdict["up_to_date"]:
        drift["changed_fraction"] = verdict.get("changed_fraction")
        drift["recommend"] = verdict["recommend"]  # "update" | "rebuild"
        drift["next"] = (
            "run `cppgraph update` to refresh the graph"
            if verdict["recommend"] == "update"
            else "drift too large for incremental — re-index the whole target and rebuild"
        )
    result["drift"] = drift
    return result


def make_export(
    store: GraphStore,
    symbol: str,
    mode: str = "deps",
    depth: int = 2,
    direction: str = "both",
    exclude_tests: bool = False,
) -> dict[str, Any] | None:
    """Build the graph.json dict for a symbol, or None if unknown (see
    `cppgraph.cli.build_export_json`)."""
    return build_export_json(
        store,
        symbol,
        mode=mode,
        depth=depth,
        direction=direction,
        exclude_tests=exclude_tests,
    )


_NO_GRAPH = {
    "error": "no cppgraph index found for the current project. Build one with "
    "scripts/index.sh (it writes <project>/.cppgraph/…), then reopen this "
    "Claude Code session from the project directory."
}


class _ReloadingStore:
    """A `GraphStore` handle that reopens when the `.graph.db` changes on disk.

    The MCP server is long-lived, but a the index wizard/`cppgraph init` run overwrites
    the graph file underneath it. Without reloading, every query (and `status`)
    would keep answering from the stale graph held open at launch until Claude Code
    restarts — the "status says 18k nodes, the rebuild made 15k" confusion. `get()`
    checks the file mtime and re-opens (closing the old handle) when it advances.
    """

    def __init__(self, graph_path: str | Path | None) -> None:
        self._path = Path(graph_path) if graph_path else None
        self._store = GraphStore(self._path) if self._path else None
        self._mtime = self._current_mtime()

    def _current_mtime(self) -> float | None:
        if self._path is None:
            return None
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def get(self) -> GraphStore | None:
        if self._path is None:
            return None
        current = self._current_mtime()
        if current is not None and (
            self._store is None or self._mtime is None or current > self._mtime
        ):
            if self._store is not None:
                self._store.close()
            self._store = GraphStore(self._path)
            self._mtime = current
        return self._store


def _server_instructions(store: GraphStore | None) -> str:
    """The `initialize` instructions string a client injects into system context.

    Steers a connecting model to prefer the graph tools over text search *for code
    within the indexed scope*, while explicitly keeping plain read/grep correct
    everywhere else (and for paging a file already located). The real scope is
    baked in from graph meta at startup, so the model needn't call `status` first.
    """
    if store is None:
        return (
            "cppgraph is connected but no indexed graph was found from the launch "
            "directory. Its tools will report that until a project here is indexed; "
            "until then, use your normal read/search tools."
        )
    m = store.meta()
    bits: list[str] = []
    if m.get("index_filter"):
        bits.append(f"path filter `{m['index_filter']}`")
    if m.get("index_tests") in ("included", "excluded"):
        bits.append(f"tests {m['index_tests']}")
    commit = (m.get("source_commit") or "")[:8]
    if commit:
        bits.append(f"built @ {commit}" + (" (dirty)" if m.get("source_dirty") == "true" else ""))
    scope = ("Indexed: " + "; ".join(bits) + ".") if bits else "Run `status` for the indexed scope."
    return (
        "cppgraph serves a precomputed, compiler-exact graph of this repo's indexed "
        f"C++ code. {scope}\n\n"
        "For code WITHIN that scope, prefer these tools over text search "
        "(grep/ripgrep/sed):\n"
        "- locate a symbol by name -> `find` (symbol-aware: no hits in comments, "
        "strings, or unrelated code; dedups overloads; returns signatures).\n"
        "- read a symbol's definition -> `explain_symbol` (include_source=true) "
        "rather than grepping or opening the file.\n"
        "- relationships (callers, callees, call paths, impact, class hierarchy) -> "
        "`who_calls`/`what_it_calls`/`path`/`impact_of`/`base_classes`/`subclasses`. "
        "Text search cannot resolve overloads or virtual dispatch, or tell a "
        "declaration from a use, so its answers here are noisy and often wrong.\n\n"
        "Keep using your normal read/search tools for: files outside the indexed "
        "scope, non-indexed languages, comments, string literals, generated/build "
        "files — and for reading a known line range of a file you have already "
        "located (cppgraph's edge is locating and relating, not paging).\n\n"
        "The graph is a snapshot; if code may have changed since it was built, "
        "`status` reports drift. The scope line above is fixed at connect time — if "
        "the project is re-indexed mid-session, ask the user to reload this MCP "
        "server (e.g. Claude Code: `/mcp`) to refresh it; query results themselves "
        "already reflect the current graph on disk."
    )


def build_server(graph_path: str | Path | None, root: str | None = None) -> Any:
    """A FastMCP server for one project's graph (opened once, reused per call).

    `graph_path` is resolved at launch — explicitly (`--graph`) or discovered
    from the cwd (`discover_graph`) — so tools never take a graph argument. If it
    is `None` (no indexed project above the cwd), the server still starts and
    every tool returns a clear "not indexed here" message. `root` is the checkout
    used for `status` drift and source snippets.
    """
    from mcp.server.fastmcp import FastMCP

    stores = _ReloadingStore(graph_path)
    mcp = FastMCP("cppgraph", instructions=_server_instructions(stores.get()))

    def _call(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run a pure `(store, …) -> dict` query, or return the no-graph notice.
        Attaches a cheap `stale` flag (git diff, no rebuild) when `root` and a
        recorded source commit make the check possible — the per-query drift
        signal `status`'s full report already computes, without its cost."""
        s = stores.get()
        if s is None:
            return dict(_NO_GRAPH)
        result = fn(s, *args, **kwargs)
        if root is not None:
            try:
                result["stale"] = is_stale(s, root, SOURCE_EXTS)
            except Exception:
                result["stale"] = None
        return result

    @mcp.tool()
    def find(
        query: str,
        limit: int = DEFAULT_LIMIT,
        hide_trivial: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Use this instead of grep/ripgrep to locate a code symbol by name.
        Find C++ symbols by name. The other tools also accept a plain name (a
        unique one resolves automatically), but `find` is how you disambiguate
        when several symbols share a name and inspect signatures/overloads. A multi-word query is an
        order-free AND (every word must appear); overloads sharing a qualified
        name group under one result, each with a source-derived `signature` so
        the arms are distinguishable. If nothing matches exactly, `find` relaxes
        once (case/separator-insensitive — `changestream` ~ `change_stream` —
        then, for a `Class#method` guess, the bare leaf name) and flags the
        response `relaxed`. Set `hide_trivial=True` to drop compiler-generated /
        boilerplate hits (lambdas, operators, `*assert`, `makeStatus`, …) —
        `trivial_hidden` reports how many were cut. `include_paths`/
        `exclude_paths` filter matches by their definition file's path prefix
        (e.g. scope out vendored deps). `limit` caps the list
        (default 40): lower it to spend fewer tokens, raise it when `truncated`."""
        return _call(
            find_symbols,
            query,
            limit=limit,
            hide_trivial=hide_trivial,
            root=root,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def who_calls(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Direct callers of a symbol (one call hop). `symbol` is a name or an
        exact SCIP string — a unique name resolves automatically, an ambiguous one
        returns candidates. Each caller is returned by human `name` + `file:line`
        (compact); set `full_symbols=True` for the raw SCIP strings. Test callers
        are dropped by default — pass `exclude_tests=False` to include them.
        `include_paths`/`exclude_paths` further filter callers by their
        definition file's path prefix (e.g. scope out vendored deps).
        `limit` caps the list (default 40): lower it to spend fewer tokens when a
        few callers are enough, raise it when `truncated` is true."""
        return _call(
            callers,
            symbol,
            limit=limit,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def what_it_calls(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
        hide_trivial: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Direct callees of a symbol (one call hop). `symbol` is a name or an
        exact SCIP string — a unique name resolves automatically, an ambiguous one
        returns candidates. Compact `name` + `file:line` by default
        (`full_symbols=True` for raw SCIP); callees in test files dropped unless
        `exclude_tests=False`. Set `hide_trivial=True` to drop ubiquitous helpers
        (operators, tassert/uassert, makeStatus, source_location, …) so the
        domain edges stand out — `trivial_hidden` reports how many were cut.
        `include_paths`/`exclude_paths` further filter callees by their
        definition file's path prefix (e.g. scope out vendored deps).
        `limit` caps the list (default 40): lower it to spend fewer tokens when a
        few callees are enough, raise it when `truncated` is true.

        NOTE: this is an unordered *set* of callees, not an execution sequence.
        It cannot tell you the order calls happen in, nor which are conditional
        (`if (…) x();`). For stage/step order, read the function body — sorting
        callees by `file:line` only approximates textual order, not runtime
        order."""
        return _call(
            callees,
            symbol,
            limit=limit,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
            hide_trivial=hide_trivial,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def base_classes(
        symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Direct base classes a type inherits from (`symbol` is a name or an exact
        SCIP type string, ending in `#`; a unique name resolves automatically).
        Compact `name` + `file:line` by default; `full_symbols=True` for raw SCIP
        strings. `limit` caps the list.
        An empty result carries a `note` (the type may be a root, or a holder
        with no base) rather than a bare `0`."""
        return _call(bases, symbol, limit=limit, full_symbols=full_symbols)

    @mcp.tool()
    def subclasses(
        symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Direct subclasses of a type (one inheritance hop). For the whole
        subtree use `impact_of` with kind="inherits". Compact `name` +
        `file:line` by default; `full_symbols=True` for raw SCIP strings. `limit`
        caps the list. An empty result carries a `note` (the type may be a leaf,
        or a holder that isn't itself a base) rather than a bare `0`."""
        return _call(subtypes, symbol, limit=limit, full_symbols=full_symbols)

    @mcp.tool()
    def find_references(
        symbol: str,
        include_source: bool = False,
        context: int = 0,
        limit: int = DEFAULT_LIMIT,
        exclude_tests: bool = True,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Exact use sites of a symbol ("where is this type/symbol used?") — the
        dependency the call graph can't show (a struct has no callers). Returns
        `file:line` coordinates; set `include_source=True` to also get the code
        **inline** (cppgraph reads it for you — no separate file read needed),
        grouped by file with overlapping windows merged (no duplicated lines).
        `context` sets the lines shown around each site. Test-file uses are
        dropped by default — pass `exclude_tests=False` to include them.
        `include_paths`/`exclude_paths` further filter uses by their file's path
        prefix (e.g. scope out vendored deps). `limit`
        caps the list. If the graph was built with `--no-references`, `available`
        is false (rebuild with references to enable this)."""
        return _call(
            references,
            symbol,
            root=root,
            include_source=include_source,
            context=context,
            limit=limit,
            exclude_tests=exclude_tests,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def path(src: str, dst: str) -> dict[str, Any]:
        """Shortest chain of `calls` edges from `src` to `dst` (names or exact
        SCIP strings; a unique name resolves automatically), as an ordered node
        list with `hops`. Returns `found=false` with
        a `hint` when there's no *static* path — which may mean the flow crosses
        runtime dispatch (a virtual call / a registered factory), not that the two
        are unrelated."""
        return _call(call_path, src, dst)

    @mcp.tool()
    def impact_of(
        symbol: str,
        depth: int | None = None,
        limit: int = DEFAULT_LIMIT,
        kind: str = "calls",
        full_symbols: bool = False,
        exclude_tests: bool = True,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reverse blast-radius: everything that transitively reaches `symbol`.
        kind="calls" (default) = transitive callers ("what could break if I
        change this function?"); kind="inherits" = every transitive subclass of
        a base type. `depth` bounds the hops. Compact `name` + `file:line` by
        default (`full_symbols=True` for raw SCIP); symbols in test files dropped
        unless `exclude_tests=False`. `include_paths`/`exclude_paths` further
        filter by definition-file path prefix (e.g. scope out vendored deps).
        `limit` caps the list (default 40): lower it
        to spend fewer tokens, raise it when `truncated`."""
        return _call(
            impact,
            symbol,
            depth=depth,
            limit=limit,
            kind=kind,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def hotspots(
        limit: int = DEFAULT_LIMIT,
        kind: str = "fan_in",
        exclude_tests: bool = False,
        full_symbols: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Global ranking by call-edge volume — "top N most-called / most-calling
        symbols in the project", the question `who_calls`/`what_it_calls` can't
        answer without N manual calls. kind="fan_in" (default) = most-called
        (incoming `calls` edges); kind="fan_out" = most-calling (outgoing);
        kind="edges" = both summed. Compact `name` + `file:line` by default
        (`full_symbols=True` for raw SCIP); pass `exclude_tests=True` to drop
        edges whose call site is in a test file. `include_paths`/`exclude_paths`
        further filter by definition-file path prefix (e.g. scope out vendored
        deps). `limit` caps the list (default
        40): lower it to spend fewer tokens, raise it when `truncated`."""
        return _call(
            hotspot_ranking,
            limit=limit,
            kind=kind,
            exclude_tests=exclude_tests,
            full_symbols=full_symbols,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def stats(
        group_by: str = "file",
        limit: int = DEFAULT_LIMIT,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Module-level aggregate counts — "how big / how dense is this part of
        the codebase?" without reading a single file: symbols defined, `calls`
        edges whose call site, and ref use sites per file (group_by="file",
        default) or rolled up per directory (group_by="dir"), sorted by the
        three summed descending. Scales an unfamiliar module/repo at a glance,
        the size/density view next to `hotspots`' "what's most-called".
        `include_paths`/`exclude_paths` drop a file's counts on path-prefix
        mismatch (e.g. scope out vendored deps). `limit` caps the list (default
        40): lower it to spend fewer tokens, raise it when `truncated` —
        `total` always reports the full group count."""
        return _call(
            stats_summary,
            group_by=group_by,
            limit=limit,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def line_span(
        limit: int = DEFAULT_LIMIT,
        exclude_tests: bool = False,
        full_symbols: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Definitions ranked by body extent — top N largest bodies
        (`end_line - start line`, largest first), the exact extents from
        `enclosing_range`, not a def→next-symbol heuristic. Needs a graph
        indexed with a #504-built scip-clang (the same binary `status`'s
        usage-view upgrade advice names — it is what emits enclosing ranges):
        on a stock-binary graph the tool returns `available: false` with the
        rebuild pointer instead of a silently empty list. `exclude_tests` drops
        definitions in test files; `include_paths`/`exclude_paths` filter by
        definition-file path prefix (e.g. scope out vendored deps). `limit`
        caps the list (default 40): lower it to spend fewer tokens, raise it
        when `truncated` — `total` always reports the full count."""
        return _call(
            line_span_ranking,
            limit=limit,
            exclude_tests=exclude_tests,
            full_symbols=full_symbols,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def no_incoming_calls(
        limit: int = DEFAULT_LIMIT,
        exclude_tests: bool = False,
        full_symbols: bool = False,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Callable definitions with zero incoming `calls` edges — the exact
        primitive behind the "dead code" question, stated as a graph fact
        (`callers_of(sym) == []`), never a verdict. Unreliable as "dead" —
        vtable dispatch, exported API, templates, entry points all have no
        static caller yet are live — so the tool states the fact (a standing
        `note` on every response) and the LLM judges. Only trustworthy on a
        graph indexed with a #504-built scip-clang: on a stock binary, caller
        attribution can mis-attribute a bodyless declaration site to the
        preceding definition — a phantom caller that turns a real 0 into a
        false 1 — so there the tool refuses (`available: false`, reason
        included) instead of answering. Compact `name` + `file:line` by
        default (`full_symbols=True` for raw SCIP). `exclude_tests` drops
        definitions in test files, but a test caller still counts as a caller.
        `include_paths`/`exclude_paths` filter by definition-file path prefix.
        `limit` caps the list (default 40) — `total` always reports the full
        count."""
        return _call(
            no_incoming_calls_report,
            limit=limit,
            exclude_tests=exclude_tests,
            full_symbols=full_symbols,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    @mcp.tool()
    def boundary_violations(
        rules: list[list[str]],
        edge_kinds: list[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
    ) -> dict[str, Any]:
        """Declared-layering conformance check. Takes rules from YOU (the
        project's intended layering — the graph doesn't know it): each rule is
        [from_prefix, forbidden_prefix], e.g. rules=[["common/", "platform/"]]
        = "no symbol defined under common/ may call one defined under
        platform/". Reports every calls/inherits edge that crosses a rule —
        each violation IS a real compiler-traced edge, so zero false positives;
        0 violations is a lower bound (no *statically indexed* edge crosses —
        runtime dispatch may), never proof the layering holds. Directory
        membership is the endpoint's own definition file, matched on a
        path-segment boundary ("common/" matches common/util.cpp, never
        commons/util.cpp). edge_kinds defaults to ["calls", "inherits"]
        ("implements" also accepted). `limit` caps the list (default 40):
        lower it to spend fewer tokens, raise it when `truncated` — `total`
        always reports the full count."""
        return _call(
            boundary_violation_report,
            rules=rules,
            edge_kinds=edge_kinds,
            limit=limit,
            full_symbols=full_symbols,
        )

    @mcp.tool()
    def outline(
        file: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Use this instead of Read/opening a file to see what's in it: the
        outline of one file — every symbol defined in it, sorted by line. A
        compact symbol list that replaces reading a 1400-line file, exact and
        available on any graph. `file` is the exact path as recorded in the
        index (relative, e.g. `src/app.cpp`, not a prefix) — `stats` lists the
        indexed files; an empty result carries a note explaining that, never a
        bare zero. Compact `name` + `file:line` by default (`full_symbols=True`
        for raw SCIP). `limit` caps the list (default 40): raise it when
        `truncated` — `total` always reports the full count."""
        return _call(file_outline_report, file, limit=limit, full_symbols=full_symbols)

    @mcp.tool()
    def class_members(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
    ) -> dict[str, Any]:
        """Every member declared on a class/struct — methods, fields, nested
        types — the class's real member list, replacing a header grep. `symbol`
        is a name or an exact SCIP string (a unique name resolves automatically,
        an ambiguous one returns candidates — a bare class name typically also
        matches its own members, so pick the `#`-terminated entry); an unknown
        or non-type symbol comes back as an error dict. Named `class_members`,
        not `public_api`, because SCIP doesn't encode C++ visibility — members
        that *exist* (a fact), not a public/private claim. Compact `name` +
        `file:line` by default (`full_symbols=True` for raw SCIP). `limit` caps
        the list (default 40): raise it when `truncated` — `total` always
        reports the full count."""
        return _call(class_members_report, symbol, limit=limit, full_symbols=full_symbols)

    @mcp.tool()
    def explain_symbol(
        symbol: str,
        include_source: bool = False,
        context: int = 3,
        limit: int = EXPLAIN_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
        hide_trivial: bool = False,
    ) -> dict[str, Any]:
        """Use this instead of grep/sed/opening the file to read a symbol's
        definition. Definition site + caller/callee summary for `symbol`. Returns
        `file:line` coordinates by default; set `include_source=True` to also get
        the definition's source snippet **inline** (cppgraph reads it for you — no
        separate file read needed), `context` lines around the definition. `limit`
        caps each of the caller/callee lists (default 10): lower it to spend fewer
        tokens, raise it when `truncated` is true and you need more. Caller/callee lists
        are compact `name` + `file:line` (`full_symbols=True` for raw SCIP) and
        drop test files unless `exclude_tests=False`. Set `hide_trivial=True` to
        also drop ubiquitous helpers (operators, `*assert`, `makeStatus`,
        `source_location`, …) — each list reports its `trivial_hidden` count.
        With a checkout configured (`--root`), also returns `signature`: the
        parameter list read from source, including any default argument value
        verbatim (e.g. `(bool useNullIfMissing = false)`) — invisible from the
        graph alone, which never carries a parsed signature."""
        return _call(
            explain,
            symbol,
            root=root,
            include_source=include_source,
            context=context,
            limit=limit,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
            hide_trivial=hide_trivial,
        )

    @mcp.tool()
    def status(force_update_check: bool = False) -> dict[str, Any]:
        """Graph provenance and drift: is this graph still current for the
        checkout? Run first — if `drift.up_to_date` is false, re-index before
        trusting the topology. Also reports `tool`: whether a newer cppgraph is
        published and, if so, at which `rebuild` level adopting it costs — `none`
        (no rebuild), `store` (rebuild the store from the existing .scip), or
        `reindex` (re-run scip-clang) — so you can warn before an upgrade blocks
        on indexing. The update check is cached; `force_update_check=True`
        refetches now."""
        return _call(status_report, root=root, force=force_update_check)

    @mcp.tool()
    def visualize(
        symbol: str,
        mode: str = "deps",
        depth: int = 2,
        direction: str = "both",
        exclude_tests: bool = False,
        open_browser: bool = True,
    ) -> dict[str, Any]:
        """Render a small graph around `symbol` as a self-contained HTML in a temp
        dir and (by default) open it in the user's browser — the "show me the
        dependency graph of X" tool. Keep it small: a big neighbourhood is an
        unreadable hairball, so prefer depth 1-2. mode="deps" (default) = the
        call/inherit subgraph; mode="usage" = a graph of where the symbol is used
        (the right view for a type, which has no call edges). Usage is drawn at
        SYMBOL granularity (symbol -> the definitions that use it) when the graph
        was built with attributed references, else at file granularity; both are
        exact. See `status` (`usage_view`) for which, and how to upgrade.
        direction (deps mode) = "both" (default) both callers and callees, "out"
        only what the symbol reaches, "in" only what reaches it. Set
        exclude_tests=True to drop test files and show production usage only. Set
        open_browser=False to just get the HTML path without launching a browser.
        Returns the HTML path and the command to open it (in case the browser
        didn't launch)."""
        from cppgraph.viz_html import open_in_browser, write_temp_html

        s = stores.get()
        if s is None:
            return dict(_NO_GRAPH)
        graph_json = make_export(
            s,
            symbol,
            mode=mode,
            depth=depth,
            direction=direction,
            exclude_tests=exclude_tests,
        )
        if graph_json is None:
            return {"error": _UNKNOWN.format(symbol=symbol)}
        html_path = write_temp_html(graph_json)
        result: dict[str, Any] = {
            "path": str(html_path),
            "mode": mode,
            "nodes": len(graph_json["nodes"]),
            "edges": len(graph_json["links"]),
            "open_command": f"open {html_path}",
        }
        if open_browser:
            launched, _ = open_in_browser(html_path)
            result["opened"] = launched
        return result

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cppgraph-mcp",
        description="MCP server exposing the cppgraph query surface to an LLM.",
    )
    parser.add_argument(
        "--graph",
        default=None,
        help="path to a graph store built by `cppgraph build`. Omit to "
        "auto-discover the current project's graph from the cwd's `.cppgraph/` "
        "(the default; lets one global registration serve every project).",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="checkout root for `status` drift checks and source snippets "
        "(defaults to the discovered project directory)",
    )
    args = parser.parse_args(argv)

    graph = args.graph
    root = args.root
    if graph is None:
        found = discover_graph()
        if found is not None:
            graph, discovered_root = found
            root = root or str(discovered_root)
    elif not Path(graph).exists():
        parser.error(f"graph store not found: {graph}")

    # If no graph (explicit or discovered), the server still starts and tools
    # report "not indexed here" — so a single global registration is harmless in
    # projects that haven't been indexed yet.
    server = build_server(graph, root=root)
    server.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
