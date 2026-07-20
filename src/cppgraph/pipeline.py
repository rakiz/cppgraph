"""The index pipeline: compdb filter -> scip-clang -> graph store.

The three stages run in-process (the graph build/update calls are Python;
scip-clang is the one external binary shelled out to). Every stage is a small,
testable function that takes an explicit *decision* (recompute this artifact?
rebuild that one?) rather than prompting — the interactive asking lives in
`init.py`, so a corrupt `.scip` or a several-hour index is never overwritten
without the caller having said so.

Per-project outputs live in the target project's own `.cppgraph/`:
  <name>.compdb.json   filtered compile_commands.json subset
  <name>.scip          scip-clang index
  <name>.graph.db      cppgraph store (interned SQLite)
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path

from cppgraph.builder import build_graph
from cppgraph.export import is_test_file
from cppgraph.init import scip_clang_bin_dir, scip_clang_info
from cppgraph.proto import scip_pb2
from cppgraph.store import GraphStore, build_provenance, update_store, write_sqlite

_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx", ".ipp", ".inl")


class PipelineError(RuntimeError):
    """A stage could not proceed (missing binary, empty filter, …). Carries a
    user-facing message; callers print it and return non-zero."""


def prepare_out_dir(project_root: Path) -> Path:
    """The project's `.cppgraph/` dir, created and gitignored (a `.gitignore` of
    `*` so the artifacts never dirty the repo)."""
    d = project_root / ".cppgraph"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.is_file():
        gi.write_text("*\n")
    return d


def num_jobs() -> int:
    try:
        return max(multiprocessing.cpu_count(), 1)
    except NotImplementedError:
        return 4


def scip_clang_path() -> Path:
    return scip_clang_bin_dir() / "scip-clang"


def filter_compdb(
    compdb: Path, out_compdb: Path, src_filter: str, no_tests: bool
) -> tuple[int, int, int]:
    """Write the filtered compdb subset. Returns `(kept, total, dropped_tests)`.

    Plain substring match on each entry's `file` (no anchoring — tolerates a compdb
    that mixes absolute and bare-relative paths for logically equivalent locations).
    Raises `PipelineError` if nothing survives the filter."""
    data = json.loads(compdb.read_text())
    filtered = [e for e in data if src_filter in e["file"]] if src_filter else list(data)
    dropped = 0
    if no_tests:
        kept = [e for e in filtered if not is_test_file(e.get("file", ""))]
        dropped = len(filtered) - len(kept)
        filtered = kept
    if not filtered:
        raise PipelineError(
            f"0 entries left after filtering (substring {src_filter!r}"
            f"{', --no-tests' if no_tests else ''}). The filter is a plain substring of "
            f"each entry's \"file\" field (e.g. 'src/', no leading slash). Check a sample "
            f"path in {compdb} and adjust."
        )
    out_compdb.write_text(json.dumps(filtered))
    return len(filtered), len(data), dropped


def git_head(project_root: Path) -> tuple[str | None, bool]:
    """`(commit, dirty)` for `project_root`, best-effort. `(None, False)` when it
    isn't a git checkout — cppgraph stays general, not git-only."""
    try:
        head = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, False
    if head.returncode != 0:
        return None, False
    commit = head.stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(project_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    dirty = bool(status.stdout.strip())
    return commit, dirty


def run_scip_clang(project_root: Path, compdb: Path, out_scip: Path, *, print_fn=print) -> None:
    """Index `compdb` into `out_scip` by running the native scip-clang from
    `project_root` (its cwd sets the project_root recorded in the index). Raises
    `PipelineError` if the binary is absent or the run fails."""
    binary = scip_clang_path()
    if not os.access(binary, os.X_OK):
        raise PipelineError(
            "no native scip-clang on this platform, and no prebuilt index to reuse.\n"
            "Generate one in a container, then rerun (it will pick the index up):\n"
            f"  scripts/index-in-container.sh {compdb} '' {out_scip.stem} {project_root}"
        )
    jobs = num_jobs()
    print_fn(f"  running scip-clang (-j {jobs}, cwd={project_root}) ...")
    proc = subprocess.run(
        [
            str(binary),
            "--compdb-path",
            str(compdb),
            "--index-output-path",
            str(out_scip),
            "-j",
            str(jobs),
            "--no-progress-report",
        ],
        cwd=str(project_root),
    )
    if proc.returncode != 0:
        raise PipelineError(f"scip-clang exited with status {proc.returncode}")


def build_store(
    scip_path: Path,
    out_graph: Path,
    *,
    attributed_refs: bool,
    src_filter: str,
    no_tests: bool,
    source_commit: str | None,
    source_dirty: bool,
    scip_variant: str | None,
    print_fn=print,
) -> None:
    """Build the SQLite store from `scip_path`, recording index scope + git
    provenance in `meta`."""
    index = scip_pb2.Index()
    with open(scip_path, "rb") as f:
        index.ParseFromString(f.read())
    graph = build_graph(index, attribute_references=attributed_refs)
    meta = build_provenance(
        index,
        source_commit=source_commit,
        source_dirty=source_dirty or None,
        scip_variant=scip_variant,
        index_filter=src_filter,  # always recorded (empty = whole tree)
        index_excludes_tests=no_tests,
    )
    write_sqlite(graph, out_graph, meta=meta)
    attributed = sum(1 for r in graph.references if r.enclosing_symbol)
    refs_note = f", {len(graph.references)} refs" if graph.references else ""
    if attributed:
        refs_note += f" ({attributed} attributed)"
    print_fn(
        f"  built graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges{refs_note} "
        f"-> {out_graph}"
    )


def full_build(
    *,
    compdb: Path,
    project_root: Path,
    name: str,
    src_filter: str,
    no_tests: bool,
    attributed_refs: bool,
    recompute_scip: bool,
    rebuild_graph: bool,
    print_fn=print,
) -> int:
    """Run the full pipeline. `recompute_scip`/`rebuild_graph` are the caller's
    decisions: an existing `.scip`/`.graph.db` is reused (never overwritten) unless
    the corresponding flag is True. Returns a process-style exit code."""
    out_dir = prepare_out_dir(project_root)
    out_compdb = out_dir / f"{name}.compdb.json"
    out_scip = out_dir / f"{name}.scip"
    out_graph = out_dir / f"{name}.graph.db"

    present, variant = scip_clang_info()
    # Attribution needs a #504 binary that emits enclosing_range; drop it otherwise.
    attr = bool(attributed_refs) and variant == "enclosing_range-504"
    if attributed_refs and not attr:
        print_fn(
            "  warning: --attributed-refs requested but the local scip-clang is not a "
            "#504 build; producing file-granularity usage instead."
        )

    # [1/3] filter compdb (derived + deterministic — always regenerated).
    tests_note = ", excluding tests" if no_tests else ""
    print_fn(f"[1/3] Filtering compile_commands.json ('{src_filter or '<all>'}'{tests_note}) ...")
    try:
        kept, total, dropped = filter_compdb(compdb, out_compdb, src_filter, no_tests)
    except PipelineError as e:
        print_fn(f"  error: {e}")
        return 1
    msg = f"  {kept} of {total} compdb entries matched"
    if no_tests:
        msg += f" ({dropped} test TU(s) excluded)"
    print_fn(msg)

    source_commit, source_dirty = git_head(project_root)
    if source_commit:
        print_fn(f"  source commit: {source_commit}{' (dirty)' if source_dirty else ''}")

    # [2/3] scip index — reuse unless the caller chose to recompute.
    if out_scip.is_file() and not recompute_scip:
        print_fn(f"[2/3] Reusing existing index: {out_scip}")
    else:
        print_fn("[2/3] Running scip-clang ...")
        try:
            run_scip_clang(project_root, out_compdb, out_scip, print_fn=print_fn)
        except PipelineError as e:
            print_fn(f"  error: {e}")
            return 1

    # [3/3] graph store — reuse unless the caller chose to rebuild.
    if out_graph.is_file() and not rebuild_graph:
        print_fn(f"[3/3] Reusing existing graph: {out_graph}")
        return 0
    print_fn("[3/3] Building the cppgraph graph ...")
    build_store(
        out_scip,
        out_graph,
        attributed_refs=attr,
        src_filter=src_filter,
        no_tests=no_tests,
        source_commit=source_commit,
        source_dirty=source_dirty,
        scip_variant=variant if present else None,
        print_fn=print_fn,
    )
    print_fn(f"Done. Graph: {out_graph}")
    return 0


def _git_diff_names(project_root: Path, base_commit: str, diff_filter: str) -> list[str]:
    """Paths changed between `base_commit` and the working tree, `--diff-filter`
    selecting the change kinds (e.g. 'd' = exclude deletions, 'D' = only deletions)."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "diff",
            "--name-only",
            f"--diff-filter={diff_filter}",
            base_commit,
            "--",
        ],
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def incremental_update(
    *,
    graph_db: Path,
    compdb: Path,
    project_root: Path,
    print_fn=print,
) -> int:
    """Re-index only the translation units that changed since the store was built,
    and apply them in place. The changed-file set comes from the store's provenance:
    it diffs `meta.source_commit` against the current working tree. The update stays
    within the graph's recorded scope (subtree filter + tests state)."""
    if not os.access(scip_clang_path(), os.X_OK):
        print_fn(
            "  error: incremental update needs a native scip-clang, absent on this "
            "platform. Do a full rebuild (it can reuse a container-built .scip)."
        )
        return 1

    out_dir = prepare_out_dir(project_root)
    name = graph_db.name
    for suffix in (".graph.db", ".db"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    part_compdb = out_dir / f"{name}.partial.compdb.json"
    part_scip = out_dir / f"{name}.partial.scip"

    store = GraphStore(graph_db)
    try:
        meta = store.meta()
    finally:
        store.close()
    base_commit = meta.get("source_commit", "")
    if not base_commit:
        print_fn(
            "  error: the store has no meta.source_commit — can't diff for an update. "
            "Rebuild it with git provenance, or do a full build."
        )
        return 1
    src_filter = meta.get("index_filter", "")
    no_tests = meta.get("index_tests") == "excluded"
    tests_state = meta.get("index_tests", "?")
    print_fn(f"  indexed scope: {src_filter or '<whole tree>'} (tests {tests_state})")
    print_fn(f"[1/4] Diffing working tree against stored commit {base_commit} ...")

    changed = _git_diff_names(project_root, base_commit, "d")
    deleted = _git_diff_names(project_root, base_commit, "D")
    if src_filter:
        changed = [c for c in changed if src_filter in c]
        deleted = [c for c in deleted if src_filter in c]
    if not changed and not deleted:
        print_fn(f"  nothing changed since {base_commit} — store is up to date.")
        return 0
    print_fn(f"  changed: {len(changed)} file(s), deleted: {len(deleted)} file(s)")
    if any(c.endswith(_HEADER_EXTS) for c in changed):
        print_fn(
            "  WARNING: the diff contains header files. Headers are only refreshed when "
            "a re-indexed TU includes them; a widely-included header change may not fully "
            "propagate. Consider a full rebuild."
        )

    print_fn("[2/4] Filtering compile_commands.json to the changed TUs ...")
    data = json.loads(compdb.read_text())
    matched = [e for e in data if any(c in e["file"] for c in changed)]
    if no_tests:
        matched = [e for e in matched if not is_test_file(e.get("file", ""))]
    part_compdb.write_text(json.dumps(matched))
    excl = " (test TUs excluded per recorded scope)" if no_tests else ""
    print_fn(f"  {len(matched)} changed TU(s) matched in the compdb{excl}")

    print_fn("[3/4] Re-indexing the changed TUs ...")
    if matched:
        try:
            run_scip_clang(project_root, part_compdb, part_scip, print_fn=print_fn)
        except PipelineError as e:
            print_fn(f"  error: {e}")
            return 1
    else:
        # Deletions only (or headers with no matching TU): an empty partial index
        # still lets `update` drop the deleted files' stale contributions.
        print_fn("  no TU to re-index; writing an empty partial index for deletions.")
        empty = scip_pb2.Index()
        empty.metadata.project_root = f"file://{project_root}"
        part_scip.write_bytes(empty.SerializeToString())

    print_fn(f"[4/4] Applying the partial re-index to {graph_db} ...")
    partial = scip_pb2.Index()
    with open(part_scip, "rb") as f:
        partial.ParseFromString(f.read())
    new_commit, new_dirty = git_head(project_root)
    _present, variant = scip_clang_info()
    upd_meta = build_provenance(
        partial,
        source_commit=new_commit,
        source_dirty=new_dirty or None,
        scip_variant=variant,
    )
    stats = update_store(graph_db, partial, deleted_files=deleted, meta=upd_meta)
    print_fn(
        f"  updated {stats.files_changed} file(s): -{stats.edges_removed}/+{stats.edges_added} "
        f"edges -> {stats.node_count} nodes, {stats.edge_count} edges"
    )
    print_fn(f"Done. {graph_db} (updated in place)")
    return 0
