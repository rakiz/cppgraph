"""Surface-agnostic query filters shared by the CLI and the MCP server.

The two query surfaces — `cppgraph callers`/`callees`/`impact` (CLI) and the
`who_calls`/`what_it_calls`/`impact_of` MCP tools — must debounce the same noise
in the same way, or the same question gives two answers. These are the pure
filter primitives both call: test-edge dropping, trivial-callee hiding, and the
label derivation those depend on. No transport, no I/O — just a store lookup.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cppgraph.export import is_test_file

if TYPE_CHECKING:
    from cppgraph.model import Edge
    from cppgraph.store import GraphStore


# Noise in a raw SCIP symbol string that a human name never needs: the scheme
# prefix (`cxx . . $ `), the enclosing-file path baked into anonymous-namespace
# and lambda symbols (`$anonymous_namespace_src/mongo/.../file.cpp/`), the
# overload disambiguator hash (`(a1b2c3…)`), and the descriptor back-ticks.
_ANON_RE = re.compile(r"`\$anonymous_namespace_[^`]*`/")
_HASH_RE = re.compile(r"\([0-9a-f]{6,}\)")


def short_label(symbol: str) -> str:
    """A readable label derived from the SCIP string itself.

    `scip-clang` doesn't populate SymbolInformation.display_name (0% on the
    MongoDB index), so the graph has no human name to fall back on — but the SCIP
    string *is* the name, wrapped in machine noise. Strip that noise so the label
    is a fraction of the raw string yet still a substring `find` can re-resolve.
    Lossy on purpose (drops the overload hash); the exact string is one
    `full_symbols=True` away.
    """
    s = symbol.split(" $ ", 1)[-1] if " $ " in symbol else symbol
    s = _ANON_RE.sub("", s)
    s = _HASH_RE.sub("", s)
    return s.replace("`", "")


def qualified_name(symbol: str) -> str:
    """The name an overload set shares: the readable label with the parameter
    signature stripped. `Foo#parse(a1).` and `Foo#parse(a2).` both reduce to
    `mongo/Foo#parse`, so overloads (distinct SCIP hashes) group under one key.
    Falls back to the label itself when there's no `(` to cut at."""
    label = short_label(symbol)
    return label.split("(", 1)[0].rstrip(".")


# Callees an LLM almost never cares about when reading "what does this call?":
# ubiquitous comparison/assertion/error-wrapping helpers and compiler builtins
# that bury the domain edges. Matched against the readable label (substring for
# families like `operator`, exact for the named helpers). Opt-in via
# `hide_trivial` so the default stays lossless.
_TRIVIAL_CALLEE_SUBSTR = ("operator", "source_location", "__builtin_")
_TRIVIAL_CALLEE_NAMES = frozenset(
    {
        "tassert",
        "uassert",
        "massert",
        "fassert",
        "iassert",
        "invariant",
        "makeStatus",
        "makeStatusOK",
        "Status",
        "StatusWith",
    }
)


def is_trivial_callee(symbol: str) -> bool:
    """True if `symbol`'s label is a ubiquitous helper (see `hide_trivial`)."""
    label = short_label(symbol)
    leaf = qualified_name(symbol).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    if leaf in _TRIVIAL_CALLEE_NAMES:
        return True
    return any(s in label for s in _TRIVIAL_CALLEE_SUBSTR)


def is_noise_symbol(symbol: str) -> bool:
    """True if `symbol` is a compiler-generated / boilerplate hit a `find` almost
    never wants: an unnamed type (a lambda surfaces as `$anonymous_type_N#…`), or
    a trivial helper (operators, `*assert`, `makeStatus`, …). Anonymous
    *namespace* functions are real code and are **not** filtered — only unnamed
    *types* are. Opt-in via `hide_trivial` so the default `find` stays lossless."""
    if "$anonymous_type" in symbol:
        return True
    return is_trivial_callee(symbol)


def _far_symbol(edge: Edge, on: str) -> str:
    return edge.src if on == "src" else edge.dst


def drop_test_edges(store: GraphStore, edges: list[Edge], *, on: str) -> list[Edge]:
    """Drop edges whose far endpoint (`src` for callers, `dst` for callees) is
    defined in a test file — resolved via the node's definition site, so a test's
    destructor teardown site (`~..._Test`) is dropped along with ordinary test
    callers."""
    kept: list[Edge] = []
    for e in edges:
        node = store.get_node(_far_symbol(e, on))
        if node is None or not is_test_file(node.file):
            kept.append(e)
    return kept
