"""Guided, deterministic onboarding: `cppgraph index`.

Asks the scope questions in a fixed order, each with the information needed to
choose well (which subtree, whether to drop tests, whether to record
symbol-granularity usage), then runs the index pipeline. It works without an LLM,
and gives an agent one deterministic path to hand the user.

Design:
- **Thin front-end over `cppgraph.pipeline`.** It reuses `summarize_compdb` /
  `is_test_file` for the breakdown and calls the pipeline for the actual work.
- **Pure helpers** (`find_compdb`, `scip_clang_info`, `out_dir_for`,
  `artifact_status`) carry the decisions and are unit-tested without any I/O;
  `run_init` is the interactive driver (the `Prompter` is injectable, so the whole
  flow is scriptable in tests).
- **Resumable via artifact detection.** The pipeline writes named artifacts per
  stage (`<name>.compdb.json` → `<name>.scip` → `<name>.graph.db`); the wizard
  reads which files exist and offers to reuse or recompute each, rather than
  blindly redoing an expensive stage.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cppgraph.compdb import CompdbSummary, format_summary, load_compdb, summarize_compdb
from cppgraph.prompt import Prompter

_COMPDB_NAME = "compile_commands.json"


def _git_toplevel(directory: Path) -> Path | None:
    """The git repository root containing `directory`, or None (not a checkout /
    git unavailable). Best-effort, never raises."""
    try:
        out = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def find_compdb(start: Path) -> Path | None:
    """Locate a `compile_commands.json` at or above `start`.

    Checks `start` itself, a conventional `build/` subdir, then walks up parents —
    the common places a build system drops it. Returns the first hit, else None.
    """
    start = start.resolve()
    candidates: list[Path] = []
    for d in (start, *start.parents):
        candidates.append(d / _COMPDB_NAME)
        candidates.append(d / "build" / _COMPDB_NAME)
    for c in candidates:
        if c.is_file():
            return c
    return None


def scip_clang_bin_dir() -> Path:
    """The per-machine scip-clang bin dir:
    `$CPPGRAPH_BIN_DIR`, else `${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin`."""
    override = os.environ.get("CPPGRAPH_BIN_DIR")
    if override:
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "cppgraph" / "bin"


def scip_clang_info(bin_dir: Path | None = None) -> tuple[bool, str | None]:
    """`(binary_present, variant)` for the local scip-clang.

    `variant` comes from the `scip-clang.json` provenance sidecar next to the
    binary — `"stock"`, `"enclosing_range-504"`,
    or None when there is no sidecar. Only `enclosing_range-504` can produce the
    `enclosing_range` that `--attributed-refs` needs, so the wizard gates that
    question on it.
    """
    bin_dir = bin_dir or scip_clang_bin_dir()
    binary = bin_dir / "scip-clang"
    present = os.access(binary, os.X_OK)
    variant: str | None = None
    sidecar = bin_dir / "scip-clang.json"
    if sidecar.is_file():
        try:
            import json

            variant = json.loads(sidecar.read_text()).get("variant") or None
        except (OSError, ValueError):
            variant = None
    return present, variant


def out_dir_for(project_root: Path) -> Path:
    """The project's `.cppgraph/` output dir (a pure path; the pipeline creates it)."""
    return project_root / ".cppgraph"


def artifact_status(out_dir: Path, name: str) -> dict[str, bool]:
    """Which pipeline stages left an artifact on disk, for resume detection:
    `compdb` (filtered subset), `scip` (index), `graph` (store)."""
    return {
        "compdb": (out_dir / f"{name}.compdb.json").is_file(),
        "scip": (out_dir / f"{name}.scip").is_file(),
        "graph": (out_dir / f"{name}.graph.db").is_file(),
    }


def default_name(project_root: Path) -> str:
    """A sensible graph name: the project directory's basename."""
    return project_root.resolve().name or "project"


def _scoped_counts(entries: list[dict], src_filter: str) -> tuple[int, int]:
    """`(tu_count, test_count)` within `src_filter` ("" = whole tree)."""
    s = summarize_compdb(entries, filter=src_filter or None)
    if src_filter:
        return s.matched, s.matched_tests
    return s.total, s.tests


def _resolve_targets(
    compdb: str | None,
    project_root: str | None,
    name: str | None,
    *,
    announce: bool,
    print_fn,
) -> tuple[Path, list[dict], Path, str] | None:
    """Resolve `(compdb_path, entries, project_root, name)` shared by the run and
    the `--plan-json` paths, so both see identical targets. Returns None (after
    printing an error) when the compdb can't be located or parsed. `announce`
    prints the auto-discovered path — off for `--plan-json` so stdout stays pure
    JSON."""
    if compdb:
        compdb_path = Path(compdb)
        if not compdb_path.is_file():
            print_fn(f"error: {compdb_path} not found")
            return None
    else:
        found = find_compdb(Path.cwd())
        if found is None:
            print_fn(
                "error: no compile_commands.json found at or above the current "
                "directory. Generate it from your build system, or pass its path."
            )
            return None
        compdb_path = found
        if announce:
            print_fn(f"Found: {compdb_path}")
    try:
        entries = load_compdb(str(compdb_path))
    except (OSError, ValueError) as e:
        print_fn(f"error: {e}")
        return None
    # Default the project root to the git top-level of the compdb's directory, NOT
    # the compdb dir: a compile_commands.json commonly lives in build/, and using
    # build/ as the root makes scip-clang drop every source under ../src/ (silently
    # broken graph — only vendored code under build/ survives). Fall back to the
    # compdb dir when it isn't a git checkout.
    if project_root:
        project_root_path = Path(project_root).resolve()
    else:
        compdb_dir = compdb_path.resolve().parent
        project_root_path = _git_toplevel(compdb_dir) or compdb_dir
    graph_name = name or default_name(project_root_path)
    return compdb_path, entries, project_root_path, graph_name


def existing_artifacts(out_dir: Path, name: str) -> dict:
    """Details of the already-present `.scip` / `.graph.db` (or None each), so a
    reuse-vs-recompute choice can show where the data came from."""
    from cppgraph.scip_introspect import describe_scip

    scip_info = describe_scip(out_dir / f"{name}.scip")
    scip = scip_info if scip_info.get("exists") else None

    graph = None
    graph_path = out_dir / f"{name}.graph.db"
    if graph_path.is_file():
        graph = {"path": str(graph_path)}
        try:
            from cppgraph.store import GraphStore

            store = GraphStore(graph_path)
            try:
                m = store.meta()
            finally:
                store.close()
            for k in (
                "source_commit",
                "built_at",
                "index_filter",
                "index_tests",
                "index_tool_version",
                "index_tool_variant",
            ):
                if m.get(k):
                    graph[k] = m[k]
        except Exception as exc:  # a corrupt/old store must not break the plan
            graph["error"] = f"{type(exc).__name__}: {exc}"
    return {"scip": scip, "graph": graph}


def _existing_summary(existing: dict) -> str:
    """One-line human description of the existing artifacts, for the reuse question."""
    parts: list[str] = []
    scip = existing.get("scip")
    if scip:
        tool = f"{scip.get('tool_name') or '?'} {scip.get('tool_version') or ''}".strip()
        parts.append(
            f"index: {tool}, {scip.get('document_count', '?')} doc(s), "
            f"generated {scip.get('mtime_iso', '?')}"
        )
    graph = existing.get("graph")
    if graph:
        bits = []
        if graph.get("index_filter") is not None:
            bits.append(f"scope '{graph['index_filter'] or '<whole tree>'}'")
        if graph.get("index_tests"):
            bits.append(f"tests {graph['index_tests']}")
        if graph.get("source_commit"):
            bits.append(f"commit {graph['source_commit'][:12]}")
        if graph.get("built_at"):
            bits.append(f"built {graph['built_at']}")
        parts.append("graph: " + (", ".join(bits) if bits else "present"))
    return "; ".join(parts) or "already indexed"


def onboarding_plan(
    compdb_path: Path, entries: list[dict], project_root_path: Path, graph_name: str
) -> dict:
    """The decision-relevant onboarding data as a plain dict (for `--plan-json`):
    the compdb breakdown, the scip-clang variant, existing artifacts, and the
    questions with the info a UI needs to ask them well. This is the single source
    of *data*; an LLM renders it in its own UI, then runs `cppgraph init -y …` to
    get the exact command deterministically."""
    summary = summarize_compdb(entries)
    present, variant = scip_clang_info()
    supports_attribution = variant == "enclosing_range-504"
    out_dir = out_dir_for(project_root_path)
    status = artifact_status(out_dir, graph_name)
    existing = existing_artifacts(out_dir, graph_name)
    total = summary.total
    tests_pct = round(100 * summary.tests / total) if total else 0

    # When the project is already indexed, the FIRST question is reuse-vs-recompute,
    # carrying the details of what's on disk so the agent can show "where it came
    # from" before the user decides — never silently reuse or clobber.
    questions: list[dict] = []
    if status["scip"] or status["graph"]:
        questions.append(
            {
                "key": "reuse",
                "type": "choice",
                "prompt": "This project is already indexed — use the existing data or recompute?",
                "default": "reuse",
                "info": _existing_summary(existing),
                "options": [
                    {"value": "reuse", "label": "use the existing index/graph (no flag)"},
                    {"value": "recompute", "label": "recompute from scratch (--from-scratch)"},
                ],
            }
        )

    return {
        "compdb": str(compdb_path),
        "project_root": str(project_root_path),
        "name": graph_name,
        "summary": {
            "total": total,
            "tests": summary.tests,
            "tests_pct": tests_pct,
            "common_prefix": summary.common_prefix,
            "groups": [{"subtree": k, "tus": n, "tests": t} for k, n, t in summary.groups],
        },
        "scip_clang": {
            "present": present,
            "variant": variant,
            "supports_attribution": supports_attribution,
        },
        "artifacts": status,
        "existing": existing,
        "questions": questions
        + [
            {
                "key": "filter",
                "type": "string",
                "prompt": "Subtree filter (path substring; empty = whole tree)",
                "default": "",
                "info": "Scope to a source subtree; skip vendored/third-party trees.",
                # Concrete candidates from the breakdown, so the agent presents real
                # choices instead of inventing them. "" = whole tree; a free-text
                # substring is also allowed.
                "options": [{"value": "", "label": f"whole tree ({total} TU(s))"}]
                + [
                    {"value": k, "label": f"{k} ({n} TU(s), {t} test(s))"}
                    for k, n, t in summary.groups
                ],
            },
            {
                "key": "no_tests",
                "type": "bool",
                "prompt": "Exclude tests?",
                "default": False,
                "info": (
                    f"Tests are {summary.tests} of {total} TU(s) ({tests_pct}%). Excluding "
                    "them speeds indexing by roughly that share, but the graph then can't "
                    "answer 'which tests exercise symbol X'."
                ),
            },
            {
                "key": "attributed_refs",
                "type": "bool",
                "prompt": "Record symbol-granularity usage (--attributed-refs)?",
                "default": False,
                "available": supports_attribution,
                "info": (
                    "'where is this type used?' answers with the functions that use it, not "
                    "just the files. Larger store (~+23%)."
                    if supports_attribution
                    else "Unavailable: needs a #504-built scip-clang; the local binary is "
                    "stock or absent."
                ),
            },
        ],
    }


def _gate_attribution(requested: bool, print_fn) -> bool:
    """Attribution needs a #504 binary; if requested without one, warn and drop it
    . Used by the non-interactive path."""
    if not requested:
        return False
    _present, variant = scip_clang_info()
    if variant == "enclosing_range-504":
        return True
    print_fn(
        "warning: --attributed-refs requested, but the local scip-clang is not a "
        "#504 build — producing file-granularity usage instead."
    )
    return False


# --- interactive driver -----------------------------------------------------
#
# `input_fn`/`print_fn` are injectable so the whole flow is scriptable in tests
# and never touches real stdio there. On EOF (piped empty input) the prompts fall
# back to their defaults, so a non-interactive invocation still produces a plan.


def run_init(
    *,
    compdb: str | None = None,
    project_root: str | None = None,
    name: str | None = None,
    run: bool | None = None,
    filter: str | None = None,
    no_tests: bool = False,
    attributed_refs: bool = False,
    non_interactive: bool = False,
    from_scratch: bool = False,
    prompter: Prompter | None = None,
    input_fn=input,
    print_fn=print,
) -> int:
    """`cppgraph index` / `cppgraph init`. Returns a process exit code.

    Two ways in off one code path:
    - **interactive** (default): ask the scope questions, showing the details needed
      to choose (selectable menus when the TUI is available);
    - **non-interactive** (`non_interactive=True`, or implied by a `filter`): take
      the scope from `filter`/`no_tests`/`attributed_refs`, no prompts — the form
      an LLM drives after gathering answers in its own UI.

    `run`: True = index now, False = print the plan only, None = ask (non-interactive
    None means print only). A `prompter` may be injected (or `input_fn`/`print_fn` for
    a scripted stdlib prompter) so the whole flow is testable.
    """
    p = prompter or Prompter(input_fn, print_fn)

    # A provided --filter is a clear non-interactive intent.
    non_interactive = non_interactive or filter is not None

    resolved = _resolve_targets(compdb, project_root, name, announce=True, print_fn=p.note)
    if resolved is None:
        return 1
    compdb_path, entries, project_root_path, graph_name = resolved

    # Show the breakdown so the scope choice is informed, not blind.
    summary: CompdbSummary = summarize_compdb(entries)
    p.note("", format_summary(summary), "")

    out_dir = out_dir_for(project_root_path)
    status = artifact_status(out_dir, graph_name)

    out_scip = out_dir / f"{graph_name}.scip"
    out_graph = out_dir / f"{graph_name}.graph.db"

    if non_interactive:
        src_filter = filter or ""
        chosen_no_tests = bool(no_tests)
        chosen_attributed = _gate_attribution(attributed_refs, p.note)
        # Non-destructive by default: reuse an existing index/graph, build only what
        # is missing. Only --from-scratch (an explicit choice) re-does — so a
        # non-interactive run can never silently discard a `.scip` (hours to rebuild)
        # or an already-built graph.
        recompute_scip = from_scratch or not status["scip"]
        rebuild_graph = from_scratch or not status["graph"]
        if status["graph"] and not rebuild_graph:
            p.note(
                f"An indexed graph already exists: {out_graph} — keeping it. Pass "
                "--from-scratch to rebuild, or `cppgraph update` to refresh changed files."
            )
    else:
        rebuild_graph = True
        # A graph already exists: reuse it, refresh it incrementally, or rebuild.
        if status["graph"]:
            p.note(f"A graph already exists: {out_graph}")
            choice = p.select(
                "What would you like to do?",
                [
                    ("update", "update incrementally (re-index only changed files)"),
                    ("rebuild", "rebuild from scratch"),
                    ("keep", "keep it as-is"),
                    ("quit", "quit"),
                ],
                "update",
            )
            if choice in ("quit", "keep"):
                if choice == "keep":
                    p.note("Kept the existing graph unchanged.")
                return 0
            if choice == "update":
                upd_run = run if run is not None else p.confirm("Run the update now?", True)
                if not upd_run:
                    p.note("", f"Plan: incremental update -> {out_graph}", "Not run.")
                    return 0
                from cppgraph.pipeline import incremental_update

                p.note("")
                return incremental_update(
                    graph_db=out_graph,
                    compdb=compdb_path,
                    project_root=project_root_path,
                    print_fn=p.note,
                )
            # else fall through to a full rebuild
        # Scope questions, in order, each with the info to choose well.
        src_filter = _ask_filter(entries, summary, p)
        chosen_no_tests = _ask_no_tests(entries, src_filter, p)
        chosen_attributed = _ask_attributed(p)
        # Reuse/recompute the index (the expensive artifact) with its details shown.
        recompute_scip = _ask_recompute_scip(out_scip, from_scratch, p)

    do_run = run
    if do_run is None:
        do_run = False if non_interactive else p.confirm("Index now?", True)

    if not do_run:
        scope = src_filter or "<whole tree>"
        if chosen_no_tests:
            scope += " (no tests)"
        if chosen_attributed:
            scope += " (attributed-refs)"
        p.note("")
        p.panel(
            "Plan",
            [
                ("scope", scope),
                ("scip", "recompute" if recompute_scip else "reuse existing"),
                ("graph", f"{'rebuild' if rebuild_graph else 'reuse existing'} -> {out_graph}"),
            ],
        )
        p.note("Not run. Re-run with --run to index.")
        return 0

    from cppgraph.pipeline import full_build

    p.note("")
    return full_build(
        compdb=compdb_path,
        project_root=project_root_path,
        name=graph_name,
        src_filter=src_filter,
        no_tests=chosen_no_tests,
        attributed_refs=chosen_attributed,
        recompute_scip=recompute_scip,
        rebuild_graph=rebuild_graph,
        print_fn=p.note,
    )


def _ask_filter(entries, summary, p: Prompter) -> str:
    """Choose a subtree filter, previewing what each candidate keeps. The concrete
    subtrees from the breakdown are offered as selectable options; "other" takes a
    free-text substring. Empty = whole tree."""
    options = [("", f"whole tree ({summary.total} TU(s))")]
    options += [
        (subtree, f"{subtree} ({tus} TU(s), {tests} test(s))")
        for subtree, tus, tests in summary.groups
    ]
    options.append(("\x00other", "other — type a path substring"))
    while True:
        choice = p.select("Scope to which sources?", options, "")
        f = p.text("Path substring (empty = whole tree)", "") if choice == "\x00other" else choice
        tus, tests = _scoped_counts(entries, f)
        if f and tus == 0:
            p.note(f"  '{f}' matches nothing — try another substring.")
            continue
        if f:
            p.note(f"  '{f}' keeps {tus} of {summary.total} TU(s) ({tests} test(s)).")
        else:
            p.note(f"  whole tree: {tus} TU(s), {tests} test(s).")
        if p.confirm("Use this scope?", True):
            return f


def _ask_no_tests(entries, src_filter, p: Prompter) -> bool:
    """Offer to drop tests, with the trade-off stated — never defaulted on."""
    tus, tests = _scoped_counts(entries, src_filter)
    if tests == 0:
        return False
    pct = round(100 * tests / tus) if tus else 0
    p.note(
        "",
        f"Tests are {tests} of {tus} TU(s) ({pct}%). Excluding them speeds indexing "
        "by roughly that share, but the graph then can't answer 'which tests "
        "exercise symbol X' (their call sites are gone).",
    )
    return p.confirm("Exclude tests?", False)


def _ask_attributed(p: Prompter) -> bool:
    """Offer symbol-granularity attribution only when the local binary can produce
    it (#504); otherwise explain why it's unavailable and skip."""
    present, variant = scip_clang_info()
    p.note("")
    if variant == "enclosing_range-504":
        p.note(
            "Your scip-clang is a #504 build, so symbol-granularity usage is "
            "available (--attributed-refs): 'where is this type used?' answers with "
            "the functions that use it, not just the files. Larger store (~+23%)."
        )
        return p.confirm("Record symbol-granularity usage?", False)
    if not present:
        p.note(
            "Note: no native scip-clang here — an existing .scip is reused if present. "
            "Symbol-granularity attribution needs a #504 build."
        )
    else:
        p.note(
            "Your scip-clang is stock (not #504), so symbol-granularity attribution "
            "is unavailable; the graph will use exact file-granularity usage."
        )
    return False


def _ask_recompute_scip(out_scip: Path, from_scratch: bool, p: Prompter) -> bool:
    """Decide whether to (re)compute the `.scip` index. Absent -> must compute. Present
    -> show what it is (tool, version, root, document count, age) and ask, so a
    several-hour index is never discarded blindly."""
    from cppgraph.scip_introspect import describe_scip

    info = describe_scip(out_scip)
    if not info.get("exists"):
        return True
    if "error" in info:
        p.note("", f"An index exists at {out_scip} but could not be read: {info['error']}")
    else:
        p.panel(
            "Existing index",
            [
                ("path", str(out_scip)),
                ("tool", f"{info.get('tool_name') or 'unknown'} {info.get('tool_version') or '?'}"),
                ("documents", str(info.get("document_count"))),
                ("project root", info.get("project_root") or "?"),
                ("generated", info.get("mtime_iso", "?")),
            ],
        )
    # Default to reusing the existing index (recomputing can take hours); when the
    # caller asked --from-scratch, default the other way.
    return p.confirm("Recompute the index (.scip)?", from_scratch)
