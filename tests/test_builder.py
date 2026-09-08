"""Unit tests for cppgraph.builder using synthetic SCIP indexes.

Synthetic instead of a checked-in real .scip: keeps tests fast and focused on
the attribution logic itself, independent of scip-clang's specific quirks
(covered separately by the MongoDB acceptance script in scratch/).
"""

from __future__ import annotations

from cppgraph.builder import (
    _is_direct_member,
    build_graph,
    is_callable_symbol,
    is_term_symbol,
    real_documentation,
)
from cppgraph.proto import scip_pb2

DEFINITION = scip_pb2.SymbolRole.Definition
FORWARD_DEFINITION = scip_pb2.SymbolRole.ForwardDefinition


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


def test_references_deduped_across_header_includes() -> None:
    sym = "cxx . . $ mongo/Foo#"
    # same occurrence surfacing from two TUs after scip-clang merges indexes
    docs = [scip_pb2.Document(relative_path="foo.h") for _ in range(2)]
    for d in docs:
        d.occurrences.append(_occurrence(sym, 3))
    index = scip_pb2.Index(documents=docs)
    graph = build_graph(index, include_references=True)
    assert len(graph.references_of(sym)) == 1


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
