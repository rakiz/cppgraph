"""Masora fact injection — the cppgraph side of the integration contract
(documented in masora's `docs/CPPGRAPH_INTEGRATION.md`, contract shape v1).

Masora is a git-backed knowledge base of claims anchored to code symbols.
When a query response is centered on symbol S and the feature is enabled,
cppgraph spawns `masora facts --repo <checkout-root> --symbol <S>` (one
subprocess per response, 2 s wall-clock budget), parses the JSON document on
stdout, and appends at most 2 terse fact lines to the response — on both the
CLI (printed text lines) and the MCP server (the `masora` string field,
rendered lines joined with newlines; absent when nothing injects).

Feature flag `CPPGRAPH_MASORA` (checked before any spawn):
- unset → OFF (the default until the integration is validated);
- `1` or `true` (case-insensitive) → ON;
- `0`, `false`, or anything else → OFF (fail-closed kill switch).
When ON, detection is `shutil.which("masora")`; a missing binary is an
instant silent skip (no subprocess, no latency, no error).

Rendering decisions (§6 latitude, pinned here):
- statuses surface as labels: resolution first (`current` / `stale` /
  `restored` / `unknown` verbatim), then `verified(<actor>)` when present —
  `unverified` renders as nothing, absence of a verify is not evidence —
  then each flag verbatim; `stale` carries a trailing `— re-check`,
  `restored` a trailing `— re-verify`.
- `resolution: "none"` (every version refuted) is negative knowledge:
  `masora NOT: <summary> [refuted]` — a label, never advice; no fact line is
  ever turned into an instruction.
- a `stale_warning: true` index renders the terse note
  `masora: [stale index — facts may be outdated]` before the fact lines —
  but only when at least one fact renders: zero matching facts injects
  nothing, stale index or not.
- token budget: ≤ 60 tokens total, estimated at ≈ 4 chars/token (a prose
  heuristic, no tokenizer dependency); when a line does not fit and at least
  one fact line is already rendered, it is dropped and the cap is reported
  visibly (`… +N more — masora search`) — the visible cap wins over the
  suggested budget, never a silent drop. At least one fact always renders.
- duplicate `lineage` ids are deduped (first occurrence wins).

Zero-change guarantee (§7): any failure mode — missing binary, non-zero
exit, timeout, unparsable output, unknown `contract_version`, any exception —
degrades to "no injection" (`query_lines` returns `[]`), never an error
surfaced to the user. cppgraph is a read-only consumer: it only ever spawns
the command with `--repo` pointing at the checkout the graph was built from
(the store's recorded `project_root`, falling back to the cwd), never reads
Masora's config or SQLite, and never writes anywhere Masora owns.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cppgraph.store import project_root_path

ENV_VAR = "CPPGRAPH_MASORA"
CONTRACT_VERSION = 1
FACTS_TIMEOUT_S = 2.0
MAX_FACTS = 2
TOKEN_BUDGET = 60
STALE_WARNING_LINE = "masora: [stale index — facts may be outdated]"
_TRUNCATION_LINE = "… +{n} more — masora search"

Runner = Callable[[str, str | None, float], str | None]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Fact:
    """One §3 fact, fields the renderer needs; unknown shapes never get here."""

    lineage: str
    summary: str
    resolution: str
    verification: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Contract:
    """The §3 document, parsed. Only contract shape v1 parses to one."""

    facts: tuple[Fact, ...]
    stale_warning: bool | None


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """The `CPPGRAPH_MASORA` flag: unset → off (default until validated),
    `1`/`true` (case-insensitive) → on, anything else (including `0`/`false`)
    → off, fail-closed."""
    raw = (os.environ if env is None else env).get(ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true")


def repo_root(meta: Mapping[str, str]) -> str:
    """The checkout root handed to `masora --repo`: the graph's recorded
    `project_root` when it exists on disk, else the cwd — never anywhere the
    graph was not built from."""
    recorded = meta.get("project_root") if meta else None
    if recorded:
        path = project_root_path(recorded)
        if path is not None and path.is_dir():
            return str(path)
    return str(Path.cwd())


def run_masora_facts(repo_root: str, symbol: str | None, timeout: float) -> str | None:
    """Spawn `masora facts --repo <root> [--symbol <scip>]`; return stdout, or
    None on any failure (spawn error, non-zero exit — including the
    binary-missing 127 — or timeout: the process is killed and nothing renders)."""
    cmd = ["masora", "facts", "--repo", repo_root]
    if symbol is not None:
        cmd += ["--symbol", symbol]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_contract(stdout: str) -> Contract | None:
    """Parse the §3 JSON document; None when it is not a contract-v1 document
    (unparsable, wrong shape, unknown/missing major version) — fail-closed,
    the caller renders nothing. Per-fact fields are equally strict: a present
    field of the wrong type (`summary`, `resolution`, `lineage`, `flags`,
    `verification`, `anchors_matched`) rejects the WHOLE document — no
    partial delivery. Absent (or null) optional fields default instead:
    `lineage` to no-dedup, `flags` to none, `verification` to unrendered,
    `anchors_matched` to empty (it is validated, never consumed — a str item
    list per §3)."""
    try:
        doc = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    version = doc.get("contract_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    if version != CONTRACT_VERSION:
        return None
    raw_facts = doc.get("facts", [])
    if not isinstance(raw_facts, list):
        return None
    facts: list[Fact] = []
    seen: set[str] = set()
    for raw in raw_facts:
        if not isinstance(raw, dict):
            return None
        summary = raw.get("summary")
        resolution = raw.get("resolution")
        if not isinstance(summary, str) or not isinstance(resolution, str):
            return None
        lineage = raw.get("lineage")
        if lineage is None:
            lineage = ""
        if not isinstance(lineage, str):
            return None
        if lineage:
            if lineage in seen:
                continue
            seen.add(lineage)
        raw_flags = raw.get("flags")
        if raw_flags is None:
            raw_flags = "-"
        if not isinstance(raw_flags, str):
            return None
        flags = tuple(f.strip() for f in raw_flags.split(",") if f.strip() and f.strip() != "-")
        verification = raw.get("verification")
        if verification is None:
            verification = ""
        if not isinstance(verification, str):
            return None
        anchors = raw.get("anchors_matched")
        if anchors is None:
            anchors = []
        if not isinstance(anchors, list) or not all(isinstance(a, str) for a in anchors):
            return None
        facts.append(
            Fact(
                lineage=lineage,
                summary=summary,
                resolution=resolution,
                verification=verification,
                flags=flags,
            )
        )
    stale = doc.get("stale_warning")
    stale_warning = stale if isinstance(stale, bool) else None
    return Contract(facts=tuple(facts), stale_warning=stale_warning)


def est_tokens(text: str) -> int:
    """The token estimate behind the §6 budget: ≈ 4 chars per token, the usual
    prose heuristic — deliberately dependency-free."""
    return max(1, math.ceil(len(text) / 4))


def _fact_line(fact: Fact) -> str:
    if fact.resolution == "none":
        return f"masora NOT: {fact.summary} [refuted]"
    labels = [fact.resolution]
    if fact.verification and fact.verification != "unverified":
        labels.append(fact.verification)
    labels.extend(fact.flags)
    rendered = ", ".join(labels)
    if fact.resolution == "stale":
        rendered += " — re-check"
    elif fact.resolution == "restored":
        rendered += " — re-verify"
    return f"masora: {fact.summary} [{rendered}]"


def render_lines(contract: Contract) -> list[str]:
    """§6: at most MAX_FACTS fact lines within the token budget, one terse
    line each, any cap reported visibly; the stale-index note (when present)
    renders first — but only alongside facts: zero matching facts injects
    nothing at all, stale index or not (§1: no output without matching
    facts). At least one fact renders even when it alone exceeds the budget —
    a lone oversized fact is still worth more than a bare cap."""
    lines: list[str] = []
    if contract.stale_warning:
        lines.append(STALE_WARNING_LINE)
    kept = 0
    dropped = 0
    for i, fact in enumerate(contract.facts):
        line = _fact_line(fact)
        over = est_tokens("\n".join([*lines, line])) > TOKEN_BUDGET
        if kept >= MAX_FACTS or (kept and over):
            dropped = len(contract.facts) - i
            break
        lines.append(line)
        kept += 1
    if not kept:
        return []
    if dropped:
        lines.append(_TRUNCATION_LINE.format(n=dropped))
    return lines


def query_lines(
    meta: Mapping[str, str],
    symbol: str | None,
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    runner: Runner = run_masora_facts,
    timeout: float = FACTS_TIMEOUT_S,
) -> list[str]:
    """The entry point query responses call to inject Masora facts: flag
    check → binary detection → one `masora facts` spawn for the resolved
    symbol → contract-v1 parse → §6 render. Returns the rendered lines —
    `[]` whenever the feature is off or nothing injects, and never raises
    (the zero-change guarantee)."""
    if not enabled(env):
        return []
    if which("masora") is None:
        return []
    root = repo_root(meta)
    try:
        stdout = runner(root, symbol, timeout)
        if stdout is None:
            return []
        contract = parse_contract(stdout)
        if contract is None:
            return []
        return render_lines(contract)
    except Exception:
        return []
