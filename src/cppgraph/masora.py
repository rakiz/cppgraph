"""Masora fact injection — the cppgraph side of the integration contract
(documented in masora's `docs/CPPGRAPH_INTEGRATION.md`, contract shape v1).

Masora is a git-backed knowledge base of claims anchored to code symbols.
When a query response is centered on symbol S and the feature is enabled,
cppgraph spawns `masora facts --repo <checkout-root> --symbol <S>` (one
subprocess per response, 2 s wall-clock budget; the child's stdin is
DETACHED — when cppgraph runs as the MCP stdio server, fd 0 is the JSON-RPC
stream — and its stdout is capped, overflow skipping the response), parses
the JSON document, and appends at most 2 terse fact lines to the response —
on both the CLI (printed text lines) and the MCP server (the `masora` string
field, rendered lines joined with newlines; absent when nothing injects).

Feature flag `CPPGRAPH_MASORA` (checked before any spawn):
- unset → OFF (the default until the integration is validated);
- `1` or `true` (case-insensitive) → ON;
- `0`, `false`, or anything else → OFF (fail-closed kill switch).
When ON, detection is `shutil.which("masora")`; a missing binary is an
instant silent skip (no subprocess, no latency, no error).

Rendering decisions (§6 latitude, pinned here):
- statuses surface as labels: resolution first (`current` / `stale` /
  `restored` / `unknown` verbatim), then `verified(<source>)` when present —
  `unverified` renders as nothing, absence of a verify is not evidence —
  then each flag verbatim; `stale` carries a trailing `— re-check`,
  `restored` a trailing `— re-verify`.
- an unknown `resolution` VALUE renders verbatim: statuses are evidence
  labels (§6), and a future contract value arriving without a `contract_version`
  bump is Masora's shape drift to report, not to guess at — fail-closed
  rejection is reserved for shape violations, not new labels.
- confidence: the §6 trust-rendering matrix is PINNED state-form and is the
  law of rendering — the verification label IS the signal and renders BARE
  (`verified(human)` / `verified(llm)` / `verified(graph)` verbatim; "nothing"
  never strips a label); `unverified` renders as nothing. The ONE
  interpretive token, and only on a `verified(llm)` fact: `effort: "low"` →
  `low-effort` (a low-effort LLM verification is the weakest trust state a
  verified label can carry); medium/high/null are silence — a high effort is
  not a distrust signal, just the absence of alarm. It NEVER renders under
  `verified(human)` (a human verification answers for the content) nor
  `verified(graph)` (the verify's currency is the resolution axis's job:
  current/stale/suspect) nor alongside `unverified` (effort qualifies the
  LLM's verification quality, so it is contract-wise always null there).
  The `masora NOT:` line carries no confidence token ever — a refutation is
  not softened by the writer's effort.
- `resolution: "none"` (every version refuted) is negative knowledge:
  `masora NOT: <summary> [refuted]` — a label, never advice; no fact line is
  ever turned into an instruction.
- a `stale_warning: true` index renders the terse note
  `masora: [stale index — facts may be outdated]` before the fact lines —
  but only when at least one fact renders: zero matching facts injects
  nothing, stale index or not.
- token budget: ≤ 60 tokens total, estimated at ≈ 4 chars/token with CJK
  (wide/fullwidth) chars counted ≈ 1 token each — a prose heuristic, no
  tokenizer dependency; when a line does not fit and at least one fact line
  is already rendered, it is dropped and the cap is reported visibly
  (`… +N more — masora search`) — the visible cap wins over the suggested
  budget, never a silent drop. At least one fact always renders.
- duplicate `lineage` ids are deduped (first occurrence wins).

Contract enrichment (still `contract_version: 1`): each fact may carry
`source` (`human|llm|graph` or null), `name` (str or null), `effort`
(`low|medium|high` or null) and `anchors` (list of str). All four are
ADDITIVE-OPTIONAL — absent or null defaults, so pre-enrichment documents
render exactly as before; a wrong TYPE rejects the whole document (the
fail-closed posture of every other field); unknown enum values (`source`,
`effort`) are tolerated and inert, same posture as an unknown `resolution`
value. `name`, `anchors` (and `anchors_matched`) are validated and never
consumed for rendering.

Contract friction to relay: that enrichment landed IN PLACE under
`contract_version: 1`, although the stated rule is "shape bumps via
contract_version, never in place". cppgraph tolerates it because the new
fields are additive-optional — but the in-place-evolution precedent erodes
the guard the version bump exists to provide; masora has ruled this
tolerated ONCE: any future shape evolution must be a major
`contract_version` bump (cppgraph already rejects unknown majors), so this
bullet is historical record, not an open request.

Zero-change guarantee (§7): any failure mode — missing binary, non-zero
exit, timeout, unparsable output, unknown `contract_version`, any exception —
degrades to "no injection" (`query_lines` returns `[]`), never an error
surfaced to the user. cppgraph is a read-only consumer: it only ever spawns
the command with `--repo` pointing at the checkout the graph was built from
(the store's recorded `project_root`; a legacy graph with no recorded root
falls back to the cwd; a recorded root missing on disk skips injection
entirely rather than risk facts for the wrong checkout), never reads
Masora's config or SQLite, and never writes anywhere Masora owns.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cppgraph.store import project_root_path

ENV_VAR = "CPPGRAPH_MASORA"
CONTRACT_VERSION = 1
FACTS_TIMEOUT_S = 2.0
MAX_FACTS = 2
TOKEN_BUDGET = 60
MAX_SUMMARY_CHARS = 120
MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB — far above any legitimate contract document
STALE_WARNING_LINE = "masora: [stale index — facts may be outdated]"
_TRUNCATION_LINE = "… +{n} more — masora search"

Runner = Callable[[str, str | None, float], str | None]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Fact:
    """One §3 fact, fields the renderer needs; unknown shapes never get here.

    Of the v1 enrichment fields only `effort` is stored (it feeds the
    confidence matrix); `source`, `name`, and `anchors` — like
    `anchors_matched` — are type-validated in `parse_contract` and never
    consumed for rendering.
    """

    lineage: str
    summary: str
    resolution: str
    verification: str
    flags: tuple[str, ...]
    effort: str | None = None


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


def repo_root(meta: Mapping[str, str]) -> str | None:
    """The checkout root handed to `masora --repo`, or None to skip injection:
    the graph's recorded `project_root` when it exists on disk; None when a
    root IS recorded but missing (moved/deleted checkout — facts for a
    different repo would violate §4, so no cwd guess); the cwd only for a
    legacy graph recording no root at all."""
    recorded = meta.get("project_root") if meta else None
    if not recorded:
        return str(Path.cwd())
    path = project_root_path(recorded)
    if path is not None and path.is_dir():
        return str(path)
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group (the child runs in its own session
    via `start_new_session`): a shell wrapper's grandchildren inherit the
    stdout write end and would hold the pipe open past a lone `proc.kill()`,
    stalling the drain for as long as they live. Fallback (no POSIX process
    groups): kill the direct child only."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_masora_facts(repo_root: str, symbol: str | None, timeout: float) -> str | None:
    """Spawn `masora facts --repo <root> [--symbol <scip>]`; return stdout, or
    None on any failure (spawn error, non-zero exit — including the
    binary-missing 127 — timeout: the process TREE is killed and nothing
    renders; or an stdout stream beyond MAX_OUTPUT_BYTES, which is treated as
    unparsable rather than buffered unboundedly). The child's stdin is
    DEVNULL: cppgraph's own fd 0 may be the MCP stdio transport, and a masora
    reading it would eat protocol frames.

    Worst case ~2× the timeout budget on the orphaned-grandchild edge (the
    child exits fast but a grandchild holds the stdout write end, so the
    post-kill `reader.join` re-blocks up to the full timeout): still silent,
    still bounded, still no injection."""
    cmd = ["masora", "facts", "--repo", repo_root]
    if symbol is not None:
        cmd += ["--symbol", symbol]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=hasattr(os, "setsid"),
        )
    except (OSError, ValueError):
        return None
    stream = proc.stdout
    if stream is None:
        _kill_tree(proc)
        proc.wait()
        return None
    chunks: list[bytes] = []
    state = {"total": 0, "overflow": False}

    def _drain() -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                chunks.append(chunk)
                state["total"] += len(chunk)
                if state["total"] > MAX_OUTPUT_BYTES:
                    state["overflow"] = True
                    _kill_tree(proc)
                    proc.wait()
                    return
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.wait()
    reader.join(timeout=timeout)
    if reader.is_alive() or state["overflow"] or proc.returncode != 0:
        # Never block on (or close) a stream a still-draining thread holds —
        # the daemon thread exits at EOF and the fd goes with it.
        return None
    try:
        stream.close()
    except OSError:
        pass
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None


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
        if len(summary) > MAX_SUMMARY_CHARS or not summary.isprintable():
            # §3: "one line, ≤ 120 chars". A newline/control char could
            # smuggle a rendered second line (an instruction — §6 forbids);
            # an overlong summary is a shape violation. Fail closed either way.
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
        # v1 enrichment (additive-optional): absent or null defaults, a wrong
        # TYPE rejects the whole document, an unknown enum VALUE is tolerated
        # and inert — the same posture as an unknown `resolution` value.
        # `name`/`anchors` are validated and never consumed.
        source = raw.get("source")
        if source is not None and not isinstance(source, str):
            return None
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            return None
        effort = raw.get("effort")
        if effort is not None and not isinstance(effort, str):
            return None
        anchors = raw.get("anchors")
        if anchors is None:
            anchors = []
        if not isinstance(anchors, list) or not all(isinstance(a, str) for a in anchors):
            return None
        anchors_matched = raw.get("anchors_matched")
        if anchors_matched is None:
            anchors_matched = []
        if not isinstance(anchors_matched, list) or not all(
            isinstance(a, str) for a in anchors_matched
        ):
            return None
        facts.append(
            Fact(
                lineage=lineage,
                summary=summary,
                resolution=resolution,
                verification=verification,
                flags=flags,
                effort=effort,
            )
        )
    stale = doc.get("stale_warning")
    if stale is not None and not isinstance(stale, bool):
        # Same fail-closed posture as the per-fact fields: present-but-wrong
        # type rejects the document; absent (or null — §3 "not comparable")
        # defaults to None.
        return None
    stale_warning = stale if isinstance(stale, bool) else None
    return Contract(facts=tuple(facts), stale_warning=stale_warning)


def est_tokens(text: str) -> int:
    """The token estimate behind the §6 budget: ≈ 4 chars per token for
    regular text, CJK (wide/fullwidth) chars counted ≈ 1 token each (they
    tokenize far denser than the 4-chars rule assumes) — a prose heuristic,
    deliberately dependency-free."""
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    return max(1, cjk + math.ceil((len(text) - cjk) / 4))


def _confidence_label(fact: Fact) -> str | None:
    """The §6 trust-rendering matrix (state-form, pinned) — the law of
    rendering: the verification label IS the signal and renders bare; human
    and graph never take an interpretive token whatever the effort (a human
    verification answers for the content; graph's currency is the resolution
    axis's job), and unverified renders nothing (effort is contract-wise
    always null there — it qualifies the LLM's verification quality). The
    ONE token, only for a `verified(llm)` fact with `effort: "low"`:
    `low-effort` — the weakest trust state a verified label can carry.
    NOT-line facts never reach here — `_fact_line` returns before the
    confidence is asked."""
    if fact.verification == "verified(llm)" and fact.effort == "low":
        return "low-effort"
    return None


def _fact_line(fact: Fact) -> str:
    if fact.resolution == "none":
        # No confidence signal on the NOT line: a refutation is not softened
        # by the writer's trust.
        return f"masora NOT: {fact.summary} [refuted]"
    labels = [fact.resolution]
    if fact.verification and fact.verification != "unverified":
        labels.append(fact.verification)
    confidence = _confidence_label(fact)
    if confidence is not None:
        labels.append(confidence)
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
    try:
        root = repo_root(meta)
        if root is None:
            return []
        stdout = runner(root, symbol, timeout)
        if stdout is None:
            return []
        contract = parse_contract(stdout)
        if contract is None:
            return []
        return render_lines(contract)
    except Exception:
        return []
