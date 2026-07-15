"""Export a cppgraph graph to the graphify-compatible ``graph.json`` schema.

The schema is deliberately the one graphify emits (`nodes: [{id, label, ...}]`,
`links: [{source, target, relation, ...}]`) so a single exported file can be
opened both by our own bundled viewer (`viz/`) *and*, as a bonus, by graphify's
own tooling. The exact, disambiguated edges are cppgraph's; only the container
format is shared.

Node ids are the SCIP symbol strings (already globally unique and stable), so
`source`/`target` on links line up with `id` on nodes with no extra interning.
"""

from __future__ import annotations

from .model import Edge, Node


def _loc(line: int | None) -> str | None:
    """Model line numbers are 0-indexed; graphify shows 1-based ``L<n>``."""
    return None if line is None else f"L{line + 1}"


def _label(node: Node) -> str:
    return node.display_name or node.symbol


def to_graphify_graph(nodes: list[Node], edges: list[Edge]) -> dict:
    """Map Node/Edge lists onto the graphify ``graph.json`` container."""
    return {
        "nodes": [
            {
                "id": n.symbol,
                "label": _label(n),
                "source_file": n.file,
                "source_location": _loc(n.line),
                "_origin": "cppgraph",
            }
            for n in nodes
        ],
        "links": [
            {
                "source": e.src,
                "target": e.dst,
                "relation": e.kind,
                "source_file": e.file,
                "source_location": _loc(e.line),
                "_origin": "cppgraph",
            }
            for e in edges
        ],
    }
