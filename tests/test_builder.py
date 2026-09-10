"""Unit tests for cppgraph.builder using synthetic SCIP indexes.

Synthetic instead of a checked-in real .scip: keeps tests fast and focused on
the attribution logic itself, independent of scip-clang's specific quirks
(covered separately by the MongoDB acceptance script in scratch/).
"""

from __future__ import annotations

from cppgraph.builder import (
    _is_direct_member,
    _merge_external_symbol,
    build_graph,
    is_callable_symbol,
    is_term_symbol,
    real_documentation,
    signature_documentation_text,
)
from cppgraph.model import Graph
from cppgraph.proto import scip_pb2

DEFINITION = scip_pb2.SymbolRole.Definition
FORWARD_DEFINITION = scip_pb2.SymbolRole.ForwardDefinition
READ_ACCESS = scip_pb2.SymbolRole.ReadAccess
WRITE_ACCESS = scip_pb2.SymbolRole.WriteAccess


def _occurrence(symbol: str, line: int, *, roles: int = 0) -> scip_pb2.Occurrence:
    occ = scip_pb2.Occurrence(symbol=symbol, symbol_roles=roles)
    occ.range.extend([line, 0, 10])
    return occ


def test_is_callable_symbol_uses_scip_method_descriptor_suffix() -> None:
    assert is_callable_symbol("cxx . . $ mongo/Foo#bar(abc123).")
    assert not is_callable_symbol("cxx . . $ mongo/Foo#field.")
    assert not is_callable_symbol("cxx . . $ mongo/Foo#")
    assert not is_callable_symbol("cxx . . $ mongo/namespace/")


def test_is_direct_member_one_descriptor_of_each_kind() -> None:
    """A remainder that is exactly one descriptor (method, field/term, or
    nested type) is a direct member."""
    assert _is_direct_member("parse(a1).")  # method
    assert _is_direct_member("count.")  # field / term
    assert _is_direct_member("Inner#")  # nested type's own symbol
    assert _is_direct_member("ns/")  # namespace


def test_is_direct_member_rejects_nested_container_members() -> None:
    """A remainder with a container boundary before its own terminator belongs
    to something nested inside the class, not the class itself — the bug this
    helper fixes (`class_members` used to leak these)."""
    assert not _is_direct_member("Inner#field.")  # Inner's field, not Foo's
    assert not _is_direct_member("Inner#method().")  # Inner's method
    assert not _is_direct_member("Inner#Innermost#method().")  # two levels down
    assert not _is_direct_member("Inner#Innermost#")  # Innermost's own symbol,
    # still nested one level too deep from Foo's perspective


def test_is_direct_member_edge_cases() -> None:
    assert not _is_direct_member("")  # nothing left after stripping the prefix
    assert not _is_direct_member("garbage")  # no recognized terminator at all


def test_is_term_symbol_descriptor_check() -> None:
    """A term is a `.`-terminated descriptor that is not a method's `).` —
    globals, fields, static data members. Locals (and function-scope statics)
    are `local <id>` symbols, MEASURED to carry enclosing data too on the #504
    binary — they must be excluded by construction, never by a scope guess."""
    assert is_term_symbol("cxx . . $ mongo/g_a.")  # file-scope global
    assert is_term_symbol("cxx . . $ mongo/Foo#field.")  # field / static member
    assert not is_term_symbol("cxx . . $ mongo/Foo#method(m1).")  # callable
    assert not is_term_symbol("cxx . . $ mongo/Foo#")  # type
    assert not is_term_symbol("cxx . . $ mongo/namespace/")
    assert not is_term_symbol("local 0")  # local/static-local: has enclosing data


def test_over_capture_two_distinct_makeresumetoken_symbols() -> None:
    """The real-world case that motivated cppgraph: a method and an unrelated
    free function share a display name but must remain separate nodes, each
    with its own, correctly attributed callers."""
    method = "cxx . . $ mongo/ChangeStreamEventTransformation#makeResumeToken(m1)."
    helper = "cxx . . $ mongo/change_stream_test_helper/makeResumeToken(h1)."
    caller_a = "cxx . . $ mongo/ChangeStreamDefaultEventTransformation#applyTransformation(a1)."
    caller_b = "cxx . . $ mongo/SomeTest_Test#TestBody(t1)."

    doc = scip_pb2.Document(relative_path="change_stream_event_transform.cpp")
    doc.occurrences.extend(
        [
            _occurrence(caller_a, line=10, roles=DEFINITION),
            _occurrence(method, line=20, roles=DEFINITION),
            _occurrence(method, line=15),  # call from caller_a's body
        ]
    )

    test_doc = scip_pb2.Document(relative_path="change_stream_test_helpers.cpp")
    test_doc.occurrences.extend(
        [
            _occurrence(caller_b, line=1, roles=DEFINITION),
            _occurrence(helper, line=5, roles=DEFINITION),
            _occurrence(helper, line=3),  # call from caller_b's body
        ]
    )

    index = scip_pb2.Index(documents=[doc, test_doc])
    graph = build_graph(index)

    assert method in graph.nodes
    assert helper in graph.nodes
    assert method != helper

    assert [e.src for e in graph.callers_of(method)] == [caller_a]
    assert [e.src for e in graph.callers_of(helper)] == [caller_b]


def test_call_attributed_to_nearest_preceding_function_definition() -> None:
    """Stands in for a virtual-dispatch call site: cppgraph attributes the
    edge purely from the SCIP-resolved callee symbol, never from the
    call-site syntax (e.g. `ptr->method()`), so dispatch through a pointer
    is captured exactly like any other call."""
    outer = "cxx . . $ mongo/Foo#outer(o1)."
    base_virtual = "cxx . . $ mongo/Base#virtualMethod(v1)."

    doc = scip_pb2.Document(relative_path="foo.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=1, roles=DEFINITION),
            _occurrence(base_virtual, line=3),  # e.g. `ptr->virtualMethod()`
        ]
    )
    index = scip_pb2.Index(documents=[doc])
    graph = build_graph(index)

    assert [e.src for e in graph.callers_of(base_virtual)] == [outer]


def _def_with_body(symbol: str, line: int, end_line: int) -> scip_pb2.Occurrence:
    """A DEFINITION occurrence carrying its own `enclosing_range` — its body extent
    `[line..end_line]` — the way a #504-built scip-clang emits it (per the SCIP
    spec, enclosing_range is on *definitions*, not on references). Packed
    `[startLine, startCol, endLine, endCol]`."""
    occ = scip_pb2.Occurrence(symbol=symbol, symbol_roles=DEFINITION)
    occ.range.extend([line, 0, 10])
    occ.enclosing_range.extend([line, 0, end_line, 0])
    return occ


def test_enclosing_range_attributes_caller_by_containment() -> None:
    """The caller is the innermost callable definition whose body *contains* the
    call site — resolved from the definitions' enclosing ranges, since the call
    occurrence itself never carries one. A nested lambda would fool
    nearest-preceding: a call after the lambda's def but still in the outer body
    sits closer to it by line. Containment names the true container."""
    outer = "cxx . . $ pkg/Outer#run(o1)."
    nested = "cxx . . $ pkg/Outer#run/lambda#operator()(l1)."
    helper = "cxx . . $ pkg/helper(h1)."

    doc = scip_pb2.Document(relative_path="outer.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(outer, line=5, end_line=30),  # run() body spans 5..30
            _def_with_body(nested, line=10, end_line=12),  # inner lambda 10..12
            _occurrence(helper, line=20),  # call in run()'s body, past the lambda
            _occurrence(helper, line=11),  # call inside the lambda
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    callers = sorted(e.src for e in graph.callers_of(helper))
    assert callers == sorted([outer, nested])  # 20 -> run (contains it), 11 -> lambda


def test_definition_body_extent_recorded_on_node() -> None:
    """#504: a definition's enclosing_range end is kept on the Node (`end_line`,
    what `line_span` ranks by) instead of being discarded after attribution; a
    stock binary's definitions keep `end_line=None`."""
    fn = "cxx . . $ pkg/render(r1)."
    stock = "cxx . . $ pkg/plain(p1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(fn, line=5, end_line=20),
            _occurrence(stock, line=30, roles=DEFINITION),  # no enclosing_range
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert (graph.nodes[fn].line, graph.nodes[fn].end_line) == (5, 20)
    assert graph.nodes[stock].end_line is None


def test_calls_fall_back_to_nearest_preceding_without_enclosing_range() -> None:
    """A stock binary (no #504) emits no `enclosing_range`, so there are no
    intervals: attribution degrades to the nearest-preceding heuristic, unchanged.
    Here that (deliberately) misattributes the call to the inner lambda."""
    outer = "cxx . . $ pkg/Outer#run(o1)."
    nested = "cxx . . $ pkg/Outer#run/lambda#operator()(l1)."
    helper = "cxx . . $ pkg/helper(h1)."

    doc = scip_pb2.Document(relative_path="outer.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=5, roles=DEFINITION),  # no enclosing_range
            _occurrence(nested, line=10, roles=DEFINITION),
            _occurrence(helper, line=20),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert [e.src for e in graph.callers_of(helper)] == [nested]


def test_forward_definition_bit_drops_declaration_site_on_stock_binary() -> None:
    """The scip-clang `ForwardDefinition` fix (TODO.md scip-clang section):
    a bodyless declaration occurrence (in-class method decl, header prototype)
    now carries `ForwardDefinition` instead of landing as plain role-0. This
    doc has NO `enclosing_range` at all (a genuinely stock-shaped graph, where
    the nearest-preceding fallback is the only attribution path) — without the
    bit, `declaredElsewhere`'s declaration site at line 15 would misattribute
    to `nested` as its "caller" via nearest-preceding, exactly the phantom-caller
    bug. With the bit, the declaration occurrence is filtered out before it ever
    becomes a `call_sites` candidate, so no phantom edge is created; a real call
    is still attributed normally."""
    outer = "cxx . . $ pkg/Outer#run(o1)."
    nested = "cxx . . $ pkg/Outer#run/lambda#operator()(l1)."
    helper = "cxx . . $ pkg/helper(h1)."
    declared_elsewhere = "cxx . . $ pkg/Foo#declaredElsewhere(d1)."

    doc = scip_pb2.Document(relative_path="outer.cpp")
    doc.occurrences.extend(
        [
            _occurrence(outer, line=5, roles=DEFINITION),  # no enclosing_range
            _occurrence(nested, line=10, roles=DEFINITION),
            _occurrence(declared_elsewhere, line=15, roles=FORWARD_DEFINITION),
            _occurrence(helper, line=20),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert [e.src for e in graph.callers_of(helper)] == [nested]
    assert graph.callers_of(declared_elsewhere) == []


def test_declaration_only_occurrence_is_not_attributed_as_a_phantom_call() -> None:
    """On a #504 doc (has enclosing_range / callable_intervals), a role-0
    occurrence not contained by any callable body (e.g. scip-clang's known gap:
    a bodyless in-class method declaration, see DESIGN.md 'Known limitation')
    must NOT fall back to nearest-preceding-definition — that misattributes it
    to an arbitrary unrelated sibling. The edge should simply be dropped, while
    a real, contained call site is still attributed exactly."""
    sibling = "cxx . . $ pkg/Foo#sibling(s1)."
    outer = "cxx . . $ pkg/Foo#outer(o1)."
    helper = "cxx . . $ pkg/helper(h1)."
    declared_elsewhere = "cxx . . $ pkg/Foo#declaredElsewhere(d1)."

    doc = scip_pb2.Document(relative_path="foo.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(sibling, line=5, end_line=10),  # unrelated earlier definition
            _occurrence(declared_elsewhere, line=15, roles=0),  # role-0, outside every body
            _def_with_body(outer, line=20, end_line=40),
            _occurrence(helper, line=25),  # a real call inside outer's body
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert [e.src for e in graph.callers_of(helper)] == [outer]
    assert graph.callers_of(declared_elsewhere) == []


def test_attribute_references_by_containment() -> None:
    """A reference is attributed to the definition whose body contains it — read
    from the definitions' enclosing ranges, not off the reference (which carries
    none). This is the 'type -> the function that uses it' usage-view edge."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(user, line=5, end_line=20),  # render() body 5..20
            _occurrence(typ, line=10),  # a use of Widget inside render()
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert [r.enclosing_symbol for r in graph.references_of(typ)] == [user]


def test_reference_attributes_to_innermost_callable_or_type() -> None:
    """Nesting: a use inside a method attributes to the method (innermost
    callable); a use at class-body scope (a field's type) attributes to the class
    (a type is a valid usage container, a namespace would be too coarse)."""
    cls = "cxx . . $ pkg/C#"
    method = "cxx . . $ pkg/C#m(m1)."
    widget = "cxx . . $ pkg/Widget#"

    doc = scip_pb2.Document(relative_path="c.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(cls, line=1, end_line=50),  # class C body 1..50
            _def_with_body(method, line=10, end_line=20),  # method body 10..20
            _occurrence(widget, line=15),  # used inside the method
            _occurrence(widget, line=3),  # used at class scope (a field's type)
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    enclosing = sorted(r.enclosing_symbol for r in graph.references_of(widget))
    assert enclosing == sorted([method, cls])  # 15 -> method (innermost), 3 -> class


def test_reference_outside_every_body_is_unattributed() -> None:
    """A use at file scope, sitting *between* sibling definition bodies (a global's
    type, an include-driven ref), belongs to no enclosing definition. It must
    attribute to None — and cheaply: this is the case the old per-point scan walked
    every preceding interval for. The sweep must still resolve a *later* use inside
    a following sibling, proving the stack recovers across the gap."""
    a = "cxx . . $ pkg/a(a1)."
    b = "cxx . . $ pkg/b(b1)."
    widget = "cxx . . $ pkg/Widget#"

    doc = scip_pb2.Document(relative_path="siblings.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(a, line=1, end_line=10),  # a() body 1..10
            _def_with_body(b, line=20, end_line=30),  # b() body 20..30
            _occurrence(widget, line=15),  # between the two bodies -> None
            _occurrence(widget, line=25),  # inside b() -> b
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    by_line = {r.line: r.enclosing_symbol for r in graph.references_of(widget)}
    assert by_line == {15: None, 25: b}


def test_references_unattributed_without_the_flag() -> None:
    """attribute_references defaults off: references stay pure locations."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend([_def_with_body(user, 5, 20), _occurrence(typ, line=10)])
    graph = build_graph(scip_pb2.Index(documents=[doc]))  # no attribute_references

    assert [r.enclosing_symbol for r in graph.references_of(typ)] == [None]


def test_references_unattributed_on_stock_binary() -> None:
    """With the flag but a stock binary (no enclosing_range on the defs), there
    are no intervals, so references degrade to pure locations — never guessed."""
    typ = "cxx . . $ pkg/Widget#"
    user = "cxx . . $ pkg/render(r1)."

    doc = scip_pb2.Document(relative_path="render.cpp")
    doc.occurrences.extend(
        [
            _occurrence(user, line=5, roles=DEFINITION),  # no enclosing_range
            _occurrence(typ, line=10),
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert [r.enclosing_symbol for r in graph.references_of(typ)] == [None]


def test_global_initializer_reads_attribute_to_the_global() -> None:
    """#504 emits `enclosing_range` on term (global) definitions too, spanning
    the whole declaration including the initializer (saveVarDecl, measured on
    the #504 binary). So a global's read of another global inside its
    initializer attributes to it — the `global_init_references` fact — and a
    callable used in an initializer attributes to the global in the usage view
    (it was a bare unattributed location before term intervals were collected).
    Mirrors the measured fixture: `int g_a = helper(); static int g_b = g_a+1;`
    """
    helper = "cxx . . $ pkg/helper(h1)."
    g_a = "cxx . . $ pkg/g_a."
    g_b = "cxx . . $ pkg/g_b."

    doc = scip_pb2.Document(relative_path="globals.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(helper, line=0, end_line=0),  # int helper() {...}
            _def_with_body(g_a, line=1, end_line=1),  # int g_a = helper();
            _occurrence(helper, line=1),  # the call in g_a's initializer
            _def_with_body(g_b, line=2, end_line=2),  # static int g_b = g_a + 1;
            _occurrence(g_a, line=2),  # the read of g_a in g_b's initializer
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert [r.enclosing_symbol for r in graph.references_of(g_a)] == [g_b]
    assert [r.enclosing_symbol for r in graph.references_of(helper)] == [g_a]


def test_term_interval_is_not_a_call_attribution_boundary() -> None:
    """Term intervals feed the usage/reference sweep only, never
    `callable_intervals`: a call inside a global's initializer region stays an
    uncontained call site on a #504 doc (dropped by the declaration rule, as
    before) — it must not fabricate a `calls` edge with the global as caller.
    The call's *reference* record still attributes to the global."""
    helper = "cxx . . $ pkg/helper(h1)."
    g_a = "cxx . . $ pkg/g_a."

    doc = scip_pb2.Document(relative_path="globals.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(helper, line=0, end_line=0),
            _def_with_body(g_a, line=1, end_line=1),  # int g_a = helper();
            _occurrence(helper, line=1),  # the call in g_a's initializer
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert graph.callers_of(helper) == []  # no phantom calls edge from the global
    assert [r.enclosing_symbol for r in graph.references_of(helper)] == [g_a]


def test_read_inside_lambda_in_global_initializer_attributes_to_the_global() -> None:
    """MEASURED on the #504 binary (scratch fixture, 2026-09-07): a lambda
    inside a global's initializer gets NO symbol and NO enclosing_range of its
    own — there is no narrower interval for innermost-wins to pick — so a read
    inside the lambda body attributes to the global. Pinned as the known
    limitation it is: the TODO hoped containment would attribute such reads to
    the lambda; it can't, because scip-clang emits no lambda interval there.
    The tool states the region fact; whether the read runs at initialization
    (this lambda runs inside compute() during init; a stored std::function
    closure would run lazily) is the reader's judgment."""
    compute = "cxx . . $ pkg/compute(c1)."
    other = "cxx . . $ pkg/other_global."
    g = "cxx . . $ pkg/g_lambda."

    doc = scip_pb2.Document(relative_path="lambda.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(other, line=0, end_line=0),  # int other_global = 7;
            _def_with_body(compute, line=1, end_line=1),  # int compute(int (*f)()){...}
            # int g = compute([]() {        <- line 2
            #     return other_global;     <- line 3, inside the lambda body
            # });                          <- line 4
            _def_with_body(g, line=2, end_line=4),  # the var decl spans lines 2..4
            _occurrence(other, line=3),  # the read inside the lambda body
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert [r.enclosing_symbol for r in graph.references_of(other)] == [g]


def test_local_variable_enclosing_data_never_hijacks_attribution() -> None:
    """MEASURED: local variables (including function-scope statics) DO carry
    `enclosing_range` data on their `local <id>` definition occurrences — but
    the term descriptor check excludes those symbols, so a read on a local's
    declaration line (where the local's own interval would be innermost)
    attributes to the enclosing *function*, exactly as before. Without the
    descriptor exclusion, local-variable intervals would steal references from
    their enclosing functions."""
    fn = "cxx . . $ pkg/fn(f1)."
    other = "cxx . . $ pkg/other_global."

    doc = scip_pb2.Document(relative_path="locals.cpp")
    doc.occurrences.extend(
        [
            _def_with_body(other, line=0, end_line=0),
            _def_with_body(fn, line=1, end_line=4),  # int fn() { ... } spans 1..4
            # int fn() {
            #     int local_var = other_global + 2;   <- line 2: local DEF + a read
            #     return local_var;                   <- line 3
            # }
            _def_with_body("local 0", line=2, end_line=2),  # the local's own range
            _occurrence(other, line=2),  # read on the local's declaration line
        ]
    )
    graph = build_graph(scip_pb2.Index(documents=[doc]), attribute_references=True)

    assert [r.enclosing_symbol for r in graph.references_of(other)] == [fn]


def test_duplicate_occurrences_from_header_merge_are_deduped() -> None:
    """A header included by multiple TUs can surface identical occurrences
    once per TU after scip-clang merges partial indexes (verified on real
    MongoDB data). Edges must be deduped by (kind, src, dst, file, line)."""
    caller = "cxx . . $ mongo/Foo#outer(o1)."
    callee = "cxx . . $ mongo/Foo#helper(h1)."

    doc = scip_pb2.Document(relative_path="foo.h")
    doc.occurrences.extend(
        [
            _occurrence(caller, line=1, roles=DEFINITION),
            _occurrence(callee, line=2),
            _occurrence(callee, line=2),  # duplicate from a second including TU
        ]
    )
    index = scip_pb2.Index(documents=[doc])
    graph = build_graph(index)

    assert len(graph.callers_of(callee)) == 1


def test_implements_relationship_becomes_an_edge() -> None:
    base = "cxx . . $ mongo/Animal#sound(a1)."
    override = "cxx . . $ mongo/Dog#sound(d1)."

    doc = scip_pb2.Document(relative_path="animal.h")
    sym_info = scip_pb2.SymbolInformation(symbol=override)
    sym_info.relationships.add(symbol=base, is_implementation=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    implements = [e for e in graph.edges if e.kind == "implements"]
    assert len(implements) == 1
    assert implements[0].src == override
    assert implements[0].dst == base
    # method override is `implements`, never `inherits`
    assert not [e for e in graph.edges if e.kind == "inherits"]


def test_references_collected_by_default_and_skippable() -> None:
    typ = "cxx . . $ mongo/ResumeTokenData#"
    user = "cxx . . $ mongo/Consumer#use()."
    doc = scip_pb2.Document(relative_path="consumer.cpp")
    doc.occurrences.append(_occurrence(user, 10, roles=DEFINITION))
    doc.occurrences.append(_occurrence(typ, 12))  # a plain use of the type
    doc.occurrences.append(_occurrence(typ, 20))
    index = scip_pb2.Index(documents=[doc])

    # on by default
    graph = build_graph(index)
    refs = graph.references_of(typ)
    assert {r.line for r in refs} == {12, 20}
    assert all(r.file == "consumer.cpp" for r in refs)
    # the referenced type becomes a node so it's interned/findable
    assert typ in graph.nodes

    # skippable for a leaner store
    assert build_graph(index, include_references=False).references == []


def test_references_exclude_definitions_and_locals() -> None:
    sym = "cxx . . $ mongo/Foo#"
    local = "local 4"
    doc = scip_pb2.Document(relative_path="f.cpp")
    doc.occurrences.append(_occurrence(sym, 5, roles=DEFINITION))  # def, not a ref
    doc.occurrences.append(_occurrence(sym, 9))  # a real ref
    doc.occurrences.append(_occurrence(local, 9))  # local, skipped
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index, include_references=True)
    assert {r.line for r in graph.references_of(sym)} == {9}
    assert graph.references_of(local) == []


def test_references_exclude_forward_definitions() -> None:
    """The ForwardDefinition bit drops a bodyless declaration from the reference
    index too — the same exclusion the calls loop applies (a decl site is
    neither a call nor a use, on either surface)."""
    sym = "cxx . . $ mongo/Foo#"
    doc = scip_pb2.Document(relative_path="f.cpp")
    doc.occurrences.append(_occurrence(sym, 5, roles=DEFINITION))  # def, not a ref
    doc.occurrences.append(_occurrence(sym, 15, roles=FORWARD_DEFINITION))  # decl, not a ref
    doc.occurrences.append(_occurrence(sym, 9))  # a real ref
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index, include_references=True)
    assert {r.line for r in graph.references_of(sym)} == {9}


def test_references_deduped_across_header_includes() -> None:
    sym = "cxx . . $ mongo/Foo#"
    # same occurrence surfacing from two TUs after scip-clang merges indexes
    docs = [scip_pb2.Document(relative_path="foo.h") for _ in range(2)]
    for d in docs:
        d.occurrences.append(_occurrence(sym, 3))
    index = scip_pb2.Index(documents=docs)
    graph = build_graph(index, include_references=True)
    assert len(graph.references_of(sym)) == 1


def test_reference_carries_write_access_role() -> None:
    # a scip-clang read-write-access-patch binary tags `g = 5;` with WriteAccess;
    # the builder must mask it through to the stored Reference.
    typ = "cxx . . $ mongo/Counter#"
    doc = scip_pb2.Document(relative_path="use.cpp")
    doc.occurrences.append(_occurrence(typ, 7, roles=WRITE_ACCESS))
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index, include_references=True)
    (ref,) = graph.references_of(typ)
    assert ref.roles == WRITE_ACCESS


def test_plain_and_read_only_references_carry_no_write_bit() -> None:
    typ = "cxx . . $ mongo/Counter#"
    doc = scip_pb2.Document(relative_path="use.cpp")
    doc.occurrences.append(_occurrence(typ, 7))  # role 0: plain read / no data
    doc.occurrences.append(_occurrence(typ, 9, roles=READ_ACCESS))
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index, include_references=True)
    assert {r.roles for r in graph.references_of(typ)} == {0, READ_ACCESS}
    assert not any(r.roles & WRITE_ACCESS for r in graph.references_of(typ))


def test_type_definition_site_is_recorded() -> None:
    # A class definition occurrence should set the node's file/line, so
    # `find`/`explain`/`bases`/`subtypes` can locate a type — not just callables.
    cls = "cxx . . $ mongo/Widget#"
    doc = scip_pb2.Document(relative_path="widget.h")
    doc.symbols.append(scip_pb2.SymbolInformation(symbol=cls, display_name="Widget"))
    doc.occurrences.append(_occurrence(cls, 41, roles=DEFINITION))
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    node = graph.nodes[cls]
    assert node.file == "widget.h"
    assert node.line == 41


def test_class_inheritance_becomes_inherits_edge() -> None:
    # scip-clang emits the same is_implementation relationship for class
    # inheritance as for method override; the two are told apart by the SCIP
    # descriptor kind (type `#` vs method `).`). A derived class carries a
    # relationship pointing at its base. src = derived, dst = base.
    base = "cxx . . $ mongo/ServerParameter#"
    derived = "cxx . . $ mongo/IDLServerParameterWithStorage#"

    doc = scip_pb2.Document(relative_path="server_parameter.h")
    sym_info = scip_pb2.SymbolInformation(symbol=derived)
    sym_info.relationships.add(symbol=base, is_implementation=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    inherits = [e for e in graph.edges if e.kind == "inherits"]
    assert len(inherits) == 1
    assert inherits[0].src == derived
    assert inherits[0].dst == base
    # class inheritance is `inherits`, never `implements`
    assert not [e for e in graph.edges if e.kind == "implements"]


# --- Relationship.is_type_definition (typed-by edges) ------------------------


def test_typed_by_relationship_becomes_an_edge() -> None:
    # scip-clang-patches/typed-by-on-v0.4.0.patch: a field's own
    # SymbolInformation carries {symbol: <type>, is_type_definition: true}.
    # src = the field, dst = the declared type.
    field = "cxx . . $ mongo/ResumeTokenData#bucketSize."
    typ = "cxx . . $ mongo/Duration#"

    doc = scip_pb2.Document(relative_path="resume_token_data.h")
    sym_info = scip_pb2.SymbolInformation(symbol=field)
    sym_info.relationships.add(symbol=typ, is_type_definition=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    typed_by = [e for e in graph.edges if e.kind == "typed-by"]
    assert len(typed_by) == 1
    assert typed_by[0].src == field
    assert typed_by[0].dst == typ


def test_typed_by_covers_field_variable_and_static_member_shapes() -> None:
    """The patch's three emitting sites — a field (saveFieldDecl), a static
    data member and a file-scope variable (saveVarDecl's SymbolInformation
    branch) — all land as `typed-by` with the same direction (src = the
    field/variable, dst = its declared type)."""
    field = "cxx . . $ mongo/Outer#f."
    static_member = "cxx . . $ mongo/Outer#s."
    global_var = "cxx . . $ mongo/g."
    typ = "cxx . . $ mongo/Value#"

    doc = scip_pb2.Document(relative_path="outer.h")
    for sym in (field, static_member, global_var):
        sym_info = scip_pb2.SymbolInformation(symbol=sym)
        sym_info.relationships.add(symbol=typ, is_type_definition=True)
        doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    typed_by = [e for e in graph.edges if e.kind == "typed-by"]
    assert {e.src for e in typed_by} == {field, static_member, global_var}
    assert all(e.dst == typ for e in typed_by)


def test_typed_by_and_implementation_flags_are_independent() -> None:
    """The proto's is_implementation and is_type_definition are independent
    boolean flags on the same Relationship message (not enum variants), so the
    builder must use a separate `if`, not elif — one relationship carrying
    both flags yields BOTH edges."""
    derived = "cxx . . $ mongo/Dog#"
    base = "cxx . . $ mongo/Animal#"

    doc = scip_pb2.Document(relative_path="dog.h")
    sym_info = scip_pb2.SymbolInformation(symbol=derived)
    sym_info.relationships.add(symbol=base, is_implementation=True, is_type_definition=True)
    doc.symbols.append(sym_info)
    index = scip_pb2.Index(documents=[doc])

    graph = build_graph(index)

    kinds = {(e.kind, e.src, e.dst) for e in graph.edges}
    assert ("inherits", derived, base) in kinds  # type -> type splits to inherits
    assert ("typed-by", derived, base) in kinds
    # and neither flag was dropped in favor of the other
    assert len([e for e in graph.edges if e.kind in ("inherits", "implements", "typed-by")]) == 2


# --- SymbolInformation.documentation ----------------------------------------


def test_real_documentation_keeps_genuine_doc_comment() -> None:
    """The ~10% genuine share measured on the mongo corpus (SCIP_AUDIT.md):
    extracted doc-comment text, kept verbatim. The proto field is `repeated
    string` — entries are newline-joined, unobservable on real data (measured:
    every one of the 810,919 corpus SymbolInformations carries at most ONE
    entry)."""
    assert real_documentation(["Returns true if the edge AB intersects."]) == (
        "Returns true if the edge AB intersects."
    )
    assert real_documentation(["line one", "line two"]) == "line one\nline two"
    # surrounding whitespace is noise, not documentation
    assert real_documentation(["  /** Doc. */\n"]) == "/** Doc. */"


def test_real_documentation_filters_placeholder_and_auto_text() -> None:
    """Everything scip-clang auto-writes must come back None — surfacing
    boilerplate as a doc comment would present a fabricated-looking "fact":
    the literal `"No documentation available."` placeholder (84.45% of corpus
    symbols), `namespace X` / `inline namespace X` / `anonymous namespace` on
    namespace symbols (the last measured 4,217 symbols the audit's "genuine"
    bucket had lumped in), and `File: Y` on the synthetic file symbol."""
    assert real_documentation(["No documentation available."]) is None
    assert real_documentation(["namespace mongo"]) is None
    assert real_documentation(["inline namespace literals"]) is None
    assert real_documentation(["anonymous namespace"]) is None
    assert real_documentation(["File: src/mongo/db/foo.cpp"]) is None
    # empty / whitespace-only / no entries at all
    assert real_documentation([]) is None
    assert real_documentation([""]) is None
    assert real_documentation(["   "]) is None


def test_build_graph_keeps_genuine_documentation_on_node() -> None:
    fn = "cxx . . $ mongo/documented(d1)."
    doc = scip_pb2.Document(relative_path="doc.cpp")
    doc.occurrences.append(_occurrence(fn, 1, roles=DEFINITION))
    doc.symbols.add(symbol=fn, documentation=["/** Extracts the shard key. */"])
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[fn].documentation == "/** Extracts the shard key. */"


def test_build_graph_filters_placeholder_and_auto_documentation() -> None:
    """One symbol per auto-text shape plus one with no documentation field at
    all: every node keeps `documentation=None` — nothing crashes, and no
    auto-text is surfaced as if it were a real doc comment."""
    cases = {
        "cxx . . $ mongo/plain(p1).": ["No documentation available."],
        "cxx . . $ mongo/ns/": ["namespace mongo"],
        "cxx . . $ mongo/inline_ns/": ["inline namespace literals"],
        "cxx . . $ mongo/anon/": ["anonymous namespace"],
        "cxx . . $ mongo/file": ["File: src/mongo/db/foo.cpp"],
    }
    doc = scip_pb2.Document(relative_path="auto.cpp")
    for sym, documentation in cases.items():
        doc.symbols.add(symbol=sym, documentation=documentation)
    doc.symbols.add(symbol="cxx . . $ mongo/nodoc(n1).")  # no documentation field
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert all(graph.nodes[sym].documentation is None for sym in cases)
    assert graph.nodes["cxx . . $ mongo/nodoc(n1)."].documentation is None


def test_build_graph_first_real_documentation_wins_across_documents() -> None:
    """A symbol's `SymbolInformation` appears once per document (a header
    included by N TUs). A placeholder-only visit must not block a later
    genuine comment (None keeps the door open), and a genuine comment already
    captured must not be overwritten by a later duplicate."""
    fn = "cxx . . $ mongo/dup(d1)."
    first = scip_pb2.Document(relative_path="a.cpp")
    first.symbols.add(symbol=fn, documentation=["No documentation available."])
    second = scip_pb2.Document(relative_path="b.cpp")
    second.symbols.add(symbol=fn, documentation=["/** The real comment. */"])
    third = scip_pb2.Document(relative_path="c.cpp")
    third.symbols.add(symbol=fn, documentation=["No documentation available."])
    graph = build_graph(scip_pb2.Index(documents=[first, second, third]))

    assert graph.nodes[fn].documentation == "/** The real comment. */"


# --- SymbolInformation.kind (kind-patched binaries only) ---------------------


def test_build_graph_captures_symbol_kind_on_node() -> None:
    """A kind-patched binary (patchset 4) fills `SymbolInformation.kind`: the
    builder carries the enum NAME on the node — additive info on top of the
    descriptor-suffix classification (`is_callable_symbol`/`is_type_symbol`/
    `is_term_symbol`), which stays the source of truth for edge typing."""
    sym = "cxx . . $ mongo/Counter#increment(a1)."
    doc = scip_pb2.Document(relative_path="counter.cpp")
    doc.symbols.add(symbol=sym, kind=scip_pb2.SymbolInformation.StaticMethod)
    doc.occurrences.append(_occurrence(sym, 1, roles=DEFINITION))
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[sym].scip_kind == "StaticMethod"


def test_build_graph_stock_scip_yields_no_kind() -> None:
    """A stock binary never sets `kind` — proto3 reads it back as 0
    (UnspecifiedKind). That is "no info", never an error: nodes keep
    `scip_kind=None` and the graph builds exactly as before this feature."""
    doc = scip_pb2.Document(relative_path="plain.cpp")
    doc.symbols.add(symbol="cxx . . $ mongo/Widget#")  # field absent
    doc.symbols.add(symbol="cxx . . $ mongo/step(d1).", kind=0)  # explicit default
    doc.occurrences.append(_occurrence("cxx . . $ mongo/Widget#", 3, roles=DEFINITION))
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert all(n.scip_kind is None for n in graph.nodes.values())


def test_build_graph_unknown_kind_value_degrades_to_none() -> None:
    """A Kind enum value newer than the vendored proto must degrade to no-data,
    not crash — the same read-defensively rule as any other optional field
    (a partially-patched or future binary must never take the tool down)."""
    sym = "cxx . . $ mongo/Future(f1)."
    doc = scip_pb2.Document(relative_path="future.cpp")
    si = doc.symbols.add(symbol=sym)
    si.kind = 9999  # not in this vendored proto's Kind enum
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[sym].scip_kind is None


def test_build_graph_first_real_scip_kind_wins_across_documents() -> None:
    """Same first-real-wins rule as documentation: a symbol's
    `SymbolInformation` appears once per document (a header included by N TUs).
    A kind-less visit must not block a later kinded one (None keeps the door
    open), and a kind already captured must not be overwritten by a later
    duplicate."""
    sym = "cxx . . $ mongo/dup(d1)."
    first = scip_pb2.Document(relative_path="a.cpp")
    first.symbols.add(symbol=sym)  # stock binary: kind field absent
    second = scip_pb2.Document(relative_path="b.cpp")
    second.symbols.add(symbol=sym, kind=scip_pb2.SymbolInformation.StaticMethod)
    third = scip_pb2.Document(relative_path="c.cpp")
    third.symbols.add(symbol=sym, kind=0)  # explicit proto3 default = no info
    graph = build_graph(scip_pb2.Index(documents=[first, second, third]))

    assert graph.nodes[sym].scip_kind == "StaticMethod"


# --- SymbolInformation.signature_documentation -------------------------------


def test_signature_documentation_text_keeps_non_empty_text() -> None:
    """A signature-emitting binary records the symbol's signature text; it is
    kept (whitespace-stripped, like documentation's)."""
    assert (
        signature_documentation_text(scip_pb2.Signature(text="void add(int a, int b)"))
        == "void add(int a, int b)"
    )
    assert signature_documentation_text(scip_pb2.Signature(text="  class Widget  ")) == (
        "class Widget"
    )


def test_signature_documentation_text_empty_is_none() -> None:
    """No placeholder exists for signatures (unlike `documentation`): an unset
    field reads back as the empty default instance, and empty/whitespace text
    is simply "no data" — None, never an error."""
    assert signature_documentation_text(scip_pb2.Signature()) is None
    assert signature_documentation_text(scip_pb2.Signature(text="")) is None
    assert signature_documentation_text(scip_pb2.Signature(text="   ")) is None


def test_build_graph_keeps_signature_documentation_on_node() -> None:
    fn = "cxx . . $ mongo/sigged(s1)."
    doc = scip_pb2.Document(relative_path="sig.cpp")
    doc.occurrences.append(_occurrence(fn, 1, roles=DEFINITION))
    doc.symbols.add(symbol=fn).signature_documentation.text = "void sigged(int a)"
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[fn].signature_documentation == "void sigged(int a)"


def test_build_graph_signature_documentation_none_when_absent() -> None:
    """A stock binary never sets the field: nodes keep
    `signature_documentation=None` and the graph builds exactly as before this
    feature."""
    doc = scip_pb2.Document(relative_path="plain.cpp")
    doc.symbols.add(symbol="cxx . . $ mongo/plain(p1).")
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes["cxx . . $ mongo/plain(p1)."].signature_documentation is None


def test_build_graph_first_signature_documentation_wins_across_documents() -> None:
    """Same first-wins rule as documentation: a symbol's `SymbolInformation`
    appears once per document (a header included by N TUs). An empty-text
    visit must not block a later signature (None keeps the door open), and a
    signature already captured must not be overwritten by a later duplicate."""
    fn = "cxx . . $ mongo/dup(d1)."
    first = scip_pb2.Document(relative_path="a.cpp")
    first.symbols.add(symbol=fn)  # stock binary: field absent -> empty text
    second = scip_pb2.Document(relative_path="b.cpp")
    second.symbols.add(symbol=fn).signature_documentation.text = "void dup(int a)"
    third = scip_pb2.Document(relative_path="c.cpp")
    third.symbols.add(symbol=fn).signature_documentation.text = "void dup(int a, int b)"
    graph = build_graph(scip_pb2.Index(documents=[first, second, third]))

    assert graph.nodes[fn].signature_documentation == "void dup(int a)"


# --- Index.external_symbols (Node.is_out_of_project) --------------------------
#
# Symbols referenced from the index but defined in an un-indexed external
# package (boost/absl/stdlib/…) — a repeated `SymbolInformation` list the STOCK
# scip-clang binary already emits. Consumed as node identity/metadata only:
# classified `is_out_of_project=True` with the same metadata merge as
# `Document.symbols` entries (which classify False), never any edges.


def _external_info(symbol: str) -> scip_pb2.SymbolInformation:
    """An external `SymbolInformation` with every metadata field populated."""
    si = scip_pb2.SymbolInformation(symbol=symbol, display_name="boost::Foo::bar")
    si.kind = scip_pb2.SymbolInformation.Method
    si.documentation.append("/** Boost doc. */")
    si.signature_documentation.text = "void bar(int)"
    return si


def test_build_graph_external_symbol_creates_classified_node() -> None:
    """An `external_symbols` entry with no document occurrence still becomes a
    node — classified out-of-project, its metadata populated from the external
    `SymbolInformation` (the same merge a `Document.symbols` entry gets). No
    definition site exists: the node's file stays None. Today such a symbol is
    either a file-less phantom (referenced somewhere) or missing entirely."""
    ext = "cxx . . $ boost/Foo#bar(a1)."
    graph = build_graph(scip_pb2.Index(external_symbols=[_external_info(ext)]))

    node = graph.nodes[ext]
    assert node.is_out_of_project is True
    assert node.display_name == "boost::Foo::bar"
    assert node.documentation == "/** Boost doc. */"
    assert node.scip_kind == "Method"
    assert node.signature_documentation == "void bar(int)"
    assert node.file is None
    assert node.line is None


def test_build_graph_external_symbols_create_no_edges() -> None:
    """The audit measured 867 relationships on the corpus's external symbols;
    none may become edges — `Graph.add_edge` needs a real project document
    path as provenance, and an external symbol has none. Node
    identity/metadata only."""
    ext = "cxx . . $ boost/Foo#bar(a1)."
    si = _external_info(ext)
    si.relationships.add(symbol="cxx . . $ boost/Foo#", is_implementation=True)
    graph = build_graph(scip_pb2.Index(external_symbols=[si]))

    assert graph.edges == []
    assert set(graph.nodes) == {ext}


def test_build_graph_doc_symbols_entry_is_project_native() -> None:
    """A `Document.symbols` entry with no external counterpart classifies the
    node project-native (`False`) — exactly as before this feature built it,
    plus the now-explicit classification."""
    fn = "cxx . . $ mongo/mine(m1)."
    doc = scip_pb2.Document(relative_path="mine.cpp")
    doc.symbols.add(symbol=fn, display_name="mine")
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[fn].is_out_of_project is False


def test_build_graph_pure_phantom_node_stays_unclassified() -> None:
    """A symbol only ever interned as an edge/reference endpoint (no
    `SymbolInformation` from either source) keeps `is_out_of_project=None` —
    "no evidence", distinct from both classifications."""
    caller = "cxx . . $ mongo/caller(c1)."
    phantom_callee = "cxx . . $ mongo/phantom(p1)."
    doc = scip_pb2.Document(relative_path="a.cpp")
    doc.symbols.add(symbol=caller)  # the caller has SymbolInformation...
    doc.occurrences.append(_occurrence(caller, 1, roles=DEFINITION))
    doc.occurrences.append(_occurrence(phantom_callee, 3))  # ...the callee doesn't
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.nodes[caller].is_out_of_project is False
    assert graph.nodes[phantom_callee].is_out_of_project is None


def test_build_graph_project_native_wins_over_external() -> None:
    """A symbol in BOTH `external_symbols` and some `Document.symbols` (a
    cross-TU merge) ends up project-native — and picks up its real file/line
    from the project definition, never the file-less external shape."""
    sym = "cxx . . $ both(b1)."
    doc = scip_pb2.Document(relative_path="proj.cpp")
    doc.symbols.add(symbol=sym, display_name="both")
    doc.occurrences.append(_occurrence(sym, 7, roles=DEFINITION))
    graph = build_graph(scip_pb2.Index(external_symbols=[_external_info(sym)], documents=[doc]))

    node = graph.nodes[sym]
    assert node.is_out_of_project is False
    assert node.file == "proj.cpp"
    assert node.line == 7


def test_external_classification_never_overwrites_project_native() -> None:
    """The precedence rule is explicit, not an accident of pass order: the
    external pass refuses to flip a node already carrying project-native
    evidence, so `False` wins whichever order the passes run in."""
    graph = Graph()
    sym = "cxx . . $ both(b1)."
    node = graph.add_node(sym)
    node.is_out_of_project = False  # as a doc.symbols entry sets it

    _merge_external_symbol(graph, _external_info(sym))

    assert graph.nodes[sym].is_out_of_project is False


def test_build_graph_marks_external_symbols_capable_even_when_empty() -> None:
    """The graph-level marker means "built with this feature", not "found at
    least one external symbol": an index whose `external_symbols` is empty
    still produces a graph the store writes the capability flag from."""
    doc = scip_pb2.Document(relative_path="plain.cpp")
    doc.symbols.add(symbol="cxx . . $ mongo/plain(p1).")
    graph = build_graph(scip_pb2.Index(documents=[doc]))

    assert graph.has_external_symbols is True
