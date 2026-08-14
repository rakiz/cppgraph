"""Tests for `cppgraph.filters` path-prefix filtering.

Prefix-only (no glob/regex) matching against `Node.file`, mirroring
`drop_test_edges`'s shape so both surfaces (CLI + MCP) stay driven by the same
primitive — see `test_mcp_server.py`/`test_cli.py` for the per-tool wiring.
"""

from __future__ import annotations

from cppgraph.filters import filter_by_path, matches_path_prefix
from cppgraph.model import Graph
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
