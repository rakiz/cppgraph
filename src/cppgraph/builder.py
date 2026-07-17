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
  index data (see `COMPARISON.md`): e.g. it correctly separates the two
  distinct `makeResumeToken` symbols and their caller sets.
- A header included by N translation units contributes the same occurrence
  (identical file/line/symbol/roles) once per TU when scip-clang merges
  their partial indexes — verified on `change_stream_event_transform.h`,
  included by 3 TUs in the pipeline subsystem, each producing an identical
  duplicate reference occurrence. Edges are deduped by
  (kind, src, dst, file, line) to avoid inflating counts.
- Inheritance vs override: scip-clang emits an `is_implementation`
  relationship for *both* class inheritance and method override. They're told
  apart by SCIP descriptor kind — a type→type relationship (`#` → `#`) is an
  `inherits` edge (derived → base), a method→method one stays `implements`.
  Verified on the pipeline index: 30445 type→type, 11950 method→method.
- Definition sites are recorded for every defined symbol (types/fields too,
  not just callables), so `find`/`explain`/`bases`/`subtypes` can locate any
  symbol. Only *callable* definitions act as caller-attribution boundaries for
  `calls`.
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


def is_type_symbol(symbol: str) -> bool:
    """True if the SCIP symbol's last descriptor is a type (class/struct/enum).

    Per the SCIP symbol grammar, `<type> ::= <name> '#'` — the descriptor kind
    ending in `#`. Used to tell class inheritance apart from method override:
    scip-clang emits an `is_implementation` relationship for both, and the only
    reliable discriminator is the descriptor kind of the two endpoints.
    """
    return symbol.endswith("#")


def _occurrence_start_line(occ: scip_pb2.Occurrence) -> int | None:
    which = occ.WhichOneof("typed_range")
    if which == "single_line_range":
        return occ.single_line_range.line
    if which == "multi_line_range":
        return occ.multi_line_range.start_line
    if occ.range:
        return occ.range[0]
    return None


def build_graph(index: scip_pb2.Index, *, include_references: bool = True) -> Graph:
    """Build the graph from a SCIP index.

    `include_references` (default on) collects an exact reference-location index
    (`Graph.references`): every non-local, non-definition occurrence as
    `symbol -> file:line`, with no enclosing attribution (so no heuristic, 100%
    exact). Set False to skip it and get a leaner store (measured ~+45% size on
    a large index). See DESIGN.md § Graph model.
    """
    graph = Graph()

    for doc in index.documents:
        for sym_info in doc.symbols:
            graph.add_node(sym_info.symbol, display_name=sym_info.display_name)
            for rel in sym_info.relationships:
                if rel.is_implementation:
                    # scip-clang uses is_implementation for both class
                    # inheritance (type -> type) and method override
                    # (method -> method). Split them by descriptor kind:
                    # class-level relationships are `inherits`, the rest
                    # (method override) stay `implements`. Verified on the
                    # pipeline index: 30445 type->type, 11950 method->method.
                    if is_type_symbol(sym_info.symbol) and is_type_symbol(rel.symbol):
                        edge_kind = "inherits"
                    else:
                        edge_kind = "implements"
                    graph.add_edge(edge_kind, sym_info.symbol, rel.symbol, doc.relative_path)

        # One pass over definition occurrences: record every symbol's
        # definition site (so types/fields, not just callables, can be located
        # by find/explain/bases/subtypes), and separately collect the callable
        # definitions that act as caller-attribution boundaries for `calls`.
        callable_defs: list[tuple[int, str]] = []
        for occ in doc.occurrences:
            if not (occ.symbol_roles & DEFINITION):
                continue
            line = _occurrence_start_line(occ)
            if line is None:
                continue
            node = graph.add_node(occ.symbol)
            if node.file is None:  # first definition site wins (header dedup)
                node.file = doc.relative_path
                node.line = line
            if is_callable_symbol(occ.symbol):
                callable_defs.append((line, occ.symbol))
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

        if include_references:
            # Exact location index: every non-local use of a symbol, as-is. No
            # attribution to an enclosing definition — that's the whole point of
            # the "C" approach (no nearest-preceding heuristic, no class-body
            # false positives). `local ...` symbols are function-scoped noise.
            for occ in doc.occurrences:
                if occ.symbol_roles & (DEFINITION | FORWARD_DEFINITION):
                    continue
                if occ.symbol.startswith("local "):
                    continue
                line = _occurrence_start_line(occ)
                if line is None:
                    continue
                graph.add_reference(occ.symbol, doc.relative_path, line)

    return graph
