"""Regression guard for the scip.proto backward-compatibility invariant.

AGENTS.md: "keep the vendored scip.proto a superset of what any supported
scip-clang emits, and read every optional field as optional — an absent repeated
field is an empty list, i.e. 'feature not present', never an error."

What these tests prove: the reading code (`build_graph`) degrades correctly when
the optional fields this repo's scip-clang patches add (`enclosing_range`,
ReadAccess/WriteAccess role bits, `SymbolInformation.kind`, …) are simply absent
on the wire — which is exactly what a .scip from an older/stock binary looks
like. We build a real serialized index touching ONLY the fields the oldest
supported scip.proto shape has (never setting the newer optional fields — for
proto3, leaving an optional/repeated field unset IS the old shape) and feed the
BYTES through the actual parse + build path, so an accidental "assume the field
is there" regression fails loudly instead of reading a default as data.

What they do NOT prove: that arbitrary future wire-format changes are safe.
That is a property of protobuf itself, not of this codebase, and no test here
can establish it. The complementary direction — a .scip carrying a field this
.proto does not know (a NEWER writer) — is asserted too: protobuf skips unknown
fields, so the reader must not break. Appending one encoded unknown field to
otherwise-real serialized bytes is deliberate: hand-crafting whole old-schema
wire bytes with different field numbers than the current proto would be fragile,
while an unknown field number is precisely what forward compatibility means.
"""

from __future__ import annotations

from cppgraph.builder import build_graph
from cppgraph.proto import scip_pb2

DEFINITION = scip_pb2.SymbolRole.Definition

CALLER = "cxx . . $ mongo/Foo#caller(a1)."
METHOD = "cxx . . $ mongo/Foo#bar(m1)."


def _minimal_old_shape_index() -> scip_pb2.Index:
    """Only fields the OLDEST supported scip.proto shape has: document path,
    occurrence symbol + range, and the upstream `symbol_roles` Definition bit.
    Never touches the patch-added optional fields (`enclosing_range`,
    ReadAccess/WriteAccess, `SymbolInformation.kind`, …) — leaving them unset
    is exactly what "absent optional field" means for proto3."""
    doc = scip_pb2.Document(relative_path="foo.cpp")
    doc.occurrences.extend(
        [
            scip_pb2.Occurrence(symbol=CALLER, symbol_roles=DEFINITION),
            scip_pb2.Occurrence(symbol=METHOD, symbol_roles=DEFINITION),
            scip_pb2.Occurrence(symbol=METHOD),  # a call from caller's body
        ]
    )
    for occ in doc.occurrences:
        occ.range.extend([1, 0, 10])
    return scip_pb2.Index(documents=[doc])


def _old_shape_bytes() -> bytes:
    return _minimal_old_shape_index().SerializeToString()


def test_index_without_newer_optional_fields_parses_and_builds() -> None:
    """An old-shape .scip (bytes round-trip, newer optional fields absent):
    parsing must not raise, absent fields must read as empty/default — never
    as data — and the graph must come out with the definitions and the call
    edge, unpolluted by the missing fields."""
    index = scip_pb2.Index()
    index.ParseFromString(_old_shape_bytes())  # must not raise

    assert index.documents[0].relative_path == "foo.cpp"
    # Absent repeated/optional fields read as empty/default on the parsed bytes:
    assert index.external_symbols == []
    for occ in index.documents[0].occurrences:
        assert list(occ.enclosing_range) == []  # no #504 attribution on the wire
        assert (
            occ.symbol_roles & (scip_pb2.SymbolRole.ReadAccess | scip_pb2.SymbolRole.WriteAccess)
            == 0
        )

    graph = build_graph(index)
    assert set(graph.nodes) == {CALLER, METHOD}
    assert [e.src for e in graph.callers_of(METHOD)] == [CALLER]
    node = graph.nodes[METHOD]
    assert node.end_line is None  # no enclosing_range -> no body extent invented
    assert node.scip_kind is None  # no kind patch data -> None, not a guess


def test_index_without_newer_fields_degrades_cleanly_with_attribution() -> None:
    """Same old-shape bytes through the attributed-refs path (#504 opt-in): the
    missing `enclosing_range` must degrade to file granularity — references
    resolve, carry no enclosing symbol, nothing raises."""
    index = scip_pb2.Index()
    index.ParseFromString(_old_shape_bytes())

    graph = build_graph(index, attribute_references=True)  # must not raise
    refs = graph.references_of(METHOD)
    assert [r.file for r in refs] == ["foo.cpp"]
    assert all(r.enclosing_symbol is None for r in refs)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _append_unknown_field(blob: bytes, field_number: int, payload: bytes) -> bytes:
    """Append a length-delimited field `field_number` the current .proto does
    NOT define — what a NEWER scip-clang's .scip looks like to this reader."""
    tag = (field_number << 3) | 2  # wire type 2 (length-delimited)
    return blob + _varint(tag) + _varint(len(payload)) + payload


def test_index_with_unknown_newer_field_still_parses_and_builds() -> None:
    """The forward-compatibility half of the invariant: a .scip written by a
    NEWER binary carries fields this vendored .proto doesn't know. Protobuf
    skips unknown fields, so the reader must parse cleanly and build the same
    graph from the fields it DOES understand — a newer writer can never break
    an older reader at the wire level."""
    blob = _append_unknown_field(_old_shape_bytes(), field_number=9999, payload=b"\x01\x02")

    index = scip_pb2.Index()
    index.ParseFromString(blob)  # must not raise
    # The unknown field must not disturb the fields this reader knows:
    assert index.documents[0].relative_path == "foo.cpp"
    assert len(index.documents[0].occurrences) == 3

    graph = build_graph(index)  # must not raise
    assert set(graph.nodes) == {CALLER, METHOD}
    assert [e.src for e in graph.callers_of(METHOD)] == [CALLER]
