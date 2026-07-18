"""Summarize a `compile_commands.json` before indexing.

The compdb is the input to `scip-clang`; its `file` entries are the translation
units that *would* be indexed. Before committing to a (heavy) index, an agent —
or a human — wants to see what's in there and choose a scope: the whole thing, a
subtree, with or without tests. This module produces that breakdown (total TUs,
where they live, how many are tests) so the choice is informed, not blind.

The grouping is prefix-based and robust to absolute/build-system paths: it strips
the longest common directory prefix (e.g. a Bazel `execroot/.../bin/`) so the
*meaningful* structure (`src/foo`, `src/third_party`, `external/…`) surfaces.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from cppgraph.export import is_test_file


@dataclass
class CompdbSummary:
    total: int
    tests: int
    common_prefix: str
    groups: list[tuple[str, int, int]] = field(default_factory=list)  # (prefix, TUs, tests)
    # Present only when a filter substring was given.
    filter: str | None = None
    matched: int = 0
    matched_tests: int = 0


def load_compdb(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of compile-command entries")
    return data


def _files(entries: list[dict]) -> list[str]:
    return [e["file"] for e in entries if isinstance(e, dict) and e.get("file")]


def _strip_common(files: list[str]) -> tuple[str, list[str]]:
    """Return (common_prefix, paths_relative_to_it). Falls back to no stripping
    when paths don't share one (mixed absolute/relative or roots)."""
    if len(files) < 2:
        return "", files
    try:
        common = os.path.commonpath(files)
    except ValueError:
        return "", files
    if not common or common in (os.sep, "."):
        return "", files
    rel = [os.path.relpath(f, common) for f in files]
    return common, rel


def _group_key(rel_path: str, depth: int) -> str:
    parts = [p for p in rel_path.split(os.sep) if p and p != "."]
    # A file's own name isn't a group; drop it so we group by directory.
    if len(parts) > 1:
        parts = parts[:-1]
    return os.sep.join(parts[:depth]) or "."


def summarize_compdb(
    entries: list[dict], *, filter: str | None = None, depth: int = 2, top: int = 20
) -> CompdbSummary:
    """Breakdown of a loaded compdb. `filter` (a path substring, the same kind
    `reindex.sh` takes) previews how many TUs it would keep."""
    files = _files(entries)
    total = len(files)
    tests = sum(1 for f in files if is_test_file(f))

    common, rel = _strip_common(files)
    counts: dict[str, list[int]] = {}
    for f, r in zip(files, rel):
        key = _group_key(r, depth)
        slot = counts.setdefault(key, [0, 0])
        slot[0] += 1
        if is_test_file(f):
            slot[1] += 1
    groups = sorted(((k, v[0], v[1]) for k, v in counts.items()), key=lambda g: g[1], reverse=True)[
        :top
    ]

    summary = CompdbSummary(
        total=total, tests=tests, common_prefix=common, groups=groups, filter=filter
    )
    if filter:
        matched = [f for f in files if filter in f]
        summary.matched = len(matched)
        summary.matched_tests = sum(1 for f in matched if is_test_file(f))
    return summary


def format_summary(s: CompdbSummary) -> str:
    lines: list[str] = []
    lines.append(f"compile_commands.json: {s.total} translation unit(s), {s.tests} test(s)")
    if s.common_prefix:
        lines.append(f"  (paths under {s.common_prefix}/)")
    if s.groups:
        width = max(len(k) for k, _, _ in s.groups)
        lines.append("")
        lines.append(f"  {'subtree'.ljust(width)}   TUs   (tests)")
        for key, n, t in s.groups:
            lines.append(f"  {key.ljust(width)}  {n:>5}   {t:>5}")
    if s.filter:
        lines.append("")
        lines.append(
            f"  filter {s.filter!r}: keeps {s.matched} of {s.total} TU(s) "
            f"({s.matched_tests} of them test(s))"
        )
    return "\n".join(lines)
