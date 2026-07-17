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

Usage:
  scripts/measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]

  TARGET_SUBSTR  pick the resolved symbol containing this substring as the one
                 the question is about (default: the first symbol `find` returns)

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
from pathlib import Path

from cppgraph import mcp_server
from cppgraph.store import GraphStore

CHARS_PER_TOKEN = 4  # rough; conservative for code (real ~3–3.5 → more tokens)


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


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.exit("usage: measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]")
    name, src_root, graph = argv[0], argv[1], argv[2]
    subtree = argv[3] if len(argv) > 3 else None
    target_substr = argv[4] if len(argv) > 4 else None

    print(f'Question: "who calls {name}?"')
    print(f"(tokens ~ chars / {CHARS_PER_TOKEN} — rough, conservative for code;")
    print(" cppgraph rows measure the MCP tool JSON, i.e. what the LLM ingests)\n")

    print("grep — raw text dumped into context (ambiguous: every name, every kind):")
    whole_c = len(_run("grep", "-rn", name, src_root))
    _row("whole tree (untargeted — realistic)", whole_c, f"[{src_root}]")
    sub_c = None
    if subtree:
        sub_c = len(_run("grep", "-rn", name, subtree))
        _row("subtree (best case — if you knew where)", sub_c, f"[{subtree}]")

    store = GraphStore(graph)

    print("\ncppgraph MCP — exact & disambiguated (JSON payload the LLM receives):")
    find_result = mcp_server.find_symbols(store, name)
    syms = [r["symbol"] for r in find_result["results"]]
    find_c = _json_chars(find_result)
    _row(f"find (resolves {len(syms)} distinct symbol(s) — grep can't)", find_c)

    # Pick the symbol the question is really about (the one who_calls is run on).
    target_sym = None
    print("  who_calls per resolved symbol (compact default):")
    for s in syms:
        compact = mcp_server.callers(store, s)  # default: derived labels + exclude_tests
        pick = bool(target_substr and target_substr in s)
        if pick or (target_substr is None and target_sym is None):
            target_sym = s
        tail = s.split("mongo/", 1)[-1][:56]
        note = f"{compact.get('total', 0)} prod callers" + ("  <- TARGET" if pick else "")
        _row(f"  {tail}", _json_chars(compact), note)

    # Break the who_calls(target) cost into what each optimisation contributes:
    #   verbose  = pre-optimisation (raw SCIP strings, test callers kept)
    #   +tests   = after #2 (test callers dropped)
    #   compact  = after #1 too (labels derived from SCIP) — the current default
    raw = mcp_server.callers(store, target_sym, full_symbols=True, exclude_tests=False)
    no_tests = mcp_server.callers(store, target_sym, full_symbols=True, exclude_tests=True)
    compact = mcp_server.callers(store, target_sym)
    raw_c, no_tests_c, compact_c = map(_json_chars, (raw, no_tests, compact))
    n_all, n_prod = raw.get("total", 0), compact.get("total", 0)

    print(f"\n  who_calls({target_sym.split('mongo/', 1)[-1][:48]}) — where our tokens go:")
    _row("verbose: raw SCIP + tests kept (pre-opt)", raw_c, f"{n_all} callers")
    _row("+ #2 drop test callers", no_tests_c,
         f"{n_prod} prod  ({100 * (1 - no_tests_c / max(raw_c, 1)):.0f}% smaller)")
    _row("+ #1 derive labels from SCIP (DEFAULT)", compact_c,
         f"{100 * (1 - compact_c / max(no_tests_c, 1)):.0f}% smaller again")

    answer_compact = find_c + compact_c
    answer_raw = find_c + raw_c
    print()
    _row("ANSWER (default) = find + who_calls(target)", answer_compact, "<- exact")
    _row("ANSWER (pre-opt) = raw SCIP + tests", answer_raw,
         f"{answer_raw / max(answer_compact, 1):.1f}x the default payload")

    print("\nratios (tokens), for answering the one specific symbol:")
    a = max(answer_compact, 1)
    print(f"  untargeted grep / cppgraph : {whole_c / a:>5.0f}x")
    if sub_c is not None:
        print(f"  targeted   grep / cppgraph : {sub_c / a:>5.0f}x")
    print(
        "\nNotes:\n"
        "- cppgraph rows are the MCP JSON the LLM ingests. The default ships a\n"
        "  readable label derived from the SCIP string (not the 150-250-char raw\n"
        "  string) and drops test callers — the two ANSWER rows show the cost with\n"
        "  and without those optimisations for the same query.\n"
        "- Measure this on who_calls / what_it_calls / impact_of / explain, where\n"
        "  each hit carries a far-end symbol: that's where the label shortening bites.\n"
        "  find_references returns file:line only, so it has no symbol to shorten.\n"
        "- The LLM isn't told how to scope its grep → expect the untargeted number;\n"
        "  and targeting barely helps here anyway.\n"
        "- grep is only raw material: to separate the same-named symbols and drop\n"
        "  decls/comments it then reads files (more tokens). cppgraph's answer is\n"
        "  already exact."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
