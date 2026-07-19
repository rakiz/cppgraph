"""Guided, deterministic onboarding: `cppgraph init`.

Onboarding used to be "an LLM reads the README and drives `reindex.sh`" — which
is non-deterministic: each agent improvises the questions and can pick the wrong
scope or forget `--no-tests`. This module asks the *same* questions in the *same
order*, each with the information needed to choose well, then runs the existing
pipeline. It works without an LLM.

Design:
- **Thin front-end, not a second pipeline.** It reuses `summarize_compdb` /
  `is_test_file` for the breakdown and *calls `scripts/reindex.sh`* to do the
  actual work — no duplicated indexing logic.
- **Pure helpers** (`find_compdb`, `scip_clang_info`, `out_dir_for`,
  `artifact_status`, `build_reindex_argv`) carry the decisions and are unit-tested
  without any I/O; `run_init` is the thin interactive driver (input/print
  injectable, so the whole flow is scriptable in tests).
- **Resumable via artifact detection.** The pipeline writes named artifacts per
  stage (`<name>.compdb.json` → `<name>.scip` → `<name>.graph.db`); the wizard
  infers where a previous run stopped from which files exist and offers to resume
  rather than blindly redo.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cppgraph.compdb import CompdbSummary, format_summary, load_compdb, summarize_compdb

_COMPDB_NAME = "compile_commands.json"


@dataclass
class IndexPlan:
    """The concrete scope decisions the wizard collected — everything needed to
    assemble the `reindex.sh` invocation."""

    compdb: Path
    project_root: Path
    name: str
    src_filter: str  # "" = whole tree
    no_tests: bool
    attributed_refs: bool


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
    """The per-machine scip-clang bin dir, mirroring `reindex.sh`:
    `$CPPGRAPH_BIN_DIR`, else `${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin`."""
    override = os.environ.get("CPPGRAPH_BIN_DIR")
    if override:
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "cppgraph" / "bin"


def scip_clang_info(bin_dir: Path | None = None) -> tuple[bool, str | None]:
    """`(binary_present, variant)` for the local scip-clang.

    `variant` comes from the `scip-clang.json` provenance sidecar next to the
    binary (the same file `reindex.sh` reads) — `"stock"`, `"enclosing_range-504"`,
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
    """The project's `.cppgraph/` output dir (mirrors `reindex.sh:out_dir_for`,
    minus the side effects — this is a pure path)."""
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


def reindex_script() -> Path | None:
    """The `scripts/reindex.sh` in this cppgraph checkout, or None if not found
    (e.g. an unusual install layout — the wizard then just prints the command)."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "reindex.sh"
    return script if script.is_file() else None


def build_reindex_argv(script: Path, plan: IndexPlan) -> list[str]:
    """The full `reindex.sh` command for a full build with `plan`'s scope.

    Leading flags first (as reindex.sh parses them), then the positional args
    `COMPDB [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]`. The filter is always passed
    (empty string = whole tree) so the later positionals keep their slots.
    """
    argv = [str(script)]
    if plan.attributed_refs:
        argv.append("--attributed-refs")
    if plan.no_tests:
        argv.append("--no-tests")
    argv += [str(plan.compdb), plan.src_filter, plan.name, str(plan.project_root)]
    return argv


def build_update_argv(script: Path, graph_db: Path, compdb: Path) -> list[str]:
    """The `reindex.sh --update` command — reuses the graph's recorded scope."""
    return [str(script), "--update", str(graph_db), str(compdb)]


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
    project_root_path = (
        Path(project_root).resolve() if project_root else compdb_path.resolve().parent
    )
    graph_name = name or default_name(project_root_path)
    return compdb_path, entries, project_root_path, graph_name


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
    status = artifact_status(out_dir_for(project_root_path), graph_name)
    total = summary.total
    tests_pct = round(100 * summary.tests / total) if total else 0
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
        "questions": [
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
    (mirrors reindex.sh). Used by the non-interactive path."""
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


def _ask(prompt: str, default: str, input_fn, print_fn) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input_fn(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _ask_yes_no(prompt: str, default: bool, input_fn, print_fn) -> bool:
    d = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} ({d})", "", input_fn, print_fn).lower()
    if not raw:
        return default
    return raw in ("y", "yes", "o", "oui")


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
    input_fn=input,
    print_fn=print,
) -> int:
    """`cppgraph init`. Returns a process exit code.

    Two ways in off one code path:
    - **interactive** (default): ask the scope questions on stdin;
    - **non-interactive** (`non_interactive=True`, or implied by a `filter`): take
      the scope from `filter`/`no_tests`/`attributed_refs`, no prompts — the form
      an LLM drives after gathering answers in its own UI.

    `run`: True = run the assembled command, False = print only, None = ask (and in
    non-interactive mode, None means print only). `input_fn`/`print_fn` are injected
    in tests to script the whole flow.
    """
    # A provided --filter is a clear non-interactive intent.
    non_interactive = non_interactive or filter is not None

    resolved = _resolve_targets(compdb, project_root, name, announce=True, print_fn=print_fn)
    if resolved is None:
        return 1
    compdb_path, entries, project_root_path, graph_name = resolved

    # Show the breakdown so the scope choice is informed, not blind.
    summary: CompdbSummary = summarize_compdb(entries)
    print_fn("")
    print_fn(format_summary(summary))
    print_fn("")

    script = reindex_script()
    out_dir = out_dir_for(project_root_path)
    status = artifact_status(out_dir, graph_name)

    if non_interactive:
        if status["graph"]:
            print_fn(
                f"Note: rebuilding the existing graph "
                f"({out_dir / f'{graph_name}.graph.db'}) with the given scope."
            )
        src_filter = filter or ""
        chosen_no_tests = bool(no_tests)
        chosen_attributed = _gate_attribution(attributed_refs, print_fn)
        if run is None:
            run = False  # never auto-run a heavy job without an explicit --run
    else:
        # Resume: if a graph already exists, offer update vs rebuild.
        if status["graph"]:
            graph_db = out_dir / f"{graph_name}.graph.db"
            print_fn(f"A graph already exists: {graph_db}")
            choice = _ask(
                "[u]pdate incrementally / [r]ebuild from scratch / [q]uit",
                "u",
                input_fn,
                print_fn,
            ).lower()
            if choice.startswith("q"):
                return 0
            if choice.startswith("u"):
                if script is None:
                    print_fn("reindex.sh not found; run: cppgraph … (see docs)")
                    return 1
                cmd = build_update_argv(script, graph_db, compdb_path)
                return _finish(cmd, run, input_fn, print_fn)
            # else fall through to a full rebuild
        elif status["scip"]:
            print_fn(
                f"Note: a partial index ({graph_name}.scip) already exists; reindex.sh "
                "will reuse it when no native scip-clang is present, or refresh it otherwise."
            )
        # Scope questions, in order, each with the info to choose well.
        src_filter = _ask_filter(entries, summary, input_fn, print_fn)
        chosen_no_tests = _ask_no_tests(entries, src_filter, input_fn, print_fn)
        chosen_attributed = _ask_attributed(input_fn, print_fn)

    plan = IndexPlan(
        compdb=compdb_path,
        project_root=project_root_path,
        name=graph_name,
        src_filter=src_filter,
        no_tests=chosen_no_tests,
        attributed_refs=chosen_attributed,
    )

    if script is None:
        print_fn(
            "reindex.sh not found in this checkout; assemble the command from the "
            "chosen scope manually (see QUICKSTART.md)."
        )
        return 1
    cmd = build_reindex_argv(script, plan)
    return _finish(cmd, run, input_fn, print_fn)


def _ask_filter(entries, summary, input_fn, print_fn) -> str:
    """Prompt for a subtree filter, previewing what each candidate keeps until the
    user accepts one. Empty = whole tree."""
    while True:
        f = _ask("Subtree filter (path substring; empty = whole tree)", "", input_fn, print_fn)
        tus, tests = _scoped_counts(entries, f)
        if f:
            if tus == 0:
                print_fn(f"  '{f}' matches nothing — try another substring.")
                continue
            print_fn(f"  '{f}' keeps {tus} of {summary.total} TU(s) ({tests} test(s)).")
        else:
            print_fn(f"  whole tree: {tus} TU(s), {tests} test(s).")
        if _ask_yes_no("Use this scope?", True, input_fn, print_fn):
            return f


def _ask_no_tests(entries, src_filter, input_fn, print_fn) -> bool:
    """Offer to drop tests, with the trade-off stated — never defaulted on."""
    tus, tests = _scoped_counts(entries, src_filter)
    if tests == 0:
        return False
    pct = round(100 * tests / tus) if tus else 0
    print_fn("")
    print_fn(
        f"Tests are {tests} of {tus} TU(s) ({pct}%). Excluding them speeds indexing "
        "by roughly that share, but the graph then can't answer 'which tests "
        "exercise symbol X' (their call sites are gone)."
    )
    return _ask_yes_no("Exclude tests?", False, input_fn, print_fn)


def _ask_attributed(input_fn, print_fn) -> bool:
    """Offer symbol-granularity attribution only when the local binary can produce
    it (#504); otherwise explain why it's unavailable and skip."""
    present, variant = scip_clang_info()
    print_fn("")
    if variant == "enclosing_range-504":
        print_fn(
            "Your scip-clang is a #504 build, so symbol-granularity usage is "
            "available (--attributed-refs): 'where is this type used?' answers with "
            "the functions that use it, not just the files. Larger store (~+23%)."
        )
        return _ask_yes_no("Record symbol-granularity usage?", False, input_fn, print_fn)
    if not present:
        print_fn(
            "Note: no native scip-clang here — reindex.sh will reuse an existing "
            ".scip if present. Symbol-granularity attribution needs a #504 build."
        )
    else:
        print_fn(
            "Your scip-clang is stock (not #504), so symbol-granularity attribution "
            "is unavailable; the graph will use exact file-granularity usage."
        )
    return False


def _finish(cmd: list[str], run: bool | None, input_fn, print_fn) -> int:
    """Show the assembled command, then run it (or not) per `run`:
    True = run, False = print only, None = ask."""
    print_fn("")
    print_fn("Command:")
    print_fn("  " + " ".join(shlex.quote(c) for c in cmd))
    if run is None:
        run = _ask_yes_no("Run it now?", True, input_fn, print_fn)
    if not run:
        print_fn("Not run. Copy the command above to index when ready.")
        return 0
    print_fn("")
    return subprocess.call(cmd)
