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


def test_obtain_non_interactive_without_source_stops(tmp_path: Path, monkeypatch) -> None:
    """Under a pipe (can_prompt=False) with no --scip-source, it must NOT default
    into a costly build/download — it stops with ACTION NEEDED."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, True))  # build-capable host
    p, out = _scripted_prompter([])  # a prompt here would raise IndexError? no — returns ""
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source=None, can_prompt=False)
    assert result == "need-input"
    assert not (bindir / "scip-clang").exists()
    assert any("ACTION NEEDED" in line for line in out)


def test_obtain_explicit_source_emulate_no_prompt(tmp_path: Path, monkeypatch) -> None:
    """An explicit --scip-source is honoured with no prompt, even non-interactive."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, True))
    result = setup_cmd.obtain_scip_clang(
        p := Prompter(_boom, lambda *a: None), bin_dir=bindir, source="emulate", can_prompt=False
    )
    assert result == "emulate"
    assert p is p  # (silence unused)


def test_obtain_invalid_source_for_platform_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, False))  # no download/build
    p, out = _scripted_prompter([])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=tmp_path / "bin", source="download")
    assert result == "failed"
    assert any("not valid on this platform" in line for line in out)


def _boom(_prompt: str) -> str:
    raise AssertionError("must not prompt when a source is given")


def test_register_mcp_skips_without_claude(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "_claude_available", lambda: False)
    p, out = _scripted_prompter([])
    assert setup_cmd.register_mcp(p) == "skipped"
    assert any("claude" in line.lower() for line in out)
