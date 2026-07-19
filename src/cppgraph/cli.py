"""cppgraph command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cppgraph.builder import build_graph
from cppgraph.export import (
    is_test_file,
    to_file_usage_graph,
    to_graphify_graph,
    to_symbol_usage_graph,
)
from cppgraph.filters import drop_test_edges, is_trivial_callee, short_label
from cppgraph.model import Edge, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import (
    GraphStore,
    build_provenance,
    changed_files_since,
    commits_behind,
    discover_graph,
    enrich_references,
    staleness_verdict,
    update_store,
    write_sqlite,
)
from cppgraph.updates import scip_update_advice, update_advice

# Extensions the graph is built from — drift in a non-C++ file (docs, build
# config, settings) never changes the code graph, so `status` ignores it to keep
# the staleness signal meaningful. (A build-flag change that alters an existing
# TU is a structural case handled by a full rebuild, not this heuristic.)
SOURCE_EXTS = (
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".cu",
    ".h",
    ".hpp",
    ".hh",
    ".hxx",
    ".ipp",
    ".inl",
    ".cuh",
)


def _print_node(node: Node, *, full_symbols: bool = True) -> None:
    loc = f"{node.file}:{node.line + 1}" if node.file is not None and node.line is not None else "?"
    if full_symbols:
        print(f"  {node.symbol}  ({node.display_name or '?'} @ {loc})")
    else:
        print(f"  {node.display_name or short_label(node.symbol)}  ({loc})")


def read_source_snippet(
    root: str | Path, rel_path: str, line0: int, *, context: int = 3
) -> list[tuple[int, str]] | None:
    """Read `line0` (0-indexed) ± `context` lines from `root/rel_path`.

    Returns a list of `(0-indexed line number, text)`, or `None` if the file
    can't be read — the checkout root is a runtime argument, so a missing file
    is an expected, recoverable condition, not an error.
    """
    try:
        text = (Path(root) / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(0, line0 - context)
    end = min(len(lines), line0 + context + 1)
    return [(i, lines[i]) for i in range(start, end)]


def _print_edge(edge: Edge, *, other: str, full_symbols: bool = True) -> None:
    line = edge.line + 1 if edge.line is not None else "?"
    label = other if full_symbols else short_label(other)
    print(f"  {label}  ({edge.file}:{line})")


def _add_query_filters(parser: argparse.ArgumentParser, *, hide_trivial: bool = False) -> None:
    """Attach the shared filter/budget flags so the CLI query commands match the
    MCP tools (`who_calls`/`what_it_calls`/`impact_of`): a result cap, test-edge
    exclusion (on by default), full-SCIP rendering, and — for `callees` — trivial
    callee hiding. Same primitives (`cppgraph.filters`) drive both surfaces, so a
    given flag combination gives the same answer either way."""
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of rows shown (default: all); the true total is always printed",
    )
    parser.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop edges/symbols defined in test files "
        "(default: on; --no-exclude-tests keeps them)",
    )
    parser.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    if hide_trivial:
        parser.add_argument(
            "--hide-trivial",
            action="store_true",
            help="hide ubiquitous helpers (operators, *assert, makeStatus, …)",
        )


def _resolve_graph(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """The graph store path: explicit `--graph`, else auto-discovered from the
    cwd (or `--root`) — the same `.cppgraph/*.graph.db` walk the MCP server does,
    so running from inside an indexed project needs no `--graph`."""
    if getattr(args, "graph", None):
        return args.graph
    found = discover_graph(getattr(args, "root", None) or None)
    if found is None:
        parser.error(
            "no --graph given and no .cppgraph/*.graph.db found from here. "
            "Pass --graph <store.db>, or run from inside an indexed project "
            "(build one with scripts/reindex.sh)."
        )
    graph, _root = found
    return str(graph)


def _resolve_symbol(
    store: GraphStore,
    query: str,
    parser: argparse.ArgumentParser,
    *,
    what: str = "symbol",
) -> str:
    """Accept a plain name, not just the exact SCIP symbol string. If `query` is
    already an exact symbol, use it; otherwise resolve via `find` — one match is
    used directly (noted on a TTY), several are listed so the caller can pick the
    exact one, none is an error. Lets `callers foo` work like `find` + `callers`."""
    if store.has_symbol(query):
        return query
    matches = store.find(query)
    if not matches:
        parser.error(f"unknown {what}: {query} (use `cppgraph find` to look it up)")
    if len(matches) == 1:
        sym = matches[0].symbol
        if sys.stderr.isatty():
            print(f"[cppgraph] resolved {query!r} -> {sym}", file=sys.stderr)
        return sym
    print(
        f"[cppgraph] {query!r} is ambiguous ({len(matches)} matches) — pass the exact SCIP symbol:",
        file=sys.stderr,
    )
    for node in matches[:10]:
        loc = (
            f"{node.file}:{node.line + 1}"
            if node.file is not None and node.line is not None
            else "?"
        )
        print(f"    {node.symbol}  ({node.display_name or '?'} @ {loc})", file=sys.stderr)
    if len(matches) > 10:
        print(f"    ... and {len(matches) - 10} more", file=sys.stderr)
    parser.error(f"ambiguous {what}: {query}")


def build_export_json(
    store: GraphStore,
    symbol: str,
    *,
    mode: str = "deps",
    depth: int = 2,
    direction: str = "both",
    exclude_tests: bool = False,
) -> dict | None:
    """The graph.json for a symbol, or None if the symbol is unknown.

    `mode="deps"` = the bounded call/inherit dependency subgraph (uses
    `depth`/`direction`); `mode="usage"` = a graph of where the symbol is used,
    from its exact references (the right view for a type, which has no call
    edges). Usage is drawn at *symbol* granularity (``symbol -> enclosing
    definition``) when the graph carries attributed references (built with
    `--attributed-refs`), else at *file* granularity — both exact. `exclude_tests`
    drops test / test-support files (usage) or symbols defined in them (deps) —
    production view only. Shared by the `export`/`view` CLI commands and the MCP.
    """
    if not store.has_symbol(symbol):
        return None
    if mode == "usage":
        node = store.get_node(symbol)
        refs = store.references_of(symbol)
        if exclude_tests:
            refs = [r for r in refs if not is_test_file(r.file)]
        label = node.display_name if node else ""
        if any(r.enclosing_symbol for r in refs):
            return to_symbol_usage_graph(symbol, label, refs)
        return to_file_usage_graph(symbol, label, refs)
    nodes, edges = store.subgraph(symbol, depth=depth, direction=direction)
    if exclude_tests:
        kept = {n.symbol for n in nodes if not is_test_file(n.file)}
        nodes = [n for n in nodes if n.symbol in kept]
        edges = [e for e in edges if e.src in kept and e.dst in kept]
    return to_graphify_graph(nodes, edges)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cppgraph",
        description="Semantically accurate code-graph for C++ (SCIP-backed).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build the graph from a SCIP index")
    p_build.add_argument("--scip", required=True, help="path to index.scip")
    p_build.add_argument("--out", required=True, help="output graph store path (SQLite .db)")
    p_build.add_argument(
        "--source-commit",
        default=None,
        help="commit hash of the indexed sources (captured at index time; "
        "recorded as provenance and used as the anchor for incremental updates). "
        "If omitted, best-effort auto-detected via git on the SCIP project_root.",
    )
    p_build.add_argument(
        "--source-dirty",
        action="store_true",
        help="mark the indexed sources as having uncommitted changes "
        "(pair with --source-commit; auto-detected otherwise)",
    )
    p_build.add_argument(
        "--scip-variant",
        default=None,
        help="the scip-clang variant that produced this index (e.g. 'stock' or "
        "'enclosing_range-504'); recorded as provenance so `cppgraph status` can "
        "flag the graph as stale when the pinned indexer changes. reindex.sh "
        "passes it from the binary's provenance sidecar.",
    )
    p_build.add_argument(
        "--references",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="build the exact reference-location index (every non-local use of "
        "a symbol as file:line) — answers 'where is this type/symbol used?', the "
        "dependency the call graph is blind to. On by default; pass "
        "--no-references for a leaner store (measured ~+45% size on a large index).",
    )
    p_build.add_argument(
        "--attributed-refs",
        action="store_true",
        help="UPGRADE the usage view from file to SYMBOL granularity: record, for "
        "each reference, the exact definition that uses it, so 'where is this type "
        "used?' answers with the *functions* that use it, not just the files. "
        "Needs a scip-clang that emits enclosing_range (a #504 build); a stock "
        "binary produces no attribution and this is a no-op. Costs extra store "
        "space (one symbol id per reference — measured ~+23%%, +146 MB on the "
        "626 MB mongo graph) — enable it when you want symbol-level usage and can "
        "pay the space; otherwise the default file granularity is already exact. "
        "Enrich an existing store later with `cppgraph enrich-refs`.",
    )

    p_enrich = sub.add_parser(
        "enrich-refs",
        help="add symbol-granularity reference attribution to an existing store "
        "from a #504 .scip, without a full rebuild",
    )
    p_enrich.add_argument(
        "--graph", required=True, help="path to the graph store to enrich in place"
    )
    p_enrich.add_argument(
        "--scip",
        required=True,
        help="a .scip for the SAME sources, produced by an enclosing_range-emitting "
        "(#504) scip-clang — its enclosing ranges supply the attribution",
    )

    p_compdb = sub.add_parser(
        "compdb-summary",
        help="summarize a compile_commands.json before indexing: how many TUs, "
        "where they live, how many are tests — so the index scope is an informed choice",
    )
    p_compdb.add_argument("compdb", help="path to a compile_commands.json")
    p_compdb.add_argument(
        "--filter",
        default=None,
        help="preview how many TUs a path-substring filter (reindex.sh's 2nd arg) would keep",
    )

    p_update = sub.add_parser(
        "update",
        help="incrementally apply a partial re-index (only changed TUs) to an existing store",
    )
    p_update.add_argument(
        "--graph", required=True, help="path to the graph store to update in place"
    )
    p_update.add_argument(
        "--scip",
        required=True,
        help="SCIP index of only the re-indexed (changed) translation units",
    )
    p_update.add_argument(
        "--deleted",
        action="append",
        default=[],
        metavar="PATH",
        help="a source file removed from the tree (no Document in --scip); repeatable",
    )
    p_update.add_argument(
        "--source-commit",
        default=None,
        help="commit hash of the sources after the change (the new provenance anchor); "
        "auto-detected via git on the SCIP project_root if omitted",
    )
    p_update.add_argument(
        "--source-dirty",
        action="store_true",
        help="mark the updated sources as having uncommitted changes",
    )
    p_update.add_argument(
        "--scip-variant",
        default=None,
        help="the scip-clang variant that produced the partial index (see "
        "`build --scip-variant`); refreshes the graph's recorded indexer identity",
    )

    p_find = sub.add_parser(
        "find", help="find symbols by name (SCIP symbol strings aren't memorable)"
    )
    p_find.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_find.add_argument(
        "query",
        help="name to match against the symbol or display name; a substring, or "
        "several space-separated words that must all appear (order-free AND)",
    )

    p_callers = sub.add_parser("callers", help="list callers of a symbol")
    p_callers.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_callers.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    _add_query_filters(p_callers)

    p_callees = sub.add_parser("callees", help="list callees of a symbol")
    p_callees.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_callees.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    _add_query_filters(p_callees, hide_trivial=True)

    p_bases = sub.add_parser("bases", help="direct base classes a type inherits from")
    p_bases.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_bases.add_argument(
        "symbol",
        help="a type name (resolved via `find`) or an exact SCIP type string ending in `#`",
    )

    p_subtypes = sub.add_parser("subtypes", help="direct subclasses of a type")
    p_subtypes.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_subtypes.add_argument(
        "symbol",
        help="a type name (resolved via `find`) or an exact SCIP type string ending in `#`",
    )

    p_refs = sub.add_parser(
        "references",
        help="exact use sites of a symbol (unless the graph was built --no-references)",
    )
    p_refs.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_refs.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    p_refs.add_argument(
        "--root",
        default=None,
        help="checkout root to read source snippets from (a runtime argument, "
        "never stored). Omit for coordinates (file:line) only.",
    )
    p_refs.add_argument(
        "--context",
        type=int,
        default=0,
        metavar="N",
        help="lines of source context around each use site, with --root (default: 0)",
    )
    p_refs.add_argument(
        "--limit", type=int, default=50, help="max use sites to print (default: 50)"
    )

    p_path = sub.add_parser("path", help="shortest call chain from one symbol to another")
    p_path.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_path.add_argument(
        "src",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    p_path.add_argument(
        "dst",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )

    p_impact = sub.add_parser(
        "impact", help="reverse blast-radius: everything that transitively calls a symbol"
    )
    p_impact.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_impact.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    p_impact.add_argument(
        "--depth", type=int, default=None, help="max hops to walk backwards (default: unbounded)"
    )
    p_impact.add_argument(
        "--kind",
        choices=("calls", "inherits"),
        default="calls",
        help="edge kind to walk: 'calls' = call blast-radius (default); "
        "'inherits' = all transitive subclasses of a base type",
    )
    _add_query_filters(p_impact)

    p_status = sub.add_parser(
        "status",
        help="show the graph's source commit and, with --root, whether the checkout has drifted",
    )
    p_status.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_status.add_argument(
        "--root",
        default=None,
        help="checkout root to compare against (runtime argument). With it, "
        "reports whether the working tree has drifted from the graph's source "
        "commit (exit 1 if stale) and lists the changed files.",
    )

    p_explain = sub.add_parser(
        "explain",
        help="summarize a symbol: definition site, source snippet, callers/callees",
    )
    p_explain.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_explain.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    p_explain.add_argument(
        "--root",
        default=None,
        help="checkout root to read a source snippet from (a runtime argument, "
        "never stored in the graph — lets the same graph serve any local clone). "
        "Omit it to get coordinates (file:line) only, e.g. when the caller already "
        "has file access and will read the source itself.",
    )
    p_explain.add_argument(
        "--context",
        type=int,
        default=3,
        metavar="N",
        help="lines of source context to show around the definition (default: 3)",
    )

    p_export = sub.add_parser(
        "export",
        help="export a viewable subgraph around a symbol as graphify-compatible "
        "graph.json (open it in viz/ or in graphify)",
    )
    p_export.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_export.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string to center the view on",
    )
    p_export.add_argument(
        "--depth",
        type=int,
        default=2,
        metavar="N",
        help="neighbourhood radius in hops around the symbol (default: 2). The "
        "full graph is too large to render; a bounded neighbourhood is the unit "
        "you actually view.",
    )
    p_export.add_argument(
        "--direction",
        choices=("in", "out", "both"),
        default="both",
        help="which way to walk edges: 'out' (what it reaches), 'in' (what "
        "reaches it), or 'both' (default)",
    )
    p_export.add_argument(
        "--mode",
        choices=("deps", "usage"),
        default="deps",
        help="'deps' (default): the call/inherit dependency subgraph around the "
        "symbol (uses --depth/--direction). 'usage': a symbol->file graph of "
        "where the symbol is used, from its exact reference locations — the right "
        "view for a type ('used in these N files'). 'usage' needs a graph built "
        "with references.",
    )
    p_export.add_argument(
        "--no-tests",
        action="store_true",
        help="drop test / test-support files (usage) or symbols defined in them "
        "(deps) — show production usage only",
    )
    p_export.add_argument(
        "--out",
        default="graph.json",
        metavar="PATH",
        help="output path for the graph.json (default: ./graph.json)",
    )

    p_view = sub.add_parser(
        "view",
        help="one-shot visualize: build the subgraph, write a self-contained "
        "HTML to a temp dir, and open it in your browser",
    )
    p_view.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_view.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string to center on",
    )
    p_view.add_argument(
        "--mode",
        choices=("deps", "usage"),
        default="deps",
        help="'deps' (call/inherit subgraph) or 'usage' (symbol->file usage graph)",
    )
    p_view.add_argument(
        "--depth",
        type=int,
        default=2,
        metavar="N",
        help="neighbourhood radius for --mode deps (default: 2)",
    )
    p_view.add_argument(
        "--direction",
        choices=("in", "out", "both"),
        default="both",
        help="edge direction for --mode deps (default: both)",
    )
    p_view.add_argument(
        "--no-tests",
        action="store_true",
        help="drop test / test-support files (production view only)",
    )
    p_view.add_argument(
        "--no-open",
        action="store_true",
        help="write the HTML but don't launch the browser (just print the path)",
    )

    args = parser.parse_args(argv)

    if args.command == "build":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        graph = build_graph(
            index,
            include_references=args.references,
            attribute_references=args.attributed_refs,
        )
        meta = build_provenance(
            index,
            source_commit=args.source_commit,
            source_dirty=True if args.source_dirty else None,
            scip_variant=args.scip_variant,
        )
        write_sqlite(graph, args.out, meta=meta)
        attributed = sum(1 for r in graph.references if r.enclosing_symbol)
        refs_note = f", {len(graph.references)} refs" if graph.references else ""
        if attributed:
            refs_note += f" ({attributed} attributed to enclosing symbols)"
        print(
            f"[cppgraph] built graph: {len(graph.nodes)} nodes, "
            f"{len(graph.edges)} edges{refs_note} -> {args.out}"
        )
        if args.attributed_refs and not attributed and graph.references:
            print(
                "[cppgraph] note: --attributed-refs was set but the .scip carries no "
                "enclosing_range — usage stays at file granularity. Re-index with a "
                "#504-built scip-clang to get symbol-granularity attribution."
            )
        commit = meta.get("source_commit")
        if commit:
            dirty = " (dirty)" if meta.get("source_dirty") == "true" else ""
            print(f"[cppgraph] source commit: {commit}{dirty}")
        return 0

    if args.command == "compdb-summary":
        from cppgraph.compdb import format_summary, load_compdb, summarize_compdb

        try:
            entries = load_compdb(args.compdb)
        except (OSError, ValueError) as e:
            parser.error(str(e))
        print(format_summary(summarize_compdb(entries, filter=args.filter)))
        return 0

    if args.command == "enrich-refs":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        try:
            attributed, total = enrich_references(args.graph, index)
        except ValueError as e:
            parser.error(str(e))
        if attributed == 0:
            print(
                "[cppgraph] no references attributed — no definition in the .scip "
                "carries an enclosing_range whose body contains a use site. Ensure "
                "the .scip was produced by a #504-built scip-clang (a stock binary "
                "emits none)."
            )
            return 1
        print(
            f"[cppgraph] enriched {attributed}/{total} reference(s) with enclosing "
            f"symbols -> {args.graph}. `usage` view is now symbol-granularity."
        )
        return 0

    if args.command == "update":
        index = scip_pb2.Index()
        with open(args.scip, "rb") as f:
            index.ParseFromString(f.read())
        meta = build_provenance(
            index,
            source_commit=args.source_commit,
            source_dirty=True if args.source_dirty else None,
            scip_variant=args.scip_variant,
        )
        stats = update_store(args.graph, index, deleted_files=args.deleted, meta=meta)
        print(
            f"[cppgraph] updated {stats.files_changed} file(s): "
            f"-{stats.edges_removed}/+{stats.edges_added} edges, "
            f"-{stats.symbols_removed} orphaned symbol(s) -> "
            f"{stats.node_count} nodes, {stats.edge_count} edges"
        )
        commit = meta.get("source_commit")
        if commit:
            dirty = " (dirty)" if meta.get("source_dirty") == "true" else ""
            print(f"[cppgraph] source commit: {commit}{dirty}")
        return 0

    if args.command == "find":
        store = GraphStore(_resolve_graph(args, parser))
        matches = store.find(args.query)
        if not matches:
            print(f"[cppgraph] no symbol matching {args.query!r}")
            return 1
        for node in matches:
            _print_node(node)
        return 0

    if args.command == "callers":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        edges = store.callers_of(args.symbol)
        if args.exclude_tests:
            edges = drop_test_edges(store, edges, on="src")
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(edges)} caller(s) of {args.symbol}{tests_note}")
        shown = edges[: args.limit] if args.limit is not None else edges
        for edge in shown:
            _print_edge(edge, other=edge.src, full_symbols=args.full_symbols)
        if len(shown) < len(edges):
            print(f"  ... and {len(edges) - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "callees":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        edges = store.callees_of(args.symbol)
        if args.exclude_tests:
            edges = drop_test_edges(store, edges, on="dst")
        trivial_hidden = 0
        if args.hide_trivial:
            kept = [e for e in edges if not is_trivial_callee(e.dst)]
            trivial_hidden = len(edges) - len(kept)
            edges = kept
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(edges)} callee(s) of {args.symbol}{tests_note}")
        shown = edges[: args.limit] if args.limit is not None else edges
        for edge in shown:
            _print_edge(edge, other=edge.dst, full_symbols=args.full_symbols)
        if len(shown) < len(edges):
            print(f"  ... and {len(edges) - len(shown)} more (raise --limit to see them)")
        if trivial_hidden:
            print(
                f"  ({trivial_hidden} trivial callee(s) hidden — drop --hide-trivial to see them)"
            )
        return 0

    if args.command == "bases":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        bases = store.bases_of(args.symbol)
        print(f"[cppgraph] {len(bases)} base class(es) of {args.symbol}")
        for node in bases:
            _print_node(node)
        return 0

    if args.command == "subtypes":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        subs = store.subtypes_of(args.symbol)
        print(f"[cppgraph] {len(subs)} subclass(es) of {args.symbol}")
        for node in subs:
            _print_node(node)
        return 0

    if args.command == "references":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        refs = store.references_of(args.symbol)
        if not refs and store.meta().get("has_references") != "true":
            print("[cppgraph] this graph was built with --no-references (no location index)")
            return 1
        print(f"[cppgraph] {len(refs)} use site(s) of {args.symbol}")
        for ref in refs[: args.limit]:
            line = ref.line + 1 if ref.line is not None else "?"
            # With attributed references, name the definition that uses it.
            used_by = (
                f"  (used by {short_label(ref.enclosing_symbol)})" if ref.enclosing_symbol else ""
            )
            print(f"  {ref.file}:{line}{used_by}")
            if args.root is not None and ref.file is not None and ref.line is not None:
                snippet = read_source_snippet(args.root, ref.file, ref.line, context=args.context)
                if snippet is None:
                    print(f"    (source not found at {args.root}/{ref.file})")
                else:
                    for lineno, text in snippet:
                        marker = ">" if lineno == ref.line else " "
                        print(f"    {marker} {lineno + 1:>6} | {text}")
        if len(refs) > args.limit:
            print(f"  ... and {len(refs) - args.limit} more")
        return 0

    if args.command == "path":
        store = GraphStore(_resolve_graph(args, parser))
        args.src = _resolve_symbol(store, args.src, parser, what="src symbol")
        args.dst = _resolve_symbol(store, args.dst, parser, what="dst symbol")
        chain = store.shortest_call_path(args.src, args.dst)
        if chain is None:
            print(f"[cppgraph] no static call path from {args.src} to {args.dst}")
            print(
                "  note: this does not prove they're unrelated — the flow may cross a "
                "runtime-dispatch boundary (a virtual call, or a registered-factory hop) "
                "that has no static edge. Try the concrete override, or bridge with "
                "references/subtypes."
            )
            return 1
        print(f"[cppgraph] {len(chain)} hop(s) from {args.src} to {args.dst}")
        print(f"  {args.src}")
        for edge in chain:
            line = edge.line + 1 if edge.line is not None else "?"
            print(f"  -> {edge.dst}  ({edge.file}:{line})")
        return 0

    if args.command == "impact":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        if args.kind == "calls" and args.symbol.rstrip().endswith("#"):
            n = len(store.references_of(args.symbol))
            print(
                f"[cppgraph] {args.symbol} is a type — it has no call-graph callers. "
                f"Its blast radius is its {n} reference site(s): use `cppgraph references`, "
                "or `impact --kind inherits` for the subclass tree."
            )
            return 0
        affected = sorted(store.impact(args.symbol, max_depth=args.depth, kind=args.kind))
        nodes = [(sym, store.get_node(sym)) for sym in affected]
        if args.exclude_tests:
            nodes = [(sym, n) for sym, n in nodes if n is None or not is_test_file(n.file)]
        verb = "transitively call" if args.kind == "calls" else "transitively inherit from"
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(nodes)} symbol(s) {verb} {args.symbol}{tests_note}")
        shown = nodes[: args.limit] if args.limit is not None else nodes
        for _sym, node in shown:
            if node is not None:
                _print_node(node, full_symbols=args.full_symbols)
        if len(shown) < len(nodes):
            print(f"  ... and {len(nodes) - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "status":
        graph_path = _resolve_graph(args, parser)
        store = GraphStore(graph_path)
        m = store.meta()
        commit = m.get("source_commit")
        dirty = m.get("source_dirty") == "true"
        print(f"[cppgraph] graph store: {graph_path}")
        if commit:
            print(f"  source commit: {commit}{' (dirty)' if dirty else ''}")
        else:
            print("  source commit: unknown (not recorded at build time)")
        if m.get("project_root"):
            print(f"  project_root:  {m['project_root']}")
        if m.get("built_at"):
            print(f"  built at:      {m['built_at']}")
        tool = m.get("index_tool")
        if tool:
            ver = m.get("index_tool_version")
            print(f"  indexed with:  {tool}{' ' + ver if ver else ''}")
        print(
            f"  nodes/edges:   {m.get('node_count', '?')} / {m.get('edge_count', '?')}"
            + (f" (+{m['ref_count']} refs)" if m.get("ref_count") else "")
        )
        if m.get("has_references") == "true":
            if m.get("has_attributed_refs") == "true":
                n_attr = m.get("attributed_ref_count", "?")
                print(
                    f"  usage view:    SYMBOL granularity "
                    f"({n_attr} refs attributed to enclosing symbols)"
                )
            else:
                print("  usage view:    file granularity (references not attributed)")
                print(
                    "                 -> upgrade to SYMBOL granularity ('where is this "
                    "type used?' answers with the functions, not just the files):"
                )
                print(
                    "                    index with a #504-built scip-clang, then either "
                    "rebuild with --attributed-refs"
                )
                print(
                    f"                    or enrich in place: cppgraph enrich-refs "
                    f"--graph {graph_path} --scip <index.scip>"
                )
                print(
                    "                 (costs extra store space — worth it for symbol-level usage)"
                )
        print(
            f"  format:        schema v{m.get('schema_version', '0 (legacy)')}"
            f", cppgraph {m.get('cppgraph_version', '?')}"
        )
        scip = scip_update_advice(
            {"version": m.get("index_tool_version"), "variant": m.get("index_tool_variant")}
        )
        if scip.get("checked"):
            line = f"  scip-clang:    pinned version {scip['pinned_version']}"
            if scip.get("installed_variant"):
                line += f", installed binary {scip['installed_variant']}"
            if scip.get("graph_variant"):
                line += f", this graph indexed with {scip['graph_variant']}"
            print(line)
            if scip.get("binary_status") in ("stale", "unknown"):
                print(f"    ! {scip['binary_message']}")
            if scip.get("reindex_recommended"):
                print(f"    ! {scip['reindex_message']}")
        tool = update_advice(m.get("cppgraph_version"))
        if tool.get("update_available"):
            print(f"    ! {tool['update_message']}")
        if tool.get("rebuild_recommended"):
            print(f"    ! {tool['rebuild_message']}")

        if args.root is None:
            if commit:
                print("  (pass --root <checkout> to check drift against the working tree)")
            return 0
        if not commit:
            print("  cannot check drift: no source commit recorded in the graph")
            return 0

        result = changed_files_since(args.root, commit)
        if result is None:
            print(f"  cannot check drift: {args.root} is not a git checkout (or git unavailable)")
            return 0
        changed = [f for f in result[0] if f.endswith(SOURCE_EXTS)]
        deleted = [f for f in result[1] if f.endswith(SOURCE_EXTS)]
        if not changed and not deleted:
            print("  status: up to date")
            return 0
        behind = commits_behind(args.root, commit)
        verdict = staleness_verdict(
            len(changed), len(deleted), store.indexed_file_count(), commits_behind=behind
        )
        behind_str = f", {behind} commit(s) behind" if behind is not None else ""
        print(
            f"  status: STALE - {len(changed)} changed, {len(deleted)} deleted "
            f"since {commit[:12]}{behind_str}"
        )
        for f in changed[:20]:
            print(f"    ~ {f}")
        for f in deleted[:20]:
            print(f"    - {f}")
        if len(changed) + len(deleted) > 40:
            print(f"    ... and {len(changed) + len(deleted) - 40} more")
        frac = verdict.get("changed_fraction")
        if frac is not None:
            print(f"  drift: {frac * 100:.0f}% of {verdict['indexed_files']} indexed files changed")
        if verdict["recommend"] == "rebuild":
            print("  recommendation: FULL REBUILD (drift too large for an incremental update)")
            print(
                "    re-index the whole target, then `cppgraph build --scip <index.scip> "
                f"--out {graph_path}`"
            )
        else:
            print("  recommendation: incremental update")
            print(f"    next: scripts/reindex.sh --update {graph_path} <compile_commands.json>")
        return 1

    if args.command == "explain":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        node = store.get_node(args.symbol)
        if node is None:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        callers = store.callers_of(args.symbol)
        callees = store.callees_of(args.symbol)

        loc = (
            f"{node.file}:{node.line + 1}"
            if node.file is not None and node.line is not None
            else "?"
        )
        print(f"[cppgraph] {node.symbol}")
        print(f"  name:       {node.display_name or '?'}")
        print(f"  defined at: {loc}")

        # --root is the sole snippet switch: given => read source, omitted =>
        # coordinates only. We never fall back to the stored project_root, which
        # is only a suggestion and may not exist on this machine (DESIGN.md:
        # "project root is a query-time parameter, never stored").
        if args.root is not None and node.file is not None and node.line is not None:
            snippet = read_source_snippet(args.root, node.file, node.line, context=args.context)
            if snippet is None:
                print(f"  (source not found at {args.root}/{node.file})")
            else:
                print("  source:")
                for lineno, text in snippet:
                    marker = ">" if lineno == node.line else " "
                    print(f"  {marker} {lineno + 1:>6} | {text}")
        elif args.root is None and node.file is not None and sys.stdout.isatty():
            # Interactive human only: teach the affordance without spending tokens
            # on every machine/LLM/MCP call (those learn --root from the schema).
            print("  (tip: pass --root <checkout> to include a source snippet)")

        print(f"  {len(callers)} caller(s):")
        for edge in callers[:10]:
            line = edge.line + 1 if edge.line is not None else "?"
            print(f"    {edge.src}  ({edge.file}:{line})")
        if len(callers) > 10:
            print(f"    ... and {len(callers) - 10} more")
        print(f"  {len(callees)} callee(s):")
        for edge in callees[:10]:
            line = edge.line + 1 if edge.line is not None else "?"
            print(f"    {edge.dst}  ({edge.file}:{line})")
        if len(callees) > 10:
            print(f"    ... and {len(callees) - 10} more")
        return 0

    if args.command == "export":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        graph_json = build_export_json(
            store,
            args.symbol,
            mode=args.mode,
            depth=args.depth,
            direction=args.direction,
            exclude_tests=args.no_tests,
        )
        if graph_json is None:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(graph_json, f, indent=1)
        n_nodes, n_links = len(graph_json["nodes"]), len(graph_json["links"])
        if args.mode == "usage":
            print(f"[cppgraph] exported usage graph: {n_links} file(s) used -> {args.out}")
            if n_links == 0:
                print("  (0 references — was the graph built with references? see `status`)")
        else:
            print(
                f"[cppgraph] exported {n_nodes} nodes, {n_links} edges "
                f"(depth {args.depth}, {args.direction}) -> {args.out}"
            )
        print(
            f"  open viz/cppgraph-viz.html and load {args.out} "
            f"(or use `cppgraph view` for a one-shot open)"
        )
        return 0

    if args.command == "view":
        store = GraphStore(_resolve_graph(args, parser))
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        graph_json = build_export_json(
            store,
            args.symbol,
            mode=args.mode,
            depth=args.depth,
            direction=args.direction,
            exclude_tests=args.no_tests,
        )
        if graph_json is None:
            parser.error(f"unknown symbol: {args.symbol} (use `cppgraph find` to look it up)")
        from cppgraph.viz_html import open_in_browser, write_temp_html

        html_path = write_temp_html(graph_json)
        n_nodes, n_links = len(graph_json["nodes"]), len(graph_json["links"])
        print(f"[cppgraph] {n_nodes} nodes, {n_links} edges -> {html_path}")
        if args.no_open:
            print(f"  open it with: open {html_path}")
        else:
            ok, cmd = open_in_browser(html_path)
            print(f"  {'opened in your browser' if ok else 'open it with'}: {cmd} {html_path}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
