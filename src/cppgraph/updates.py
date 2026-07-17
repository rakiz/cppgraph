"""Update / rebuild advice for `cppgraph status`.

`status` answers "should I trust this graph?" for the *checkout*; this module
answers the same for the *tool*: is a newer cppgraph published, and — the part
that actually stings — will adopting it (or the version already installed)
require a full graph rebuild? A rebuild is minutes of indexing, so the whole
point is to warn *before* the user upgrades and finds themselves blocked.

The source of truth is a small `versions.json` hosted on GitHub (see
`versions.json` at the repo root): `latest` plus a per-release `requires_rebuild`
flag. We fetch it best-effort (short timeout, on-disk cache, silent when
offline) and derive two independent signals:

- **tool update**: a version newer than the running binary exists;
  `update_requires_rebuild` if any release in `(current, latest]` needs a rebuild.
- **rebuild now**: the *installed* binary is already newer than the version the
  graph was built with, across a `requires_rebuild` boundary — so the graph is
  silently degraded and should be rebuilt regardless of any further update.

The comparison/advice logic (`compute_advice`) is pure and unit-tested; the
network+cache layer around it fails soft, so `status` never breaks or hangs on a
missing network.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_VERSIONS_URL = "https://raw.githubusercontent.com/rakiz/cppgraph/main/versions.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60
_FETCH_TIMEOUT_SECONDS = 2.0
_ENV_DISABLE = "CPPGRAPH_NO_UPDATE_CHECK"
_ENV_URL = "CPPGRAPH_VERSIONS_URL"


def _git_describe() -> str | None:
    """`git describe --tags` of the checkout this package lives in, or None.

    cppgraph is pure Python installed editable from a git checkout, so a version
    *is* a tag: describing the working tree reports the truth live, without a
    build step or a hand-maintained version constant — checkout a different tag
    and the reported version follows, no reinstall. None when there are no tags
    yet (fresh clone), or the source isn't a git checkout (tarball install)."""
    import subprocess

    pkg_dir = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(pkg_dir), "describe", "--tags", "--dirty"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def current_version() -> str | None:
    """The running version. Preferred source is the git tag of the checkout
    (`git describe`), so it tracks the tag you have out; falls back to installed
    package metadata, then the hard-coded `__version__`. None if none resolve."""
    described = _git_describe()
    if described:
        return described
    try:
        from importlib.metadata import version

        return version("cppgraph")
    except Exception:
        try:
            from cppgraph import __version__

            return __version__
        except Exception:
            return None


def parse_version(v: str | None) -> tuple[int, ...]:
    """Lenient dotted-numeric parse for ordering: `"0.2.10"` -> `(0, 2, 10)`.

    Non-numeric trailing bits (e.g. a `-rc1` suffix) are dropped at the first
    unpar­seable component; missing/empty -> `()` which sorts lowest.
    """
    if not v:
        return ()
    out: list[int] = []
    for part in v.strip().lstrip("v").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        out.append(int(num))
    return tuple(out)


def _releases_between(
    releases: list[dict[str, Any]], low: str | None, high: str | None
) -> list[dict[str, Any]]:
    """Releases with `low < version <= high` (half-open at the bottom), so a jump
    *to* `high` includes `high` itself but not the version you're already on."""
    lo, hi = parse_version(low), parse_version(high)
    return [r for r in releases if lo < parse_version(r.get("version")) <= hi]


def compute_advice(
    data: dict[str, Any], current: str | None, graph_built_with: str | None
) -> dict[str, Any]:
    """Pure advice from a parsed `versions.json`, the running version, and the
    version the graph was built with. No I/O — this is the unit-tested core."""
    releases = data.get("releases") or []
    latest = data.get("latest")
    cur_v, latest_v = parse_version(current), parse_version(latest)

    # Scan the WHOLE jump `(current, latest]` — a rebuild boundary several
    # versions back still counts, so being multiple versions behind is handled.
    update_available = bool(latest) and latest_v > cur_v
    update_reqs = _releases_between(releases, current, latest) if update_available else []
    rebuild_versions = [r.get("version") for r in update_reqs if r.get("requires_rebuild")]

    # Rebuild-now: the installed binary is ahead of the graph's build version
    # across a requires_rebuild boundary -> the graph is stale for THIS binary.
    rebuild_reqs = _releases_between(releases, graph_built_with, current)
    rebuild_now = bool(graph_built_with) and any(r.get("requires_rebuild") for r in rebuild_reqs)

    advice: dict[str, Any] = {
        "checked": True,
        "current_version": current,
        "latest_version": latest,
        "update_available": update_available,
    }
    if update_available:
        advice["update_requires_rebuild"] = bool(rebuild_versions)
        if rebuild_versions:
            advice["rebuild_required_at"] = rebuild_versions  # boundary version(s) in the jump
        advice["update_message"] = (
            f"cppgraph {latest} is available (you have {current or '?'})."
            + (
                f" Upgrading crosses a graph-format change ({', '.join(rebuild_versions)}),"
                " so it will require a full re-index — budget indexing time before you switch."
                if rebuild_versions
                else " No graph rebuild needed to upgrade."
            )
        )
    if rebuild_now:
        advice["rebuild_recommended"] = True
        advice["rebuild_message"] = (
            f"this graph was built with cppgraph {graph_built_with} but you now run "
            f"{current or '?'}, whose format changed — rebuild it "
            "(scripts/reindex.sh) for correct and complete results."
        )
    return advice


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "cppgraph" / "versions.json"


def _load_cache(path: Path, ttl: float) -> dict[str, Any] | None:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(blob["fetched_at"]) <= ttl:
            return blob["data"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _store_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")
    except OSError:
        pass


def fetch_versions(
    *, url: str | None = None, force: bool = False, ttl: float = _CACHE_TTL_SECONDS
) -> dict[str, Any] | None:
    """The `versions.json` payload: fresh cache if within `ttl` (unless `force`),
    else a network fetch with a short timeout, re-caching on success. `None` when
    offline/unreachable and no usable cache — the caller treats that as "unknown"."""
    resolved_url = url or os.environ.get(_ENV_URL) or DEFAULT_VERSIONS_URL
    cache = _cache_path()
    if not force:
        cached = _load_cache(cache, ttl)
        if cached is not None:
            return cached
    try:
        with urllib.request.urlopen(resolved_url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return _load_cache(cache, float("inf")) if force else None  # stale-but-better-than-nothing
    _store_cache(cache, data)
    return data


def update_advice(graph_built_with: str | None, *, force: bool = False) -> dict[str, Any]:
    """Top-level entry for `status`: honour the opt-out env var, fetch (cached),
    and return advice. Always returns a dict; `checked=False` when the check was
    disabled or the registry couldn't be reached."""
    if os.environ.get(_ENV_DISABLE):
        return {"checked": False, "reason": f"disabled via {_ENV_DISABLE}"}
    data = fetch_versions(force=force)
    if data is None:
        return {"checked": False, "reason": "version registry unreachable (offline?)"}
    return compute_advice(data, current_version(), graph_built_with)
