"""Build a Graph from a parsed SCIP Index.

`scip-clang` (verified v0.4.0) does not populate `SymbolInformation.kind` or
`Occurrence.enclosing_range` — every symbol comes back as `UnspecifiedKind`
and every enclosing range is empty. So this builder cannot rely on either
field. Instead:

- Callability is read off the SCIP symbol string itself: the descriptor
  grammar (see `scip.proto`'s `Symbol` docs) terminates method/constructor
  descriptors with `).`, which is a property of the compiler-assigned symbol
  identity, not a syntactic guess.
- The caller of a call site is attributed as the nearest preceding
  callable-symbol definition in the same document (by start line), since we
  have no enclosing-range to contain it directly. Verified against real
  MongoDB data: this correctly finds both real callers of
  `ChangeStreamEventTransformation::makeResumeToken`.
- A header included by N translation units contributes the same occurrence
  (identical file/line/symbol/roles) once per TU when scip-clang merges
  their partial indexes — verified on `change_stream_event_transform.h`,
  included by 3 TUs in the pipeline subsystem, each producing an identical
  duplicate reference occurrence. `calls`/`implements` edges are deduped by
  (kind, src, dst, file, line) to avoid inflating caller counts.
"""

from __future__ import annotations

import bisect

from cppgraph.model import Graph
from cppgraph.proto import scip_pb2

DEFINITION = scip_pb2.SymbolRole.Definition
FORWARD_DEFINITION = scip_pb2.SymbolRole.ForwardDefinition


def is_callable_symbol(symbol: str) -> bool:
    """True if the SCIP symbol's last descriptor is a method/constructor.

    Per the SCIP symbol grammar, `<method> ::= <name> '(' (<disambiguator>)? ').'`
    — the only descriptor kind ending in `).`.
    """
    return symbol.endswith(").")


def _occurrence_start_line(occ: scip_pb2.Occurrence) -> int | None:
    which = occ.WhichOneof("typed_range")
    if which == "single_line_range":
        return occ.single_line_range.line
    if which == "multi_line_range":
        return occ.multi_line_range.start_line
    if occ.range:
        return occ.range[0]
    return None


def build_graph(index: scip_pb2.Index) -> Graph:
    graph = Graph()

    for doc in index.documents:
        for sym_info in doc.symbols:
            graph.add_node(sym_info.symbol, display_name=sym_info.display_name)
            for rel in sym_info.relationships:
                if rel.is_implementation:
                    graph.add_edge(
                        "implements", sym_info.symbol, rel.symbol, doc.relative_path
                    )

        # Callable-symbol definitions in this document, sorted by start line,
        # used both as call-graph nodes and as caller-attribution boundaries.
        callable_defs: list[tuple[int, str]] = []
        for occ in doc.occurrences:
            if not (occ.symbol_roles & DEFINITION):
                continue
            if not is_callable_symbol(occ.symbol):
                continue
            line = _occurrence_start_line(occ)
            if line is None:
                continue
            callable_defs.append((line, occ.symbol))
            node = graph.add_node(occ.symbol)
            if node.file is None:
                node.file = doc.relative_path
                node.line = line
        callable_defs.sort()
        boundary_lines = [line for line, _ in callable_defs]

        for occ in doc.occurrences:
            if occ.symbol_roles & (DEFINITION | FORWARD_DEFINITION):
                continue
            if not is_callable_symbol(occ.symbol):
                continue
            line = _occurrence_start_line(occ)
            if line is None:
                continue
            pos = bisect.bisect_right(boundary_lines, line) - 1
            if pos < 0:
                continue  # no enclosing callable definition found in this document
            _, caller_symbol = callable_defs[pos]
            graph.add_edge("calls", caller_symbol, occ.symbol, doc.relative_path, line)

    return graph
