"""`cppgraph setup` — obtain the scip-clang indexer, register the MCP server, then
hand off to the project index wizard.

The Python venv itself is created by the thin `scripts/setup.sh` launcher (it has
to exist before this code can run); everything past that lives here. Each stage
detects what is already in place and asks before doing (or redoing) it, so a
per-machine binary that took 30-60 minutes to build is never clobbered silently.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cppgraph.init import find_compdb, scip_clang_bin_dir
from cppgraph.prompt import Prompter, make_prompter

_DEFAULT_SCIP_VERSION = "0.4.0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pinned_scip_version() -> str:
    """The pinned scip-clang version from versions.json, else a known-good default."""
    try:
        data = json.loads((_repo_root() / "versions.json").read_text())
        v = str((data.get("scip_clang") or {}).get("version", "")).lstrip("v")
        return v or _DEFAULT_SCIP_VERSION
    except (OSError, ValueError):
        return _DEFAULT_SCIP_VERSION


def platform_sources() -> tuple[str | None, bool]:
    """`(native_asset, host_can_build)` for this machine. `native_asset` is the
    prebuilt release asset name, or None when none is published; `host_can_build` is
    True on Linux (a local #504 build compiles a Linux binary for the host)."""
    system, machine = platform.system(), platform.machine()
    native = {
        ("Darwin", "arm64"): "scip-clang-arm64-darwin",
        ("Linux", "x86_64"): "scip-clang-x86_64-linux",
    }.get((system, machine))
    host_can_build = system == "Linux" and machine in ("x86_64", "aarch64", "arm64")
    return native, host_can_build


def read_sidecar(bin_dir: Path) -> dict | None:
    sidecar = bin_dir / "scip-clang.json"
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None


def _write_sidecar(bin_dir: Path, version: str, variant: str, source: str) -> None:
    (bin_dir / "scip-clang.json").write_text(
        json.dumps(
            {
                "version": version,
                "variant": variant,
                "source": source,
                "installed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    )


def _download_scip(bin_dir: Path, asset: str, version: str, p: Prompter) -> bool:
    binary = bin_dir / "scip-clang"
    tag = f"v{version}"
    url = f"https://github.com/sourcegraph/scip-clang/releases/download/{tag}/{asset}"
    p.note(f"==> Downloading scip-clang {tag} ({asset})")
    proc = subprocess.run(["curl", "-fL", "--retry", "3", "-o", str(binary), url])
    if proc.returncode != 0:
        binary.unlink(missing_ok=True)
        p.note(f"error: failed to download from {url} — check network/proxy and retry.")
        return False
    binary.chmod(0o755)
    _write_sidecar(bin_dir, version, "stock", "download")
    return True


def _build_scip(bin_dir: Path, p: Prompter) -> bool:
    build = _repo_root() / "docker" / "build-scip-clang" / "build.sh"
    if not build.is_file():
        p.note(f"error: build script not found at {build}.")
        return False
    p.note("==> Building scip-clang locally with enclosing_range / #504 (~30-60 min)")
    proc = subprocess.run([str(build), str(bin_dir)])
    return proc.returncode == 0


def obtain_scip_clang(
    p: Prompter, *, bin_dir: Path | None = None, from_scratch: bool = False
) -> str:
    """Stage S2. Returns one of: `present` (a usable binary is in place),
    `emulate` (no host binary — index via a container later), `aborted`, `failed`."""
    bin_dir = bin_dir or scip_clang_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary = bin_dir / "scip-clang"
    native, host_can_build = platform_sources()
    version = _pinned_scip_version()

    if os.access(binary, os.X_OK) and not from_scratch:
        side = read_sidecar(bin_dir) or {}
        p.panel(
            "scip-clang already installed",
            [
                ("path", str(binary)),
                ("variant", side.get("variant", "unknown")),
                ("version", side.get("version", "unknown")),
                ("installed", side.get("installed_at", "unknown")),
            ],
        )
        if not p.confirm("Re-obtain it (replace the current binary)?", default=False):
            return "present"

    # Build the selectable source list for this host, each with its rough cost.
    options: list[tuple[str, str]] = []
    if native:
        options.append(("download", "download prebuilt binary (stock, no #504) — ~1 min"))
    if host_can_build:
        options.append(("build", "build #504 locally — ~30-60 min, needs Docker"))
    options.append(("emulate", "no host binary; index via an x86 container — slower later"))
    options.append(("abort", "don't install — stop setup"))

    default = "download" if native else ("build" if host_can_build else "emulate")
    choice = p.select("How should scip-clang be obtained?", options, default)

    if choice == "abort":
        p.note("Aborted — scip-clang not installed.")
        return "aborted"
    if choice == "download":
        if not native:
            p.note("error: no prebuilt binary for this platform.")
            return "failed"
        return "present" if _download_scip(bin_dir, native, version, p) else "failed"
    if choice == "build":
        if not host_can_build:
            p.note("error: a local build only works on a Linux host.")
            return "failed"
        return "present" if _build_scip(bin_dir, p) else "failed"
    # emulate
    p.note("==> No host binary installed — indexing goes through an x86 container.")
    return "emulate"


def _claude_available() -> bool:
    from shutil import which

    return which("claude") is not None


def _mcp_registered() -> bool:
    try:
        proc = subprocess.run(["claude", "mcp", "get", "cppgraph"], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def register_mcp(p: Prompter, *, from_scratch: bool = False) -> str:
    """Stage S3. Registers the cppgraph MCP server (user scope). Returns
    `registered`, `kept`, `skipped` (no claude CLI), or `failed`."""
    if not _claude_available():
        p.note("note: the `claude` CLI was not found — skipping MCP registration.")
        return "skipped"
    mcp_bin = _repo_root() / ".venv" / "bin" / "cppgraph-mcp"
    if not mcp_bin.is_file():
        p.note(f"note: {mcp_bin} not found — is the venv set up? Skipping MCP registration.")
        return "skipped"
    if _mcp_registered() and not from_scratch:
        if not p.confirm("The MCP server 'cppgraph' is already registered. Re-register?", False):
            return "kept"
    subprocess.run(["claude", "mcp", "remove", "cppgraph", "--scope", "user"], capture_output=True)
    proc = subprocess.run(
        ["claude", "mcp", "add", "cppgraph", "--scope", "user", "--", str(mcp_bin)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        p.note(f"error: MCP registration failed: {proc.stderr.strip()}")
        return "failed"
    p.note("==> Registered the MCP server 'cppgraph' (user scope).")
    return "registered"


def run_setup(
    *,
    prompter: Prompter | None = None,
    from_scratch: bool = False,
    chain_index: bool = True,
) -> int:
    """`cppgraph setup`: obtain scip-clang (S2), register the MCP server (S3), then
    hand off to the project index wizard (S4). Returns a process exit code."""
    p = prompter or make_prompter()

    scip = obtain_scip_clang(p, from_scratch=from_scratch)
    if scip in ("aborted", "failed"):
        return 3 if scip == "aborted" else 1

    register_mcp(p, from_scratch=from_scratch)

    p.note("")
    p.note("Tool setup complete.")
    if not chain_index:
        return 0

    # S4: index a project now if we're standing in one, else point the way.
    if find_compdb(Path.cwd()) is not None:
        p.note("Found a compile_commands.json here — starting the project index wizard.")
        p.note("")
        from cppgraph.init import run_init

        return run_init()
    p.note("To index a project, run this from the project directory:")
    p.note(f"  {_repo_root() / 'scripts' / 'index.sh'}")
    return 0
