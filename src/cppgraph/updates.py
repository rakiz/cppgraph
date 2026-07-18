"""Update / rebuild advice for `cppgraph status`.

`status` answers "should I trust this graph?" for the *checkout*; this module
answers the same for the *tool*: is a newer cppgraph published, and — the part
that actually stings — will adopting it (or the version already installed)
require a full graph rebuild? A rebuild is minutes of indexing, so the whole
point is to warn *before* the user upgrades and finds themselves blocked.

The source of truth is a small `versions.json` hosted on GitHub (see
`versions.json` at the repo root): `latest` plus a per-release `rebuild` level.
The level says *which layer of the index stack* a release invalidates — cheapest
to most expensive: `none` (query-code only, store stays valid), `store` (rebuild
the SQLite store from the existing `.scip` — no recompile), `reindex` (re-run
scip-clang, then rebuild the store). We fetch best-effort (short timeout,
on-disk cache, silent when offline) and derive two independent signals:

- **tool update**: a version newer than the running binary exists;
  `update_rebuild` is the max level across `(current, latest]`
  (`update_requires_rebuild` stays as its boolean shorthand).
- **rebuild now**: the *installed* binary is already newer than the version the
  graph was built with, across a rebuild boundary — so the graph is silently
  degraded and should be rebuilt (at `rebuild_level`) regardless of any update.

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


# What a release invalidates in the index stack, cheapest -> most expensive:
#   "none"    — query-code only; the existing graph store stays valid.
#   "store"   — the SQLite store format changed; rebuild it from the existing
#               `.scip` (`cppgraph build`, no recompile — seconds/minutes).
#   "reindex" — the graph model / builder changed; re-run scip-clang to
#               regenerate the `.scip`, then rebuild the store (tens of minutes).
# See TODO.md "Rebuild advice" and DESIGN.md for the stack.
_REBUILD_LEVELS = ("none", "store", "reindex")
_LEVEL_RANK = {name: i for i, name in enumerate(_REBUILD_LEVELS)}


def _rebuild_level(release: dict[str, Any]) -> str:
    """The rebuild level a release demands, from its `rebuild` field; `none` if
    absent or unrecognised (a release that says nothing invalidates nothing)."""
    lvl = release.get("rebuild")
    return lvl if isinstance(lvl, str) and lvl in _LEVEL_RANK else "none"


def _max_level(releases: list[dict[str, Any]]) -> str:
    """The most expensive rebuild level across `releases` (they compound: a jump
    that crosses both a `store` and a `reindex` boundary needs the `reindex`)."""
    level = "none"
    for r in releases:
        if _LEVEL_RANK[_rebuild_level(r)] > _LEVEL_RANK[level]:
            level = _rebuild_level(r)
    return level


# ---- scip-clang dependency pin ---------------------------------------------
# The indexer is a versioned dependency with an identity of (version, variant):
# "stock" is the unpatched upstream release binary; a non-stock variant (e.g.
# "enclosing_range-504") is built from source with a patch. The two emit
# different `.scip`, so both the installed binary and each graph are compared
# against the pin in versions.json (`scip_clang`). See DESIGN.md § Source of truth.


def scip_clang_pin(data: dict[str, Any]) -> dict[str, Any] | None:
    """The pinned indexer identity from versions.json `scip_clang`, or None."""
    pin = data.get("scip_clang")
    return pin if isinstance(pin, dict) and pin.get("version") else None


def _scip_bin_dir() -> Path:
    """Where the scip-clang binary + its provenance sidecar live — the same
    resolution the shell scripts use (CPPGRAPH_BIN_DIR, else XDG_DATA_HOME)."""
    override = os.environ.get("CPPGRAPH_BIN_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "cppgraph" / "bin"


def installed_scip_clang() -> dict[str, Any] | None:
    """The `scip-clang.json` provenance sidecar next to the installed binary, or
    None (never installed, or installed before provenance was recorded)."""
    try:
        return json.loads((_scip_bin_dir() / "scip-clang.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scip_identity(d: dict[str, Any] | None) -> tuple[str, str] | None:
    """`(version, variant)` from a pin/sidecar/graph-meta dict, variant defaulting
    to `"stock"`; None when there's no version to compare."""
    if not d or not d.get("version"):
        return None
    return (str(d["version"]).lstrip("v"), str(d.get("variant") or "stock"))


def _scip_source_for(variant: str) -> str:
    """Which `setup.sh --scip-source` produces this variant: the stock release is
    a download, anything patched must be built."""
    return "download" if variant == "stock" else "build"


def compute_scip_advice(
    pin: dict[str, Any] | None,
    installed: dict[str, Any] | None,
    graph_scip: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pure advice about the scip-clang dependency: is the installed binary the
    pinned identity, and was *this* graph indexed with it? No I/O — unit-tested."""
    if not pin:
        return {"checked": False}
    want = _scip_identity(pin)
    if want is None:
        return {"checked": False}
    advice: dict[str, Any] = {
        "checked": True,
        "pinned": {"version": want[0], "variant": want[1]},
        "rebuild": _rebuild_level(pin),
    }
    have = _scip_identity(installed)
    if have is None:
        advice["binary_status"] = "unknown"
        advice["binary_message"] = (
            "scip-clang provenance unknown (installed before it was recorded, or not "
            "installed) — re-run scripts/setup.sh to record it."
        )
    elif have != want:
        src = _scip_source_for(want[1])
        advice["binary_status"] = "stale"
        advice["binary_message"] = (
            f"scip-clang installed is {have[0]}-{have[1]} but the pin is "
            f"{want[0]}-{want[1]} — update it: scripts/setup.sh --scip-source {src}."
        )
    else:
        advice["binary_status"] = "ok"

    g = _scip_identity(graph_scip)
    if g is not None and g != want and _rebuild_level(pin) != "none":
        advice["reindex_recommended"] = True
        advice["reindex_message"] = (
            f"this graph was indexed with scip-clang {g[0]}-{g[1]}, but the pin is "
            f"{want[0]}-{want[1]} — re-index (scripts/reindex.sh) to match."
        )
    return advice


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
    update_level = _max_level(update_reqs)
    rebuild_versions = [r.get("version") for r in update_reqs if _rebuild_level(r) != "none"]

    # Rebuild-now: the installed binary is ahead of the graph's build version
    # across a rebuild boundary -> the graph is stale for THIS binary.
    rebuild_reqs = _releases_between(releases, graph_built_with, current)
    rebuild_level = _max_level(rebuild_reqs)
    rebuild_now = bool(graph_built_with) and rebuild_level != "none"

    advice: dict[str, Any] = {
        "checked": True,
        "current_version": current,
        "latest_version": latest,
        "update_available": update_available,
    }
    if update_available:
        advice["update_requires_rebuild"] = update_level != "none"
        advice["update_rebuild"] = update_level  # "none" | "store" | "reindex"
        if rebuild_versions:
            advice["rebuild_required_at"] = rebuild_versions  # boundary version(s) in the jump
        advice["update_message"] = (
            f"cppgraph {latest} is available (you have {current or '?'})."
            + _update_rebuild_clause(update_level, rebuild_versions)
        )
    if rebuild_now:
        advice["rebuild_recommended"] = True
        advice["rebuild_level"] = rebuild_level  # "store" | "reindex"
        advice["rebuild_message"] = _rebuild_now_message(graph_built_with, current, rebuild_level)
    return advice


def _update_rebuild_clause(level: str, versions: list[Any]) -> str:
    """The rebuild sentence appended to an update message, worded per level."""
    if level == "none":
        return " No graph rebuild needed to upgrade."
    at = ", ".join(str(v) for v in versions)
    if level == "store":
        return (
            f" Upgrading crosses a store-format change ({at}), so it needs a graph store"
            " rebuild (`cppgraph build` from the existing .scip — no recompile)."
        )
    return (
        f" Upgrading crosses a graph-format change ({at}), so it will require a full"
        " re-index (re-run scip-clang) — budget indexing time before you switch."
    )


def _rebuild_now_message(graph_built_with: str | None, current: str | None, level: str) -> str:
    """The 'your graph is stale for this binary' message, worded per level."""
    head = (
        f"this graph was built with cppgraph {graph_built_with} but you now run {current or '?'}, "
    )
    if level == "store":
        return head + (
            "whose store format changed — rebuild the store (`cppgraph build` from the "
            "existing .scip, no recompile) for correct results."
        )
    return head + (
        "whose graph format changed — re-index it (scripts/reindex.sh) for correct "
        "and complete results."
    )


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


def scip_update_advice(graph_scip: dict[str, Any] | None, *, force: bool = False) -> dict[str, Any]:
    """Top-level entry for `status`: scip-clang dependency advice — installed
    binary and this graph vs the pinned identity. Fetches `versions.json` (cached,
    same source as the tool check); `checked=False` when disabled or unreachable."""
    if os.environ.get(_ENV_DISABLE):
        return {"checked": False, "reason": f"disabled via {_ENV_DISABLE}"}
    data = fetch_versions(force=force)
    if data is None:
        return {"checked": False, "reason": "version registry unreachable (offline?)"}
    return compute_scip_advice(scip_clang_pin(data), installed_scip_clang(), graph_scip)
