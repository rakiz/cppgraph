"""cppgraph command-line entry point."""

from __future__ import annotations

import argparse
import sys

from cppgraph.builder import build_graph
from cppgraph.proto import scip_pb2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cppgraph",
        description="Semantically accurate code-graph for C++ (SCIP-backed).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build the graph from a SCIP index")
    p_build.add_argument("--scip", required=True, help="path to index.scip")
    p_build.add_argument("--out", required=True, help="output graph store path (JSON)")

    args = parser.parse_args(argv)

    if args.command == "build":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        graph = build_graph(index)
        graph.save_json(args.out)
        print(f"[cppgraph] built graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges -> {args.out}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
