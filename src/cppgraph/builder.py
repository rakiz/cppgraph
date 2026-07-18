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


def _occurrence_enclosing_range(occ: scip_pb2.Occurrence) -> tuple[int, int] | None:
    """The `(start_line, end_line)` of the occurrence's `enclosing_range`, or None.

    Per the SCIP spec (`scip.proto`), `enclosing_range` on a **definition** is the
    full extent of that definition (a function/class body); it is *not* emitted on
    references pointing at their enclosing function. So we read it off definition
    occurrences and use the resulting intervals to attribute references/calls by
    containment (see `build_graph`). A #504-built scip-clang fills the deprecated
    `enclosing_range` (field 7, packed `[startLine, startCol, endLine, endCol]`, or
    3 elements when single-line); newer producers may use the
    `typed_enclosing_range` oneof. A stock binary emits neither -> None."""
    which = occ.WhichOneof("typed_enclosing_range")
    if which == "single_line_enclosing_range":
        return (occ.single_line_enclosing_range.line, occ.single_line_enclosing_range.line)
    if which == "multi_line_enclosing_range":
        r = occ.multi_line_enclosing_range
        return (r.start_line, r.end_line)
    er = occ.enclosing_range
    if er:
        return (er[0], er[2] if len(er) >= 4 else er[0])
    return None


def _innermost_enclosing(
    intervals: list[tuple[int, int, str]], starts: list[int], line: int
) -> str | None:
    """The innermost callable definition whose `enclosing_range` interval contains
    `line`, or None.

    `intervals` is `(start_line, end_line, symbol)` for callable definitions,
    sorted by `start`; `starts` is their start lines (for a bisect). SCIP ranges
    nest cleanly, so scanning from the greatest start <= line downwards, the first
    interval that still contains `line` (`end >= line`) is the innermost match —
    e.g. a call sitting past a nested lambda's body attributes to the outer
    function, not the lambda."""
    pos = bisect.bisect_right(starts, line) - 1
    while pos >= 0:
        start, end, symbol = intervals[pos]
        if end >= line:
            return symbol
        pos -= 1
    return None


def build_graph(
    index: scip_pb2.Index,
    *,
    include_references: bool = True,
    attribute_references: bool = False,
) -> Graph:
    """Build the graph from a SCIP index.

    `include_references` (default on) collects an exact reference-location index
    (`Graph.references`): every non-local, non-definition occurrence as
    `symbol -> file:line`. Set False to skip it and get a leaner store (measured
    ~+45% size on a large index). See DESIGN.md § Graph model.

    `attribute_references` (default off) additionally records, for each
    reference, its *enclosing definition* symbol — the "type → the function that
    uses it" attribution powering the symbol-granularity usage view. It resolves
    by *containment*: `enclosing_range` is emitted on **definitions** (their own
    body extent, per the SCIP spec), so each use site is attributed to the
    innermost definition whose interval contains it — not by reading
    `enclosing_range` off the reference, which never carries it. Needs a binary
    that emits `enclosing_range` (#504); with a stock binary there are no
    intervals, so references keep `enclosing_symbol = None` and degrade to file
    granularity. Opt-in because it is exact but larger. No effect unless
    `include_references` is also on.
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

        # One pass over definition occurrences: record every symbol's definition
        # site (so types/fields, not just callables, are locatable), collect the
        # callable definitions used as the nearest-preceding fallback, and collect
        # the `enclosing_range` intervals — each definition's own body extent —
        # that drive *exact* attribution by containment. Two interval sets: calls
        # attribute to the enclosing *callable*; the usage view also allows a
        # *type* container (a field's type is "used by" its class), but never a
        # namespace (too coarse to be a useful "user").
        callable_defs: list[tuple[int, str]] = []
        callable_intervals: list[tuple[int, int, str]] = []
        usage_intervals: list[tuple[int, int, str]] = []
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
            enclosing = _occurrence_enclosing_range(occ)
            if enclosing is not None:
                interval = (enclosing[0], enclosing[1], occ.symbol)
                if is_callable_symbol(occ.symbol):
                    callable_intervals.append(interval)
                    usage_intervals.append(interval)
                elif is_type_symbol(occ.symbol):
                    usage_intervals.append(interval)
        callable_defs.sort()
        boundary_lines = [line for line, _ in callable_defs]
        callable_intervals.sort()
        callable_starts = [iv[0] for iv in callable_intervals]
        usage_intervals.sort()
        usage_starts = [iv[0] for iv in usage_intervals]

        for occ in doc.occurrences:
            if occ.symbol_roles & (DEFINITION | FORWARD_DEFINITION):
                continue
            if not is_callable_symbol(occ.symbol):
                continue
            line = _occurrence_start_line(occ)
            if line is None:
                continue
            # Exact when the binary emits enclosing_range (#504): the caller is the
            # innermost callable definition whose body *contains* the call site.
            # enclosing_range lives on *definitions* (their own extent), never on
            # the call occurrence — so we test containment against those intervals,
            # not by reading it off the call. Falls back to the nearest-preceding
            # callable definition when no intervals exist (stock binary).
            caller_symbol = _innermost_enclosing(callable_intervals, callable_starts, line)
            if caller_symbol is None:
                pos = bisect.bisect_right(boundary_lines, line) - 1
                if pos < 0:
                    continue  # no enclosing callable definition found in this document
                _, caller_symbol = callable_defs[pos]
            graph.add_edge("calls", caller_symbol, occ.symbol, doc.relative_path, line)

        if include_references:
            # Exact location index: every non-local use of a symbol. With
            # `attribute_references`, each also gets its enclosing definition — the
            # innermost callable-or-type whose `enclosing_range` contains the use
            # site — so "where is this type used?" answers with the using function
            # (or class), not just the file. Needs a binary that emits
            # enclosing_range (#504); a stock binary has no intervals, so the
            # reference stays a pure location. `local ...` symbols are noise.
            for occ in doc.occurrences:
                if occ.symbol_roles & (DEFINITION | FORWARD_DEFINITION):
                    continue
                if occ.symbol.startswith("local "):
                    continue
                line = _occurrence_start_line(occ)
                if line is None:
                    continue
                enclosing_symbol: str | None = None
                if attribute_references:
                    enclosing_symbol = _innermost_enclosing(usage_intervals, usage_starts, line)
                graph.add_reference(occ.symbol, doc.relative_path, line, enclosing_symbol)

    return graph
