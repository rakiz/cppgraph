"""cppgraph command-line entry point."""

from __future__ import annotations

import argparse
import sys

from cppgraph.builder import build_graph
from cppgraph.model import Edge, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, build_provenance, update_store, write_sqlite


def _print_node(node: Node) -> None:
    loc = f"{node.file}:{node.line + 1}" if node.file is not None and node.line is not None else "?"
    print(f"  {node.symbol}  ({node.display_name or '?'} @ {loc})")


def _print_edge(edge: Edge, *, other: str) -> None:
    line = edge.line + 1 if edge.line is not None else "?"
    print(f"  {other}  ({edge.file}:{line})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cppgraph",
        description="Semantically accurate code-graph for C++ (SCIP-backed).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build the graph from a SCIP index")
    p_build.add_argument("--scip", required=True, help="path to index.scip")
    p_build.add_argument("--out", required=True, help="output graph store path (SQLite .db)")
    p_build.add_argument(
        "--source-commit",
        default=None,
        help="commit hash of the indexed sources (captured at index time; "
        "recorded as provenance and used as the anchor for incremental updates). "
        "If omitted, best-effort auto-detected via git on the SCIP project_root.",
    )
    p_build.add_argument(
        "--source-dirty",
        action="store_true",
        help="mark the indexed sources as having uncommitted changes "
        "(pair with --source-commit; auto-detected otherwise)",
    )

    p_update = sub.add_parser(
        "update",
        help="incrementally apply a partial re-index (only changed TUs) to an existing store",
    )
    p_update.add_argument("--graph", required=True, help="path to the graph store to update in place")
    p_update.add_argument(
        "--scip", required=True,
        help="SCIP index of only the re-indexed (changed) translation units",
    )
    p_update.add_argument(
        "--deleted", action="append", default=[], metavar="PATH",
        help="a source file removed from the tree (no Document in --scip); repeatable",
    )
    p_update.add_argument(
        "--source-commit", default=None,
        help="commit hash of the sources after the change (the new provenance anchor); "
        "auto-detected via git on the SCIP project_root if omitted",
    )
    p_update.add_argument(
        "--source-dirty", action="store_true",
        help="mark the updated sources as having uncommitted changes",
    )

    p_find = sub.add_parser("find", help="find symbols by name (SCIP symbol strings aren't memorable)")
    p_find.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    p_find.add_argument("query", help="substring to match against symbol or display name")

    p_callers = sub.add_parser("callers", help="list callers of a symbol")
    p_callers.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    p_callers.add_argument("symbol", help="exact SCIP symbol string (see `find`)")

    p_callees = sub.add_parser("callees", help="list callees of a symbol")
    p_callees.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    p_callees.add_argument("symbol", help="exact SCIP symbol string (see `find`)")

    p_path = sub.add_parser("path", help="shortest call chain from one symbol to another")
    p_path.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    p_path.add_argument("src", help="exact SCIP symbol string (see `find`)")
    p_path.add_argument("dst", help="exact SCIP symbol string (see `find`)")

    p_impact = sub.add_parser("impact", help="reverse blast-radius: everything that transitively calls a symbol")
    p_impact.add_argument("--graph", required=True, help="path to a graph store built by `cppgraph build`")
    p_impact.add_argument("symbol", help="exact SCIP symbol string (see `find`)")
    p_impact.add_argument(
        "--depth", type=int, default=None, help="max call hops to walk backwards (default: unbounded)"
    )

    args = parser.parse_args(argv)

    if args.command == "build":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        graph = build_graph(index)
        meta = build_provenance(
            index,
            source_commit=args.source_commit,
            source_dirty=True if args.source_dirty else None,
        )
        write_sqlite(graph, args.out, meta=meta)
        print(f"[cppgraph] built graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges -> {args.out}")
        commit = meta.get("source_commit")
        if commit:
            dirty = " (dirty)" if meta.get("source_dirty") == "true" else ""
            print(f"[cppgraph] source commit: {commit}{dirty}")
        return 0

    if args.command == "update":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        meta = build_provenance(
            index,
            source_commit=args.source_commit,
            source_dirty=True if args.source_dirty else None,
        )
        stats = update_store(args.graph, index, deleted_files=args.deleted, meta=meta)
        print(
            f"[cppgraph] updated {stats.files_changed} file(s): "
            f"-{stats.edges_removed}/+{stats.edges_added} edges, "
            f"-{stats.symbols_removed} orphaned symbol(s) -> "
            f"{stats.node_count} nodes, {stats.edge_count} edges"
        )
        commit = meta.get("source_commit")
        if commit:
            dirty = " (dirty)" if meta.get("source_dirty") == "true" else ""
            print(f"[cppgraph] source commit: {commit}{dirty}")
        return 0

    if args.command == "find":
        store = GraphStore(args.graph)
        matches = store.find(args.query)
        if not matches:
            print(f"[cppgraph] no symbol matching {args.query!r}")
            return 1
        for node in matches:
            _print_node(node)
        return 0

    if args.command == "callers":
        store = GraphStore(args.graph)
        if not store.has_symbol(args.symbol):
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        edges = store.callers_of(args.symbol)
        print(f"[cppgraph] {len(edges)} caller(s) of {args.symbol}")
        for edge in edges:
            _print_edge(edge, other=edge.src)
        return 0

    if args.command == "callees":
        store = GraphStore(args.graph)
        if not store.has_symbol(args.symbol):
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        edges = store.callees_of(args.symbol)
        print(f"[cppgraph] {len(edges)} callee(s) of {args.symbol}")
        for edge in edges:
            _print_edge(edge, other=edge.dst)
        return 0

    if args.command == "path":
        store = GraphStore(args.graph)
        if not store.has_symbol(args.src):
            parser.error(f"unknown symbol: {args.src} (use `cppgraph find` to look it up)")
        if not store.has_symbol(args.dst):
            parser.error(f"unknown symbol: {args.dst} (use `cppgraph find` to look it up)")
        chain = store.shortest_call_path(args.src, args.dst)
        if chain is None:
            print(f"[cppgraph] no call path from {args.src} to {args.dst}")
            return 1
        print(f"[cppgraph] {len(chain)} hop(s) from {args.src} to {args.dst}")
        print(f"  {args.src}")
        for edge in chain:
            line = edge.line + 1 if edge.line is not None else "?"
            print(f"  -> {edge.dst}  ({edge.file}:{line})")
        return 0

    if args.command == "impact":
        store = GraphStore(args.graph)
        if not store.has_symbol(args.symbol):
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        affected = store.impact(args.symbol, max_depth=args.depth)
        print(f"[cppgraph] {len(affected)} symbol(s) transitively call {args.symbol}")
        for symbol in affected:
            node = store.get_node(symbol)
            if node is not None:
                _print_node(node)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
