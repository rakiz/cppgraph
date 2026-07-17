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
from cppgraph.store import (
    GraphStore,
    changed_files_since,
    commits_behind,
    discover_graph,
    staleness_verdict,
)
from cppgraph.updates import update_advice

if TYPE_CHECKING:
    from cppgraph.model import Edge, Node

# Default cap on any list a tool returns. Big enough to be useful for reasoning,
# small enough that a hub symbol's callers don't blow the context budget. The
# caller can raise it per-query, and always learns the true `total`.
DEFAULT_LIMIT = 25
# `explain` bundles callers + callees in one payload, so it caps each side lower.
EXPLAIN_LIMIT = 10

_UNKNOWN = "unknown symbol {symbol!r} — use the `find` tool to look up its exact SCIP symbol string"


def _line1(line0: int | None) -> int | None:
    """0-indexed store line -> 1-indexed for display; None stays None."""
    return None if line0 is None else line0 + 1


# Noise in a raw SCIP symbol string that a human name never needs: the scheme
# prefix (`cxx . . $ `), the enclosing-file path baked into anonymous-namespace
# and lambda symbols (`$anonymous_namespace_src/mongo/.../file.cpp/`), the
# overload disambiguator hash (`(a1b2c3…)`), and the descriptor back-ticks.
_ANON_RE = re.compile(r"`\$anonymous_namespace_[^`]*`/")
_HASH_RE = re.compile(r"\([0-9a-f]{6,}\)")


def _short_label(symbol: str) -> str:
    """A readable label derived from the SCIP string itself.

    `scip-clang` doesn't populate SymbolInformation.display_name (0% on the
    MongoDB index), so the graph has no human name to fall back on — but the SCIP
    string *is* the name, wrapped in machine noise. Strip that noise so the label
    is a fraction of the raw string yet still a substring `find` can re-resolve.
    Lossy on purpose (drops the overload hash); the exact string is one
    `full_symbols=True` away.
    """
    s = symbol.split(" $ ", 1)[-1] if " $ " in symbol else symbol
    s = _ANON_RE.sub("", s)
    s = _HASH_RE.sub("", s)
    return s.replace("`", "")


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


def _far_symbol(edge: Edge, on: str) -> str:
    return edge.src if on == "src" else edge.dst


def _drop_test_edges(store: GraphStore, edges: list[Edge], *, on: str) -> list[Edge]:
    """Drop edges whose far endpoint (`src` for callers, `dst` for callees) is
    defined in a test file — resolved via the node's definition site, so a test's
    destructor teardown site (`~..._Test`) is dropped along with ordinary test
    callers."""
    kept: list[Edge] = []
    for e in edges:
        node = store.get_node(_far_symbol(e, on))
        if node is None or not is_test_file(node.file):
            kept.append(e)
    return kept


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


def find_symbols(store: GraphStore, query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Symbols whose SCIP string or display name contains `query`.

    The entry point to every other tool: SCIP symbol strings aren't memorable,
    so an LLM resolves a human name here first, then feeds the exact string on.
    """
    matches = store.find(query)
    shown, truncated = _capped(matches, limit)
    return {
        "query": query,
        "total": len(matches),
        "truncated": truncated,
        "results": [_node_dict(n, full_symbols=True) for n in shown],
    }


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
) -> dict[str, Any]:
    """Direct callees of `symbol` (one `calls` hop). Error dict if unknown.

    Callees defined in test files are dropped by default (`exclude_tests`); pass
    `full_symbols=True` for the raw SCIP strings."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    edges = store.callees_of(symbol)
    if exclude_tests:
        edges = _drop_test_edges(store, edges, on="dst")
    shown, truncated = _capped(edges, limit)
    return {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "excluded_tests": exclude_tests,
        "callees": [_edge_dict(e, e.dst, store, full_symbols) for e in shown],
    }


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
    return {
        "symbol": symbol,
        "total": len(nodes),
        "truncated": truncated,
        "bases": [_node_dict(n, full_symbols) for n in shown],
    }


def subtypes(
    store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
) -> dict[str, Any]:
    """Direct subclasses of `symbol` (one `inherits` hop backward), each with
    its own definition site."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    nodes = store.subtypes_of(symbol)
    shown, truncated = _capped(nodes, limit)
    return {
        "symbol": symbol,
        "total": len(nodes),
        "truncated": truncated,
        "subtypes": [_node_dict(n, full_symbols) for n in shown],
    }


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
        return {"src": src, "dst": dst, "found": False, "path": None}
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
) -> dict[str, Any]:
    """Definition site + caller/callee summary for `symbol`.

    Coordinates only by default (token-cheap). `include_source=True` adds a
    source snippet, but only if `root` (a checkout) is given — otherwise there's
    no file to read. `source` is `None` (not omitted) when a snippet was asked
    for but the file couldn't be read, so the caller can tell "not requested"
    from "requested, unavailable". Test-file callers/callees are dropped by
    default (`exclude_tests`); `full_symbols=True` keeps the raw SCIP strings in
    the caller/callee lists.
    """
    node = store.get_node(symbol)
    if node is None:
        return {"error": _UNKNOWN.format(symbol=symbol)}
    caller_edges = store.callers_of(symbol)
    callee_edges = store.callees_of(symbol)
    if exclude_tests:
        caller_edges = _drop_test_edges(store, caller_edges, on="src")
        callee_edges = _drop_test_edges(store, callee_edges, on="dst")
    shown_callers, callers_trunc = _capped(caller_edges, limit)
    shown_callees, callees_trunc = _capped(callee_edges, limit)

    result: dict[str, Any] = {
        "symbol": node.symbol,
        "name": node.display_name or None,
        "defined_at": {"file": node.file, "line": _line1(node.line)},
        "excluded_tests": exclude_tests,
        "callers": {
            "total": len(caller_edges),
            "truncated": callers_trunc,
            "items": [_edge_dict(e, e.src, store, full_symbols) for e in shown_callers],
        },
        "callees": {
            "total": len(callee_edges),
            "truncated": callees_trunc,
            "items": [_edge_dict(e, e.dst, store, full_symbols) for e in shown_callees],
        },
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
    def find(query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Find C++ symbols by name substring. Start here: other tools need the
        exact SCIP symbol string this returns, not a human name."""
        return _call(find_symbols, query, limit=limit)

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
        are dropped by default — pass `exclude_tests=False` to include them."""
        return _call(
            callers, symbol, limit=limit, full_symbols=full_symbols, exclude_tests=exclude_tests
        )

    @mcp.tool()
    def what_it_calls(
        symbol: str,
        limit: int = DEFAULT_LIMIT,
        full_symbols: bool = False,
        exclude_tests: bool = True,
    ) -> dict[str, Any]:
        """Direct callees of a symbol (one call hop). `symbol` is an exact SCIP
        string from `find`. Compact `name` + `file:line` by default
        (`full_symbols=True` for raw SCIP); callees in test files dropped unless
        `exclude_tests=False`."""
        return _call(
            callees, symbol, limit=limit, full_symbols=full_symbols, exclude_tests=exclude_tests
        )

    @mcp.tool()
    def base_classes(
        symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Direct base classes a type inherits from (`symbol` is an exact SCIP
        type string from `find`, ending in `#`). Compact `name` + `file:line` by
        default; `full_symbols=True` for raw SCIP strings."""
        return _call(bases, symbol, limit=limit, full_symbols=full_symbols)

    @mcp.tool()
    def subclasses(
        symbol: str, limit: int = DEFAULT_LIMIT, full_symbols: bool = False
    ) -> dict[str, Any]:
        """Direct subclasses of a type (one inheritance hop). For the whole
        subtree use `impact_of` with kind="inherits". Compact `name` +
        `file:line` by default; `full_symbols=True` for raw SCIP strings."""
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
        `context` sets lines around each site. Test-file uses are dropped by
        default — pass `exclude_tests=False` to include them."""
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
        """Shortest call chain from `src` to `dst` (exact SCIP strings)."""
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
        unless `exclude_tests=False`."""
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
    ) -> dict[str, Any]:
        """Definition site + caller/callee summary for `symbol`. Returns
        `file:line` coordinates by default; set `include_source=True` to also get
        the definition's source snippet **inline** (cppgraph reads it for you — no
        separate file read needed). `limit` caps each of the caller/callee lists
        (raise it when `truncated` is true and you need more). Caller/callee lists
        are compact `name` + `file:line` (`full_symbols=True` for raw SCIP) and
        drop test files unless `exclude_tests=False`."""
        return _call(
            explain,
            symbol,
            root=root,
            include_source=include_source,
            context=context,
            limit=limit,
            full_symbols=full_symbols,
            exclude_tests=exclude_tests,
        )

    @mcp.tool()
    def status(force_update_check: bool = False) -> dict[str, Any]:
        """Graph provenance and drift: is this graph still current for the
        checkout? Run first — if `drift.up_to_date` is false, re-index before
        trusting the topology. Also reports `tool`: whether a newer cppgraph is
        published and whether adopting it (or the installed version) needs a full
        graph rebuild — so you can warn before an upgrade blocks on re-indexing.
        The update check is cached; `force_update_check=True` refetches now."""
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
        symbol is used (the right view for a type, which has no call edges). Set
        exclude_tests=True to drop test files and show production usage only.
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
