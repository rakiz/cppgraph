"""Shared wording and caps for the CLI and MCP surfaces.

Every capability-gate reason, standing caveat, and budget cap a query surface
can emit lives here ONCE, so the two surfaces (the `cppgraph` CLI and the MCP
server's tools) cannot drift apart in what they tell the caller. The MCP
constant wording is the base — it is the more complete of the two wherever
they used to differ (e.g. the zero-callers caveat names both failure modes and
the rebuild path); a surface adds only its own local render detail (the CLI's
`  note: ` prefix, the `[cppgraph]` line prefix).

No imports, no logic — pure strings and integers, safe to import from
anywhere (including `store`, which gates on these messages).
"""

from __future__ import annotations

# --- shared caps -------------------------------------------------------------
#
# Default cap on any list a query returns. Big enough to be useful for
# reasoning, small enough that a hub symbol's callers don't blow the context
# budget. The caller can tune it per-query either way — lower to spend fewer
# tokens, raise to see more — and always learns the true `total`. Set to 40
# (not 25): a real function with ~30 stage/callee edges had genuine edges
# pushed past a cap of 25, so the default now clears a typical fan-out.
DEFAULT_LIMIT = 40
# `explain` bundles callers + callees in one payload, so it caps each side
# lower than the general DEFAULT_LIMIT.
EXPLAIN_LIMIT = 10
# Ambiguous-name candidate listing: enough alternatives to pick from in one
# reply (a terminal CLI error listing, or an MCP `candidates` array) without
# dumping a hundred substring matches. Both the CLI's `_resolve_symbol` and
# the MCP `_resolve` truncate at this and report the true total — one cap, so
# the same ambiguous name lists the same number of candidates either way.
AMBIGUOUS_CANDIDATE_LIMIT = 10

# --- capability-gate reasons (`available: false` / refusal bodies) -----------

# The graph was built `--no-references`: there is no location index to answer
# "where is this used?" from.
NO_REFERENCES_REASON = "graph built with --no-references (no location index)"
# The graph carries no read/write role data, so an access filter would guess.
# `{access}` is the requested filter value, formatted with `!r` by the caller.
NO_ACCESS_ROLES_REASON = (
    "graph carries no read/write access data — cannot filter by "
    "access={access!r}; rebuild with a scip-clang binary carrying the "
    "ReadAccess/WriteAccess patch"
)

# Degrade-cleanly responses for the enclosing-range-gated (#504) tools, the
# same `available: false` + `reason` convention `references` uses when the
# graph was built `--no-references`: say why, point at the upgrade, never a
# silently empty/wrong list.
NO_ENCLOSING_RANGES = (
    "this graph carries no definition body extents: `enclosing_range` is emitted "
    "only by a #504-built scip-clang, and a stock-binary graph cannot answer "
    "this (an empty list would read as 'none', which is wrong). Index with a "
    "#504-built scip-clang and rebuild the store (`cppgraph build --scip "
    "<index.scip>`) to enable it"
)
STOCK_ATTRIBUTION_UNRELIABLE = (
    "not reliable on this graph: it was indexed with a stock scip-clang, whose "
    "caller attribution (nearest-preceding definition, no enclosing ranges) can "
    "mis-attribute a bodyless member declaration to the preceding definition — "
    "a phantom caller that turns a real 0 into a false 1, exactly the answer "
    "this tool must get right. Index with a #504-built scip-clang and rebuild "
    "the store to enable it"
)
# `explain` reports its caller count (never refuses), but a 0 on a stock-binary
# graph is not guaranteed — the same asymmetry `STOCK_ATTRIBUTION_UNRELIABLE`
# refuses `no_incoming_calls` over, stated as a caveat traveling with the fact.
ZERO_CALLERS_STOCK_NOTE = (
    "0 callers is only trustworthy on a #504-built index: this graph was "
    "indexed with a stock scip-clang, whose caller attribution "
    "(nearest-preceding definition, no enclosing ranges) has a separate false-"
    "positive path that can fabricate a phantom caller from a bodyless "
    "declaration site, but this reported 0 can hide a real caller when a call "
    "site with no preceding callable definition in its document is dropped — "
    "so this 0 is not guaranteed to be a true 0. Index with a "
    "#504-built scip-clang and rebuild the store for an exact 0"
)
# Degrade-cleanly response for the attributed-refs-gated tool: same convention,
# naming the exact rebuild path (a #504 index AND --attributed-refs/enrich-refs).
NO_ATTRIBUTED_REFS = (
    "this graph carries no attributed references: initializer-region attribution "
    "needs a #504-built scip-clang AND a store built with `--attributed-refs` "
    "(or `cppgraph enrich-refs` on an existing one). Without it an initializer's "
    "reads are plain locations, and an empty list here would read as "
    "'references nothing', which is wrong"
)

# --- standing caveats (attached to every response of a tool) -----------------

# On every global_init_references response, both halves of the
# facts-not-judgments rule: the reference is a fact (constant-initialized inits
# are safe), and a read inside a lambda body in the region may run lazily — the
# measured limitation that scip-clang emits no separate lambda interval there.
GLOBAL_INIT_NOTE = (
    "each entry is a graph fact — this global's initializer region references "
    "that symbol — not a verdict: a `constexpr`/`constinit` initializer is "
    "constant-initialized (safe), and a read inside a lambda body in the region "
    "may run lazily or never rather than at initialization (scip-clang emits no "
    "separate interval for such a lambda, so it cannot be split out). The tool "
    "reports the reference; the hazard judgment is yours."
)
# On every boundary_violations response, both directions of the
# facts-not-judgments rule: a listed violation is exact (an edge that exists),
# while an empty list is a lower bound (the static graph under-reports runtime
# reachability), never a conformance verdict.
BOUNDARY_NOTE = (
    "each violation is a real compiler-traced edge — zero false positives. The "
    "converse is a lower bound: 0 violations means no *statically indexed* edge "
    "crosses these rules — runtime dispatch (virtual calls, function pointers, "
    "registered factories) can cross a boundary with no static edge, and only "
    "the given rules and edge kinds were checked. The rules are supplied by "
    "you (the project's declared layering); the graph stores no intended "
    "architecture of its own."
)
# On every strongly_connected_components response: component membership is a
# graph fact (these symbols can all reach each other), never a verdict —
# mutual recursion is often legitimate architecture, and the reader judges
# which cycles, if any, are a problem.
SCC_NOTE = (
    "each component is a graph fact — these symbols can all reach each other "
    "via calls edges — not a verdict. Mutual recursion is often completely "
    "legitimate architecture (a visitor pattern, a recursive-descent parser's "
    "mutually-recursive rules); which cycles, if any, are actually a problem "
    "is your judgment. Filters drop a component only when every member is "
    "filtered out; a reported component always lists all its members (the "
    "cycle is a fact about the compiled binary)."
)
# On every reachable_from response: a static call graph under-reports runtime
# reachability (virtual dispatch, function pointers, registered factories have
# no static edge), so the result is a lower bound — "at least these are
# reachable" — never a set whose absent members are safe to ignore.
REACHABLE_NOTE = (
    "a lower bound on runtime reachability: every symbol listed is reached via "
    "a compiler-traced static edge, but virtual dispatch, function pointers "
    "and runtime registration (e.g. factories) have no static edge, so the "
    "true reachable set may be larger. Read it as 'at least these are "
    "reachable', never as a set whose absent members are safe to ignore."
)

# --- no-answer hints (a real, good outcome stated plainly) -------------------

# Shared by `path` and `visualize(mode="path")` when the static chain doesn't
# exist.
NO_STATIC_CHAIN_HINT = (
    "No *static* call chain — this does not prove the two are unrelated. "
    "The flow may cross a runtime-dispatch boundary the static graph can't "
    "link: a virtual call, or a registered-factory hop (e.g. a "
    "DocumentSource built by a pipeline parser and later run via "
    "doGetNext), where the edge exists only at runtime. Try `path` "
    "against the concrete override/implementation, or bridge the boundary "
    "with `find_references` / `subclasses`."
)
# Shared by `visualize(mode="cycle")` when a valid symbol is in no multi-member
# cycle: the same shape as the no-static-chain case — a real (good) outcome
# stated plainly, no HTML written or opened, never an error.
NO_CYCLE_NOTE = (
    "this symbol is not part of any multi-member call cycle (strongly-connected "
    "component) — a lower bound, as always: runtime dispatch (virtual calls, "
    "registered factories) can complete a cycle with no static edge. "
    "`strongly_connected_components` lists every cycle the graph does contain."
)
