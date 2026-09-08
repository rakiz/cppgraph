#!/usr/bin/env python3
"""Regenerate the per-command tables in CLI_REFERENCE.md from the real CLI.

CLI_REFERENCE.md groups the subcommands by purpose — hand-written prose the
parser can't infer — but every table row (command name, aliases, one-line
purpose, argument list) is generated from the argparse subparsers in
`cppgraph/cli.py`, so those rows cannot drift from `cppgraph <command> --help`.

Each section of the doc carries a marker listing its commands in display order;
this script rewrites what sits between the markers:

    <!-- cppgraph-gen: callers callees path -->
    | Command | Purpose (from `--help`) | Arguments |
    ...
    <!-- /cppgraph-gen -->

A subcommand that exists in the CLI but is placed in no section (or a marker
naming a command that no longer exists) is reported on stderr and fails the
run — the prompt to curate the newcomer's place in the doc, not a silent
omission. Usage (repository venv):

    .venv/bin/python scripts/gen-cli-reference.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from cppgraph import cli

DOC = Path(__file__).resolve().parent.parent / "CLI_REFERENCE.md"

TABLE_HEADER = (
    "| Command | Purpose (from `--help`) | Arguments |",
    "|---|---|---|",
)
_BLOCK = re.compile(
    r"<!-- cppgraph-gen:(?P<names>[^>]*?) -->\n.*?^<!-- /cppgraph-gen -->",
    re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class CommandFact:
    name: str
    aliases: tuple[str, ...]
    help: str
    args: tuple[str, ...]


class _Captured(Exception):
    """Control flow: stops cli.main() right after it assembles its parser."""


def _root_parser() -> argparse.ArgumentParser:
    """The parser `cli.main()` assembles. It is built inline inside main()
    (there is no standalone build_parser() to import), so capture it by
    standing in for `ArgumentParser.parse_args` for one call: `main([])`
    reaches that call with the fully-built parser as `self`, before any
    validation runs."""
    box: dict[str, argparse.ArgumentParser] = {}

    def grab(self: argparse.ArgumentParser, args=None, namespace=None):
        box["parser"] = self
        raise _Captured

    real = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = grab  # type: ignore[method-assign]
    try:
        cli.main([])
    except _Captured:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real  # type: ignore[method-assign]
    return box["parser"]


def _fmt_action(a: argparse.Action) -> str:
    """One compact token for an argument: `<symbol>` / `[<compdb>]` for
    positionals, `--flag`/`--flag METAVAR` for options (both choices of a
    BooleanOptionalAction), `*` suffix when required."""
    if isinstance(a, argparse._HelpAction) or a.help is argparse.SUPPRESS:
        return ""
    if a.option_strings:
        text = "/".join(a.option_strings)
        if a.metavar:
            text += f" {a.metavar}"
        return text + ("*" if a.required else "")
    name = a.metavar or a.dest
    return f"[<{name}>]" if a.nargs == "?" else f"<{name}>"


def command_facts() -> dict[str, CommandFact]:
    """One CommandFact per subcommand, keyed by canonical name (first of the
    alias group), in parser registration order. Reads only the argparse
    structures — the same `help=` strings `cppgraph --help` prints."""
    parser = _root_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {pa.dest: pa.help for pa in sub._choices_actions}
    names_by_parser: dict[int, list[str]] = {}
    for name, sp in sub.choices.items():
        names_by_parser.setdefault(id(sp), []).append(name)
    facts: dict[str, CommandFact] = {}
    for names in names_by_parser.values():
        name = names[0]
        sp = sub.choices[name]
        args = tuple(s for s in (_fmt_action(a) for a in sp._actions) if s)
        facts[name] = CommandFact(name, tuple(names[1:]), helps.get(name, ""), args)
    return facts


def _row(f: CommandFact) -> str:
    name = f"`{f.name}`" + (f" (alias `{f.aliases[0]}`)" if f.aliases else "")
    purpose = " ".join(f.help.split()).replace("|", "\\|")
    args = ", ".join(f.args).replace("|", "\\|")
    return f"| {name} | {purpose} | {args} |"


def regenerate(text: str, facts: dict[str, CommandFact]) -> tuple[str, list[str]]:
    """Rewrite the generated blocks in the doc text; return (new_text,
    problems). Problems are drift alarms: a marker naming an unknown command,
    a command placed in no section, or one placed twice."""
    problems: list[str] = []
    placed: list[str] = []

    def render(match: re.Match) -> str:
        names = match.group("names").split()
        rows = []
        for n in names:
            if n not in facts:
                problems.append(f"marker names unknown command: {n!r}")
                continue
            placed.append(n)
            rows.append(_row(facts[n]))
        return (
            f"<!-- cppgraph-gen:{' '.join(names)} -->\n"
            + "\n".join((*TABLE_HEADER, *rows))
            + "\n"
            + "<!-- /cppgraph-gen -->"
        )

    new_text = _BLOCK.sub(render, text)
    dupes = sorted({n for n in placed if placed.count(n) > 1})
    problems += [f"command placed in more than one section: {n}" for n in dupes]
    problems += [
        f"subcommand in no section (add it to a cppgraph-gen marker): {n}"
        for n in facts
        if n not in placed
    ]
    return new_text, problems


def main() -> int:
    facts = command_facts()
    text = DOC.read_text(encoding="utf-8")
    new_text, problems = regenerate(text, facts)
    for problem in problems:
        print(f"[gen-cli-reference] {problem}", file=sys.stderr)
    if problems:
        return 1
    if new_text != text:
        DOC.write_text(new_text, encoding="utf-8")
        print(f"[gen-cli-reference] updated {DOC.name} ({len(facts)} commands)")
    else:
        print(f"[gen-cli-reference] {DOC.name} up to date ({len(facts)} commands)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
