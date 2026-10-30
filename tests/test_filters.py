"""Tests for `cppgraph.filters` path-prefix filtering.

Prefix-only (no glob/regex) matching against `Node.file`, mirroring
`drop_test_edges`'s shape so both surfaces (CLI + MCP) stay driven by the same
primitive — see `test_mcp_server.py`/`test_cli.py` for the per-tool wiring.
"""

from __future__ import annotations

from cppgraph.filters import (
    ambiguous_candidate_hint,
    filter_by_path,
    filter_node_pairs,
    matches_path_prefix,
    prose_artifact_candidates,
    sentence_period_candidates,
)
from cppgraph.model import Graph, Node
from cppgraph.store import GraphStore, write_sqlite


def test_matches_path_prefix_no_op_when_neither_given() -> None:
    assert matches_path_prefix("vendor/lib/foo.cpp", include=None, exclude=None)
    assert matches_path_prefix(None, include=None, exclude=None)


def test_matches_path_prefix_include_keeps_only_matching_prefix() -> None:
    include = ["src/myproject/"]
    assert matches_path_prefix("src/myproject/foo.cpp", include=include, exclude=None)
    assert not matches_path_prefix("vendor/somelib/bar.cpp", include=include, exclude=None)


def test_matches_path_prefix_exclude_drops_matching_prefix() -> None:
    exclude = ["vendor/"]
    assert not matches_path_prefix("vendor/somelib/bar.cpp", include=None, exclude=exclude)
    assert matches_path_prefix("src/myproject/foo.cpp", include=None, exclude=exclude)


def test_matches_path_prefix_include_then_exclude_narrows_further() -> None:
    include = ["src/"]
    exclude = ["src/generated/"]
    assert matches_path_prefix("src/myproject/foo.cpp", include=include, exclude=exclude)
    assert not matches_path_prefix("src/generated/bar.cpp", include=include, exclude=exclude)
    assert not matches_path_prefix("vendor/bar.cpp", include=include, exclude=exclude)


def test_matches_path_prefix_none_file_excluded_when_include_given() -> None:
    # can't match a prefix it doesn't have
    assert not matches_path_prefix(None, include=["src/"], exclude=None)


def test_matches_path_prefix_none_file_kept_when_only_exclude_given() -> None:
    # can't match an exclude prefix it doesn't have either
    assert matches_path_prefix(None, include=None, exclude=["vendor/"])


def test_matches_path_prefix_normalizes_backslashes() -> None:
    assert matches_path_prefix("src\\myproject\\foo.cpp", include=["src/myproject/"], exclude=None)


def test_matches_path_prefix_is_segment_boundary_not_bare_string_prefix() -> None:
    """A bare `str.startswith` would false-positive: "src/foo" is a string
    prefix of "src/foobar.cpp" but not its containing directory, so excluding
    "src/foo" must NOT drop the unrelated sibling file "src/foobar.cpp"."""
    assert matches_path_prefix("src/foobar.cpp", include=None, exclude=["src/foo"])
    assert not matches_path_prefix("src/foo/bar.cpp", include=None, exclude=["src/foo"])
    # the prefix itself, as an exact file, still matches (segment boundary, not
    # "must have a child")
    assert not matches_path_prefix("src/foo", include=None, exclude=["src/foo"])


def test_matches_path_prefix_prefix_itself_is_slash_normalized() -> None:
    """Not just the candidate file — a Windows-style prefix must normalize too."""
    assert matches_path_prefix("src/foo/bar.cpp", include=["src\\foo"], exclude=None)


def test_matches_path_prefix_empty_list_is_same_as_not_given() -> None:
    """`include_paths=[]`/`exclude_paths=[]` (e.g. from an untouched CLI/MCP
    default) must behave exactly like `None` — including for a symbol with no
    recorded definition site, which distinguishes "not given" from "given but
    unmatchable" only via `is None`."""
    assert matches_path_prefix("src/foo.cpp", include=[], exclude=[])
    assert matches_path_prefix(None, include=[], exclude=[])
    assert matches_path_prefix(None, include=None, exclude=[])


def test_matches_path_prefix_root_slash_matches_nothing_not_everything() -> None:
    """A degenerate `"/"` (or empty-string) prefix has no segment left after
    normalization — it must not silently become a universal match, or
    `exclude=["/"]` would drop every relative path."""
    assert matches_path_prefix("src/foo.cpp", include=None, exclude=["/"])
    assert not matches_path_prefix("src/foo.cpp", include=["/"], exclude=None)


def _store(tmp_path) -> GraphStore:
    graph = Graph()
    graph.add_edge("calls", "proj_caller", "target", file="src/myproject/caller.cpp", line=1)
    graph.add_edge("calls", "vendor_caller", "target", file="vendor/somelib/caller.cpp", line=2)
    graph.nodes["proj_caller"].file = "src/myproject/caller.cpp"
    graph.nodes["vendor_caller"].file = "vendor/somelib/caller.cpp"
    path = tmp_path / "graph.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_filter_by_path_is_noop_when_neither_given(tmp_path) -> None:
    store = _store(tmp_path)
    edges = store.callers_of("target")
    assert filter_by_path(store, edges, on="src", include_paths=None, exclude_paths=None) == edges


def test_filter_by_path_exclude_drops_vendored_caller(tmp_path) -> None:
    store = _store(tmp_path)
    edges = store.callers_of("target")
    kept = filter_by_path(store, edges, on="src", include_paths=None, exclude_paths=["vendor/"])
    assert {e.src for e in kept} == {"proj_caller"}


def test_filter_by_path_include_keeps_only_project_caller(tmp_path) -> None:
    store = _store(tmp_path)
    edges = store.callers_of("target")
    kept = filter_by_path(
        store, edges, on="src", include_paths=["src/myproject/"], exclude_paths=None
    )
    assert {e.src for e in kept} == {"proj_caller"}


# --- ambiguous_candidate_hint: qualified-query normalization ------------------

BASE = "cxx . . $ mongo/DocumentSource#"
M1 = "cxx . . $ mongo/DocumentSource#createFromBson(a0)."
M2 = "cxx . . $ mongo/DocumentSource#doGetNext(a1)."
CONV_OP = "cxx . . $ mongo/OtherClass#operator mongo/DocumentSource()(a0)."


def _hint_candidates(*symbols: str) -> list[Node]:
    return [Node(symbol=s, file="d.cpp", line=1) for s in symbols]


def test_ambiguous_hint_type_itself_fires_for_qualified_query() -> None:
    """The repro: a query already in SCIP form (`mongo/Foo#` — exactly what an
    LLM copy-pastes from a prior `find` result) must be normalized to the bare
    leaf before comparing, or the type-itself hint never fires."""
    hint = ambiguous_candidate_hint("mongo/DocumentSource#", _hint_candidates(BASE, M1, M2))
    assert "type itself" in hint
    assert "mongo/DocumentSource#" in hint


def test_ambiguous_hint_type_itself_fires_for_bare_leaf_query() -> None:
    # The pre-existing bare-leaf shape must keep working exactly as before.
    hint = ambiguous_candidate_hint("DocumentSource", _hint_candidates(BASE, M1, M2))
    assert "type itself" in hint
    assert "mongo/DocumentSource#" in hint


def test_ambiguous_hint_operator_fires_for_qualified_query() -> None:
    """Same gap on the operator half: the qualified query's bare leaf must
    substring-match the conversion operator's label (`OtherClass#operator
    mongo/DocumentSource()`), where the raw qualified string never could."""
    hint = ambiguous_candidate_hint("mongo/DocumentSource#", _hint_candidates(BASE, CONV_OP))
    assert "type itself" in hint
    assert "operator" in hint
    # And the bare spelling keeps firing the operator half too.
    bare = ambiguous_candidate_hint("DocumentSource", _hint_candidates(BASE, CONV_OP))
    assert "operator" in bare


def test_ambiguous_hint_silent_for_member_only_candidates() -> None:
    # Neither trap present: methods of other shapes must not trip the hints.
    assert ambiguous_candidate_hint("mongo/DocumentSource#", _hint_candidates(M1, M2)) == ""
    assert ambiguous_candidate_hint("createFromBson", _hint_candidates(M1, M2)) == ""


# --- input hygiene: unmatched path prefixes are an explicit error, not silence ---


def test_unmatched_path_prefixes_segment_boundary() -> None:
    """The unmatched check applies the SAME segment-boundary rule as
    `matches_path_prefix` — factored, not duplicated: a prefix that matches
    nothing under the filter's own rule is exactly the one reported."""
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/foo/bar.cpp"}
    assert unmatched_path_prefixes(files, ["src/foo"], None) == ([], False)
    assert unmatched_path_prefixes(files, ["src/fo"], None) == (["src/fo"], False)


def test_unmatched_path_prefixes_normalizes_separators_and_trailing_slash() -> None:
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/foo/bar.cpp"}
    assert unmatched_path_prefixes(files, ["src/foo/"], None) == ([], False)
    assert unmatched_path_prefixes(files, ["src\\foo"], None) == ([], False)


def test_unmatched_path_prefixes_empty_lists_are_no_constraint() -> None:
    """An empty (or None) include/exclude list is 'no constraint', the same as
    `matches_path_prefix` treats it — it can never be an error."""
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/foo/bar.cpp"}
    for include, exclude in ((None, None), ([], []), ([], None), (None, [])):
        assert unmatched_path_prefixes(files, include, exclude) == ([], False)


def test_unmatched_path_prefixes_exclude_typo_is_inert() -> None:
    """An exclude prefix matching zero files excludes nothing — the result set
    is simply unfiltered by it, never an error (unlike an include typo, which
    silently empties the result and must be named)."""
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/foo/bar.cpp"}
    assert unmatched_path_prefixes(files, None, ["no/such"]) == ([], False)
    assert unmatched_path_prefixes(files, ["src/foo"], ["no/such"]) == ([], False)


def test_unmatched_path_prefixes_eligible_empty_composition() -> None:
    """Every include prefix individually matches something, yet the combined
    filter (include ∧ ¬exclude) selects zero files: eligible_empty=True — the
    message must blame the composition, not a single typo. Only computed when
    an include filter is given: excludes alone never flag it (see the
    exclude-only pins below)."""
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/foo/bar.cpp"}
    assert unmatched_path_prefixes(files, ["src/foo"], ["src/foo"]) == ([], True)
    # A composition that IS satisfiable stays False.
    both = {"src/a/a.cpp", "src/b/b.cpp"}
    assert unmatched_path_prefixes(both, ["src/a"], ["src/b"]) == ([], False)


def test_path_prefix_error_exclude_only_never_errors() -> None:
    """An EXCLUDE-only filter is never an error, whatever it does to the
    result: matching zero files it filters nothing (inert), and removing every
    file it legitimately narrows — silence in both cases, never a message
    blaming a filter the user's includes didn't break."""
    from cppgraph.filters import path_prefix_error, unmatched_path_prefixes

    # An inert exclude on a graph with no indexed definition files at all.
    assert unmatched_path_prefixes(set(), None, ["no/such"]) == ([], False)
    assert path_prefix_error(set(), None, ["no/such"]) is None
    # An exclude that removes every indexed file: a normal empty result.
    files = {"src/foo/a.cpp"}
    assert unmatched_path_prefixes(files, None, ["src"]) == ([], False)
    assert path_prefix_error(files, None, ["src"]) is None


def test_unmatched_path_prefixes_reports_unmatched_includes_in_given_order() -> None:
    """Only INCLUDE prefixes are ever reported unmatched: an exclude prefix
    that matches nothing is inert, so it must not join the error list (and an
    include typo must be named even when an exclude typo sits alongside it —
    never blame the includes' side for the excludes' non-match, nor vice
    versa)."""
    from cppgraph.filters import unmatched_path_prefixes

    files = {"src/a/a.cpp"}
    unmatched, eligible_empty = unmatched_path_prefixes(files, ["src/a", "src/x"], ["src/y"])
    assert unmatched == ["src/x"]
    assert eligible_empty is False


def test_unmatched_path_prefixes_empty_file_set_matches_nothing() -> None:
    """No indexed definition files at all: any given prefix matches nothing —
    the honest answer (and nodes with file=None are simply not in the set, so
    they never distort the check)."""
    from cppgraph.filters import unmatched_path_prefixes

    assert unmatched_path_prefixes(set(), ["src"], None) == (["src"], False)
    assert unmatched_path_prefixes(None, ["src"], None) == (["src"], False)


def test_path_prefix_error_messages_name_the_prefix() -> None:
    """The shared message both surfaces report: names the offending INCLUDE
    prefix (mirroring the init wizard's 'matches nothing — try another'
    wording) or the unsatisfiable composition; satisfiable filters give None.
    An EXCLUDE prefix matching nothing is never an error — it filters
    nothing, so the result set is simply unfiltered by it."""
    from cppgraph.filters import path_prefix_error

    files = {"src/foo/bar.cpp"}
    assert path_prefix_error(files, ["src/typo"], None) == (
        "no file matches path prefix 'src/typo' — try another prefix "
        "(segment-boundary match on definition-file paths; `stats` lists the "
        "indexed files)"
    )
    # The exclude typo sits alongside, but the error names only the include —
    # the excludes were not the problem here.
    msg = path_prefix_error(files, ["src/x"], ["src/y"])
    assert "no file matches path prefix" in msg
    assert "'src/x'" in msg
    assert "'src/y'" not in msg
    assert path_prefix_error(files, ["src/foo"], ["src/foo"]) == (
        "path filters match no file: include 'src/foo' minus exclude 'src/foo'"
    )
    assert path_prefix_error(files, ["src/foo"], None) is None
    # Exclude-side non-matches: inert on their own and next to a valid include.
    assert path_prefix_error(files, None, ["no/such"]) is None
    assert path_prefix_error(files, ["src/foo"], ["no/such"]) is None
    assert path_prefix_error(files, None, None) is None


# --- module scoping: dir prefix / exact file / glob (api_surface, dependency_cost) ---


def test_module_scope_directory_prefix_unchanged() -> None:
    """A directory-prefix entry keeps the segment-boundary rule byte-for-byte:
    matches the directory's files (at any depth) and itself, never a sibling
    that merely shares characters — the existing `matches_path_prefix`
    behavior, now via `matches_module_scope`."""
    from cppgraph.filters import matches_module_scope

    entries = ["src/pipeline"]
    assert matches_module_scope("src/pipeline/util.cpp", entries)
    assert matches_module_scope("src/pipeline/deep/x.cpp", entries)
    assert matches_module_scope("src/pipeline", entries)
    assert not matches_module_scope("src/pipeline_extra/x.cpp", entries)
    assert not matches_module_scope("src/other/x.cpp", entries)
    # Backslash and trailing-slash spellings normalize into the same rule.
    assert matches_module_scope("src/pipeline/util.cpp", ["src\\pipeline/"])


def test_module_scope_file_entry_matches_that_exact_file() -> None:
    """An exact file path scopes to that one file: it matches itself and
    nothing else — no directory-prefix widening, no sibling."""
    from cppgraph.filters import matches_module_scope

    assert matches_module_scope("mod/api.cpp", ["mod/api.cpp"])
    assert not matches_module_scope("mod/api2.cpp", ["mod/api.cpp"])
    assert not matches_module_scope("mod/api.cpp.bak", ["mod/api.cpp"])
    # No recorded definition site is in no module, whatever the scope.
    assert not matches_module_scope(None, ["mod/api.cpp", "mod/"])


def test_module_scope_glob_entry_is_deterministic_fnmatch() -> None:
    """A glob entry matches fnmatch-style on the whole relative path,
    case-sensitively (`fnmatchcase`, never the platform-cased `fnmatch`):
    the same pattern gives the same answer on every machine. `*` crosses `/`
    (fnmatch has no gitignore semantics), so one pattern spans directories —
    the point of the feature."""
    from cppgraph.filters import matches_module_scope

    assert matches_module_scope("mod/util.cpp", ["mod/*.cpp"])
    # `*` crossing `/`: a feature spread across subdirectories scopes in one entry.
    assert matches_module_scope("mod/deep/nested/x.cpp", ["mod/*.cpp"])
    assert matches_module_scope("src/a/change_stream.cpp", ["src/**/change_*.cpp"])
    assert matches_module_scope("src/b/pipeline/change_stream.cpp", ["src/**/change_*.cpp"])
    assert not matches_module_scope("src/a/unrelated.cpp", ["src/**/change_*.cpp"])
    # Case-sensitive on every platform.
    assert not matches_module_scope("MOD/util.cpp", ["mod/*.cpp"])


def test_module_scope_literal_path_with_glob_chars_matches_itself() -> None:
    """A real file whose NAME contains a glob metacharacter must match its own
    exact-path entry: `fnmatchcase("foo[1].cpp", "foo[1].cpp")` is False (the
    entry's `[1]` reads as a character class matching `foo1.cpp`), so literal
    equality is checked first — fail-closed would otherwise make such a path
    unreachable by the very entry that names it. The glob reading still works
    alongside (it cannot over-match: equality is one exact path)."""
    from cppgraph.filters import matches_module_scope, unmatched_module_scope

    entry = "src/gen/foo[1].cpp"
    assert matches_module_scope("src/gen/foo[1].cpp", [entry])  # literal self-match
    assert matches_module_scope("src/gen/foo1.cpp", [entry])  # the char-class reading
    assert not matches_module_scope("src/gen/foo2.cpp", [entry])
    assert not matches_module_scope("src/gen/fooA.cpp", [entry])  # [1] is not [A-Za-z]
    # The zero-match check agrees: the entry matched, so it is not reported.
    assert unmatched_module_scope({"src/gen/foo[1].cpp"}, [entry]) == []


def test_module_scope_mixed_dir_file_glob_entries() -> None:
    """ANY-of semantics across entry kinds: a scope mixing a directory
    prefix, an exact file, and a glob matches the union — the shape a feature
    spanning directories needs."""
    from cppgraph.filters import matches_module_scope

    entries = ["mod/", "src/single.cpp", "lib/**/change_*.cpp"]
    assert matches_module_scope("mod/util.cpp", entries)  # the dir prefix
    assert matches_module_scope("src/single.cpp", entries)  # the exact file
    assert matches_module_scope("lib/a/change_stream.cpp", entries)  # the glob
    assert matches_module_scope("lib/a/b/change_stream.cpp", entries)  # glob crosses /
    assert not matches_module_scope("lib/a/other.cpp", entries)
    assert not matches_module_scope("app/main.cpp", entries)


def test_unmatched_module_scope_names_the_entry() -> None:
    """The zero-match policy of the path-prefix hygiene check, applied to
    module scopes: an entry matching zero indexed files is reported — named,
    deduplicated, in given order — never a silent 'no matches'. Every entry
    kind participates: a typo'd glob is exactly as dead as a typo'd prefix."""
    from cppgraph.filters import unmatched_module_scope

    files = {"mod/util.cpp", "app/main.cpp"}
    # A never-matching glob and a never-matching file path, named in order.
    assert unmatched_module_scope(files, ["no/such/*.cpp", "app/main.cpp", "no/such.cpp"]) == [
        "no/such/*.cpp",
        "no/such.cpp",
    ]
    # Deduplication.
    assert unmatched_module_scope(files, ["no/such/", "no/such/"]) == ["no/such/"]
    # Every entry matches something: no error.
    assert unmatched_module_scope(files, ["mod/", "app/main.cpp", "mod/*.cpp"]) == []
    # Empty/None scope: no constraint, never an error.
    assert unmatched_module_scope(files, []) == []
    assert unmatched_module_scope(files, None) == []
    # A graph with no definitions makes every given entry unmatched.
    assert unmatched_module_scope(set(), ["mod/"]) == ["mod/"]
    assert unmatched_module_scope(None, ["mod/"]) == ["mod/"]


def test_module_scope_error_message_names_the_entry() -> None:
    """The single wording both surfaces report: names the offending entry and
    states what an entry can be; a satisfiable scope gives None (the
    `path_prefix_error` convention)."""
    from cppgraph.filters import module_scope_error

    files = {"src/foo/bar.cpp"}
    assert module_scope_error(files, ["src/typo"]) == (
        "no file matches module scope entry 'src/typo' — each entry is a "
        "directory prefix (segment-boundary match), an exact file path, or a "
        "shell-style glob on definition-file paths (`*` crosses `/`); "
        "`stats` lists the indexed files"
    )
    msg = module_scope_error(files, ["no/*.cpp", "also/missing.cpp"])
    assert "no file matches module scope entries" in msg
    assert "'no/*.cpp'" in msg and "'also/missing.cpp'" in msg
    assert module_scope_error(files, ["src/foo", "src/foo/bar.cpp", "src/*.cpp"]) is None
    assert module_scope_error(files, None) is None


# --- filter_node_pairs: the impact/reachable_from pair filter (both surfaces) ---


def _pair_store(tmp_path) -> GraphStore:
    graph = Graph()
    for sym, file, line in [
        ("prod_sym", "src/myproject/prod.cpp", 1),
        ("test_sym", "tests/test_foo.cpp", 2),
        ("vendor_sym", "vendor/somelib/bar.cpp", 3),
    ]:
        graph.add_node(sym)
        graph.nodes[sym].file = file
        graph.nodes[sym].line = line
    path = tmp_path / "pairs.db"
    write_sqlite(graph, path)
    return GraphStore(path)


def test_filter_node_pairs_noop_when_no_filters_given(tmp_path) -> None:
    store = _pair_store(tmp_path)
    pairs = [(s, store.get_node(s)) for s in ("prod_sym", "test_sym", "vendor_sym", "ghost")]
    assert (
        filter_node_pairs(pairs, exclude_tests=False, include_paths=None, exclude_paths=None)
        == pairs
    )


def test_filter_node_pairs_exclude_tests_drops_test_defined_keeps_unknown(tmp_path) -> None:
    """A node with no recorded definition site is kept — the same
    unresolvable-endpoint treatment as `drop_test_edges`."""
    store = _pair_store(tmp_path)
    pairs = [(s, store.get_node(s)) for s in ("prod_sym", "test_sym", "ghost")]
    kept = filter_node_pairs(pairs, exclude_tests=True, include_paths=None, exclude_paths=None)
    assert [s for s, _ in kept] == ["prod_sym", "ghost"]


def test_filter_node_pairs_include_paths(tmp_path) -> None:
    store = _pair_store(tmp_path)
    pairs = [(s, store.get_node(s)) for s in ("prod_sym", "vendor_sym")]
    kept = filter_node_pairs(
        pairs, exclude_tests=False, include_paths=["src/myproject/"], exclude_paths=None
    )
    assert [s for s, _ in kept] == ["prod_sym"]


def test_filter_node_pairs_exclude_paths(tmp_path) -> None:
    store = _pair_store(tmp_path)
    pairs = [(s, store.get_node(s)) for s in ("prod_sym", "vendor_sym")]
    kept = filter_node_pairs(
        pairs, exclude_tests=False, include_paths=None, exclude_paths=["vendor/"]
    )
    assert [s for s, _ in kept] == ["prod_sym"]


def test_filter_node_pairs_combines_both_filters(tmp_path) -> None:
    store = _pair_store(tmp_path)
    pairs = [(s, store.get_node(s)) for s in ("prod_sym", "test_sym", "vendor_sym")]
    kept = filter_node_pairs(
        pairs, exclude_tests=True, include_paths=None, exclude_paths=["vendor/"]
    )
    assert [s for s, _ in kept] == ["prod_sym"]


def test_sentence_period_candidates_strips_the_period() -> None:
    assert sentence_period_candidates("Foo#makeResumeToken.") == ["Foo#makeResumeToken"]


def test_sentence_period_candidates_clean_name_is_empty() -> None:
    assert sentence_period_candidates("Foo#makeResumeToken") == []
    assert sentence_period_candidates("") == []


def test_sentence_period_candidates_never_touches_a_hyphen() -> None:
    # `GraphStore.resolve` consumes ONLY this function, and a hyphen-join can
    # produce a different identifier — so the dot-only helper must not
    # silently include the hyphen repair.
    assert sentence_period_candidates("Widget#foo-bar") == []


def test_prose_artifact_candidates_trailing_period() -> None:
    assert prose_artifact_candidates("Foo#makeResumeToken.") == ["Foo#makeResumeToken"]


def test_prose_artifact_candidates_wrapped_hyphen() -> None:
    assert prose_artifact_candidates("_determine-ChangeStreamReaderVersion") == [
        "_determineChangeStreamReaderVersion"
    ]


def test_prose_artifact_candidates_combines_both_artifacts() -> None:
    # Fixed order, least-stripped first: the dot-only and hyphen-only repairs
    # come before the fully stripped one.
    assert prose_artifact_candidates("Determine-Change.") == [
        "Determine-Change",
        "DetermineChange.",
        "DetermineChange",
    ]


def test_prose_artifact_candidates_clean_name_is_empty() -> None:
    assert prose_artifact_candidates("Foo#makeResumeToken") == []
    assert prose_artifact_candidates("") == []


def test_prose_artifact_candidates_leaves_operator_hyphen_alone() -> None:
    # A `-` flanked by identifier characters is a markdown wrap artifact; a
    # trailing one (operator-, operator-=) is part of a genuine C++ spelling
    # and is never stripped.
    assert prose_artifact_candidates("operator-") == []
    assert prose_artifact_candidates("operator->") == []
