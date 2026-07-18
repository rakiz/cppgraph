"""Build a Graph from a parsed SCIP Index.

Stock `scip-clang` (verified v0.4.0) does not populate `SymbolInformation.kind`
or `Occurrence.enclosing_range` — every symbol comes back as `UnspecifiedKind`
and every enclosing range is empty. So this builder never *requires* either
field, but uses `enclosing_range` for exact caller attribution when a binary
does emit it (a #504-built scip-clang). Specifically:

- Callability is read off the SCIP symbol string itself: the descriptor
  grammar (see `scip.proto`'s `Symbol` docs) terminates method/constructor
  descriptors with `).`, which is a property of the compiler-assigned symbol
  identity, not a syntactic guess.
- The caller of a call site is the callable definition whose `enclosing_range`
  contains it, when the binary emits enclosing ranges (#504) — exact, no
  heuristic. When it doesn't (stock binary), attribution falls back to the
  nearest preceding callable-symbol definition in the same document (by start
  line). Verified against real index data (see `COMPARISON.md`): even the
  heuristic correctly separates the two distinct `makeResumeToken` symbols and
  their caller sets; enclosing ranges additionally fix nested-definition cases
  (a call in an outer body sitting past an inner lambda's definition).
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


def _occurrence_enclosing_start_line(occ: scip_pb2.Occurrence) -> int | None:
    """Start line of the occurrence's *enclosing definition*, or None if the
    binary didn't emit one. A #504-built scip-clang fills the deprecated
    `enclosing_range` (field 7, packed like `range`); newer producers may use the
    `typed_enclosing_range` oneof instead. A stock binary emits neither, so this
    returns None and attribution falls back to the nearest-preceding heuristic."""
    which = occ.WhichOneof("typed_enclosing_range")
    if which == "single_line_enclosing_range":
        return occ.single_line_enclosing_range.line
    if which == "multi_line_enclosing_range":
        return occ.multi_line_enclosing_range.start_line
    if occ.enclosing_range:
        return occ.enclosing_range[0]
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
        callable_by_start_line: dict[int, str] = {}
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
                # First callable def at a given start line wins, mirroring the
                # header-dedup rule above; used for exact enclosing attribution.
                callable_by_start_line.setdefault(line, occ.symbol)
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
            # Exact attribution when the binary emits enclosing ranges (#504): the
            # caller is the callable definition that *contains* the call site,
            # identified by its start line. Falls back to the nearest-preceding
            # callable definition when no enclosing range is present (stock binary)
            # or when it names a non-callable container (e.g. a field initializer).
            caller_symbol: str | None = None
            enclosing_line = _occurrence_enclosing_start_line(occ)
            if enclosing_line is not None:
                caller_symbol = callable_by_start_line.get(enclosing_line)
            if caller_symbol is None:
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
