"""In-memory graph model: nodes (SCIP symbols) and edges between them."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Node:
    symbol: str
    display_name: str = ""
    file: str | None = None
    line: int | None = None  # 0-indexed start line of the defining occurrence


@dataclass
class Edge:
    kind: str  # "calls" | "implements" | "inherits"
    src: str
    dst: str
    file: str
    line: int | None = None


@dataclass
class Reference:
    """A single non-definition use of a symbol at a source location.

    An exact position where the symbol is used. `enclosing_symbol` is the
    definition that contains the use site (the "type → the function that uses it"
    attribution), populated only when built with attribution from a binary that
    emits `enclosing_range` (#504); it stays None otherwise, and the reference
    remains an exact location either way — the enclosing attribution is additive,
    never a heuristic. See DESIGN.md § Graph model.
    """

    symbol: str
    file: str
    line: int | None = None
    enclosing_symbol: str | None = None


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    _edge_keys: set[tuple] = field(default_factory=set, repr=False)
    _ref_keys: set[tuple] = field(default_factory=set, repr=False)

    def add_node(self, symbol: str, *, display_name: str = "") -> Node:
        node = self.nodes.get(symbol)
        if node is None:
            node = Node(symbol=symbol, display_name=display_name)
            self.nodes[symbol] = node
        elif display_name and not node.display_name:
            node.display_name = display_name
        return node

    def add_edge(self, kind: str, src: str, dst: str, file: str, line: int | None = None) -> None:
        self.add_node(src)
        self.add_node(dst)
        key = (kind, src, dst, file, line)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append(Edge(kind=kind, src=src, dst=dst, file=file, line=line))

    def add_reference(
        self,
        symbol: str,
        file: str,
        line: int | None = None,
        enclosing_symbol: str | None = None,
    ) -> None:
        """Record a use of `symbol` at `file:line`, deduped by (symbol, file,
        line) — a header included by N TUs surfaces the same occurrence N times.

        The referenced symbol becomes a node so it is interned and findable even
        if it is defined outside the indexed set (e.g. a `std::` type used here).
        `enclosing_symbol` (when known, from an enclosing_range-emitting binary)
        is the definition that contains the use site.
        """
        self.add_node(symbol)
        key = (symbol, file, line)
        if key in self._ref_keys:
            return
        self._ref_keys.add(key)
        self.references.append(
            Reference(symbol=symbol, file=file, line=line, enclosing_symbol=enclosing_symbol)
        )

    def references_of(self, symbol: str) -> list[Reference]:
        return [r for r in self.references if r.symbol == symbol]

    def callers_of(self, symbol: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == "calls" and e.dst == symbol]

    def callees_of(self, symbol: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == "calls" and e.src == symbol]

    def _calls_adjacency(self) -> dict[str, list[Edge]]:
        adjacency: dict[str, list[Edge]] = {}
        for e in self.edges:
            if e.kind == "calls":
                adjacency.setdefault(e.src, []).append(e)
        return adjacency

    def shortest_call_path(self, src: str, dst: str) -> list[Edge] | None:
        """Shortest chain of `calls` edges from `src` to `dst`, BFS (unweighted).

        Returns `[]` if src == dst, `None` if no path exists or either symbol
        is unknown.
        """
        if src not in self.nodes or dst not in self.nodes:
            return None
        if src == dst:
            return []
        adjacency = self._calls_adjacency()
        visited = {src}
        queue: deque[tuple[str, list[Edge]]] = deque([(src, [])])
        while queue:
            node, path = queue.popleft()
            for edge in adjacency.get(node, []):
                if edge.dst == dst:
                    return path + [edge]
                if edge.dst not in visited:
                    visited.add(edge.dst)
                    queue.append((edge.dst, path + [edge]))
        return None

    def impact(self, symbol: str, max_depth: int | None = None) -> set[str]:
        """Symbols that transitively call `symbol` (reverse blast-radius).

        `max_depth` bounds the number of `calls` hops walked backwards;
        `None` means unbounded.
        """
        if symbol not in self.nodes:
            return set()
        reverse_adjacency: dict[str, list[str]] = {}
        for e in self.edges:
            if e.kind == "calls":
                reverse_adjacency.setdefault(e.dst, []).append(e.src)

        visited = {symbol}
        frontier = [symbol]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            next_frontier = []
            for node in frontier:
                for caller in reverse_adjacency.get(node, []):
                    if caller not in visited:
                        visited.add(caller)
                        next_frontier.append(caller)
            frontier = next_frontier
            depth += 1
        visited.discard(symbol)
        return visited

    def find(self, query: str) -> list[Node]:
        """Nodes matching `query` (case-sensitive).

        A single token is a substring test; a multi-token query (whitespace-
        separated) is an order-free AND — every token must appear in the symbol
        or the display name. SCIP symbol strings aren't memorable, so this is how
        a user locates the exact symbol to pass to `callers_of`/`callees_of`.
        """
        tokens = query.split()
        if not tokens:
            return []
        return [
            n
            for n in self.nodes.values()
            if all(t in n.symbol or t in n.display_name for t in tokens)
        ]

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {"symbol": n.symbol, "display_name": n.display_name, "file": n.file, "line": n.line}
                for n in self.nodes.values()
            ],
            "edges": [
                {"kind": e.kind, "src": e.src, "dst": e.dst, "file": e.file, "line": e.line}
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Graph:
        graph = cls()
        for n in data["nodes"]:
            graph.nodes[n["symbol"]] = Node(**n)
        for e in data["edges"]:
            graph.edges.append(Edge(**e))
        return graph

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load_json(cls, path: str | Path) -> Graph:
        return cls.from_dict(json.loads(Path(path).read_text()))
