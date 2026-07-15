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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
