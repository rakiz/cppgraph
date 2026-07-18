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

from collections import Counter

from .model import Edge, Node, Reference


def is_test_file(path: str | None) -> bool:
    """Heuristic: is `path` a C++ test / test-support file?

    Covers the common conventions: a `_test` / `_tests` /
    `_unittest` suffix, a `test_` prefix, a `_test_` infix (catches
    `*_test_helpers.cpp`), or a `test/` / `tests/` directory in the path. Used
    by `--no-tests` to show production usage only.
    """
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    if "/test/" in p or "/tests/" in p:
        return True
    stem = p.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return (
        stem.startswith("test_")
        or stem.endswith(("_test", "_tests", "_unittest"))
        or "_test_" in stem
    )


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


def to_file_usage_graph(symbol: str, label: str, references: list[Reference]) -> dict:
    """A drawable ``symbol -> file`` usage graph from a symbol's references.

    cppgraph records references as exact *locations* (``file:line``) with no
    enclosing-symbol attribution (the deliberate "C" design), so a type's usage
    isn't a symbol graph. We can still draw it exactly at *file* granularity:
    one edge ``symbol -> file`` per distinct file, weighted by how many use
    sites live there. 100% exact, zero heuristic — every reference carries a
    real file. (When scip-clang emits ``enclosing_range`` we can upgrade this to
    ``symbol -> enclosing symbol`` edges; see TODO / DESIGN.md § Graph model.)
    """
    counts = Counter(r.file for r in references if r.file)
    nodes: list[dict] = [{"id": symbol, "label": label or symbol, "_origin": "cppgraph"}]
    links: list[dict] = []
    for path, n in sorted(counts.items()):
        file_id = f"file:{path}"
        nodes.append(
            {
                "id": file_id,
                "label": path.rsplit("/", 1)[-1],
                "source_file": path,
                "kind": "file",
                "_origin": "cppgraph",
            }
        )
        links.append(
            {
                "source": symbol,
                "target": file_id,
                "relation": "references",
                "weight": n,
                "_origin": "cppgraph",
            }
        )
    return {"nodes": nodes, "links": links}


def to_symbol_usage_graph(symbol: str, label: str, references: list[Reference]) -> dict:
    """A drawable ``symbol -> enclosing symbol`` usage graph.

    The symbol-granularity upgrade of `to_file_usage_graph`, available when the
    graph was built with `--attributed-refs` from an enclosing_range-emitting
    binary (#504): instead of "used somewhere in file F", each edge names the
    *definition that uses it* (the function/type containing the use site),
    weighted by the number of use sites there. Still 100% exact — every edge
    traces to a real occurrence whose enclosing_range names its container.

    References that carry no `enclosing_symbol` (a stock-built subset, or a use
    at file scope) fall back to a ``file:<path>`` node, so the graph stays
    complete and exact whatever the attribution coverage.
    """
    from .filters import short_label  # local: filters imports is_test_file from here

    sym_counts: Counter[str] = Counter()
    file_counts: Counter[str] = Counter()
    for r in references:
        if r.enclosing_symbol:
            sym_counts[r.enclosing_symbol] += 1
        elif r.file:
            file_counts[r.file] += 1

    nodes: list[dict] = [{"id": symbol, "label": label or symbol, "_origin": "cppgraph"}]
    links: list[dict] = []
    for encl, n in sorted(sym_counts.items()):
        nodes.append({"id": encl, "label": short_label(encl), "_origin": "cppgraph"})
        links.append(
            {
                "source": symbol,
                "target": encl,
                "relation": "used_by",
                "weight": n,
                "_origin": "cppgraph",
            }
        )
    for path, n in sorted(file_counts.items()):
        file_id = f"file:{path}"
        nodes.append(
            {
                "id": file_id,
                "label": path.rsplit("/", 1)[-1],
                "source_file": path,
                "kind": "file",
                "_origin": "cppgraph",
            }
        )
        links.append(
            {
                "source": symbol,
                "target": file_id,
                "relation": "references",
                "weight": n,
                "_origin": "cppgraph",
            }
        )
    return {"nodes": nodes, "links": links}
