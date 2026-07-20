#!/usr/bin/env python3
"""Measure the token cost of answering "who calls <name>?" — grep vs cppgraph.

This measures what an **LLM actually ingests**, so the cppgraph side is the
**MCP tool JSON** (the payload the model receives over the protocol), not the
human-facing CLI text. Three ways to answer the question:

  1. grep over the WHOLE source tree — you don't know where the symbol lives, so
     this is the realistic case (the LLM isn't told how to scope its search).
  2. grep over a SUBTREE you already know — grep's best case.
  3. cppgraph MCP — `find <name>` (disambiguates the name into distinct symbols,
     which grep cannot) plus `who_calls` on the symbol you actually mean.

For cppgraph we print both the **compact default** (human name + file:line, test
callers filtered) and the **verbose** variant (`full_symbols=True`,
`exclude_tests=False`) so the effect of the token-lean defaults is visible.

Tokens are approximated as characters / CHARS_PER_TOKEN. That's the rough rule
for prose; code and SCIP symbol strings (punctuation, hex hashes, paths)
tokenize *denser*, so real counts are HIGHER on every row — deliberately
conservative, and the ratios hold. Tune CHARS_PER_TOKEN or re-run on any
symbol/project to check or correct the numbers.

Two modes:

  # detailed single-symbol breakdown
  scripts/measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]

  # tiered suite — one run spanning grep-wins → realistic-win → crush
  scripts/measure_tokens.py --suite SRC_ROOT GRAPH_DB

  TARGET_SUBSTR  pick the resolved symbol containing this substring as the one
                 the question is about (default: the symbol with the most
                 callers — the meaningful one to ask "who calls?" about)

The `--suite` list is curated against a MongoDB `src/mongo` graph: it spans the
whole spectrum on purpose — a case where grep is *cheaper* than us (a uniquely
named symbol grep finds in two lines), realistic wins, and names so common that
grep drowns in noise while cppgraph stays exact. It is a fair benchmark, not a
cherry-pick: the losing case is in the table.

Example (a symbol with real callers *and* test noise, so both optimisations show):
  scripts/measure_tokens.py ResumeToken \\
    /path/to/mongo/src/mongo /path/to/mongo/.cppgraph/mongo.graph.db \\
    /path/to/mongo/src/mongo/db/pipeline \\
    'ResumeToken#parse'
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field

from cppgraph import mcp_server
from cppgraph.store import GraphStore

CHARS_PER_TOKEN = 4  # rough; conservative for code (real ~3–3.5 → more tokens)

# grep can't tell a call from a declaration/comment/same-named symbol by itself.
# To reach cppgraph's exactness the agent must *read around* each hit to judge
# it — which is exactly `grep -C N`. So the honest grep cost is the context dump,
# not the raw line count (that's only a floor). 10 lines each way is a
# conservative window: real disambiguation often needs the whole enclosing
# function or to chase a type.
GREP_VERIFY_CONTEXT = 10

# `find` returns at most this many symbols, so the cppgraph `find` cost is
# bounded no matter how ambiguous the name — on very common names the true
# disambiguation is larger, which only widens the gap in our favour.
FIND_CAP = 40

# A realistic single-context budget (tokens). Past this a "grep + read" ratio is
# a fiction: nobody ingests millions of tokens. So the honest verdict switches
# from a multiplier to *infeasible* — grep can't answer within one context.
# Below it: grep raw fits but verifying every hit ('grep+read') overflows →
# grep gives a dump it can't afford to disambiguate. Below that: a real ratio.
CONTEXT_BUDGET_TOKENS = 200_000


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def _tok(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def _json_chars(payload: object) -> int:
    """Chars of the JSON the LLM actually receives over MCP (compact separators,
    matching how a transport serialises it)."""
    return len(json.dumps(payload, separators=(",", ":")))


def _row(label: str, chars: int, note: str = "") -> None:
    print(f"  {label:<48}{chars:>9,} chars  ~{_tok(chars):>7,} tok  {note}")


@dataclass
class Measurement:
    """Char counts for one "who calls NAME?" question, both tools."""

    name: str
    whole_c: int  # grep over the whole tree (raw match lines — a floor)
    whole_ctx_c: int  # grep -C: the same dump the agent reads to verify each hit
    sub_c: int | None  # grep over a known subtree (grep's best case), or None
    find_c: int  # cppgraph `find` JSON
    n_syms: int  # distinct symbols `find` resolved
    target_sym: str  # the symbol who_calls is run on
    raw_c: int  # who_calls verbose: raw SCIP + tests kept
    no_tests_c: int  # who_calls verbose, test callers dropped
    compact_c: int  # who_calls default (derived labels, no tests)
    n_all: int  # caller count incl. tests
    n_prod: int  # caller count, prod only
    grep_lines: int  # grep lines with a file:line
    on_target: int  # grep lines that are a real call site of the target
    per_symbol: list[tuple[str, int, int, bool]] = field(default_factory=list)

    @property
    def answer_compact(self) -> int:
        return self.find_c + self.compact_c

    @property
    def answer_raw(self) -> int:
        return self.find_c + self.raw_c

    @property
    def noise_pct(self) -> float:
        return 100 * (1 - self.on_target / self.grep_lines) if self.grep_lines else 0.0

    @property
    def ratio(self) -> float:
        """vs grep's raw match dump (a floor — grep still has to read to verify)."""
        return self.whole_c / max(self.answer_compact, 1)

    @property
    def real_ratio(self) -> float:
        """vs grep + the reads it needs to disambiguate — the honest comparison."""
        return self.whole_ctx_c / max(self.answer_compact, 1)

    @property
    def verdict(self) -> str:
        """A ratio while grep stays within one context budget; past it the honest
        answer is not a (fictional) multiplier but *infeasible* — the grep+read
        cost shown alongside is a theoretical ceiling nobody actually ingests."""
        if _tok(self.whole_ctx_c) > CONTEXT_BUDGET_TOKENS:
            return "infeasible"
        r = self.real_ratio
        return f"{r:.0f}x" if r >= 10 else f"{r:.1f}x"


def measure(
    name: str,
    src_root: str,
    store: GraphStore,
    subtree: str | None = None,
    target_substr: str | None = None,
) -> Measurement:
    """Run both tools for "who calls NAME?" and collect the char counts.

    The target symbol (the one `who_calls` runs on) is the one matching
    `target_substr`; with no match, the symbol with the most callers — the
    meaningful thing to ask "who calls?" about, not whichever `find` lists first
    (which is often a field with no call sites and skews signal/noise)."""
    whole_out = _run("grep", "-rn", name, src_root)
    whole_c = len(whole_out)
    whole_ctx_c = len(_run("grep", "-rn", "-C", str(GREP_VERIFY_CONTEXT), name, src_root))
    sub_c = len(_run("grep", "-rn", name, subtree)) if subtree else None

    find_result = mcp_server.find_symbols(store, name)
    syms = [r["symbol"] for r in find_result["results"]]
    find_c = _json_chars(find_result)

    # Resolve the target: an explicit substring match wins; else max callers.
    target_sym = None
    per_symbol: list[tuple[str, int, int, bool]] = []
    best_total = -1
    for s in syms:
        compact = mcp_server.callers(store, s)  # default: labels + exclude_tests
        total = compact.get("total", 0)
        matched = bool(target_substr and target_substr in s)
        if matched and target_sym is None:
            target_sym = s
        if target_sym is None and total > best_total:
            best_total = total
        tail = s.split("mongo/", 1)[-1][:56]
        per_symbol.append((tail, _json_chars(compact), total, matched))
    if target_sym is None:  # no substring match → pick the most-called symbol
        target_sym = max(syms, key=lambda s: mcp_server.callers(store, s).get("total", 0))
    for i, (tail, chars, total, matched) in enumerate(per_symbol):
        per_symbol[i] = (tail, chars, total, matched or syms[i] == target_sym)

    raw = mcp_server.callers(store, target_sym, full_symbols=True, exclude_tests=False)
    no_tests = mcp_server.callers(store, target_sym, full_symbols=True, exclude_tests=True)
    compact = mcp_server.callers(store, target_sym)

    # Correctness: how much of grep's dump is actually the answer? cppgraph's
    # answer is exact by construction (compiler-resolved callers); grep matches
    # the name in comments/strings/decls and across every same-named symbol, so
    # most of what it dumps is noise for *this* question. Match a grep path by
    # suffix against cppgraph's relative file to count the coincidences.
    sites = {
        (c["file"], c["line"])
        for c in mcp_server.callers(
            store, target_sym, full_symbols=True, exclude_tests=False, limit=10**9
        ).get("callers", [])
    }
    grep_lines = on_target = 0
    for line in whole_out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        grep_lines += 1
        path, n = parts[0], int(parts[1])
        if any(n == sl and path.endswith(sf) for sf, sl in sites):
            on_target += 1

    return Measurement(
        name=name,
        whole_c=whole_c,
        whole_ctx_c=whole_ctx_c,
        sub_c=sub_c,
        find_c=find_c,
        n_syms=len(syms),
        target_sym=target_sym,
        raw_c=_json_chars(raw),
        no_tests_c=_json_chars(no_tests),
        compact_c=_json_chars(compact),
        n_all=raw.get("total", 0),
        n_prod=compact.get("total", 0),
        grep_lines=grep_lines,
        on_target=on_target,
        per_symbol=per_symbol,
    )


def report_detail(m: Measurement, src_root: str, subtree: str | None) -> None:
    """The full single-symbol breakdown: every row, where the tokens go."""
    print(f'Question: "who calls {m.name}?"')
    print(f"(tokens ~ chars / {CHARS_PER_TOKEN} — rough, conservative for code;")
    print(" cppgraph rows measure the MCP tool JSON, i.e. what the LLM ingests)\n")

    print("grep — raw text dumped into context (ambiguous: every name, every kind):")
    _row("whole tree, raw matches (a floor)", m.whole_c, f"[{src_root}]")
    _row(
        f"whole tree + read to verify (grep -C {GREP_VERIFY_CONTEXT})",
        m.whole_ctx_c,
        "<- grep's honest cost (can't tell call from decl without reading)",
    )
    if m.sub_c is not None:
        _row("subtree, raw (best case — if you knew where)", m.sub_c, f"[{subtree}]")

    print("\ncppgraph MCP — exact & disambiguated (JSON payload the LLM receives):")
    cap_note = f" — capped at {FIND_CAP}" if m.n_syms >= FIND_CAP else ""
    _row(f"find (resolves {m.n_syms} distinct symbol(s){cap_note} — grep can't)", m.find_c)
    print("  who_calls per resolved symbol (compact default):")
    for tail, chars, total, is_target in m.per_symbol:
        note = f"{total} prod callers" + ("  <- TARGET" if is_target else "")
        _row(f"  {tail}", chars, note)

    print(f"\n  who_calls({m.target_sym.split('mongo/', 1)[-1][:48]}) — where our tokens go:")
    _row("verbose: raw SCIP + tests kept (pre-opt)", m.raw_c, f"{m.n_all} callers")
    _row(
        "+ #2 drop test callers",
        m.no_tests_c,
        f"{m.n_prod} prod  ({100 * (1 - m.no_tests_c / max(m.raw_c, 1)):.0f}% smaller)",
    )
    _row(
        "+ #1 derive labels from SCIP (DEFAULT)",
        m.compact_c,
        f"{100 * (1 - m.compact_c / max(m.no_tests_c, 1)):.0f}% smaller again",
    )

    print()
    _row("ANSWER (default) = find + who_calls(target)", m.answer_compact, "<- exact")
    _row(
        "ANSWER (pre-opt) = raw SCIP + tests",
        m.answer_raw,
        f"{m.answer_raw / max(m.answer_compact, 1):.1f}x the default payload",
    )

    print("\nsignal vs noise (grep is raw text; cppgraph is compiler-exact):")
    print(f"  grep lines dumped                 : {m.grep_lines}")
    print(
        f"  ...that are a real call site      : {m.on_target}  -> {m.noise_pct:.1f}% noise for this question"  # noqa: E501
    )
    print(
        f"  cppgraph callers (target)         : {m.n_all}  -> exact (100% signal, no reading to filter)"  # noqa: E501
    )

    print("\nverdict (tokens), for answering the one specific symbol:")
    print(f"  raw grep floor      / cppgraph : {m.ratio:>6.1f}x  (grep's dump alone)")
    budget_note = (
        "  <- grep+read overflows a context; grep can't answer"
        if _tok(m.whole_ctx_c) > CONTEXT_BUDGET_TOKENS
        else "  <- the real comparison"
    )
    print(f"  grep + read (honest)/ cppgraph : {m.verdict:>7}{budget_note}")
    if m.sub_c is not None:
        print(f"  targeted raw grep   / cppgraph : {m.sub_c / max(m.answer_compact, 1):>6.1f}x")
    print(
        "\nNotes:\n"
        "- The honest grep cost is 'grep + read', not the raw match count: grep\n"
        "  cannot tell a call from a declaration/comment/same-named symbol, so to\n"
        "  answer the question it must read around each hit. Even a rare, unique\n"
        "  name — grep's best case on the raw floor — flips to a cppgraph win once\n"
        "  that reading is counted. cppgraph is ~always leaner.\n"
        f"- Past a ~{CONTEXT_BUDGET_TOKENS // 1000}k-token context budget the grep+read cost is a\n"
        "  theoretical ceiling nobody ingests: the honest verdict is 'infeasible',\n"
        "  not a multiplier — grep simply can't answer a hot symbol within a context.\n"
        "- cppgraph rows are the MCP JSON the LLM ingests. The default ships a\n"
        "  readable label derived from the SCIP string (not the 150-250-char raw\n"
        "  string) and drops test callers — the two ANSWER rows show the cost with\n"
        "  and without those optimisations for the same query.\n"
        f"- `find` is capped at {FIND_CAP} symbols, so our cost is bounded even on the\n"
        "  most ambiguous names; the true disambiguation is larger, widening the gap.\n"
        "- Measure this on who_calls / what_it_calls / impact_of / explain, where\n"
        "  each hit carries a far-end symbol: that's where the label shortening bites.\n"
        "  find_references returns file:line only, so it has no symbol to shorten."
    )


# Curated tiered suite for a MongoDB `src/mongo` graph. Each entry:
# (name, target_substr, note). Tiers span the whole spectrum so the benchmark is
# honest — the first tier is where grep *beats* us.
_SUITE: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "closest to grep — rare unique name (grep's raw dump is cheaper, until it reads)",
        [
            ("setBlockNewUserShardedDDL", "setBlockNewUserShardedDDL", "raw grep wins; loses on read"),  # noqa: E501
            (
                "_amIFreshEnoughForPriorityTakeover",
                "_amIFreshEnoughForPriorityTakeover",
                "",
            ),
        ],
    ),
    (
        "realistic win — a real class/method you'd actually navigate",
        [
            ("ResumeToken", "ResumeToken#parse", "prod callers + test noise"),
            ("PlanExecutor", "PlanExecutor#getPostBatchResumeToken", ""),
            ("BSONObjBuilder", "BSONObjBuilder#obj", "4000+ callers, answer stays capped"),
        ],
    ),
    (
        "crush — name so common grep drowns; cppgraph stays exact",
        [
            ("OperationContext", "OperationContext#getClient", "873 callers"),
            ("NamespaceString", "NamespaceString#NamespaceString", "709 callers"),
        ],
    ),
]


def run_suite(src_root: str, store: GraphStore) -> None:
    """Measure the curated tiered list and print one grouped comparison table."""
    print('Suite: "who calls NAME?" — grep vs cppgraph (untargeted, whole tree)')
    print(f"(tokens ~ chars / {CHARS_PER_TOKEN}, conservative for code; cppgraph = MCP JSON)\n")
    header = (
        f"  {'symbol':<38}{'grep raw':>9}{'grep+read':>12}{'cppgraph':>10}"
        f"{'verdict*':>12}   noise"
    )
    for tier, entries in _SUITE:
        print(f"── {tier}")
        print(header)
        for name, target, note in entries:
            m = measure(name, src_root, store, subtree=None, target_substr=target)
            label = target if target else name
            print(
                f"  {label[:38]:<38}{_tok(m.whole_c):>9,}{_tok(m.whole_ctx_c):>12,}"
                f"{_tok(m.answer_compact):>10,}{m.verdict:>12}   {m.noise_pct:.0f}%"
                + (f"   ({note})" if note else "")
            )
        print()
    print(
        f"* verdict vs cppgraph. 'grep raw' is only the match dump (a floor); grep\n"
        "can't tell a call from a decl/comment/same-named symbol, so to actually\n"
        f"answer it must read around each hit ('grep+read' = grep -C {GREP_VERIFY_CONTEXT}).\n"
        "'cppgraph' = find + who_calls(target), exact, nothing left to filter.\n"
        f"A ratio holds while grep+read fits one context (~{CONTEXT_BUDGET_TOKENS // 1000}k tok);\n"
        "past that the grep+read figure is a theoretical ceiling nobody ingests, so\n"
        "the honest verdict is 'infeasible' — grep can't answer within a context at\n"
        "all. Either way cppgraph answers in a few k tokens, exact."
    )


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--suite":
        if len(argv) < 3:
            sys.exit("usage: measure_tokens.py --suite SRC_ROOT GRAPH_DB")
        run_suite(argv[1], GraphStore(argv[2]))
        return 0

    if len(argv) < 3:
        sys.exit(
            "usage: measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]\n"
            "   or: measure_tokens.py --suite SRC_ROOT GRAPH_DB"
        )
    name, src_root, graph = argv[0], argv[1], argv[2]
    subtree = argv[3] if len(argv) > 3 else None
    target_substr = argv[4] if len(argv) > 4 else None
    m = measure(name, src_root, GraphStore(graph), subtree, target_substr)
    report_detail(m, src_root, subtree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
