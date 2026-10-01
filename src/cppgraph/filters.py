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

from cppgraph.builder import READ_ACCESS, WRITE_ACCESS
from cppgraph.export import is_test_file

if TYPE_CHECKING:
    from cppgraph.model import Edge, Node, Reference
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


def _segment_match(path: str, prefix: str) -> bool:
    """One prefix's segment-boundary match against an already-normalized
    (`/`-slashed) path — THE matching rule, factored so
    `matches_path_prefix` and `unmatched_path_prefixes` cannot drift apart.
    rstrip("/") only trims a *trailing* separator (e.g. a user-given
    "src/foo/"); a prefix that's only "/" (or empty) has nothing left to
    match on a segment boundary — treat it as matching nothing, not as a
    universal match (a `not prefix` special case here would make
    `exclude=["/"]` silently drop every relative path)."""
    prefix = prefix.replace("\\", "/").rstrip("/")
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def matches_path_prefix(
    file: str | None, *, include: list[str] | None, exclude: list[str] | None
) -> bool:
    """True if `file` should be KEPT under `include`/`exclude` path prefixes.

    Prefix match only — no glob, no regex — against the normalized (`/`-slashed)
    file path AND the normalized prefixes, on a path-*segment* boundary (`"src/foo"`
    matches `"src/foo/bar.cpp"` and `"src/foo"` itself, never `"src/foobar.cpp"` —
    a bare string prefix would false-positive on a sibling file/directory that
    merely shares characters). Mirrors `is_test_file`'s own slash normalization.
    An empty `include`/`exclude` list is treated as not given (no constraint from
    that side), matching every other filter in this module. `file=None` (no
    recorded definition site) is excluded when `include` is given (it can't match
    a prefix it lacks) and kept when only `exclude` is given (same reasoning, the
    other way).
    """
    include = include or None
    exclude = exclude or None
    if file is None:
        return include is None
    p = file.replace("\\", "/")

    if include and not any(_segment_match(p, prefix) for prefix in include):
        return False
    if exclude and any(_segment_match(p, prefix) for prefix in exclude):
        return False
    return True


def unmatched_path_prefixes(
    files: set[str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
) -> tuple[list[str], bool]:
    """The path-prefix filters that CANNOT match anything, plus whether the
    combined filter is unsatisfiable — the pure check behind the input-hygiene
    rule "a filter that matches nothing is an explicit error, never a silent
    narrowing": a typo'd prefix must not be indistinguishable from "no
    matches".

    Returns `(unmatched, eligible_empty)`:
    - `unmatched` — the given include AND exclude prefixes matching ZERO of
      `files`, under the same segment-boundary rule `matches_path_prefix`
      applies (it is factored out and shared, never re-implemented here);
      deduplicated, in given order. An empty/None list contributes no prefixes
      and never errors — an absent filter is "no constraint", not a broken
      one.
    - `eligible_empty` — True when every given prefix individually matches
      something yet the COMBINED filter (include ∧ ¬exclude) selects zero
      files — e.g. `include=["src/foo"], exclude=["src/foo"]`: each prefix is
      well-spelled, but their composition cannot be satisfied, so the error
      must blame the composition rather than name a typo.

    `files` is the universe of definition-file paths to match against
    (`GraphStore.definition_files`). A node with `file=None` has no path and
    is irrelevant to this check — it simply isn't in the set (`matches_path_prefix`
    already drops such nodes when an include filter is given); the check never
    sees a None entry. An empty/None `files` makes every given prefix
    unmatched — the honest answer on a graph with no definitions."""
    include = include or []
    exclude = exclude or []
    if not include and not exclude:
        return [], False
    normalized = {f.replace("\\", "/") for f in files} if files else set()
    unmatched: list[str] = []
    seen: set[str] = set()
    for prefix in [*include, *exclude]:
        if prefix in seen:
            continue
        seen.add(prefix)
        if not any(_segment_match(f, prefix) for f in normalized):
            unmatched.append(prefix)
    eligible_empty = False
    if not unmatched:
        eligible_empty = not any(
            matches_path_prefix(f, include=include, exclude=exclude) for f in normalized
        )
    return unmatched, eligible_empty


def path_prefix_error(
    files: set[str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
) -> str | None:
    """The error message for path filters that can match nothing, or None when
    they are satisfiable — the single wording both surfaces report, so a CLI
    `--include-path` typo and the MCP `include_paths` one read the same. Mirrors
    the init wizard's unmatched-substring note ("'f' matches nothing — try
    another substring."): state the fact, name the prefix, suggest the fix."""
    unmatched, eligible_empty = unmatched_path_prefixes(files, include, exclude)
    if unmatched:
        listed = ", ".join(repr(p) for p in unmatched)
        plural = "es" if len(unmatched) > 1 else ""
        return (
            f"no file matches path prefix{plural} {listed} — try another prefix "
            "(segment-boundary match on definition-file paths; `stats` lists "
            "the indexed files)"
        )
    if eligible_empty:
        message = "path filters match no file"
        if include:
            message += ": include " + ", ".join(repr(p) for p in include)
        if exclude:
            message += " minus exclude " + ", ".join(repr(p) for p in exclude)
        return message
    return None


def filter_by_path(
    store: GraphStore,
    edges: list[Edge],
    *,
    on: str,
    include_paths: list[str] | None,
    exclude_paths: list[str] | None,
) -> list[Edge]:
    """Keep edges whose far endpoint (`src` for callers, `dst` for callees)
    passes `matches_path_prefix` — the path-prefix analog of `drop_test_edges`.
    No-op (returns `edges` unchanged) when neither `include_paths` nor
    `exclude_paths` is given."""
    if not include_paths and not exclude_paths:
        return edges
    kept: list[Edge] = []
    for e in edges:
        node = store.get_node(_far_symbol(e, on))
        file = node.file if node is not None else None
        if matches_path_prefix(file, include=include_paths, exclude=exclude_paths):
            kept.append(e)
    return kept


# --- read/write access roles (scip-clang read-write-access patch) ------------


def access_tag(roles: int) -> str:
    """Human suffix for a use site: ` (write)` on a plain write, ` (read+write)`
    on a compound one (both bits). A plain read — and a graph without role
    data — gets '' : reads are the default, and silence never fabricates."""
    if not roles & WRITE_ACCESS:
        return ""
    return " (read+write)" if roles & READ_ACCESS else " (write)"


def filter_by_access(refs: list[Reference], access: str) -> list[Reference]:
    """Keep the use sites matching an `--access`/`access` filter: 'write' keeps
    sites tagged WriteAccess; 'read' keeps sites known NOT to be a write (plain
    reads — a read+write site is a write, so it goes). Caller must gate on the
    graph's `has_access_roles` first: without role data nothing is filtered
    meaningfully, and pretending every site is a read would fabricate. Raises
    ValueError on anything but 'read'/'write' — the MCP surface doesn't
    constrain the value the way the CLI's argparse does."""
    if access == "write":
        return [r for r in refs if r.roles & WRITE_ACCESS]
    if access == "read":
        return [r for r in refs if not r.roles & WRITE_ACCESS]
    raise ValueError(f"invalid access filter {access!r}: valid choices are 'read', 'write'")


# --- ambiguous-resolve hint (shared by the MCP `_resolve` and the CLI) --------


# An operator candidate (`operator Foo()`, `operator+`): "operator" at the start
# of the label or right after a `#` member separator, not part of a longer name
# (`operatorate`). Matched against the readable label — display_name is often
# empty (scip-clang doesn't populate it), so the SCIP string is what's there.
_OPERATOR_RE = re.compile(r"(?:^|#)operator(?![A-Za-z0-9_])")


def ambiguous_candidate_hint(query: str, candidates: list[Node]) -> str:
    """Extra `hint` sentences for an ambiguous resolve, from what the candidates
    *are* — the same guidance on both surfaces. Two recurring lookalike traps:
    the type itself (`Foo#`, a symbol ending in `#`) sitting next to its own
    members, and a conversion/overloaded operator (`operator Foo()`) that merely
    shares the name. The query is normalized the same way a candidate's leaf is
    (namespace prefix and trailing `#` stripped), so a qualified query — e.g.
    `mongo/Foo#` copied from a prior `find` result — compares against the bare
    leaf instead of never matching. States the fact — never picks a candidate.
    Empty when neither trap is present."""
    q = query.casefold().rsplit("/", 1)[-1].rstrip("#").rsplit("#", 1)[-1]
    type_labels: list[str] = []
    has_operator = False
    for n in candidates:
        label = n.display_name or short_label(n.symbol)
        leaf = label.rsplit("/", 1)[-1]
        if n.symbol.endswith("#") and leaf.rstrip("#").rsplit("#", 1)[-1].casefold() == q:
            type_labels.append(label)
        if not has_operator and q in label.casefold() and _OPERATOR_RE.search(label):
            has_operator = True
    parts: list[str] = []
    if len(type_labels) == 1:
        parts.append(
            f"one candidate is the type itself ({type_labels[0]}, a symbol ending in `#`), "
            "alongside members/related symbols — if you meant the type, use that one"
        )
    elif type_labels:
        parts.append(
            f"{len(type_labels)} candidates are types themselves ({', '.join(type_labels)} — "
            "symbols ending in `#`), alongside members/related symbols — if you meant one of "
            "the types, use its exact symbol"
        )
    if has_operator:
        parts.append(
            "some candidates are operator symbols (e.g. a conversion operator sharing "
            "the name), not the named type/function itself"
        )
    return "; ".join(parts)
