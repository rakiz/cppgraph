"""Schema-versioned JSON documents for the query commands' `--json` flag.

Workspace convention (NEXT-SESSION.md): every command that ANSWERS A QUESTION
gets `--json`; commands that DO something or are interactive get none. The JSON
document is a de facto contract the moment an agent consumes it, so each
document carries `"schema": 1` (strict integer) at the top level — one schema
per command, versioned independently.

The documents are NOT rendered from forked logic: every command's payload is
produced by the SAME pure function the MCP tool uses (`cppgraph.mcp_server`'s
`callers`/`find_symbols`/`impact`/… — the CLI/MCP parity rule in AGENTS.md).
This module adds only the envelope (`schema`, `command`) and the few
contract fields the MCP payload doesn't already carry (e.g. `find`'s
`match_state`), so the two surfaces cannot drift.

Bounded by construction: every list honours the command's `--limit` (defaulting
to the shared `DEFAULT_LIMIT` where the text output is uncapped), and
truncation is VISIBLE (`truncated: true` + a `total` field), never silent.

Round-trip guarantee: every result row carries the exact SCIP `symbol` string
(`full_symbols=True` into the shared builders) plus `name`/`file`/`line`, so a
resolved name, count, and file:line value survive into the document untouched.

The import of `cppgraph.mcp_server` is lazy (inside the builders) by
necessity, not style: `mcp_server` imports `cli` at module load, and `cli`
imports this module — a top-level import here would be circular.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cppgraph.store import GraphStore

SCHEMA = 1


def envelope(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The contract envelope: `schema` first, then the command name, then the
    payload's own fields."""
    return {"schema": SCHEMA, "command": command, **payload}


def _mcp() -> Any:
    from cppgraph import mcp_server

    return mcp_server


def _exit_code(payload: dict[str, Any]) -> int:
    """1 when the document answers no question (unknown symbol / ambiguous
    name — the MCP error/candidates conventions — or the capability-gated
    `available: false` refusal), else 0. The document is still printed either
    way: a machine consumer reads the answer from the JSON, not the exit code
    alone."""
    if "error" in payload or "ambiguous" in payload or payload.get("available") is False:
        return 1
    return 0


def _find_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    """`find --json`: masora's anchor-resolution contract. On top of the shared
    `find_symbols` payload it adds the unambiguous signal —
    `match_state: "none" | "unique" | "multiple"` — the exact `unique_symbol`
    when unique, and a flat `candidates` list of exact SCIP identities when
    multiple (overload arms included), so "0 / 1 / N matches" is never guessed
    from list lengths."""
    m = _mcp()
    payload = m.find_symbols(
        store,
        args.query,
        # Bounded by default (the text output is uncapped without --limit; a
        # machine contract is not): the shared DEFAULT_LIMIT applies unless
        # --limit says otherwise, with `truncated`/`total` reporting the cut.
        limit=args.limit if args.limit is not None else m.DEFAULT_LIMIT,
        hide_trivial=args.hide_trivial,
        root=args.root,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    results = payload["results"]
    if not results:
        state = "none"
    elif len(results) == 1 and "signatures" not in results[0]:
        state = "unique"
        payload["unique_symbol"] = results[0]["symbol"]
    else:
        state = "multiple"
        payload["candidates"] = m.response_symbols(payload)
    payload["match_state"] = state
    m._masora_field(payload, store, m.response_symbols(payload))
    return envelope("find", payload), (0 if state != "none" else 1)


def _callers_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    """`callers --json`: masora's proof-query replay contract — a per-symbol
    caller identity list (exact SCIP `symbol` + `name` + call site file:line),
    bounded by --limit (the shared DEFAULT_LIMIT when absent), truncation
    visible via `truncated`/`total`."""
    m = _mcp()
    payload = m.callers(
        store,
        args.symbol,
        limit=args.limit if args.limit is not None else m.DEFAULT_LIMIT,
        full_symbols=True,
        exclude_tests=args.exclude_tests,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    m._masora_field(payload, store)
    return envelope("callers", payload), _exit_code(payload)


def _callees_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.callees(
        store,
        args.symbol,
        limit=args.limit if args.limit is not None else m.DEFAULT_LIMIT,
        full_symbols=True,
        exclude_tests=args.exclude_tests,
        hide_trivial=args.hide_trivial,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    m._masora_field(payload, store)
    return envelope("callees", payload), _exit_code(payload)


def _bases_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().bases(store, args.symbol, limit=_mcp().DEFAULT_LIMIT, full_symbols=True)
    return envelope("bases", payload), _exit_code(payload)


def _subtypes_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().subtypes(store, args.symbol, limit=_mcp().DEFAULT_LIMIT, full_symbols=True)
    return envelope("subtypes", payload), _exit_code(payload)


def _references_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    # exclude_tests=False: the CLI `references` command has no test-exclusion
    # flag and its text output keeps test-file sites — the JSON document
    # reports the same set (deliberately unlike the MCP tool's default).
    payload = m.references(
        store,
        args.symbol,
        root=args.root,
        include_source=args.root is not None,
        context=args.context,
        limit=args.limit,
        exclude_tests=False,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
        access=args.access,
    )
    return envelope("references", payload), _exit_code(payload)


def _path_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().call_path(store, args.src, args.dst)
    return envelope("path", payload), (0 if payload.get("found") else 1)


def _impact_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.impact(
        store,
        args.symbol,
        depth=args.depth,
        limit=args.limit if args.limit is not None else m.DEFAULT_LIMIT,
        kind=args.kind,
        full_symbols=True,
        exclude_tests=args.exclude_tests,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("impact", payload), _exit_code(payload)


def _reachable_from_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.reachable_from_report(
        store,
        args.symbol,
        depth=args.depth,
        limit=args.limit if args.limit is not None else m.DEFAULT_LIMIT,
        kind=args.kind,
        full_symbols=True,
        exclude_tests=args.exclude_tests,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("reachable-from", payload), _exit_code(payload)


def _hotspots_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().hotspot_ranking(
        store,
        limit=args.limit,
        kind=args.kind,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("hotspots", payload), _exit_code(payload)


def _dependency_cost_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().dependency_cost_report(
        store,
        args.target_paths,
        limit=args.limit,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("dependency-cost", payload), _exit_code(payload)


def _stats_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().stats_summary(
        store,
        group_by=args.group_by,
        limit=args.limit,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("stats", payload), _exit_code(payload)


def _line_span_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().line_span_ranking(
        store,
        limit=args.limit,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("line-span", payload), _exit_code(payload)


def _no_incoming_calls_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().no_incoming_calls_report(
        store,
        limit=args.limit,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("no-incoming-calls", payload), _exit_code(payload)


def _global_init_references_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().global_init_references_report(
        store, args.symbol, limit=args.limit, full_symbols=True
    )
    return envelope("global-init-references", payload), _exit_code(payload)


def _boundary_violations_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.boundary_violation_report(
        store,
        [list(r) for r in (rules or [])],
        edge_kinds=list(args.kind) if args.kind else None,
        limit=args.limit,
        full_symbols=True,
    )
    if args.out and "error" not in payload and payload.get("violations") is not None:
        # --out keeps working in JSON mode: the graph.json side file is an
        # artifact, not stdout, and the same shared renderer builds it.
        from cppgraph.cli import boundary_violations_graph

        graph_json = boundary_violations_graph(store, payload["violations"])
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(graph_json, f, indent=1)
    return envelope("boundary-violations", payload), _exit_code(payload)


def _api_surface_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().api_surface_report(
        store,
        args.module_prefix,
        limit=args.limit,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
    )
    return envelope("api-surface", payload), _exit_code(payload)


def _outline_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.file_outline_report(store, args.file, limit=args.limit, full_symbols=True)
    m._masora_field(
        payload,
        store,
        [d["symbol"] for d in payload["definitions"] if "symbol" in d],
    )
    return envelope("outline", payload), _exit_code(payload)


def _class_members_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().class_members_report(store, args.symbol, limit=args.limit, full_symbols=True)
    return envelope("class-members", payload), _exit_code(payload)


def _scc_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    payload = _mcp().scc_report(
        store,
        limit=args.limit,
        exclude_tests=args.exclude_tests,
        full_symbols=True,
        include_paths=args.include_paths,
        exclude_paths=args.exclude_paths,
    )
    return envelope("strongly-connected-components", payload), _exit_code(payload)


def _status_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.status_report(store, root=args.root)
    # status_report is surface-agnostic except its transport stamp; the CLI
    # document says which surface produced it (AGENTS.md: a copied status block
    # must identify its origin).
    payload["transport"] = "cli"
    payload["graph_path"] = graph_path
    up_to_date = payload.get("drift", {}).get("up_to_date", True)
    return envelope("status", payload), (0 if up_to_date else 1)


def _explain_doc(
    store: GraphStore, args: Any, *, graph_path: str | None = None, rules: Any = None
) -> tuple[dict[str, Any], int]:
    m = _mcp()
    payload = m.explain(
        store,
        args.symbol,
        root=args.root,
        include_source=args.root is not None,
        context=args.context,
        limit=m.EXPLAIN_LIMIT,
        full_symbols=True,
        # exclude_tests=False: the CLI `explain` text keeps test-file
        # callers/callees (no --exclude-tests flag on the command) — the JSON
        # document reports the same set (deliberately unlike the MCP default).
        exclude_tests=False,
    )
    m._masora_field(payload, store)
    return envelope("explain", payload), _exit_code(payload)


_BUILDERS: dict[str, Callable[..., tuple[dict[str, Any], int]]] = {
    "find": _find_doc,
    "callers": _callers_doc,
    "callees": _callees_doc,
    "bases": _bases_doc,
    "subtypes": _subtypes_doc,
    "references": _references_doc,
    "path": _path_doc,
    "impact": _impact_doc,
    "reachable-from": _reachable_from_doc,
    "hotspots": _hotspots_doc,
    "dependency-cost": _dependency_cost_doc,
    "stats": _stats_doc,
    "line-span": _line_span_doc,
    "no-incoming-calls": _no_incoming_calls_doc,
    "global-init-references": _global_init_references_doc,
    "boundary-violations": _boundary_violations_doc,
    "api-surface": _api_surface_doc,
    "outline": _outline_doc,
    "class-members": _class_members_doc,
    "strongly-connected-components": _scc_doc,
    "status": _status_doc,
    "explain": _explain_doc,
}


def run(
    command: str,
    store: GraphStore,
    args: Any,
    *,
    graph_path: str | None = None,
    rules: list[tuple[str, str]] | None = None,
) -> int:
    """The CLI's whole `--json` path for a query command: build the schema:1
    document from the shared pure function, print ONE JSON document on stdout
    (stderr stays free for the usual diagnostics), return the exit code."""
    doc, code = _BUILDERS[command](store, args, graph_path=graph_path, rules=rules)
    print(json.dumps(doc, indent=2))
    return code
