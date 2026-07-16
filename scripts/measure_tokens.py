#!/usr/bin/env python3
"""Measure the token cost of answering "who calls <name>?" — grep vs cppgraph.

  1. grep over the WHOLE source tree — you don't know where the symbol lives, so
     this is the realistic case (the LLM isn't told how to scope its search).
  2. grep over a SUBTREE you already know — grep's best case.
  3. cppgraph — `find <name>` (disambiguates the name into distinct symbols,
     which grep cannot) plus `who_calls` on the symbol you actually mean.

The headline comparison is for one specific symbol (`TARGET`), because that is
the real question ("who calls *this* method?"). A per-symbol `who_calls`
breakdown is also printed: a name with many genuine callers costs more, but that
is the *exact* answer grep can't produce at all.

Tokens are approximated as characters / CHARS_PER_TOKEN. That's the rough rule
for prose; code and SCIP symbol strings (punctuation, hex hashes, paths)
tokenize *denser*, so real counts are HIGHER on every row — deliberately
conservative, and the ratios hold. Tune CHARS_PER_TOKEN or re-run on any
symbol/project to check or correct the numbers.

Usage:
  scripts/measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]

  TARGET_SUBSTR  pick the resolved symbol containing this substring as the one
                 the question is about (default: the first symbol `find` returns)

Example:
  scripts/measure_tokens.py makeResumeToken \\
    /path/to/mongo/src/mongo /path/to/mongo/.cppgraph/mongo.graph.db \\
    /path/to/mongo/src/mongo/db/pipeline \\
    'ChangeStreamEventTransformation#makeResumeToken'
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHARS_PER_TOKEN = 4  # rough; conservative for code (real ~3–3.5 → more tokens)


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout


def _tok(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def _row(label: str, chars: int, note: str = "") -> None:
    print(f"  {label:<46}{chars:>9,} chars  ~{_tok(chars):>7,} tok  {note}")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.exit("usage: measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]")
    name, src_root, graph = argv[0], argv[1], argv[2]
    subtree = argv[3] if len(argv) > 3 else None
    target_substr = argv[4] if len(argv) > 4 else None
    cppgraph = str(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "cppgraph")

    print(f'Question: "who calls {name}?"')
    print(f"(tokens ~ chars / {CHARS_PER_TOKEN} — rough, conservative for code)\n")

    print("grep — raw text dumped into context (ambiguous: every name, every kind):")
    whole_c = len(_run("grep", "-rn", name, src_root))
    _row("whole tree (untargeted — realistic)", whole_c, f"[{src_root}]")
    sub_c = None
    if subtree:
        sub_c = len(_run("grep", "-rn", name, subtree))
        _row("subtree (best case — if you knew where)", sub_c, f"[{subtree}]")

    print("\ncppgraph — exact & disambiguated:")
    find_out = _run(cppgraph, "find", name, "--graph", graph)
    syms = [ln.split("  (? @")[0].strip() for ln in find_out.splitlines() if ln.strip()]
    find_c = len(find_out)
    _row(f"find (resolves {len(syms)} distinct symbol(s) — grep can't)", find_c)

    target_sym, target_calls_c = (syms[0] if syms else None), 0
    print("  who_calls per resolved symbol:")
    for s in syms:
        out = _run(cppgraph, "callers", s, "--graph", graph)
        n = max(sum(1 for ln in out.splitlines() if ln.startswith("  ")), 0)
        tail = s.split("mongo/", 1)[-1][:60]
        pick = target_substr and target_substr in s
        if pick:
            target_sym, target_calls_c = s, len(out)
        _row(f"  {tail}", len(out), f"{n} callers" + ("  <- TARGET" if pick else ""))
    if target_substr is None and syms:
        target_calls_c = len(_run(cppgraph, "callers", syms[0], "--graph", graph))

    answer_c = find_c + target_calls_c
    print()
    _row("ANSWER for one symbol = find + who_calls(target)", answer_c, "<- exact")

    print("\nratios (tokens), for answering the one specific symbol:")
    a = max(answer_c, 1)
    print(f"  untargeted grep / cppgraph : {whole_c / a:>5.0f}x")
    if sub_c is not None:
        print(f"  targeted   grep / cppgraph : {sub_c / a:>5.0f}x")
    print(
        "\nNotes:\n"
        "- The LLM isn't told how to scope its grep → expect the untargeted number;\n"
        "  and targeting barely helps here anyway.\n"
        "- grep is only raw material: to separate the same-named symbols and drop\n"
        "  decls/comments it then reads files (more tokens). cppgraph's answer is\n"
        "  already exact.\n"
        "- A symbol with many genuine callers costs more in cppgraph too — but that\n"
        "  is the complete, attributed list grep cannot produce at all."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
