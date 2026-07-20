"""Tests for `cppgraph setup` stages, driven by a scripted stdlib Prompter."""

from __future__ import annotations

import json
from pathlib import Path

from cppgraph import setup_cmd
from cppgraph.prompt import Prompter


def _scripted_prompter(answers: list[str]) -> tuple[Prompter, list[str]]:
    q = list(answers)
    out: list[str] = []

    def _input(_prompt: str) -> str:
        return q.pop(0) if q else ""

    def _print(*args) -> None:
        out.append(" ".join(str(a) for a in args))

    return Prompter(_input, _print), out


def test_obtain_reuses_present_binary_when_declined(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "scip-clang"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (bindir / "scip-clang.json").write_text(json.dumps({"variant": "stock", "version": "0.4.0"}))

    # Answer "no" to "re-obtain?" -> keep the existing binary.
    p, out = _scripted_prompter(["n"])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "present"
    assert binary.read_text() == "#!/bin/sh\n"  # untouched
    assert any("already installed" in line for line in out)


def test_obtain_abort_stops_setup(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    # No binary present; the source menu ends with "abort". Force the abort choice
    # by making platform_sources report nothing downloadable/buildable so the menu
    # is [emulate, abort]; pick abort by its index.
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, False))
    # options: [emulate(1), abort(2)]
    p, out = _scripted_prompter(["2"])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "aborted"


def test_obtain_emulate_installs_nothing(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, False))
    # options: [emulate(1), abort(2)] -> pick emulate.
    p, out = _scripted_prompter(["1"])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "emulate"
    assert not (bindir / "scip-clang").exists()


def test_register_mcp_skips_without_claude(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "_claude_available", lambda: False)
    p, out = _scripted_prompter([])
    assert setup_cmd.register_mcp(p) == "skipped"
    assert any("claude" in line.lower() for line in out)
