"""In-memory graph model: nodes (SCIP symbols) and edges between them.

`Graph` is the builder's accumulation buffer (interning + dedup while a SCIP
index is walked), consumed by `write_sqlite`; it is never queried at runtime —
the query surface lives on `GraphStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# `slots=True`: these three are created in the millions during a build (mongo:
# ~0.8M nodes, ~3.5M edges, ~7.2M references). Without slots each instance carries
# a per-object `__dict__` (a full hash table); slots stores the fields in a compact
# fixed layout instead — markedly less memory and faster creation/attribute access,
# which is where the build spends ~51% of its time (pure-Python object churn).
@dataclass(slots=True)
class Node:
    symbol: str
    display_name: str = ""
    file: str | None = None
    line: int | None = None  # 0-indexed start line of the defining occurrence
    # 0-indexed end line of the definition's body extent (`enclosing_range`),
    # present only when the indexing binary emits it (#504); None on stock.
    # Drives `line_span` (rank by body size) and gates `no_incoming_calls`.
    end_line: int | None = None
    # The symbol's genuine doc-comment text, extracted by scip-clang into
    # `SymbolInformation.documentation` (placeholder/auto-generated text
    # filtered out — see `builder.real_documentation`); None when there is
    # none. Surfaced by `explain` without a source read.
    documentation: str | None = None
    # The symbol's fine-grained SCIP `SymbolInformation.kind` enum name (e.g.
    # "StaticMethod", "Enum"), emitted only by a kind-patched binary (patchset
    # 4); None when there is none (stock binary, or a Decl the patch leaves
    # unclassified — proto3's 0/UnspecifiedKind reads as "no info", never an
    # error). Additive info on top of the descriptor-suffix classification
    # (`builder.is_callable_symbol`/`is_type_symbol`/`is_term_symbol`), which
    # stays the source of truth for edge typing; surfaced by `explain`/`find`
    # and flagged `has_symbol_kind` in meta.
    scip_kind: str | None = None
    # The symbol's signature as recorded by a signature-emitting scip-clang in
    # `SymbolInformation.signature_documentation.text` (a `Signature` message
    # mirroring `Document`'s shape), or None when there is none — a stock
    # binary never sets the field, and empty text reads as None (no placeholder
    # exists for signatures, unlike `documentation`). Surfaced by `explain`
    # without a source read; distinct from the source-derived `signature`
    # extraction (`--root`), which also captures defaulted parameters the
    # recorded text may lack.
    signature_documentation: str | None = None
    # Whether the symbol is defined in an un-indexed external package
    # (`Index.external_symbols` — boost/absl/stdlib/…, a field stock
    # scip-clang already emits): True = external, False = project-native (a
    # `SymbolInformation` in some `Document.symbols` — project evidence always
    # wins over the external classification), None = no `SymbolInformation`
    # from either source (a pure phantom node, only ever an edge/reference
    # endpoint). Gated by the `has_external_symbols` meta flag — a graph built
    # before this feature carries neither the flag nor the column, and the
    # field reads None like any other no-data column.
    is_out_of_project: bool | None = None


@dataclass(slots=True)
class Edge:
    kind: str  # "calls" | "implements" | "inherits" | "typed-by"
    src: str
    dst: str
    file: str
    line: int | None = None


@dataclass(slots=True)
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
    # ReadAccess/WriteAccess bits from `Occurrence.symbol_roles` (masked to just
    # those two) — 0 when the graph doesn't carry this data (stock binary, or a
    # store built before this column existed). 0x4 = write, 0x8 = read.
    roles: int = 0


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    _edge_keys: set[tuple] = field(default_factory=set, repr=False)
    _ref_keys: set[tuple] = field(default_factory=set, repr=False)
    # Set by `build_graph` (always — the builder classifies
    # `Index.external_symbols` into `Node.is_out_of_project`, even when that
    # index listed none): "this graph was produced by a builder that carries
    # the external-symbol feature", not "at least one external symbol was
    # found". `write_sqlite` turns it into the `has_external_symbols` meta
    # flag on a FULL build; the incremental paths (`apply_update`,
    # `enrich_references`) also build partial graphs via `build_graph` but
    # never consult it — a partial index covers only changed TUs and cannot
    # classify the whole pre-existing store, so it must never claim
    # capability-completeness.
    has_external_symbols: bool = False

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
        roles: int = 0,
    ) -> None:
        """Record a use of `symbol` at `file:line`, deduped by (symbol, file,
        line) — a header included by N TUs surfaces the same occurrence N times.

        The referenced symbol becomes a node so it is interned and findable even
        if it is defined outside the indexed set (e.g. a `std::` type used here).
        `enclosing_symbol` (when known, from an enclosing_range-emitting binary)
        is the definition that contains the use site. `roles` carries the
        ReadAccess/WriteAccess bits (0 when the binary doesn't tag them).
        """
        self.add_node(symbol)
        key = (symbol, file, line)
        if key in self._ref_keys:
            return
        self._ref_keys.add(key)
        self.references.append(
            Reference(
                symbol=symbol, file=file, line=line, enclosing_symbol=enclosing_symbol, roles=roles
            )
        )

    def references_of(self, symbol: str) -> list[Reference]:
        """Filter `references` by symbol — kept as the verification helper the
        builder tests assert through (mirrors `GraphStore.references_of`)."""
        return [r for r in self.references if r.symbol == symbol]

    def callers_of(self, symbol: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == "calls" and e.dst == symbol]
