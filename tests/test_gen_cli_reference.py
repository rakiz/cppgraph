"""`scripts/gen-cli-reference.py` keeps CLI_REFERENCE.md's per-command tables
in lockstep with the real argparse subparsers in `cppgraph/cli.py`, so the
reference page cannot drift from `cppgraph <command> --help`."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gen-cli-reference.py"
DOC = REPO / "CLI_REFERENCE.md"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_cli_reference", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # register before exec: the script's dataclass resolves its annotations
    # through sys.modules (Python 3.14), which is None for an unregistered module
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_reference_doc_tables_match_the_real_subparsers():
    mod = _generator()
    facts = mod.command_facts()

    # the introspection sees the real command set (30 parsers, 12 aliased)
    assert "find" in facts and "view" in facts
    assert facts["init"].aliases == ("index",)
    assert len(facts) >= 29

    # every subcommand is placed in a section of the doc, and the checked-in
    # tables are already what the parser would emit (no drift)
    text = DOC.read_text(encoding="utf-8")
    regenerated, problems = mod.regenerate(text, facts)
    assert problems == []
    assert regenerated == text

    # the doc mentions every command by name
    missing = [name for name in facts if f"`{name}`" not in text]
    assert missing == []
