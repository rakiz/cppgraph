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
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, None, False))
    # options: [emulate(1), abort(2)]
    p, out = _scripted_prompter(["2"])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "aborted"


def test_obtain_emulate_installs_nothing(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, None, False))
    # options: [emulate(1), abort(2)] -> pick emulate.
    p, out = _scripted_prompter(["1"])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "emulate"
    assert not (bindir / "scip-clang").exists()


def test_obtain_non_interactive_without_source_stops(tmp_path: Path, monkeypatch) -> None:
    """Under a pipe (can_prompt=False) with no --scip-source, it must NOT default
    into a costly build/download — it stops with ACTION NEEDED."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, None, True)
    )  # build-capable host
    p, out = _scripted_prompter([])  # a prompt here would raise IndexError? no — returns ""
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source=None, can_prompt=False)
    assert result == "need-input"
    assert not (bindir / "scip-clang").exists()
    assert any("ACTION NEEDED" in line for line in out)


def test_obtain_explicit_source_emulate_no_prompt(tmp_path: Path, monkeypatch) -> None:
    """An explicit --scip-source is honoured with no prompt, even non-interactive."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(setup_cmd, "platform_sources", lambda: (None, None, True))
    result = setup_cmd.obtain_scip_clang(
        p := Prompter(_boom, lambda *a: None), bin_dir=bindir, source="emulate", can_prompt=False
    )
    assert result == "emulate"
    assert p is p  # (silence unused)


def test_obtain_invalid_source_for_platform_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, None, False)
    )  # no download/build
    p, out = _scripted_prompter([])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=tmp_path / "bin", source="download")
    assert result == "failed"
    assert any("not valid on this platform" in line for line in out)


def test_obtain_download_504_verifies_checksum(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, "scip-clang-504-arm64-darwin", False)
    )

    payload = b"fake-binary-bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""

        if "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(payload)
            return R()
        r = R()
        r.stdout = f"{digest}  scip-clang-504-arm64-darwin"
        return r

    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    p, out = _scripted_prompter([])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source="download-504")
    assert result == "present"
    assert (bindir / "scip-clang").read_bytes() == payload
    side = json.loads((bindir / "scip-clang.json").read_text())
    assert side["variant"] == "504"


def test_obtain_download_504_rejects_bad_checksum(tmp_path: Path, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, "scip-clang-504-arm64-darwin", False)
    )

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""

        if "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"fake-binary-bytes")
            return R()
        r = R()
        r.stdout = f"{'a' * 64}  scip-clang-504-arm64-darwin"  # well-formed but wrong
        return r

    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    p, out = _scripted_prompter([])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source="download-504")
    assert result == "failed"
    assert not (bindir / "scip-clang").exists()
    assert any("checksum mismatch" in line for line in out)


def test_obtain_download_504_rejects_malformed_sha(tmp_path: Path, monkeypatch) -> None:
    """A .sha256 sidecar whose first token isn't a 64-hex digest is a fetch
    failure — no unverified binary is kept."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, "scip-clang-504-arm64-darwin", False)
    )

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 0
            stdout = ""

        if "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"fake-binary-bytes")
            return R()
        r = R()
        r.stdout = "deadbeef  scip-clang-504-arm64-darwin"
        return r

    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    p, out = _scripted_prompter([])
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source="download-504")
    assert result == "failed"
    assert not (bindir / "scip-clang").exists()
    assert any("no valid sha256" in line for line in out)


def test_obtain_download_504_curl_failures_cleanup(tmp_path: Path, monkeypatch) -> None:
    """curl failing on the binary itself or on the .sha256 sidecar: both fail and
    no partial file is left behind."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, "scip-clang-504-arm64-darwin", False)
    )

    def attempt(binary_ok: bool, sha_ok: bool) -> str:
        def fake_run(cmd, **kwargs):
            class R:
                returncode = 0
                stdout = ""

            if "-o" in cmd:  # the binary download; leaves a partial file even on failure
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"partial-")
                r = R()
                r.returncode = 0 if binary_ok else 1
                return r
            r = R()  # the .sha256 fetch
            r.returncode = 0 if sha_ok else 1
            return r

        monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
        p, _ = _scripted_prompter([])
        return setup_cmd.obtain_scip_clang(p, bin_dir=bindir, source="download-504")

    assert attempt(binary_ok=False, sha_ok=True) == "failed"
    assert not (bindir / "scip-clang").exists()
    assert attempt(binary_ok=True, sha_ok=False) == "failed"
    assert not (bindir / "scip-clang").exists()


def test_obtain_interactive_menu_defaults_to_download_504(tmp_path: Path, monkeypatch) -> None:
    """Interactive with no --scip-source and a native #504 asset: the menu's
    default (what an empty answer accepts) is download-504."""
    bindir = tmp_path / "bin"
    monkeypatch.setattr(
        setup_cmd, "platform_sources", lambda: (None, "scip-clang-504-arm64-darwin", False)
    )

    seen: dict[str, object] = {}
    real_select = Prompter.select

    def recording_select(self, message, options, default):
        seen["default"] = default
        seen["first"] = options[0][0]
        return real_select(self, message, options, default)

    monkeypatch.setattr(Prompter, "select", recording_select)
    downloads: list[str] = []

    def fake_download_504(bin_dir, asset, version, p):
        downloads.append(asset)
        return True

    monkeypatch.setattr(setup_cmd, "_download_504", fake_download_504)

    p, out = _scripted_prompter([""])  # empty answer accepts the default
    result = setup_cmd.obtain_scip_clang(p, bin_dir=bindir)
    assert result == "present"
    assert seen["default"] == "download-504"
    assert seen["first"] == "download-504"
    assert downloads == ["scip-clang-504-arm64-darwin"]


def _boom(_prompt: str) -> str:
    raise AssertionError("must not prompt when a source is given")


def test_register_mcp_skips_without_claude(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup_cmd, "_claude_available", lambda: False)
    p, out = _scripted_prompter([])
    assert setup_cmd.register_mcp(p) == "skipped"
    assert any("claude" in line.lower() for line in out)


def _skill_env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Isolate HOME/XDG_CONFIG_HOME/PATH; return (claude_dest, opencode_dest)."""
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    (home / ".claude").mkdir(parents=True)
    (xdg / "opencode").mkdir(parents=True)
    (tmp_path / "bin").mkdir()  # no claude/opencode CLI on PATH
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    return home / ".claude/skills/cppgraph/SKILL.md", xdg / "opencode/skills/cppgraph/SKILL.md"


def test_install_skill_installs_for_both_detected(tmp_path: Path, monkeypatch) -> None:
    claude_dest, opencode_dest = _skill_env(tmp_path, monkeypatch)
    p, out = _scripted_prompter([])
    statuses = setup_cmd.install_skill(p)
    assert statuses == {"claude": "installed", "opencode": "installed"}
    source = setup_cmd._repo_root() / "skills" / "cppgraph" / "SKILL.md"
    assert claude_dest.read_bytes() == source.read_bytes()
    assert opencode_dest.read_bytes() == source.read_bytes()
    assert any("Installed the cppgraph skill" in line for line in out)


def test_install_skill_skips_when_neither_detected(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    p, out = _scripted_prompter([])
    statuses = setup_cmd.install_skill(p)
    assert statuses == {"claude": "not_detected", "opencode": "not_detected"}
    assert not (home / ".claude" / "skills").exists()
    assert any("no Claude Code or OpenCode" in line for line in out)


def test_install_skill_keeps_identical(tmp_path: Path, monkeypatch) -> None:
    claude_dest, opencode_dest = _skill_env(tmp_path, monkeypatch)
    source = setup_cmd._repo_root() / "skills" / "cppgraph" / "SKILL.md"
    for dest in (claude_dest, opencode_dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
    p, _ = _scripted_prompter([])
    statuses = setup_cmd.install_skill(p)
    assert statuses == {"claude": "kept", "opencode": "kept"}
    assert claude_dest.read_bytes() == source.read_bytes()
    assert opencode_dest.read_bytes() == source.read_bytes()


def test_install_skill_overwrites_stale(tmp_path: Path, monkeypatch) -> None:
    claude_dest, opencode_dest = _skill_env(tmp_path, monkeypatch)
    source = setup_cmd._repo_root() / "skills" / "cppgraph" / "SKILL.md"
    for dest in (claude_dest, opencode_dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"stale content")
    p, out = _scripted_prompter([])
    statuses = setup_cmd.install_skill(p)
    assert statuses == {"claude": "installed", "opencode": "installed"}
    assert claude_dest.read_bytes() == source.read_bytes()
    assert opencode_dest.read_bytes() == source.read_bytes()


def test_install_skill_claude_cli_only(tmp_path: Path, monkeypatch) -> None:
    """claude found on PATH but no ~/.claude dir: the skill is still installed."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\n")
    (bin_dir / "claude").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    p, _ = _scripted_prompter([])
    statuses = setup_cmd.install_skill(p)
    dest = home / ".claude/skills/cppgraph/SKILL.md"
    assert statuses["claude"] == "installed"
    assert dest.read_bytes() == (setup_cmd._repo_root() / "skills/cppgraph/SKILL.md").read_bytes()
