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
  binds one long-lived `GraphStore` (fixed at launch via `--graph`, so tools
  don't re-pass a path the LLM would have to guess/repeat) to those functions.

Token-economy defaults mirror the CLI's `explain`: coordinates (`file:line`)
only, never source text, unless the caller explicitly asks (`include_source`)
*and* the server was launched with a `--root` checkout. The premise is that an
LLM calling these tools already has file access — coordinates are enough and far
cheaper. `--root` also drives the `status` drift check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cppgraph.cli import SOURCE_EXTS, build_export_json, read_source_snippet
from cppgraph.store import GraphStore, changed_files_since

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


def _node_dict(node: Node) -> dict[str, Any]:
    return {
        "symbol": node.symbol,
        "name": node.display_name or None,
        "file": node.file,
        "line": _line1(node.line),
    }


def _edge_dict(edge: Edge, other: str) -> dict[str, Any]:
    """An edge as seen from one endpoint: `other` is the symbol at the far end
    (the caller for a callers query, the callee for a callees query)."""
    return {"symbol": other, "file": edge.file, "line": _line1(edge.line)}


def _capped(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    return items[:limit], len(items) > limit


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
        "results": [_node_dict(n) for n in shown],
    }


def callers(store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Direct callers of `symbol` (one `calls` hop). Error dict if unknown."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    edges = store.callers_of(symbol)
    shown, truncated = _capped(edges, limit)
    return {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "callers": [_edge_dict(e, e.src) for e in shown],
    }


def callees(store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Direct callees of `symbol` (one `calls` hop). Error dict if unknown."""
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    edges = store.callees_of(symbol)
    shown, truncated = _capped(edges, limit)
    return {
        "symbol": symbol,
        "total": len(edges),
        "truncated": truncated,
        "callees": [_edge_dict(e, e.dst) for e in shown],
    }


def bases(store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
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
        "bases": [_node_dict(n) for n in shown],
    }


def subtypes(store: GraphStore, symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
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
        "subtypes": [_node_dict(n) for n in shown],
    }


def references(
    store: GraphStore,
    symbol: str,
    root: str | None = None,
    include_source: bool = False,
    context: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Exact use sites of `symbol` (the `--references` location index).

    Answers "where is this type/symbol used?" — the dependency the call graph
    can't express (a plain struct has no callers). Positions are exact (no
    enclosing-attribution heuristic). Coordinates only by default; with
    `include_source=True` *and* a `root`, each site also carries a snippet the
    tool reads itself. `available` is False (not an error) when the graph was
    built with `--no-references`, so the caller knows to rebuild.
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
    shown, truncated = _capped(refs, limit)
    items: list[dict[str, Any]] = []
    for ref in shown:
        entry: dict[str, Any] = {"file": ref.file, "line": _line1(ref.line)}
        if include_source and root is not None and ref.file is not None and ref.line is not None:
            snippet = read_source_snippet(root, ref.file, ref.line, context=context)
            entry["source"] = (
                None if snippet is None else [{"line": n + 1, "text": t} for n, t in snippet]
            )
        items.append(entry)
    return {
        "symbol": symbol,
        "available": True,
        "total": len(refs),
        "truncated": truncated,
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
) -> dict[str, Any]:
    """Reverse blast-radius: everything that transitively reaches `symbol`.

    `kind="calls"` (default) = transitive callers ("what breaks if I change this
    function?"); `kind="inherits"` = all transitive subclasses of a base type.
    `depth` bounds the backward hops (None = unbounded). Results are symbols
    (with their definition site); capped like the other fan-out tools.
    """
    if not store.has_symbol(symbol):
        return {"error": _UNKNOWN.format(symbol=symbol)}
    affected = sorted(store.impact(symbol, max_depth=depth, kind=kind))
    shown, truncated = _capped(affected, limit)
    out: list[dict[str, Any]] = []
    for sym in shown:
        node = store.get_node(sym)
        out.append(_node_dict(node) if node is not None else {"symbol": sym, "file": None, "line": None})
    return {
        "symbol": symbol,
        "kind": kind,
        "depth": depth,
        "total": len(affected),
        "truncated": truncated,
        "reached_by": out,
    }


def explain(
    store: GraphStore,
    symbol: str,
    root: str | None = None,
    include_source: bool = False,
    context: int = 3,
    limit: int = EXPLAIN_LIMIT,
) -> dict[str, Any]:
    """Definition site + caller/callee summary for `symbol`.

    Coordinates only by default (token-cheap). `include_source=True` adds a
    source snippet, but only if `root` (a checkout) is given — otherwise there's
    no file to read. `source` is `None` (not omitted) when a snippet was asked
    for but the file couldn't be read, so the caller can tell "not requested"
    from "requested, unavailable".
    """
    node = store.get_node(symbol)
    if node is None:
        return {"error": _UNKNOWN.format(symbol=symbol)}
    caller_edges = store.callers_of(symbol)
    callee_edges = store.callees_of(symbol)
    shown_callers, callers_trunc = _capped(caller_edges, limit)
    shown_callees, callees_trunc = _capped(callee_edges, limit)

    result: dict[str, Any] = {
        "symbol": node.symbol,
        "name": node.display_name or None,
        "defined_at": {"file": node.file, "line": _line1(node.line)},
        "callers": {
            "total": len(caller_edges),
            "truncated": callers_trunc,
            "items": [_edge_dict(e, e.src) for e in shown_callers],
        },
        "callees": {
            "total": len(callee_edges),
            "truncated": callees_trunc,
            "items": [_edge_dict(e, e.dst) for e in shown_callees],
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


def status_report(store: GraphStore, root: str | None = None) -> dict[str, Any]:
    """The graph's provenance and, with `root`, whether the checkout has drifted.

    The "should I trust this graph?" check an LLM runs first: `drift.up_to_date`
    False means re-index before relying on the topology. Only C++ source changes
    count as drift (docs/build-config edits don't change the call graph).
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
            ) or None,
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
    if root is None or not commit:
        if root is not None and not commit:
            result["drift"] = {"checked": False, "reason": "no source commit recorded in the graph"}
        return result

    changes = changed_files_since(root, commit)
    if changes is None:
        result["drift"] = {"checked": False, "reason": f"{root} is not a git checkout (or git unavailable)"}
        return result
    changed = [f for f in changes[0] if f.endswith(SOURCE_EXTS)]
    deleted = [f for f in changes[1] if f.endswith(SOURCE_EXTS)]
    result["drift"] = {
        "checked": True,
        "up_to_date": not changed and not deleted,
        "changed": changed[:DEFAULT_LIMIT],
        "deleted": deleted[:DEFAULT_LIMIT],
        "changed_total": len(changed),
        "deleted_total": len(deleted),
    }
    if not (changed and deleted):
        # nothing more to say beyond the flag; keep the hint only when stale
        pass
    if changed or deleted:
        result["drift"]["next"] = "run scripts/reindex.sh --update to refresh the graph"
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
        store, symbol, mode=mode, depth=depth,
        direction=direction, exclude_tests=exclude_tests,
    )


def build_server(graph_path: str | Path, root: str | None = None) -> Any:
    """A FastMCP server bound to one graph store (opened once, reused per call).

    `graph_path` is fixed at launch so tools never take a graph argument — the
    LLM shouldn't have to know or repeat a filesystem path. `root`, if given, is
    the default checkout used for `status` drift and `explain` source snippets.
    """
    from mcp.server.fastmcp import FastMCP

    store = GraphStore(graph_path)
    mcp = FastMCP("cppgraph")

    @mcp.tool()
    def find(query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Find C++ symbols by name substring. Start here: other tools need the
        exact SCIP symbol string this returns, not a human name."""
        return find_symbols(store, query, limit=limit)

    @mcp.tool()
    def who_calls(symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Direct callers of a symbol (one call hop). `symbol` is an exact SCIP
        string from `find`."""
        return callers(store, symbol, limit=limit)

    @mcp.tool()
    def what_it_calls(symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Direct callees of a symbol (one call hop). `symbol` is an exact SCIP
        string from `find`."""
        return callees(store, symbol, limit=limit)

    @mcp.tool()
    def base_classes(symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Direct base classes a type inherits from (`symbol` is an exact SCIP
        type string from `find`, ending in `#`)."""
        return bases(store, symbol, limit=limit)

    @mcp.tool()
    def subclasses(symbol: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Direct subclasses of a type (one inheritance hop). For the whole
        subtree use `impact_of` with kind="inherits"."""
        return subtypes(store, symbol, limit=limit)

    @mcp.tool()
    def find_references(
        symbol: str, include_source: bool = False, context: int = 0, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        """Exact use sites of a symbol ("where is this type/symbol used?") — the
        dependency the call graph can't show (a struct has no callers). Needs a
        graph unless built --no-references. Coordinates only unless include_source=True
        and the server was launched with --root."""
        return references(store, symbol, root=root, include_source=include_source,
                          context=context, limit=limit)

    @mcp.tool()
    def path(src: str, dst: str) -> dict[str, Any]:
        """Shortest call chain from `src` to `dst` (exact SCIP strings)."""
        return call_path(store, src, dst)

    @mcp.tool()
    def impact_of(
        symbol: str, depth: int | None = None, limit: int = DEFAULT_LIMIT, kind: str = "calls"
    ) -> dict[str, Any]:
        """Reverse blast-radius: everything that transitively reaches `symbol`.
        kind="calls" (default) = transitive callers ("what could break if I
        change this function?"); kind="inherits" = every transitive subclass of
        a base type. `depth` bounds the hops."""
        return impact(store, symbol, depth=depth, limit=limit, kind=kind)

    @mcp.tool()
    def explain_symbol(
        symbol: str, include_source: bool = False, context: int = 3, limit: int = EXPLAIN_LIMIT
    ) -> dict[str, Any]:
        """Definition site + caller/callee summary for `symbol`. Coordinates only
        by default; set `include_source=True` to also get a source snippet (needs
        the server launched with --root). `limit` caps each of the caller/callee
        lists (raise it when `truncated` is true and you need more)."""
        return explain(
            store, symbol, root=root, include_source=include_source, context=context, limit=limit
        )

    @mcp.tool()
    def status() -> dict[str, Any]:
        """Graph provenance and drift: is this graph still current for the
        checkout? Run first — if `drift.up_to_date` is false, re-index before
        trusting the topology."""
        return status_report(store, root=root)

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

        graph_json = make_export(
            store, symbol, mode=mode, depth=depth,
            direction=direction, exclude_tests=exclude_tests,
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
    parser.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    parser.add_argument(
        "--root", default=None,
        help="default checkout root for `status` drift checks and `explain` "
        "source snippets (a runtime argument, never stored in the graph)",
    )
    args = parser.parse_args(argv)

    if not Path(args.graph).exists():
        parser.error(f"graph store not found: {args.graph}")

    server = build_server(args.graph, root=args.root)
    server.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
