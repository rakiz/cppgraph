"""Masora fact injection — the cppgraph side of the integration contract
(documented in masora's `docs/CPPGRAPH_INTEGRATION.md`, contract shape v3).

Masora is a git-backed knowledge base of claims anchored to code symbols.
When a query response is centered on symbol S and the feature is enabled,
cppgraph spawns `masora facts --repo <checkout-root> --symbol <S>` (one
subprocess per response — for the multi-symbol responses `find`/`outline`,
ONE batched spawn carrying every result symbol as repeated `--symbol`
flags, §9.10; 2 s wall-clock budget; the child's stdin is
DETACHED — when cppgraph runs as the MCP stdio server, fd 0 is the JSON-RPC
stream — and its stdout is capped, overflow skipping the response), parses
the JSON document, and appends at most 2 terse fact lines to the response —
on both the CLI (printed text lines) and the MCP server (the `masora` string
field, rendered lines joined with newlines; absent when nothing injects).
On a zero-fact response the rendering is DIFFERENTIATED (§6, v3): an empty
base renders masora's own `presence_hint` VERBATIM (masora owns the wording
— cppgraph never hardcodes it), a filtered no-match against a non-empty
base renders the cppgraph-derived staleness line from the counters — both
capability/absence notices, never knowledge or advice.

Feature flag `CPPGRAPH_MASORA` (checked before any spawn):
- unset → OFF (the default until the integration is validated);
- `1` or `true` (case-insensitive) → ON;
- `0`, `false`, or anything else → OFF (fail-closed kill switch).
When ON, detection is `shutil.which("masora")`; a missing binary is an
instant silent skip (no subprocess, no latency, no error).

Fact count `CPPGRAPH_MASORA_MAX_FACTS` (§9.8b): the at-most count of rendered
fact lines. Unset → the default MAX_FACTS (2); any integer ≥ 1 is accepted
(higher is allowed); unset-mangling — non-integer, empty, or < 1 — silently
uses the default, never an error (a config typo is a zero-change case, like
every other failure here). The ≤ 60-token budget guidance STANDS: it wins
over the fact count, so a higher count renders fewer lines plus the visible
truncation line whenever the budget runs out first.

Contract shape v3 (the current shape; NO dual-version window — masora
ruled at v2 "v2 or nothing" and kept the posture, so cppgraph accepts
exactly `contract_version: 3` and rejects v2 and every other major like
any unknown version — an older producer gets the "update masora"
advisory): each fact REQUIRES the four context stamps —
`established_relation` (`str | null`; the known enum is
`in_line | ahead | out_of_line | unknown`, and an unrecognized string
value is parser-inert), `established_commit` (`str | null`, short 12 hex
chars, presentation-only — a pointer for the rendered context, never an
input to any comparison), `off_version` (a STRICT `bool` — a JSON int/str
is a violation) and `context_ordering` (`str`; known enum
`exact | degraded`, an unrecognized value is parser-inert) — plus the v3
`anchor_leaf` (`str | null`; the SHORT leaf masora derived from the first
matched anchor identity, a RENDERING INPUT for §6's multi-symbol rule;
null when no `--symbol` was passed). A fact MISSING any of the five is a
shape violation → the WHOLE document rejects, fail-closed as every other
shape error; explicit `null` is accepted for the three nullable ones
(`established_relation`, `established_commit`, `anchor_leaf`) but
`context_ordering` null is a violation. The DOCUMENT likewise REQUIRES
the v3 fields: `lineages_examined` / `lineages_matched` (STRICT ints — a
JSON bool is a violation) and `presence_hint` (`str | null`; masora owns
the wording — cppgraph renders it verbatim and never hardcodes it).

Context rendering (§6 — "Masora evaluates, cppgraph renders"; the stamps
are the trigger, cppgraph never computes git state): an indented context
line renders under a fact line IFF ANY trigger fires — `off_version` is
true; or `established_relation` is not null and not `in_line` (`ahead`,
`out_of_line`, `unknown`, or an unrecognized value); or `context_ordering`
is `degraded` (an unrecognized ordering value does NOT trigger). A null
`established_relation` never triggers alone — its phrase, "context
unproven", renders only when another trigger fired. ONE exception,
short-circuited BEFORE the trigger test: a fact with `resolution: "none"`
renders NO context line at all (nothing displays on a refuted lineage; the
NOT: envelope already carries the negative knowledge). The line itself:
two-space indent + `context: `, then in order `off-version` (when
off_version), the relation phrase — `ahead` → "established ahead of this
checkout", `out_of_line` → "established on another line", `unknown`/null →
"context unproven", an unrecognized value → `established relation <value>`
verbatim in the neutral forward-compatibility template, never mapped onto
a pinned phrase, never guessed — with the short `established_commit`
beside the phrase as `(12-hex)` when non-null, then `selection ordering
degraded` (when degraded), the parts joined with `" — "` (a single part
renders alone). The stamps never gate, upgrade or downgrade a status
label, never reorder, filter or drop a fact, never join the dedup key
(that stays `lineage`), and no branch name is ever rendered — the contract
carries none. A fact line and its context line are ATOMIC in the budget
and dedup logic: they render together or drop together, and the context
line counts in the 60-token estimate (`est_tokens` runs over the joined
text).

Version-mismatch advisory — a deliberate carve-out from the silent skip:
after a successful spawn, a stdout that parses as a JSON dict whose
`contract_version` is a strict integer (a JSON bool is invalid even where
the language conflates bool and int) OTHER than 3 returns exactly ONE
advisory line instead of facts — newer: `masora: [facts contract v{n}`
unsupported — update cppgraph]`; older: `masora: [facts contract v{n} —
update masora]`. Every other failure mode stays silent. Contract friction
to relay to masora: the contract's letter says an unknown major version
"renders nothing" — cppgraph renders no FACTS from an unknown shape (that
posture is unchanged), but surfaces the toolchain mismatch itself, one
terse line, because a silent skip is indistinguishable from "no facts" and
would strand a user on a stale toolchain with no signal at all.

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
- the multi-symbol leaf suffix (§6, v3): on a MULTI-symbol response
  (`find`/`outline` batched spawn), a fact with a non-null `anchor_leaf`
  renders `masora: <summary> [status] — <anchor_leaf>` — the leaf
  disambiguates WHICH queried symbol the fact answers for (masora derives
  it because masora owns the identity format; cppgraph renders it, never
  re-derives it). A null leaf renders the plain form; single-symbol
  responses are unchanged (no suffix whatever the leaf); the NOT: envelope
  takes the suffix too ("each fact"). The suffix is part of the fact line —
  it counts in the token estimate and the atomic block rule.
- the zero-fact rendering is DIFFERENTIATED by the counters (§6, v3) —
  masora is PRESENT for the checkout (the flag on, the binary found, the
  root resolved, the contract parsed) and ZERO facts rendered, in either
  flavor: `lineages_examined == 0` (empty base) → masora's `presence_hint`
  renders VERBATIM — the exact string masora shipped, cppgraph never
  hardcodes the wording (a tool rename is a masora-side change, never a
  cppgraph literal drift); `lineages_examined > 0` (a filtered no-match
  against a non-empty base) → the cppgraph-derived staleness line
  `<n> lineage(s) examined, none matched — the base may predate this
  rebuild.`, derived from the counters, naming NO tools. Either line
  renders at most once per response, whatever the number of batched
  symbols, and both are capability/absence notices, NEVER knowledge and
  NEVER code advice — no claim, no status, no context. The counters are
  the trigger: a drifted non-null hint does not render once lineages were
  examined, and a null hint at zero examined stays silent (cppgraph never
  invents capability wording). The absent modes render nothing — not even
  a hint: flag off, binary missing, unresolvable root, spawn failure,
  unparsable output; a version mismatch renders the advisory line only.

Contract enrichment (additive-optional since v1, carried into v2
unchanged): each fact may carry
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
exit, timeout, unparsable output, any exception — degrades to "no
injection" (`query_lines` returns `[]`), never an error surfaced to the
user; the one deliberate carve-out is the strict-integer version-mismatch
advisory above (one advisory line, still never an error, still no facts
from an unknown shape). cppgraph is a read-only consumer: it only ever spawns
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cppgraph.store import project_root_path

ENV_VAR = "CPPGRAPH_MASORA"
MAX_FACTS_ENV_VAR = "CPPGRAPH_MASORA_MAX_FACTS"
CONTRACT_VERSION = 3
FACTS_TIMEOUT_S = 2.0
MAX_FACTS = 2
TOKEN_BUDGET = 60
MAX_SUMMARY_CHARS = 120
MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB — far above any legitimate contract document
STALE_WARNING_LINE = "masora: [stale index — facts may be outdated]"
# The DERIVED half of §6's zero-fact flavors (v3): cppgraph's own wording —
# unlike masora's `presence_hint`, which renders verbatim and is never
# hardcoded here. Names no tools: it is an absence notice, not a capability.
_ZERO_MATCH_LINE = (
    "masora: {n} lineage(s) examined, none matched — the base may predate this rebuild."
)
_TRUNCATION_LINE = "… +{n} more — masora search"

Runner = Callable[[str, "str | Sequence[str] | None", float], str | None]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class Fact:
    """One §3 fact, fields the renderer needs; unknown shapes never get here.

    The four v2 context stamps (§3, required) ride along for the §6 context
    rule: `established_relation` / `established_commit` are the git stamps —
    rendered, never analyzed — and `off_version` / `context_ordering`
    qualify the display. Of the v1 enrichment fields only `effort` is stored
    (it feeds the confidence matrix); `source`, `name`, and `anchors` — like
    `anchors_matched` — are type-validated in `parse_contract` and never
    consumed for rendering. The v3 `anchor_leaf` IS a rendering input: the
    short leaf masora derived from the first matched anchor identity,
    suffixed on multi-symbol responses only (§6).
    """

    lineage: str
    summary: str
    resolution: str
    verification: str
    flags: tuple[str, ...]
    established_relation: str | None
    established_commit: str | None
    off_version: bool
    context_ordering: str
    effort: str | None = None
    anchor_leaf: str | None = None


@dataclass(frozen=True)
class Contract:
    """The §3 document, parsed. Only contract shape v3 parses to one.

    The v3 fields: `lineages_examined` (the population masora's matching
    loop walked, 0 on an empty base), `lineages_matched` (always ==
    len(facts) — masora's emission guarantee, sanity-tested, not
    enforced), and `presence_hint` — masora's capability line, rendered
    VERBATIM when zero facts render and zero lineages were examined
    (§6's differentiated zero-fact rule)."""

    facts: tuple[Fact, ...]
    stale_warning: bool | None
    lineages_examined: int = 0
    lineages_matched: int = 0
    presence_hint: str | None = None


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """The `CPPGRAPH_MASORA` flag: unset → off (default until validated),
    `1`/`true` (case-insensitive) → on, anything else (including `0`/`false`)
    → off, fail-closed."""
    raw = (os.environ if env is None else env).get(ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true")


def max_facts(env: Mapping[str, str] | None = None) -> int:
    """The configured fact count from `CPPGRAPH_MASORA_MAX_FACTS` (§9.8b):
    unset → the default MAX_FACTS; any integer ≥ 1 is accepted (higher than
    the default is allowed); non-integer, empty, or < 1 silently falls back to
    the default — like `enabled`, this NEVER raises (the zero-change guarantee
    covers config typos: a broken value means "default", not "error" and
    never "zero facts"). The ≤ 60-token budget still wins over whatever count
    is configured — see `render_lines`."""
    raw = (os.environ if env is None else env).get(MAX_FACTS_ENV_VAR)
    if raw is None:
        return MAX_FACTS
    try:
        configured = int(raw)
    except ValueError:
        return MAX_FACTS
    return configured if configured >= 1 else MAX_FACTS


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


def run_masora_facts(
    repo_root: str, symbols: str | Sequence[str] | None, timeout: float
) -> str | None:
    """Spawn `masora facts --repo <root> [--symbol <scip> [--symbol <scip> …]]`;
    return stdout, or None on any failure (spawn error, non-zero exit —
    including the binary-missing 127 — timeout: the process TREE is killed and
    nothing renders; or an stdout stream beyond MAX_OUTPUT_BYTES, which is
    treated as unparsable rather than buffered unboundedly). `symbols` is one
    SCIP string, a sequence of them (§2's repeatable flag: ONE batched spawn
    for a multi-symbol response's result symbols, OR-matched by masora), or
    None (no flag — every lineage of the matching base(s)). The child's stdin
    is DEVNULL: cppgraph's own fd 0 may be the MCP stdio transport, and a
    masora reading it would eat protocol frames.

    Worst case ~2× the timeout budget on the orphaned-grandchild edge (the
    child exits fast but a grandchild holds the stdout write end, so the
    post-kill `reader.join` re-blocks up to the full timeout): still silent,
    still bounded, still no injection."""
    cmd = ["masora", "facts", "--repo", repo_root]
    if symbols is not None:
        for symbol in [symbols] if isinstance(symbols, str) else symbols:
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
    """Parse the §3 JSON document; None when it is not a contract-v3 document
    (unparsable, wrong shape, unknown/missing major version — no dual-version
    window: a v2 document rejects like any other unknown version) —
    fail-closed, the caller renders nothing. Per-fact fields are equally
    strict: a present field of the wrong type (`summary`, `resolution`,
    `lineage`, `flags`, `verification`, `anchors_matched`) AND a MISSING
    required field (the four v2 context stamps plus the v3 `anchor_leaf`) reject
    the WHOLE document — no partial delivery. Nullable requirements: explicit
    `null` is accepted for the three nullable ones (`established_relation`,
    `established_commit`, `anchor_leaf`); `context_ordering` null is a
    violation. Enum VALUES are not shape: an unrecognized
    `established_relation` / `context_ordering` (like an unknown
    `resolution` / `effort` / `source`) is parser-inert — rendering, not
    rejection, is where unknown values get a voice. Absent (or null)
    optional fields default instead: `lineage` to no-dedup, `flags` to none,
    `verification` to unrendered, `anchors_matched` to empty (it is
    validated, never consumed — a str item list per §3). The v3 DOCUMENT
    fields are equally required and strictly typed: `lineages_examined` /
    `lineages_matched` (strict int — a JSON bool is a violation) and
    `presence_hint` (`str | null`, masora's wording, rendered verbatim)."""
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
    # v3 document fields (§3, REQUIRED, fail-closed like every shape error):
    # the counters are strict ints (a JSON bool would slip past a plain int
    # check — the same strictness as `contract_version` itself), and
    # `presence_hint` is str-or-null — masora's wording, never rewritten.
    counters: dict[str, int] = {}
    for field in ("lineages_examined", "lineages_matched"):
        value = doc.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        counters[field] = value
    # REQUIRED-present (may be null): a missing key is a shape violation —
    # only an explicit null means "masora shipped no capability line".
    if "presence_hint" not in doc:
        return None
    presence_hint = doc.get("presence_hint")
    if presence_hint is not None and not isinstance(presence_hint, str):
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
        # v2 context stamps (§3, REQUIRED — a fact missing one is a shape
        # violation): fail-closed like every other shape error. A wrong TYPE
        # rejects the whole document; an unknown enum VALUE is parser-inert
        # (rendering decides what it means — see `_context_line`).
        if "established_relation" not in raw:
            return None
        established_relation = raw["established_relation"]
        if established_relation is not None and not isinstance(established_relation, str):
            return None
        if "established_commit" not in raw:
            return None
        established_commit = raw["established_commit"]
        if established_commit is not None and not isinstance(established_commit, str):
            return None
        off_version = raw.get("off_version")
        if not isinstance(off_version, bool):
            # STRICT bool — a JSON int (0/1), str, null, or absent field is a
            # violation (the same strictness as `contract_version` itself,
            # which a bool would otherwise slip past numerically).
            return None
        if "context_ordering" not in raw:
            return None
        context_ordering = raw["context_ordering"]
        if not isinstance(context_ordering, str):
            # Null is a violation here (the selection's ordering is always
            # known: exact/degraded) — unlike the two nullable stamps above.
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
        # v3 per-fact field (§3, REQUIRED — a fact missing it is a shape
        # violation, the same posture as the four v2 stamps): masora's
        # derived short leaf of the first matched anchor identity — a
        # RENDERING input for §6's multi-symbol rule, null when no
        # `--symbol` was passed. cppgraph renders it, never re-derives it.
        if "anchor_leaf" not in raw:
            return None
        anchor_leaf = raw["anchor_leaf"]
        if anchor_leaf is not None and not isinstance(anchor_leaf, str):
            return None
        facts.append(
            Fact(
                lineage=lineage,
                summary=summary,
                resolution=resolution,
                verification=verification,
                flags=flags,
                established_relation=established_relation,
                established_commit=established_commit,
                off_version=off_version,
                context_ordering=context_ordering,
                effort=effort,
                anchor_leaf=anchor_leaf,
            )
        )
    stale = doc.get("stale_warning")
    if stale is not None and not isinstance(stale, bool):
        # Same fail-closed posture as the per-fact fields: present-but-wrong
        # type rejects the document; absent (or null — §3 "not comparable")
        # defaults to None.
        return None
    stale_warning = stale if isinstance(stale, bool) else None
    return Contract(
        facts=tuple(facts),
        stale_warning=stale_warning,
        lineages_examined=counters["lineages_examined"],
        lineages_matched=counters["lineages_matched"],
        presence_hint=presence_hint,
    )


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


def _context_line(fact: Fact) -> str | None:
    """The §6 context label, rendered from the stamps ONLY — never computed,
    never treated as validity. Renders iff any trigger fires (`off_version`,
    a non-`in_line` relation, a `degraded` ordering); `resolution: "none"`
    short-circuits to None BEFORE the trigger test (nothing displays on a
    refuted lineage — the NOT: envelope carries the negative knowledge). A
    null relation never triggers alone, but renders "context unproven" when
    another trigger fired; an unrecognized relation value renders VERBATIM
    in the neutral forward-compatibility template, never guessed. The short
    `established_commit` renders beside the relation phrase as a pointer
    when non-null — with no relation phrase (an `in_line` fact) there is
    nothing to point at, so it does not render."""
    if fact.resolution == "none":
        return None
    relation = fact.established_relation
    degraded = fact.context_ordering == "degraded"
    relation_triggers = relation is not None and relation != "in_line"
    if not (fact.off_version or relation_triggers or degraded):
        return None
    parts: list[str] = []
    if fact.off_version:
        parts.append("off-version")
    if relation != "in_line":
        if relation is None or relation == "unknown":
            phrase = "context unproven"
        elif relation == "ahead":
            phrase = "established ahead of this checkout"
        elif relation == "out_of_line":
            phrase = "established on another line"
        else:
            # Forward compatibility (§6): a value this reader does not know
            # renders verbatim in the neutral template — never mapped onto a
            # pinned phrase, never guessed.
            phrase = f"established relation {relation}"
        if fact.established_commit is not None:
            phrase = f"{phrase} ({fact.established_commit})"
        parts.append(phrase)
    if degraded:
        parts.append("selection ordering degraded")
    return "  context: " + " — ".join(parts)


def render_lines(
    contract: Contract, *, max_facts: int = MAX_FACTS, multi_symbol: bool = False
) -> list[str]:
    """§6: at most `max_facts` fact lines within the token budget, one terse
    line each (plus its indented context line when the §6 v2 trigger fires —
    the pair is ATOMIC: it renders together or drops together, and the
    context line counts in the estimate), any cap reported visibly; the
    stale-index note (when present) renders first — but only alongside
    facts: zero matching facts renders the §6 v3 zero-fact flavors instead
    (see below). At least one fact renders even when it alone exceeds the
    budget — a lone oversized fact (context line included) is still worth
    more than a bare cap.

    On a MULTI-symbol response (`multi_symbol=True` — the `find`/`outline`
    batched spawn), a fact with a non-null `anchor_leaf` renders the
    `— <anchor_leaf>` suffix after its status bracket: the leaf
    disambiguates WHICH queried symbol the fact answers for. A null leaf
    renders the plain form; single-symbol responses (the default) are
    unchanged — no suffix whatever the leaf. The suffix is part of the fact
    line: it counts in the token estimate and drops with its block.

    Zero facts rendered → the v3 differentiated flavors, keyed on the
    counters: `lineages_examined == 0` (empty base) renders masora's
    `presence_hint` VERBATIM — cppgraph never hardcodes or rewrites the
    wording; a null hint there stays silent (nothing to render verbatim).
    `lineages_examined > 0` (a filtered no-match) renders the derived
    staleness line from the counters, naming no tools — and a drifted
    non-null hint does not render once lineages were examined (the counters
    are the trigger). Either flavor renders at most once per response and
    is never knowledge or advice.

    `max_facts` is the configured count (`max_facts(env)` — default
    MAX_FACTS), and the token budget WINS over it: when rendering the
    configured count would exceed the budget, fewer facts render plus the
    visible truncation line. A configured count below 1 is clamped to 1 so
    the never-zero rule stays unconditional."""
    lines: list[str] = []
    if contract.stale_warning:
        lines.append(STALE_WARNING_LINE)
    kept = 0
    dropped = 0
    cap = max(1, max_facts)
    for i, fact in enumerate(contract.facts):
        fact_line = _fact_line(fact)
        if multi_symbol and fact.anchor_leaf:
            # §6 v3: the leaf suffix — part of the fact line, so the budget
            # estimate and the atomic block rule see it (est_tokens runs
            # over the joined text).
            fact_line += f" — {fact.anchor_leaf}"
        context = _context_line(fact)
        # The fact line and its context line are ATOMIC (§6): they render
        # together or drop together, and the context line counts in the
        # token estimate — est_tokens runs over the joined text, as it ran
        # over the lone line before v2. The returned list stays FLAT: one
        # element per rendered line, the context line right after its fact.
        block = fact_line if context is None else f"{fact_line}\n{context}"
        over = est_tokens("\n".join([*lines, block])) > TOKEN_BUDGET
        if kept >= cap or (kept and over):
            dropped = len(contract.facts) - i
            break
        lines.append(fact_line)
        if context is not None:
            lines.append(context)
        kept += 1
    if not kept:
        # §6 v3, the differentiated zero-fact flavors — the counters decide.
        if contract.lineages_examined == 0:
            # Empty base: masora's capability line, verbatim — or silence
            # when masora shipped none (cppgraph never invents wording).
            return [contract.presence_hint] if contract.presence_hint is not None else []
        return [_ZERO_MATCH_LINE.format(n=contract.lineages_examined)]
    if dropped:
        lines.append(_TRUNCATION_LINE.format(n=dropped))
    return lines


def mismatch_advisory(stdout: str) -> str | None:
    """The version-mismatch advisory (a deliberate carve-out from the silent
    skip): when stdout parses as a JSON dict whose `contract_version` is a
    strict integer (a JSON bool is invalid even where the language conflates
    bool and int) OTHER than `CONTRACT_VERSION`, return the one-line
    advisory — a newer contract: `masora: [facts contract v{n} unsupported —
    update cppgraph]`; an older one: `masora: [facts contract v{n} — update
    masora]`. None — silence, the zero-change posture — for unparsable JSON,
    a non-dict document, a missing/bool/non-integer version, and the
    matching version itself. The advisory carries no facts: cppgraph knows
    nothing about another major's shape, so nothing from it ever renders."""
    try:
        doc = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    version = doc.get("contract_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    if version == CONTRACT_VERSION:
        return None
    if version > CONTRACT_VERSION:
        return f"masora: [facts contract v{version} unsupported — update cppgraph]"
    return f"masora: [facts contract v{version} — update masora]"


def query_lines(
    meta: Mapping[str, str],
    symbols: str | Sequence[str] | None,
    *,
    env: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    runner: Runner = run_masora_facts,
    timeout: float = FACTS_TIMEOUT_S,
) -> list[str]:
    """The entry point query responses call to inject Masora facts: flag
    check → binary detection → one `masora facts` spawn for the resolved
    symbol(s) → version-mismatch advisory (the one-line carve-out from silent
    skip — see `mismatch_advisory`) → contract-v3 parse → §6 render (the
    fact count resolved from `CPPGRAPH_MASORA_MAX_FACTS` in the same env the
    flag is read from).

    `symbols` is the single symbol a single-symbol response centers on, or
    every result symbol of a multi-symbol response (`find`/`outline`): ONE
    batched spawn carries them all as repeated `--symbol` flags (§9.10),
    deduplicated in order; the ≤ 2-fact / ≤ 60-token budget is per RESPONSE,
    not per symbol. An EMPTY symbol list skips the spawn entirely — a
    response with no result symbols asks nothing (spawning flag-less would
    return every lineage of the matching base(s), which is not what an empty
    find/outline means).

    Returns the rendered lines — `[]` whenever the feature is off or nothing
    injects, the single advisory line on a strict-integer version mismatch,
    and on a zero-fact response one of §6's v3 differentiated flavors (the
    counters decide: masora's `presence_hint` verbatim on an empty base, the
    derived staleness line above zero examined) — and never a raise (the
    zero-change guarantee)."""
    if not enabled(env):
        return []
    if which("masora") is None:
        return []
    try:
        root = repo_root(meta)
        if root is None:
            return []
        batch: Sequence[str] | None
        if isinstance(symbols, str):
            batch = [symbols]
        elif symbols is None:
            batch = None
        elif not symbols:
            return []
        else:
            batch = list(dict.fromkeys(symbols))
        stdout = runner(root, batch, timeout)
        if stdout is None:
            return []
        advisory = mismatch_advisory(stdout)
        if advisory is not None:
            # The carve-out: a strict-integer version mismatch is worth one
            # terse line (update cppgraph / update masora) where every other
            # failure stays silent — see `mismatch_advisory`.
            return [advisory]
        contract = parse_contract(stdout)
        if contract is None:
            return []
        # §6 v3: the leaf suffix renders on MULTI-symbol responses only — a
        # one-symbol batch (or a flag-less full enumeration) is symbol-
        # centered, the plain form.
        multi = batch is not None and len(batch) > 1
        return render_lines(contract, max_facts=max_facts(env), multi_symbol=multi)
    except Exception:
        return []
