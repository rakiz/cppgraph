"""Tests for `cppgraph.filters` path-prefix filtering.

Prefix-only (no glob/regex) matching against `Node.file`, mirroring
`drop_test_edges`'s shape so both surfaces (CLI + MCP) stay driven by the same
primitive — see `test_mcp_server.py`/`test_cli.py` for the per-tool wiring.
"""

from __future__ import annotations

from cppgraph.filters import (
    ambiguous_candidate_hint,
    filter_by_path,
    matches_path_prefix,
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
