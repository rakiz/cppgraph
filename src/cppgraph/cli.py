"""cppgraph command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from cppgraph.builder import build_graph
from cppgraph.export import (
    is_test_file,
    to_file_usage_graph,
    to_graphify_graph,
    to_symbol_usage_graph,
)
from cppgraph.filters import (
    access_tag,
    ambiguous_candidate_hint,
    drop_test_edges,
    filter_by_access,
    filter_by_path,
    is_trivial_callee,
    matches_path_prefix,
    short_label,
)
from cppgraph.model import Edge, Node
from cppgraph.proto import scip_pb2
from cppgraph.store import (
    GraphStore,
    build_provenance,
    changed_files_since,
    commits_behind,
    discover_graph,
    enrich_references,
    is_stale,
    project_root_path,
    read_dirty_fingerprints,
    staleness_verdict,
    update_store,
    write_sqlite,
)
from cppgraph.updates import attributed_refs_cost_note, scip_update_advice, update_advice

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


def _print_node(node: Node, *, full_symbols: bool = True, external: bool = False) -> None:
    loc = f"{node.file}:{node.line + 1}" if node.file is not None and node.line is not None else "?"
    # Fine-grained SCIP kind (kind-patched binary only); nodes from queries
    # that don't select the column simply carry None and print unchanged.
    kind = f"  [{node.scip_kind}]" if node.scip_kind else ""
    # Out-of-project marker, only for a concrete external symbol, only when
    # the caller gated on the capability (`has_external_symbols`) — native
    # (False) and unknown (None) results stay unmarked.
    marker = "  [out-of-project]" if external else ""
    if full_symbols:
        print(f"  {node.symbol}  ({node.display_name or '?'} @ {loc}){kind}{marker}")
    else:
        print(f"  {node.display_name or short_label(node.symbol)}  ({loc}){kind}{marker}")


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


def extract_signature(root: str | None, file: str | None, line0: int | None) -> str | None:
    """A best-effort readable parameter signature — including any default
    argument values, verbatim as written — read from the source at a
    definition site.

    `scip-clang` disambiguates overloads by an opaque hash, not by argument
    types, so grouped overloads (`find`) are otherwise indistinguishable. A
    patched scip-clang can carry a stored `signature_documentation` on the
    graph itself (`explain`/`explain_symbol`'s "signature (stored):"), but it
    is a pretty-printed declaration, not this verbatim-source extraction.
    Since cppgraph has the checkout (`root`), it reads the def line and captures
    the text from the first `(` to its matching `)` verbatim — so a defaulted
    parameter (`bool useNullIfMissing = false`) is visible without opening the
    header. Display-only, so templates / macros / multi-line params are
    tolerated (whitespace collapsed). `None` if there's no root, the file can't
    be read, no parameter list is found, or the declaration at this line ends
    (`;`/`{`) before any `(` — a field/variable has no parameter list of its
    own, and without this check a bare `int x;` followed a few lines later by
    an unrelated function would misattribute that function's parameter list
    to the field (observed in practice on a field declared just above a
    `friend bool operator==(...)`)."""
    if root is None or file is None or line0 is None:
        return None
    snippet = read_source_snippet(root, file, line0, context=8)
    if not snippet:
        return None
    text = " ".join(t for i, t in snippet if i >= line0)
    start = text.find("(")
    if start < 0:
        return None
    # A `;` or `{` before the first `(` ends the current declaration/statement
    # without ever opening a parameter list — this line/lookahead window
    # belongs to a DIFFERENT, later declaration (e.g. a field with no `(` of
    # its own, followed within the lookahead window by an unrelated function).
    stop = min((i for i in (text.find(";"), text.find("{")) if i >= 0), default=-1)
    if stop >= 0 and stop < start:
        return None
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return " ".join(text[start : j + 1].split())
    return None


def _add_query_filters(parser: argparse.ArgumentParser, *, hide_trivial: bool = False) -> None:
    """Attach the shared filter/budget flags so the CLI query commands match the
    MCP tools (`who_calls`/`what_it_calls`/`impact_of`): a result cap, test-edge
    exclusion (on by default), path-prefix filtering, full-SCIP rendering, and —
    for `callees` — trivial callee hiding. Same primitives (`cppgraph.filters`)
    drive both surfaces, so a given flag combination gives the same answer
    either way."""
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
    _add_path_filters(parser)
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


def _add_path_filters(parser: argparse.ArgumentParser) -> None:
    """Attach `--include-path`/`--exclude-path` (repeatable): filter results by
    `Node.file`-prefix (`cppgraph.filters.matches_path_prefix`) — simple prefix
    match, no glob/regex — so a query can scope to "my code" vs vendored deps."""
    parser.add_argument(
        "--include-path",
        action="append",
        dest="include_paths",
        metavar="PREFIX",
        help="keep only results whose definition file starts with PREFIX "
        "(repeatable; prefix match only, no glob)",
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        dest="exclude_paths",
        metavar="PREFIX",
        help="drop results whose definition file starts with PREFIX "
        "(repeatable; prefix match only, no glob)",
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
            "(build one with scripts/index.sh)."
        )
    graph, _root = found
    return str(graph)


def _open_store_checked(args: argparse.Namespace, parser: argparse.ArgumentParser) -> GraphStore:
    """Resolve the graph, open it, and — best-effort — warn on stderr if it has
    drifted from its source commit (same cheap `git diff` `status`'s drift check
    runs, no rebuild triggered). Mirrors the MCP tools' per-call `stale` flag for
    the CLI's plain-text output."""
    graph_path = _resolve_graph(args, parser)
    store = GraphStore(graph_path)
    root = getattr(args, "root", None)
    if root is None:
        # The store's own recorded `project_root` is authoritative — it's the
        # checkout the graph was actually built from, unlike `discover_graph()`
        # which searches from the CWD and can resolve to a *different* project's
        # root when `--graph` points elsewhere (spurious drift / wrong commit).
        # Only fall back to the CWD search for an older graph built before this
        # field was recorded.
        recorded_root = store.meta().get("project_root")
        root = project_root_path(recorded_root) if recorded_root else None
        if root is None:
            found = discover_graph()
            root = found[1] if found else None
    if root is not None and is_stale(store, root, SOURCE_EXTS):
        print(
            "[cppgraph] warning: graph may be stale (source changed since indexing) "
            "— run `cppgraph status` for details, `cppgraph update` to refresh",
            file=sys.stderr,
        )
    return store


def _resolve_symbol(
    store: GraphStore,
    query: str,
    parser: argparse.ArgumentParser,
    *,
    what: str = "symbol",
) -> str:
    """Accept a plain name, not just the exact SCIP symbol string, via the shared
    `GraphStore.resolve` (same behaviour as the MCP tools). One match is used
    directly (noted on a TTY), several are listed so the caller can pick the exact
    one, none is an error. Lets `callers foo` work like `find` + `callers`."""
    resolved, candidates = store.resolve(query)
    if resolved is not None:
        if resolved != query and sys.stderr.isatty():
            print(f"[cppgraph] resolved {query!r} -> {resolved}", file=sys.stderr)
        return resolved
    if not candidates:
        parser.error(f"unknown {what}: {query} (use `cppgraph find` to look it up)")
    print(
        f"[cppgraph] {query!r} is ambiguous ({len(candidates)} matches) — "
        "pass the exact SCIP symbol:",
        file=sys.stderr,
    )
    for node in candidates[:10]:
        loc = (
            f"{node.file}:{node.line + 1}"
            if node.file is not None and node.line is not None
            else "?"
        )
        print(f"    {node.symbol}  ({node.display_name or '?'} @ {loc})", file=sys.stderr)
    if len(candidates) > 10:
        print(f"    ... and {len(candidates) - 10} more", file=sys.stderr)
    extra = ambiguous_candidate_hint(query, candidates)
    if extra:
        print(f"[cppgraph] note: {extra}.", file=sys.stderr)
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
        "'patched'); recorded as provenance so `cppgraph status` can "
        "flag the graph as stale when the pinned indexer changes. the index wizard "
        "passes it from the binary's provenance sidecar.",
    )
    p_build.add_argument(
        "--index-filter",
        default=None,
        help="the subtree substring the sources were filtered to before indexing "
        "(empty string = whole tree). Recorded as the graph's index scope so "
        "`cppgraph status` shows it and an incremental update reuses it. the index wizard "
        "passes the filter it applied.",
    )
    p_build.add_argument(
        "--index-no-tests",
        action="store_true",
        help="record that test TUs were excluded from the index (the scope "
        "counterpart of --no-tests). Provenance only — the actual "
        "filtering happens upstream when the compdb is built.",
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
        help="preview how many TUs a path-substring filter (the index wizard's 2nd arg) would keep",
    )

    p_init = sub.add_parser(
        "init",
        aliases=["index"],
        help="guided onboarding: find the compdb, show what's indexable, ask the "
        "scope questions (subtree / tests / attribution) in order, then index",
    )
    p_init.add_argument(
        "compdb", nargs="?", help="path to compile_commands.json (auto-found if omitted)"
    )
    p_init.add_argument(
        "--project-root", default=None, help="git checkout to index (default: compdb dir)"
    )
    p_init.add_argument("--name", default=None, help="graph name (default: project dir basename)")
    p_init.add_argument(
        "--run",
        dest="run",
        action="store_true",
        default=None,
        help="run the assembled command instead of asking",
    )
    p_init.add_argument(
        "--print", dest="run", action="store_false", help="only print the command, do not run it"
    )
    p_init.add_argument(
        "-y",
        "--non-interactive",
        action="store_true",
        help="don't prompt — take the scope from the flags below (the form an "
        "agent drives after asking the user in its own UI)",
    )
    p_init.add_argument(
        "--filter",
        default=None,
        help="subtree path-substring scope (implies --non-interactive; empty = whole tree)",
    )
    p_init.add_argument(
        "--no-tests", action="store_true", help="exclude test TUs (non-interactive scope)"
    )
    p_init.add_argument(
        "--attributed-refs",
        action="store_true",
        help="symbol-granularity usage (non-interactive scope; needs a #504 binary, else dropped)",
    )
    p_init.add_argument(
        "--from-scratch",
        action="store_true",
        help="re-walk every stage, defaulting existing artifacts toward recompute "
        "(still asks before discarding an index)",
    )
    p_init.add_argument(
        "--plan-json",
        action="store_true",
        help="emit the onboarding data (breakdown, questions, binary variant, "
        "artifacts) as JSON and exit — for an agent to render the questions itself",
    )

    p_setup = sub.add_parser(
        "setup",
        help="obtain the scip-clang indexer, register the MCP server, then index this "
        "project (the interactive per-machine setup; run via scripts/setup.sh)",
    )
    p_setup.add_argument(
        "--scip-source",
        choices=["download-patched", "download", "build", "emulate"],
        default=None,
        help="how to obtain scip-clang (skips the menu; required when non-interactive)",
    )
    p_setup.add_argument(
        "-y",
        "--yes",
        dest="assume_yes",
        action="store_true",
        help="don't re-prompt to replace an existing binary / MCP registration (keep them)",
    )
    p_setup.add_argument(
        "--from-scratch",
        action="store_true",
        help="re-walk every setup stage, re-obtaining artifacts that already exist",
    )
    p_setup.add_argument(
        "--no-index",
        dest="chain_index",
        action="store_false",
        help="stop after tool setup; don't chain into the project index wizard",
    )

    p_update = sub.add_parser(
        "update",
        help="refresh a graph for changed source files: with no args, auto-discovers "
        "the graph + compdb and re-indexes incrementally; with --scip, applies an "
        "already-produced partial re-index instead",
    )
    p_update.add_argument(
        "--graph",
        default=None,
        help="path to the graph store to update in place "
        "(default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_update.add_argument(
        "--scip",
        default=None,
        help="SCIP index of only the re-indexed (changed) translation units. Omit this "
        "(and --graph) to run a full incremental update in one step: diff the "
        "discovered graph's checkout, re-index the changed TUs, and apply",
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
    _add_path_filters(p_find)

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
    p_refs.add_argument(
        "--access",
        choices=["read", "write"],
        default=None,
        help="filter by the indexer's read/write analysis: 'write' keeps only sites "
        "tagged WriteAccess, 'read' only sites known NOT to be a write (plain reads). "
        "Needs a graph built with a scip-clang binary carrying the "
        "ReadAccess/WriteAccess patch — on a graph without that data this reports "
        "and shows nothing rather than guessing",
    )
    _add_path_filters(p_refs)

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
        choices=("calls", "inherits", "typed-by"),
        default="calls",
        help="edge kind to walk: 'calls' = call blast-radius (default); "
        "'inherits' = all transitive subclasses of a base type; "
        "'typed-by' = reverse impact on a type returns the fields/variables "
        "typed as that type",
    )
    _add_query_filters(p_impact)

    p_reachable = sub.add_parser(
        "reachable-from",
        help="forward reachability: everything a symbol transitively calls (a lower bound)",
    )
    p_reachable.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_reachable.add_argument(
        "symbol",
        help="a symbol name (resolved via `find`) or an exact SCIP string",
    )
    p_reachable.add_argument(
        "--depth", type=int, default=None, help="max hops to walk forwards (default: unbounded)"
    )
    p_reachable.add_argument(
        "--kind",
        choices=("calls", "inherits", "typed-by"),
        default="calls",
        help="edge kind to walk: 'calls' = forward call reachability from an entry "
        "point (default); 'inherits' = the transitive base hierarchy above a "
        "derived type; 'typed-by' = forward reachability from a field/variable "
        "returns its declared type",
    )
    _add_query_filters(p_reachable)

    p_hotspots = sub.add_parser("hotspots", help="global ranking of symbols by call-edge volume")
    p_hotspots.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_hotspots.add_argument(
        "--kind",
        choices=("fan_in", "fan_out", "edges"),
        default="fan_in",
        help="'fan_in' = most-called (default); 'fan_out' = most-calling; 'edges' = both summed",
    )
    p_hotspots.add_argument("--limit", type=int, default=20, help="max rows to show (default: 20)")
    p_hotspots.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop edges where either endpoint symbol is itself defined in a test file "
        "(default: off)",
    )
    p_hotspots.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    _add_path_filters(p_hotspots)

    p_dep_cost = sub.add_parser(
        "dependency-cost",
        help="call-site count against a target library — 'if I replace/remove this "
        "library, how many call sites change?' (exact count of calls edges into it)",
    )
    p_dep_cost.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_dep_cost.add_argument(
        "--target-path",
        action="append",
        dest="target_paths",
        metavar="PREFIX",
        required=True,
        help="count call sites whose callee is defined under PREFIX (repeatable)",
    )
    p_dep_cost.add_argument(
        "--limit",
        type=int,
        default=20,
        help="max target symbols to show in the breakdown (default: 20)",
    )
    p_dep_cost.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop edges where either endpoint symbol is itself defined in a test file "
        "(default: off)",
    )
    p_dep_cost.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    # Caller-side only here (--target-path owns the callee side): the
    # asymmetric-filter mode of hotspots.
    _add_path_filters(p_dep_cost)

    p_stats = sub.add_parser(
        "stats", help="aggregate counts (symbols, call edges, refs) per file or directory"
    )
    p_stats.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_stats.add_argument(
        "--group-by",
        choices=("file", "dir"),
        default="file",
        help="'file' (default): counts per file; 'dir': rolled up per directory via dirname",
    )
    p_stats.add_argument("--limit", type=int, default=20, help="max rows to show (default: 20)")
    _add_path_filters(p_stats)

    p_line_span = sub.add_parser(
        "line_span",
        help="rank definitions by body extent (end_line - start, largest first); "
        "needs a graph indexed with a #504-built scip-clang",
    )
    p_line_span.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_line_span.add_argument("--limit", type=int, default=20, help="max rows to show (default: 20)")
    p_line_span.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop definitions in test files (default: off)",
    )
    p_line_span.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    _add_path_filters(p_line_span)

    p_no_incoming = sub.add_parser(
        "no_incoming_calls",
        help="defined callables with zero incoming calls edges (a fact, not a "
        "dead-code verdict); needs a graph indexed with a #504-built scip-clang",
    )
    p_no_incoming.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_no_incoming.add_argument(
        "--limit", type=int, default=20, help="max rows to show (default: 20)"
    )
    p_no_incoming.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop definitions in test files (default: off; a test caller still "
        "counts as a caller)",
    )
    p_no_incoming.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    _add_path_filters(p_no_incoming)

    p_gir = sub.add_parser(
        "global_init_references",
        help="globals referenced by one global's initializer region (the fact "
        "behind the static-init-order question, not a verdict); needs a "
        "#504-built graph with --attributed-refs",
    )
    p_gir.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_gir.add_argument(
        "symbol",
        help="the global: a plain name or an exact SCIP symbol string",
    )
    p_gir.add_argument("--limit", type=int, default=40, help="max rows to show (default: 40)")
    p_gir.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )

    p_boundary = sub.add_parser(
        "boundary-violations",
        help="declared-layering conformance: list calls/inherits edges that "
        "cross a rule you supply (zero false positives — each hit is a real edge)",
    )
    p_boundary.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_boundary.add_argument(
        "--rule",
        action="append",
        dest="rules",
        required=True,
        metavar="FROM:FORBIDDEN",
        help="layering rule, repeatable: no edge from a symbol defined under "
        "FROM to one defined under FORBIDDEN (e.g. --rule common/:platform/ = "
        "common/ must not call platform/)",
    )
    p_boundary.add_argument(
        "--kind",
        action="append",
        choices=("calls", "inherits", "implements", "typed-by"),
        default=None,
        help="edge kind to check (repeatable; default: calls and inherits; "
        "'implements' and 'typed-by' are opt-in — 'typed-by' checks "
        "type-usage crossing the boundary: a field/variable typed as a type "
        "on the other side)",
    )
    p_boundary.add_argument("--limit", type=int, default=40, help="max rows to show (default: 40)")
    p_boundary.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )

    p_api = sub.add_parser(
        "api-surface",
        help="the actually-used external surface of a module: definitions "
        "inside a prefix called/referenced from outside it",
    )
    p_api.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_api.add_argument(
        "module_prefix",
        help="directory prefix of the module (segment-boundary match, e.g. "
        "src/pipeline covers src/pipeline/util.cpp, never src/pipeline_extra/)",
    )
    p_api.add_argument("--limit", type=int, default=40, help="max rows to show (default: 40)")
    p_api.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop uses whose use site or used definition is in a test file "
        "(default: off; a test caller still counts as an external use)",
    )
    p_api.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )

    p_outline = sub.add_parser(
        "outline",
        help="the outline of one file: every symbol defined in it, sorted by line",
    )
    p_outline.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_outline.add_argument(
        "file",
        help="exact file path as recorded in the index (relative, e.g. src/app.cpp)",
    )
    p_outline.add_argument("--limit", type=int, default=200, help="max rows to show (default: 200)")
    p_outline.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )

    p_members = sub.add_parser(
        "class-members",
        help="members declared on a class/struct (methods, fields, nested types), by line",
    )
    p_members.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_members.add_argument(
        "symbol",
        help="class name or exact SCIP symbol (must be a type; it ends in '#')",
    )
    p_members.add_argument("--limit", type=int, default=200, help="max rows to show (default: 200)")
    p_members.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )

    p_scc = sub.add_parser(
        "strongly-connected-components",
        help="cycles in the calls graph: groups of 2+ symbols that can all "
        "reach each other (a fact, not a bad-architecture verdict)",
    )
    p_scc.add_argument(
        "--graph",
        required=False,
        default=None,
        help="graph store path (default: auto-discovered from the cwd's .cppgraph/)",
    )
    p_scc.add_argument("--limit", type=int, default=40, help="max components to show (default: 40)")
    p_scc.add_argument(
        "--exclude-tests",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="drop components whose every member is defined in test files "
        "(default: off; a mixed component is reported whole)",
    )
    p_scc.add_argument(
        "--full-symbols",
        action="store_true",
        help="print the raw SCIP symbol strings instead of readable labels",
    )
    _add_path_filters(p_scc)

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
        help="summarize a symbol: definition site, doc comment, source snippet, callers/callees",
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
        # Record the tests state definitely whenever a scope is being recorded (a
        # scope-aware caller like the index wizard always passes --index-filter), so
        # "tests included" is explicit rather than indistinguishable from a legacy
        # graph that stored no scope at all.
        scope_recorded = args.index_filter is not None or args.index_no_tests
        meta = build_provenance(
            index,
            source_commit=args.source_commit,
            source_dirty=True if args.source_dirty else None,
            scip_variant=args.scip_variant,
            index_filter=args.index_filter,
            index_excludes_tests=bool(args.index_no_tests) if scope_recorded else None,
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

    if args.command == "setup":
        from cppgraph.setup_cmd import run_setup

        return run_setup(
            from_scratch=args.from_scratch,
            chain_index=args.chain_index,
            scip_source=args.scip_source,
            assume_yes=args.assume_yes,
        )

    if args.command in ("init", "index"):
        from cppgraph.init import onboarding_plan, run_init

        if args.plan_json:
            from cppgraph.init import _resolve_targets

            resolved = _resolve_targets(
                args.compdb, args.project_root, args.name, announce=False, print_fn=print
            )
            if resolved is None:
                return 1
            print(json.dumps(onboarding_plan(*resolved), indent=2))
            return 0
        from cppgraph.prompt import interactive, make_prompter

        # Interactive prompts need a real terminal; a piped run (e.g. Claude Code's
        # `! …`) gets EOF and would silently take defaults. Stop instead, unless the
        # scope was given as flags (-y / --filter) or only the plan is requested.
        if not args.non_interactive and not interactive():
            print(
                "[cppgraph] `index` needs an interactive terminal for the scope "
                "questions. Re-run in a real shell, or pass the scope as flags: "
                "cppgraph index <compdb> -y --filter <sub> [--no-tests] "
                "[--attributed-refs] --run"
            )
            return 3
        return run_init(
            compdb=args.compdb,
            project_root=args.project_root,
            name=args.name,
            run=args.run,
            filter=args.filter,
            no_tests=args.no_tests,
            attributed_refs=args.attributed_refs,
            non_interactive=args.non_interactive,
            from_scratch=args.from_scratch,
            prompter=make_prompter(),
        )

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
        if args.scip is None:
            from cppgraph.init import find_compdb
            from cppgraph.pipeline import incremental_update

            if args.graph:
                graph_path = Path(args.graph)
                store = GraphStore(graph_path)
                try:
                    recorded_root = store.meta().get("project_root")
                finally:
                    store.close()
                # The store's own recorded `project_root` is authoritative — an
                # explicit `--graph` isn't required to live at the conventional
                # `<project_root>/.cppgraph/<name>.graph.db` depth (a copy, a
                # symlink, a non-default layout), so guessing two directories up
                # can silently pick the wrong checkout/compdb. Only fall back to
                # the depth guess for an older graph built before this field was
                # recorded.
                project_root = (
                    project_root_path(recorded_root) if recorded_root else None
                ) or graph_path.resolve().parent.parent
            else:
                found = discover_graph()
                if found is None:
                    parser.error(
                        "no --graph given and no .cppgraph/*.graph.db found from here. "
                        "Pass --graph <store.db>, or run from inside an indexed project."
                    )
                graph_path, project_root = found
            compdb_path = find_compdb(project_root)
            if compdb_path is None:
                parser.error(
                    f"no compile_commands.json found at or above {project_root}; pass "
                    "--scip to apply an already-produced partial index instead."
                )
            return incremental_update(
                graph_db=graph_path,
                compdb=compdb_path,
                project_root=project_root,
                print_fn=print,
            )
        if not args.graph:
            parser.error("--graph is required together with --scip")
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
        store = _open_store_checked(args, parser)
        matches = store.find(args.query)
        if args.include_paths or args.exclude_paths:
            matches = [
                n
                for n in matches
                if matches_path_prefix(
                    n.file, include=args.include_paths, exclude=args.exclude_paths
                )
            ]
        if not matches:
            print(f"[cppgraph] no symbol matching {args.query!r}")
            return 1
        show_external = store.meta().get("has_external_symbols") == "true"
        for node in matches:
            _print_node(node, external=show_external and node.is_out_of_project is True)
        return 0

    if args.command == "callers":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        edges = store.callers_of(args.symbol)
        if args.exclude_tests:
            edges = drop_test_edges(store, edges, on="src")
        edges = filter_by_path(
            store,
            edges,
            on="src",
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(edges)} caller(s) of {args.symbol}{tests_note}")
        shown = edges[: args.limit] if args.limit is not None else edges
        for edge in shown:
            _print_edge(edge, other=edge.src, full_symbols=args.full_symbols)
        if len(shown) < len(edges):
            print(f"  ... and {len(edges) - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "callees":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        edges = store.callees_of(args.symbol)
        if args.exclude_tests:
            edges = drop_test_edges(store, edges, on="dst")
        edges = filter_by_path(
            store,
            edges,
            on="dst",
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
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
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        bases = store.bases_of(args.symbol)
        print(f"[cppgraph] {len(bases)} base class(es) of {args.symbol}")
        for node in bases:
            _print_node(node)
        return 0

    if args.command == "subtypes":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        subs = store.subtypes_of(args.symbol)
        print(f"[cppgraph] {len(subs)} subclass(es) of {args.symbol}")
        for node in subs:
            _print_node(node)
        return 0

    if args.command == "references":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        refs = store.references_of(args.symbol)
        if not refs and store.meta().get("has_references") != "true":
            print("[cppgraph] this graph was built with --no-references (no location index)")
            return 1
        if args.include_paths or args.exclude_paths:
            refs = [
                r
                for r in refs
                if matches_path_prefix(
                    r.file, include=args.include_paths, exclude=args.exclude_paths
                )
            ]
        if args.access:
            if store.meta().get("has_access_roles") != "true":
                print(
                    "[cppgraph] this graph carries no read/write access data — rebuild "
                    "with a scip-clang binary carrying the ReadAccess/WriteAccess patch "
                    "(--access would otherwise guess)"
                )
                return 1
            refs = filter_by_access(refs, args.access)
        print(f"[cppgraph] {len(refs)} use site(s) of {args.symbol}")
        for ref in refs[: args.limit]:
            line = ref.line + 1 if ref.line is not None else "?"
            # With attributed references, name the definition that uses it.
            used_by = (
                f"  (used by {short_label(ref.enclosing_symbol)})" if ref.enclosing_symbol else ""
            )
            # With access-role data, tag writes; a plain read stays untagged.
            print(f"  {ref.file}:{line}{used_by}{access_tag(ref.roles)}")
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
        store = _open_store_checked(args, parser)
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
        store = _open_store_checked(args, parser)
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
        if args.include_paths or args.exclude_paths:
            nodes = [
                (sym, n)
                for sym, n in nodes
                if matches_path_prefix(
                    n.file if n is not None else None,
                    include=args.include_paths,
                    exclude=args.exclude_paths,
                )
            ]
        verb = {
            "calls": "transitively call",
            "inherits": "transitively inherit from",
            "typed-by": "are typed as",
        }[args.kind]
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(nodes)} symbol(s) {verb} {args.symbol}{tests_note}")
        shown = nodes[: args.limit] if args.limit is not None else nodes
        for _sym, node in shown:
            if node is not None:
                _print_node(node, full_symbols=args.full_symbols)
        if len(shown) < len(nodes):
            print(f"  ... and {len(nodes) - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "reachable-from":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        if args.kind == "calls" and args.symbol.rstrip().endswith("#"):
            print(
                f"[cppgraph] {args.symbol} is a type — it makes no calls itself. Its "
                "call-graph reachability lives in its methods: use `cppgraph "
                "class-members`, then `reachable-from` on a method (or `--kind "
                "inherits` for its base hierarchy)."
            )
            return 0
        reached = sorted(store.reachable_from(args.symbol, max_depth=args.depth, kind=args.kind))
        nodes = [(sym, store.get_node(sym)) for sym in reached]
        if args.exclude_tests:
            nodes = [(sym, n) for sym, n in nodes if n is None or not is_test_file(n.file)]
        if args.include_paths or args.exclude_paths:
            nodes = [
                (sym, n)
                for sym, n in nodes
                if matches_path_prefix(
                    n.file if n is not None else None,
                    include=args.include_paths,
                    exclude=args.exclude_paths,
                )
            ]
        verb = "transitively reached from"
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] {len(nodes)} symbol(s) {verb} {args.symbol}{tests_note}")
        print(
            "  note: a lower bound — at least these are reachable. Static "
            "compiler-traced edges only; virtual dispatch, function pointers "
            "and runtime registration are not captured, so the true reachable "
            "set may be larger."
        )
        shown = nodes[: args.limit] if args.limit is not None else nodes
        for _sym, node in shown:
            if node is not None:
                _print_node(node, full_symbols=args.full_symbols)
        if len(shown) < len(nodes):
            print(f"  ... and {len(nodes) - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "hotspots":
        store = _open_store_checked(args, parser)
        ranked, total = store.hotspots(
            limit=args.limit,
            kind=args.kind,
            exclude_tests=args.exclude_tests,
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] top {len(ranked)} of {total} symbol(s) by {args.kind}{tests_note}")
        for symbol, count in ranked:
            node = store.get_node(symbol)
            label = symbol if args.full_symbols else short_label(symbol)
            if node is not None and node.file is not None and node.line is not None:
                loc = f"{node.file}:{node.line + 1}"
            else:
                loc = "?"
            print(f"  {count:>6}  {label}  ({loc})")
        if total > len(ranked):
            print(f"  ... and {total - len(ranked)} more (raise --limit to see them)")
        return 0

    if args.command == "dependency-cost":
        store = _open_store_checked(args, parser)
        # limit=None: the aggregate must sum over the full ranking, --limit
        # caps only the displayed breakdown (same shape as the MCP report).
        ranked, total = store.hotspots(
            limit=None,
            kind="fan_in",
            exclude_tests=args.exclude_tests,
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
            target_paths=args.target_paths,
        )
        shown = ranked[: args.limit]
        total_call_sites = sum(n for _, n in ranked)
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(
            f"[cppgraph] {total_call_sites} call site(s) into "
            f"{', '.join(args.target_paths)} from {total} target symbol(s){tests_note}"
        )
        for symbol, count in shown:
            node = store.get_node(symbol)
            label = symbol if args.full_symbols else short_label(symbol)
            if node is not None and node.file is not None and node.line is not None:
                loc = f"{node.file}:{node.line + 1}"
            else:
                loc = "?"
            print(f"  {count:>6}  {label}  ({loc})")
        if total > len(shown):
            print(f"  ... and {total - len(shown)} more (raise --limit to see them)")
        return 0

    if args.command == "stats":
        store = _open_store_checked(args, parser)
        groups, total = store.stats(
            group_by=args.group_by,
            limit=args.limit,
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
        unit = "file(s)" if args.group_by == "file" else "dir(s)"
        print(f"[cppgraph] top {len(groups)} of {total} {unit} by symbols+edges+refs")
        for g in groups:
            print(
                f"  {g['symbols']:>6} sym  {g['edges']:>5} edges  {g['refs']:>5} refs"
                f"  {g[args.group_by]}"
            )
        if total > len(groups):
            print(f"  ... and {total - len(groups)} more (raise --limit to see them)")
        return 0

    if args.command == "line_span":
        store = _open_store_checked(args, parser)
        result = store.line_span(
            limit=args.limit,
            exclude_tests=args.exclude_tests,
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
        if result is None:
            print(
                "[cppgraph] line_span unavailable: this graph carries no definition "
                "body extents (enclosing_range), which only a #504-built scip-clang "
                "emits — index with one and rebuild the store to enable it"
            )
            return 1
        ranked, total = result
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(f"[cppgraph] top {len(ranked)} of {total} definition(s) by body span{tests_note}")
        for symbol, span in ranked:
            node = store.get_node(symbol)
            label = symbol if args.full_symbols else short_label(symbol)
            if node is not None and node.file is not None and node.line is not None:
                loc = f"{node.file}:{node.line + 1}"
            else:
                loc = "?"
            print(f"  {span:>6}  {label}  ({loc})")
        if total > len(ranked):
            print(f"  ... and {total - len(ranked)} more (raise --limit to see them)")
        return 0

    if args.command == "no_incoming_calls":
        store = _open_store_checked(args, parser)
        result = store.no_incoming_calls(
            limit=args.limit,
            exclude_tests=args.exclude_tests,
            include_paths=args.include_paths,
            exclude_paths=args.exclude_paths,
        )
        if result is None:
            print(
                "[cppgraph] no_incoming_calls is not reliable on this graph: a stock "
                "scip-clang's caller attribution (nearest-preceding definition, no "
                "enclosing ranges) can mis-attribute a bodyless member declaration "
                "to the preceding definition — a phantom caller that turns a real 0 "
                "into a false 1. Index with a #504-built scip-clang and rebuild the "
                "store to enable it"
            )
            return 1
        symbols, total = result
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(
            f"[cppgraph] {len(symbols)} of {total} defined callable(s) with zero "
            f"incoming calls{tests_note}"
        )
        print(
            "  note: 0 static callers is a fact, not proof of dead code — vtable "
            "dispatch, exported API, templates, entry points can have no static "
            "caller and still be live"
        )
        for symbol in symbols:
            node = store.get_node(symbol)
            if node is not None:
                _print_node(node, full_symbols=args.full_symbols)
            else:
                print(f"  {symbol if args.full_symbols else short_label(symbol)}  (?)")
        if total > len(symbols):
            print(f"  ... and {total - len(symbols)} more (raise --limit to see them)")
        return 0

    if args.command == "global_init_references":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        try:
            result = store.global_init_references(args.symbol, limit=args.limit)
        except ValueError as e:
            parser.error(str(e))
        if result is None:
            print(
                "[cppgraph] global_init_references unavailable: this graph carries "
                "no attributed references — it needs a #504-built scip-clang AND "
                "a store built with --attributed-refs (or `cppgraph enrich-refs`). "
                "Rebuild to enable it"
            )
            return 1
        refs, total = result
        print(
            f"[cppgraph] {len(refs)} of {total} global(s) referenced by "
            f"{args.symbol}'s initializer region"
        )
        print(
            "  note: a reference is a fact, not a verdict — a constexpr/constinit "
            "initializer is constant-initialized (safe), and a read inside a "
            "lambda body in the region may run lazily rather than at initialization"
        )
        for ref in refs:
            label = ref.symbol if args.full_symbols else short_label(ref.symbol)
            line = ref.line + 1 if ref.line is not None else "?"
            site = f"{ref.file}:{line}" if ref.file is not None else "?"
            print(f"  {label}  ({site})")
        if total > len(refs):
            print(f"  ... and {total - len(refs)} more (raise --limit to see them)")
        return 0

    if args.command == "boundary-violations":
        store = _open_store_checked(args, parser)
        rules: list[tuple[str, str]] = []
        for value in args.rules:
            parts = value.split(":", 1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                parser.error(f"--rule {value!r}: expected FROM:FORBIDDEN (e.g. common/:platform/)")
            rules.append((parts[0], parts[1]))
        kinds = tuple(args.kind) if args.kind else ("calls", "inherits")
        try:
            violations, total = store.boundary_violations(rules, edge_kinds=kinds, limit=args.limit)
        except ValueError as e:
            parser.error(str(e))
        print(f"[cppgraph] {len(violations)} of {total} violation(s) across {len(rules)} rule(s)")
        print(
            "  note: each violation is a real compiler-traced edge (zero false positives); "
            "0 violations means no statically indexed edge crosses the rules — "
            "runtime dispatch (virtual calls, function pointers) can cross a "
            "boundary with no static edge"
        )
        for v in violations:
            src = v["src"] if args.full_symbols else short_label(v["src"])
            dst = v["dst"] if args.full_symbols else short_label(v["dst"])
            line = v["line"] + 1 if v["line"] is not None else "?"
            site = f"{v['file']}:{line}" if v["file"] is not None else "?"
            print(f"  [{v['rule']}] {v['kind']}  {src} -> {dst}  ({site})")
        if total > len(violations):
            print(f"  ... and {total - len(violations)} more (raise --limit to see them)")
        return 0

    if args.command == "api-surface":
        store = _open_store_checked(args, parser)
        try:
            ranked, total, has_refs = store.api_surface(
                args.module_prefix, limit=args.limit, exclude_tests=args.exclude_tests
            )
        except ValueError as e:
            parser.error(str(e))
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(
            f"[cppgraph] top {len(ranked)} of {total} externally-used symbol(s) under "
            f"{args.module_prefix}{tests_note}"
        )
        if not has_refs:
            print(
                "  note: this graph carries no reference index (built --no-references) — "
                "call sites only, a type used outside the module is invisible to this "
                "list; rebuild with references to count those uses too"
            )
        for row in ranked:
            label = row["symbol"] if args.full_symbols else short_label(row["symbol"])
            line = row["line"] + 1 if row["line"] is not None else "?"
            loc = f"{row['file']}:{line}" if row["file"] is not None else "?"
            print(
                f"  {row['external_calls']:>5} calls  {row['external_refs']:>5} refs"
                f"  {label}  ({loc})"
            )
        if total == 0 and has_refs:
            print(
                "  note: no external uses found — either the module is genuinely "
                "self-contained or the prefix matches no indexed path; `cppgraph "
                "stats` lists the indexed paths (the match is on a path-segment boundary)"
            )
        if total > len(ranked):
            print(f"  ... and {total - len(ranked)} more (raise --limit to see them)")
        return 0

    if args.command == "outline":
        store = _open_store_checked(args, parser)
        nodes, total = store.outline(args.file, limit=args.limit)
        print(f"[cppgraph] {len(nodes)} of {total} definition(s) in {args.file}, by line")
        for node in nodes:
            _print_node(node, full_symbols=args.full_symbols)
        if total == 0:
            print(
                "  note: no symbols defined in this file in the index — the path must "
                "match the index's recorded relative path exactly (not a prefix, not "
                "absolute); `cppgraph stats` lists the indexed files"
            )
        if total > len(nodes):
            print(f"  ... and {total - len(nodes)} more (raise --limit to see them)")
        return 0

    if args.command == "class-members":
        store = _open_store_checked(args, parser)
        args.symbol = _resolve_symbol(store, args.symbol, parser)
        result = store.class_members(args.symbol, limit=args.limit)
        if result is None:
            parser.error(
                f"{args.symbol} is not a type (class/struct/enum) — class-members lists "
                "the members declared on a class (use `cppgraph find` to locate the "
                "class symbol; it ends in '#')"
            )
        members, total = result
        print(f"[cppgraph] {len(members)} of {total} member(s) of {args.symbol}, by line")
        for node in members:
            _print_node(node, full_symbols=args.full_symbols)
        if total == 0:
            print(
                "  note: no members recorded on this type — it may be genuinely "
                "memberless, defined outside the indexed scope, or only "
                "forward-declared; `cppgraph refs` shows where it is used"
            )
        if total > len(members):
            print(f"  ... and {total - len(members)} more (raise --limit to see them)")
        return 0

    if args.command == "strongly-connected-components":
        store = _open_store_checked(args, parser)
        try:
            components, total = store.strongly_connected_components(
                limit=args.limit,
                exclude_tests=args.exclude_tests,
                include_paths=args.include_paths,
                exclude_paths=args.exclude_paths,
            )
        except ValueError as e:
            parser.error(str(e))
        tests_note = " (excluding tests)" if args.exclude_tests else ""
        print(
            f"[cppgraph] {len(components)} of {total} cyclic group(s) of 2+ symbols, "
            f"biggest first{tests_note}"
        )
        print(
            "  note: a cycle is a graph fact, not a bad-architecture verdict — mutual "
            "recursion is often legitimate (a visitor, a recursive-descent parser's "
            "mutually recursive rules)"
        )
        for comp in components:
            print(f"  component of {len(comp)} symbol(s):")
            for symbol in comp:
                node = store.get_node(symbol)
                label = symbol if args.full_symbols else short_label(symbol)
                if node is not None and node.file is not None and node.line is not None:
                    loc = f"{node.file}:{node.line + 1}"
                else:
                    loc = "?"
                print(f"    {label}  ({loc})")
        if total > len(components):
            print(f"  ... and {total - len(components)} more (raise --limit to see them)")
        return 0

    if args.command == "status":
        graph_path = _resolve_graph(args, parser)
        store = GraphStore(graph_path)
        m = store.meta()
        commit = m.get("source_commit")
        dirty = m.get("source_dirty") == "true"
        print(f"[cppgraph] graph store: {graph_path}")
        print("  transport:     cli")
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
        index_filter = m.get("index_filter")
        if index_filter is not None:
            scope = index_filter if index_filter else "whole tree"
            tests = m.get("index_tests")
            tests_note = f" (tests {tests})" if tests else ""
            print(f"  indexed scope: {scope}{tests_note}")
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
                # Estimated cost from THIS graph's ref_count (measured per-ref
                # constant, see `attributed_refs_cost_note`); None -> the old
                # number-free wording rather than a fabricated "~0 bytes".
                cost = attributed_refs_cost_note(m.get("ref_count"))
                if cost is None:
                    print(
                        "                 (costs extra store space — worth it for "
                        "symbol-level usage)"
                    )
                else:
                    print(
                        textwrap.fill(
                            f"estimated {cost} — worth it for symbol-level usage",
                            width=78,
                            initial_indent=" " * 17,
                            subsequent_indent=" " * 17,
                            break_on_hyphens=False,
                        )
                    )
            if m.get("has_access_roles") == "true":
                print("  access roles:  read/write tags present on reference sites")
            else:
                print("  access roles:  none (references carry no read/write tags)")
                print(
                    "                 -> to tag writes ('who mutates this global/field?'), "
                    "index with a scip-clang binary"
                )
                print("                    carrying the ReadAccess/WriteAccess patch, then rebuild")
        if m.get("has_symbol_kind") == "true":
            print("  symbol kinds:  fine-grained SCIP kinds present (Class/Method/Enum/...)")
        else:
            print("  symbol kinds:  none (symbols carry no SCIP kind data)")
            print(
                "                 -> to get them (Class vs Struct, Method vs StaticMethod, …), "
                "index with a scip-clang"
            )
            print(
                "                    binary carrying the SymbolInformation.kind patch, then rebuild"
            )
        if m.get("has_external_symbols") == "true":
            print("  external symbols: external-package symbol metadata present")
        else:
            print("  external symbols: none (no external-package symbol metadata)")
            print(
                "                 -> rebuilt graphs carry it (boost/absl/stdlib symbols "
                "classified in explain/find); no special binary needed"
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
            if scip.get("patchset_status") == "stale":
                print(f"    ! {scip['patchset_message']}")
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

        result = changed_files_since(
            args.root, commit, dirty_fingerprints=read_dirty_fingerprints(m)
        )
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
            print("    next: cppgraph update")
        return 1

    if args.command == "explain":
        store = _open_store_checked(args, parser)
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
        if node.scip_kind is not None:
            # Fine-grained SCIP kind from a kind-patched binary
            # (`has_symbol_kind`); additive to the descriptor-suffix
            # classification, absent on stock graphs — never an empty value.
            print(f"  kind:       {node.scip_kind}")
        if store.meta().get("has_external_symbols") == "true":
            # External-package classification (`Index.external_symbols`):
            # printed only when the graph carries the capability — omitted
            # entirely on older graphs (absent, not "unknown").
            if node.is_out_of_project is True:
                print("  out of project: yes")
            elif node.is_out_of_project is False:
                print("  out of project: no")
            else:
                print("  out of project: unknown")
        if node.documentation:
            # Genuine doc comment from the graph (extracted at index time) —
            # no --root needed, unlike the signature below.
            doc_lines = node.documentation.splitlines()
            print(f"  documentation: {doc_lines[0]}")
            for doc_line in doc_lines[1:]:
                print(f"    {doc_line}")
        if node.signature_documentation:
            # Signature recorded in the graph at index time (signature-emitting
            # binary): printed with no --root, like documentation. Labelled
            # apart from the source-derived `signature:` below so the two are
            # never conflated (the source read also captures defaulted
            # parameters the recorded text may lack).
            sig_lines = node.signature_documentation.splitlines()
            print(f"  signature (stored): {sig_lines[0]}")
            for sig_line in sig_lines[1:]:
                print(f"    {sig_line}")
        if args.root is not None:
            sig = extract_signature(args.root, node.file, node.line)
            if sig is not None:
                print(f"  signature:  {sig}")

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
        if not callers and store.meta().get("has_enclosing_ranges") != "true":
            # The same stock-attribution caveat `no_incoming_calls` refuses on:
            # the count is still reported, so the caveat travels with it.
            print(
                "  note: 0 callers is only trustworthy on a #504-built index — "
                "this stock-binary graph's caller attribution (nearest-preceding "
                "definition, no enclosing ranges) has a separate false-positive "
                "path that can fabricate a phantom caller from a bodyless "
                "declaration site, but this reported 0 can hide a real caller "
                "when a call site with no preceding callable definition in its "
                "document is dropped, so this 0 is not guaranteed"
            )
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
        store = _open_store_checked(args, parser)
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
        store = _open_store_checked(args, parser)
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
