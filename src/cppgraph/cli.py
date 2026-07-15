"""cppgraph command-line entry point."""

from __future__ import annotations

import argparse
import sys

from cppgraph.builder import build_graph
from cppgraph.model import Edge, Graph, Node
from cppgraph.proto import scip_pb2


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
    p_build.add_argument("--out", required=True, help="output graph store path (JSON)")

    p_find = sub.add_parser("find", help="find symbols by name (SCIP symbol strings aren't memorable)")
    p_find.add_argument("--graph", required=True, help="path to a graph.json built by `cppgraph build`")
    p_find.add_argument("query", help="substring to match against symbol or display name")

    p_callers = sub.add_parser("callers", help="list callers of a symbol")
    p_callers.add_argument("--graph", required=True, help="path to a graph.json built by `cppgraph build`")
    p_callers.add_argument("symbol", help="exact SCIP symbol string (see `find`)")

    p_callees = sub.add_parser("callees", help="list callees of a symbol")
    p_callees.add_argument("--graph", required=True, help="path to a graph.json built by `cppgraph build`")
    p_callees.add_argument("symbol", help="exact SCIP symbol string (see `find`)")

    p_path = sub.add_parser("path", help="shortest call chain from one symbol to another")
    p_path.add_argument("--graph", required=True, help="path to a graph.json built by `cppgraph build`")
    p_path.add_argument("src", help="exact SCIP symbol string (see `find`)")
    p_path.add_argument("dst", help="exact SCIP symbol string (see `find`)")

    p_impact = sub.add_parser("impact", help="reverse blast-radius: everything that transitively calls a symbol")
    p_impact.add_argument("--graph", required=True, help="path to a graph.json built by `cppgraph build`")
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
        graph.save_json(args.out)
        print(f"[cppgraph] built graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges -> {args.out}")
        return 0

    if args.command == "find":
        graph = Graph.load_json(args.graph)
        matches = graph.find(args.query)
        if not matches:
            print(f"[cppgraph] no symbol matching {args.query!r}")
            return 1
        for node in matches:
            _print_node(node)
        return 0

    if args.command == "callers":
        graph = Graph.load_json(args.graph)
        if args.symbol not in graph.nodes:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        edges = graph.callers_of(args.symbol)
        print(f"[cppgraph] {len(edges)} caller(s) of {args.symbol}")
        for edge in edges:
            _print_edge(edge, other=edge.src)
        return 0

    if args.command == "callees":
        graph = Graph.load_json(args.graph)
        if args.symbol not in graph.nodes:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        edges = graph.callees_of(args.symbol)
        print(f"[cppgraph] {len(edges)} callee(s) of {args.symbol}")
        for edge in edges:
            _print_edge(edge, other=edge.dst)
        return 0

    if args.command == "path":
        graph = Graph.load_json(args.graph)
        if args.src not in graph.nodes:
            parser.error(f"unknown symbol: {args.src} (use `cppgraph find` to look it up)")
        if args.dst not in graph.nodes:
            parser.error(f"unknown symbol: {args.dst} (use `cppgraph find` to look it up)")
        chain = graph.shortest_call_path(args.src, args.dst)
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
        graph = Graph.load_json(args.graph)
        if args.symbol not in graph.nodes:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        affected = graph.impact(args.symbol, max_depth=args.depth)
        print(f"[cppgraph] {len(affected)} symbol(s) transitively call {args.symbol}")
        for symbol in affected:
            _print_node(graph.nodes[symbol])
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
