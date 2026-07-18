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

from cppgraph.cli import SOURCE_EXTS, build_export_json, read_source_snippet
from cppgraph.export import is_test_file
from cppgraph.filters import drop_test_edges as _drop_test_edges
from cppgraph.filters import is_noise_symbol as _is_noise_symbol
from cppgraph.filters import is_trivial_callee as _is_trivial_callee
from cppgraph.filters import qualified_name as _qualified_name
from cppgraph.filters import short_label as _short_label
from cppgraph.store import (
    GraphStore,
    changed_files_since,
    commits_behind,
    discover_graph,
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


def _extract_signature(root: str | None, file: str | None, line0: int | None) -> str | None:
    """A best-effort readable parameter signature for an overload, read from the
    source at its definition site.

    `scip-clang` disambiguates overloads by an opaque hash, not by argument
    types, so grouped overloads are otherwise indistinguishable. Since cppgraph
    has the checkout (`root`), it reads the def line and captures the text from
    the first `(` to its matching `)` — display-only, so templates / macros /
    multi-line params are tolerated (whitespace collapsed). `None` if there's no
    root, the file can't be read, or no parameter list is found."""
    if root is None or file is None or line0 is None:
        return None
    snippet = read_source_snippet(root, file, line0, context=8)
    if not snippet:
        return None
    text = " ".join(t for i, t in snippet if i >= line0)
    start = text.find("(")
    if start < 0:
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


def find_symbols(
    store: GraphStore,
    query: str,
    limit: int = DEFAULT_LIMIT,
    hide_trivial: bool = False,
    root: str | None = None,
) -> dict[str, Any]:
    """Symbols whose SCIP string or display name contains `query`.

    The entry point to every other tool: SCIP symbol strings aren't memorable,
    so an LLM resolves a human name here first, then feeds the exact string on.
    With `hide_trivial=True`, compiler-generated / boilerplate hits (unnamed-type
    lambdas, operators, `*assert`/`makeStatus`, …) are dropped and counted as
    `trivial_hidden`, so a broad query isn't buried in noise.

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
                sig = _extract_signature(root, m.file, m.line)
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
    return result


def callers(
    store: GraphStore,
    symbol: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
    exclude_tests: bool = True,
) -> dict[str, Any]:
    """Direct callers of `symbol` (one `calls` hop). Error dict if unknown.

    Test callers (and destructor teardown sites) are dropped by default
    (`exclude_tests`); pass `full_symbols=True` for the raw SCIP strings."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    edges = store.callers_of(symbol)
    if exclude_tests:
        edges = _drop_test_edges(store, edges, on="src")
    shown, truncated = _capped(edges, limit)
    return {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "callers": [_edge_dict(e, e.src, store, full_symbols) for e in shown],
    }


def callees(
    store: GraphStore,
    symbol: str,
    limit: int = DEFAULT_LIMIT,
    full_symbols: bool = False,
    exclude_tests: bool = True,
    hide_trivial: bool = False,
) -> dict[str, Any]:
    """Direct callees of `symbol` (one `calls` hop). Error dict if unknown.

    Callees defined in test files are dropped by default (`exclude_tests`); pass
    `full_symbols=True` for the raw SCIP strings. With `hide_trivial=True`,
    ubiquitous helpers (`operator==`, `tassert`/`uassert`, `makeStatus`,
    `source_location`, …) are dropped so the domain edges stand out; the count of
    hidden edges is reported as `trivial_hidden`."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    edges = store.callees_of(symbol)
    if exclude_tests:
        edges = _drop_test_edges(store, edges, on="dst")
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
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
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
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
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
) -> dict[str, Any]:
    """Exact use sites of `symbol` (the `--references` location index).

    Answers "where is this type/symbol used?" — the dependency the call graph
    can't express (a plain struct has no callers). Positions are exact (no
    enclosing-attribution heuristic). Test-file uses are dropped by default
    (`exclude_tests`). Coordinates only by default; with `include_source=True`
    *and* a `root`, sites are grouped by file and each file carries one merged
    snippet (overlapping `± context` windows are collapsed, not re-sent per hit).
    `available` is False (not an error) when the graph was built with
    `--no-references`, so the caller knows to rebuild.
    """
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    refs = store.references_of(symbol)
    if not refs and store.meta().get("has_references") != "true":
        return {
            "symbol": symbol,
            "available": False,
            "reason": "graph built with --no-references (no location index)",
        }
    if exclude_tests:
        refs = [r for r in refs if not is_test_file(r.file)]
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
        items = [{"file": ref.file, "line": _line1(ref.line)} for ref in shown]

    return {
        "symbol": symbol,
        "available": True,
        "total": len(refs),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "uses": items,
    }


def call_path(store: GraphStore, src: str, dst: str) -> dict[str, Any]:
    """Shortest `calls` chain from `src` to `dst`, as an ordered node list.

    A bounded answer by construction (one shortest path), so it isn't capped.
    """
    if not store.has_symbol(src):
        return {"error": _UNKNOWN.format(symbol=src)}
    if not store.has_symbol(dst):
        return {"error": _UNKNOWN.format(symbol=dst)}
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
) -> dict[str, Any]:
    """Reverse blast-radius: everything that transitively reaches `symbol`.

    `kind="calls"` (default) = transitive callers ("what breaks if I change this
    function?"); `kind="inherits"` = all transitive subclasses of a base type.
    `depth` bounds the backward hops (None = unbounded). Results are symbols
    (with their definition site); capped like the other fan-out tools. Symbols
    defined in test files are dropped by default (`exclude_tests`).
    """
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}

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
        "reached_by": out,
    }


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
    the caller/callee lists, each side reporting its `trivial_hidden` count."""
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
        "graph_meta": {
            "source_commit": commit,
            "source_dirty": m.get("source_dirty") == "true",
            "project_root": m.get("project_root"),
            "built_at": m.get("built_at"),
            "indexed_with": " ".join(
                v for v in (m.get("index_tool"), m.get("index_tool_version")) if v
            )
            or None,
            "schema_version": m.get("schema_version"),
            "cppgraph_version": m.get("cppgraph_version"),
            "has_references": m.get("has_references") == "true",
            "node_count": m.get("node_count"),
            "edge_count": m.get("edge_count"),
            "ref_count": m.get("ref_count"),
        },
        "source_commit": commit,
        "drift": {"checked": False},
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

    changes = changed_files_since(root, commit)
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
            "run scripts/reindex.sh --update to refresh the graph"
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
    "scripts/reindex.sh (it writes <project>/.cppgraph/…), then reopen this "
    "Claude Code session from the project directory."
}


def build_server(graph_path: str | Path | None, root: str | None = None) -> Any:
    """A FastMCP server for one project's graph (opened once, reused per call).

    `graph_path` is resolved at launch — explicitly (`--graph`) or discovered
    from the cwd (`discover_graph`) — so tools never take a graph argument. If it
    is `None` (no indexed project above the cwd), the server still starts and
    every tool returns a clear "not indexed here" message. `root` is the checkout
    used for `status` drift and source snippets.
    """
    from mcp.server.fastmcp import FastMCP

    store = GraphStore(graph_path) if graph_path else None
    mcp = FastMCP("cppgraph")

    def _call(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run a pure `(store, …) -> dict` query, or return the no-graph notice."""
        if store is None:
            return dict(_NO_GRAPH)
        return fn(store, *args, **kwargs)

    @mcp.tool()
    def find(query: str, limit: int = DEFAULT_LIMIT, hide_trivial: bool = False) -> dict[str, Any]:
        """Find C++ symbols by name. Start here: other tools need the exact SCIP
        symbol string this returns, not a human name. A multi-word query is an
        order-free AND (every word must appear); overloads sharing a qualified
        name group under one result, each with a source-derived `signature` so
        the arms are distinguishable. If nothing matches exactly, `find` relaxes
        once (case/separator-insensitive — `changestream` ~ `change_stream` —
        then, for a `Class#method` guess, the bare leaf name) and flags the
        response `relaxed`. Set `hide_trivial=True` to drop compiler-generated /
        boilerplate hits (lambdas, operators, `*assert`, `makeStatus`, …) —
        `trivial_hidden` reports how many were cut. `limit` caps the list
        (default 40): lower it to spend fewer tokens, raise it when `truncated`."""
        return _call(find_symbols, query, limit=limit, hide_trivial=hide_trivial, root=root)

    @mcp.tool()
    def who_calls(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
    ) -> dict[str, Any]:
        """Direct callers of a symbol (one call hop). `symbol` is an exact SCIP
        string from `find`. Each caller is returned by human `name` + `file:line`
        (compact); set `full_symbols=True` for the raw SCIP strings. Test callers
        are dropped by default — pass `exclude_tests=False` to include them.
        `limit` caps the list (default 40): lower it to spend fewer tokens when a
        few callers are enough, raise it when `truncated` is true."""
        return _call(
            callers, symbol, limit=limit, full_symbols=full_symbols, exclude_tests=exclude_tests
        )

    @mcp.tool()
    def what_it_calls(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
        hide_trivial: bool = False,
    ) -> dict[str, Any]:
        """Direct callees of a symbol (one call hop). `symbol` is an exact SCIP
        string from `find`. Compact `name` + `file:line` by default
        (`full_symbols=True` for raw SCIP); callees in test files dropped unless
        `exclude_tests=False`. Set `hide_trivial=True` to drop ubiquitous helpers
        (operators, tassert/uassert, makeStatus, source_location, …) so the
        domain edges stand out — `trivial_hidden` reports how many were cut.
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
        )

    @mcp.tool()
    def base_classes(
        symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Direct base classes a type inherits from (`symbol` is an exact SCIP
        type string from `find`, ending in `#`). Compact `name` + `file:line` by
        default; `full_symbols=True` for raw SCIP strings. `limit` caps the list.
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
    ) -> dict[str, Any]:
        """Exact use sites of a symbol ("where is this type/symbol used?") — the
        dependency the call graph can't show (a struct has no callers). Returns
        `file:line` coordinates; set `include_source=True` to also get the code
        **inline** (cppgraph reads it for you — no separate file read needed),
        grouped by file with overlapping windows merged (no duplicated lines).
        `context` sets the lines shown around each site. Test-file uses are
        dropped by default — pass `exclude_tests=False` to include them. `limit`
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
        )

    @mcp.tool()
    def path(src: str, dst: str) -> dict[str, Any]:
        """Shortest chain of `calls` edges from `src` to `dst` (exact SCIP
        strings), as an ordered node list with `hops`. Returns `found=false` with
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
    ) -> dict[str, Any]:
        """Reverse blast-radius: everything that transitively reaches `symbol`.
        kind="calls" (default) = transitive callers ("what could break if I
        change this function?"); kind="inherits" = every transitive subclass of
        a base type. `depth` bounds the hops. Compact `name` + `file:line` by
        default (`full_symbols=True` for raw SCIP); symbols in test files dropped
        unless `exclude_tests=False`. `limit` caps the list (default 40): lower it
        to spend fewer tokens, raise it when `truncated`."""
        return _call(
            impact,
            symbol,
            depth=depth,
            limit=limit,
            kind=kind,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
        )

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
        """Definition site + caller/callee summary for `symbol`. Returns
        `file:line` coordinates by default; set `include_source=True` to also get
        the definition's source snippet **inline** (cppgraph reads it for you — no
        separate file read needed), `context` lines around the definition. `limit`
        caps each of the caller/callee lists (default 10): lower it to spend fewer
        tokens, raise it when `truncated` is true and you need more. Caller/callee lists
        are compact `name` + `file:line` (`full_symbols=True` for raw SCIP) and
        drop test files unless `exclude_tests=False`. Set `hide_trivial=True` to
        also drop ubiquitous helpers (operators, `*assert`, `makeStatus`,
        `source_location`, …) — each list reports its `trivial_hidden` count."""
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
        call/inherit subgraph; mode="usage" = a symbol->file graph of where the
        symbol is used (the right view for a type, which has no call edges).
        direction (deps mode) = "both" (default) both callers and callees, "out"
        only what the symbol reaches, "in" only what reaches it. Set
        exclude_tests=True to drop test files and show production usage only. Set
        open_browser=False to just get the HTML path without launching a browser.
        Returns the HTML path and the command to open it (in case the browser
        didn't launch)."""
        from cppgraph.viz_html import open_in_browser, write_temp_html

        if store is None:
            return dict(_NO_GRAPH)
        graph_json = make_export(
            store,
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
