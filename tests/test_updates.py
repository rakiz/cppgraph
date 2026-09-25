"""Tests for the update/rebuild advice (`cppgraph.updates`).

The comparison logic is pure and tested directly against a fixture registry; the
network+cache layer is exercised only for its fail-soft behaviour (no real HTTP).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cppgraph import updates

REGISTRY = {
    "latest": "0.3.0",
    "releases": [
        {"version": "0.1.0", "rebuild": "none"},
        {"version": "0.2.0", "rebuild": "reindex"},  # graph model / extraction change
        {"version": "0.3.0", "rebuild": "none"},
    ],
}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def test_git_describe_ignores_non_version_tags_from_this_repo(tmp_path: Path) -> None:
    """Regression: this repo's tag namespace also carries
    `scip-clang-patched-vX.Y.Z-pN` / `scip-clang-504-vX.Y.Z` release tags
    (published on the SAME repo by scripts/publish-scip-clang-patched.sh),
    alongside cppgraph's own `vX.Y.Z` tags. An unfiltered `git describe --tags`
    picks whichever tag is nearest in the commit graph, which can be one of
    those scip-clang tags instead of a real cppgraph version — observed in
    practice breaking `status`'s update-advice comparison. `_git_describe`
    must only ever consider `vX.Y.Z`-shaped tags."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "v0.2.0 commit")
    _git(tmp_path, "tag", "v0.2.0")

    (tmp_path / "a.txt").write_text("2\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "later, tagged as a scip-clang binary release")
    # A tag nearer than v0.2.0, on the same repo, shaped like our own release
    # tags — but not a cppgraph version at all.
    _git(tmp_path, "tag", "scip-clang-patched-v0.4.0-p6")

    described = updates._git_describe(pkg_dir=tmp_path)
    assert described is not None
    assert described.startswith("v0.2.0-"), described
    assert "scip-clang-patched" not in described


def test_git_describe_still_finds_exact_version_tag(tmp_path: Path) -> None:
    """Sanity check the fix doesn't just always fail to match: an exact
    cppgraph version tag on HEAD is still reported plainly."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("1\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "tag", "v0.3.0")

    assert updates._git_describe(pkg_dir=tmp_path) == "v0.3.0"


def test_parse_version_orders_and_tolerates_noise() -> None:
    assert updates.parse_version("0.2.10") > updates.parse_version("0.2.9")
    assert updates.parse_version("v1.0") == (1, 0)
    assert updates.parse_version("0.2.0-rc1") == (0, 2, 0)
    assert updates.parse_version(None) == ()  # sorts lowest


def test_version_ordering_is_numeric_not_lexical() -> None:
    # the classic trap: as strings "10.0.0" < "2.0.0"; as parsed ints it must not
    assert updates.parse_version("10.0.0") > updates.parse_version("2.0.0")
    assert updates.parse_version("1.10.0") > updates.parse_version("1.9.0")
    reg = {
        "latest": "10.0.0",
        "releases": [
            {"version": "2.0.0", "rebuild": "reindex"},
            {"version": "10.0.0", "rebuild": "none"},
        ],
    }
    # on 3.0.0 -> jump to 10.0.0 must NOT re-include the older 2.0.0 boundary
    adv = updates.compute_advice(reg, current="3.0.0", graph_built_with="3.0.0")
    assert adv["update_available"] is True
    assert adv.get("update_requires_rebuild") is False
    assert adv["update_rebuild"] == "none"


def test_update_available_flags_rebuild_when_crossing_boundary() -> None:
    # on 0.1.0, latest 0.3.0 -> the 0.2.0 rebuild boundary is in (0.1.0, 0.3.0]
    adv = updates.compute_advice(REGISTRY, current="0.1.0", graph_built_with="0.1.0")
    assert adv["update_available"] is True
    assert adv["update_requires_rebuild"] is True
    assert adv["update_rebuild"] == "reindex"
    assert adv["rebuild_required_at"] == ["0.2.0"]  # names the boundary version
    assert "0.2.0" in adv["update_message"]


def test_rebuild_boundary_detected_when_several_versions_behind() -> None:
    # installed 0.0.5, way behind: the jump to 0.3.0 spans 0.1.0/0.2.0/0.3.0,
    # and 0.2.0 needs a rebuild -> flagged even though it's a middle version.
    reg = {
        "latest": "0.3.0",
        "releases": [
            {"version": "0.1.0", "rebuild": "none"},
            {"version": "0.2.0", "rebuild": "reindex"},
            {"version": "0.3.0", "rebuild": "none"},
        ],
    }
    adv = updates.compute_advice(reg, current="0.0.5", graph_built_with="0.0.5")
    assert adv["update_requires_rebuild"] is True
    assert adv["rebuild_required_at"] == ["0.2.0"]


def test_update_available_without_rebuild() -> None:
    # on 0.2.0, latest 0.3.0 -> only 0.3.0 in range, which needs no rebuild
    adv = updates.compute_advice(REGISTRY, current="0.2.0", graph_built_with="0.2.0")
    assert adv["update_available"] is True
    assert adv["update_requires_rebuild"] is False
    assert adv["update_rebuild"] == "none"
    assert "no graph rebuild" in adv["update_message"].lower()


def test_no_update_when_current_is_latest() -> None:
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.3.0")
    assert adv["update_available"] is False
    assert "update_message" not in adv


def test_rebuild_now_when_binary_ahead_of_graph_across_boundary() -> None:
    # graph built with 0.1.0, binary already upgraded to 0.3.0 -> crossed 0.2.0
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.1.0")
    assert adv["update_available"] is False  # already on latest
    assert adv["rebuild_recommended"] is True
    assert adv["rebuild_level"] == "reindex"
    assert "index" in adv["rebuild_message"].lower()


def test_no_rebuild_now_when_graph_built_after_boundary() -> None:
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.2.0")
    assert "rebuild_recommended" not in adv


def test_store_level_is_cheaper_than_reindex() -> None:
    # a store-only boundary: flagged as a rebuild, but level "store", and the
    # message points at `cppgraph build`, not a full re-index.
    reg = {
        "latest": "0.2.0",
        "releases": [
            {"version": "0.1.0", "rebuild": "none"},
            {"version": "0.2.0", "rebuild": "store"},
        ],
    }
    adv = updates.compute_advice(reg, current="0.1.0", graph_built_with="0.1.0")
    assert adv["update_requires_rebuild"] is True
    assert adv["update_rebuild"] == "store"
    assert "cppgraph build" in adv["update_message"]
    assert "re-index" not in adv["update_message"]

    # rebuild-now at store level
    adv2 = updates.compute_advice(reg, current="0.2.0", graph_built_with="0.1.0")
    assert adv2["rebuild_recommended"] is True
    assert adv2["rebuild_level"] == "store"


def test_reindex_dominates_store_across_multi_version_jump() -> None:
    # a jump crossing both a store and a reindex boundary needs the reindex
    reg = {
        "latest": "0.3.0",
        "releases": [
            {"version": "0.1.0", "rebuild": "store"},
            {"version": "0.2.0", "rebuild": "reindex"},
            {"version": "0.3.0", "rebuild": "none"},
        ],
    }
    adv = updates.compute_advice(reg, current="0.0.5", graph_built_with="0.0.5")
    assert adv["update_rebuild"] == "reindex"
    assert adv["rebuild_required_at"] == ["0.1.0", "0.2.0"]


def test_update_advice_respects_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPPGRAPH_NO_UPDATE_CHECK", "1")
    adv = updates.update_advice("0.1.0")
    assert adv["checked"] is False
    assert "disabled" in adv["reason"]


def test_update_advice_fails_soft_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CPPGRAPH_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(updates, "fetch_versions", lambda **_: None)  # simulate offline
    adv = updates.update_advice("0.1.0")
    assert adv["checked"] is False
    assert "unreachable" in adv["reason"]


# ---- scip-clang dependency advice (compute_scip_advice) --------------------

# VERSION and (for a "patched" binary) the patchset are pinned; the variant
# (stock vs patched) itself is reported for information, never treated as stale.
_PIN = {"version": "0.4.0", "rebuild": "reindex", "patchset_version": 2}


def test_scip_advice_no_pin_is_unchecked() -> None:
    assert updates.compute_scip_advice(None, {"version": "0.4.0"}, None)["checked"] is False
    assert updates.compute_scip_advice({"rebuild": "reindex"}, None, None)["checked"] is False


def test_scip_advice_binary_ok_when_version_matches() -> None:
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "stock"}, None)
    assert adv["binary_status"] == "ok"
    assert adv["pinned_version"] == "0.4.0"


def test_scip_advice_variant_difference_is_not_stale() -> None:
    # A patched binary against a version-only check is fine, not "stale" — variant
    # is a capability level, not a staleness axis. It's reported for information.
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "patched"}, None)
    assert adv["binary_status"] == "ok"
    assert adv["installed_variant"] == "patched"


def test_scip_advice_binary_stale_on_version_mismatch() -> None:
    adv = updates.compute_scip_advice(_PIN, {"version": "0.3.0", "variant": "stock"}, None)
    assert adv["binary_status"] == "stale"
    assert "0.3.0" in adv["binary_message"] and "0.4.0" in adv["binary_message"]


def test_scip_advice_binary_unknown_without_sidecar() -> None:
    adv = updates.compute_scip_advice(_PIN, None, None)
    assert adv["binary_status"] == "unknown"


def test_scip_advice_reindex_on_graph_version_mismatch() -> None:
    graph = {"version": "0.3.0", "variant": "stock"}
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0"}, graph)
    assert adv.get("reindex_recommended") is True
    assert "re-index" in adv["reindex_message"]


def test_scip_advice_no_reindex_on_variant_only_difference() -> None:
    # Same version, different variant -> NOT a reindex trigger; just reported.
    graph = {"version": "0.4.0", "variant": "patched"}
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "stock"}, graph)
    assert "reindex_recommended" not in adv
    assert adv["graph_variant"] == "patched"


def test_scip_advice_no_reindex_when_rebuild_none() -> None:
    pin = {"version": "0.4.0", "rebuild": "none"}
    graph = {"version": "0.3.0", "variant": "stock"}
    adv = updates.compute_scip_advice(pin, {"version": "0.4.0"}, graph)
    assert "reindex_recommended" not in adv


# ---- patchset staleness (patched-family binaries: "patched" + pre-rename
# spellings "504"/"enclosing_range-504") ---------------------------------------


def test_scip_advice_patchset_stale_when_installed_older() -> None:
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 1},
        None,
    )
    assert adv["binary_status"] == "ok"  # version matches; only the patchset lags
    assert adv["patchset_status"] == "stale"
    m = adv["patchset_message"]
    assert "p1" in m and "p2" in m
    # Actionable, both steps in order: fix the binary, THEN re-index the graphs
    # (the message must not read as "only matters for a future reindex").
    assert ("re-download" in m) or ("rebuild" in m)
    assert "THEN" in m
    assert "re-index your graphs" in m
    assert "cppgraph update" in m
    assert "every TU" in m or "every translation unit" in m


def test_scip_advice_patchset_quiet_when_current() -> None:
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 2},
        None,
    )
    assert "patchset_status" not in adv


def test_scip_advice_patchset_missing_sidecar_field_counts_as_1() -> None:
    # A "patched" sidecar from before the field existed: unknown -> 1, not a crash.
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "patched"}, None)
    assert adv["patchset_status"] == "stale"


def test_scip_advice_patchset_missing_field_counts_as_1_for_pre_rename_504() -> None:
    # Pre-rename sidecars on disk spell the variant "504" — same patched family,
    # so a missing patchset field must trigger the same p1 advisory as "patched".
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "504"}, None)
    assert adv["binary_status"] == "ok"
    assert adv["patchset_status"] == "stale"
    assert "p1" in adv["patchset_message"] and "p2" in adv["patchset_message"]


def test_scip_advice_patchset_missing_field_counts_as_1_for_pre_rename_enclosing_range() -> None:
    # Same, for the old local-build spelling "enclosing_range-504".
    adv = updates.compute_scip_advice(
        _PIN, {"version": "0.4.0", "variant": "enclosing_range-504"}, None
    )
    assert adv["binary_status"] == "ok"
    assert adv["patchset_status"] == "stale"


def test_scip_advice_patchset_missing_field_quiet_when_pin_is_1() -> None:
    # Missing counts as p1: when the pin IS p1 they match, so no advisory —
    # the absence default must not manufacture a false "you're stale" nag.
    pin = {"version": "0.4.0", "rebuild": "reindex", "patchset_version": 1}
    adv = updates.compute_scip_advice(pin, {"version": "0.4.0", "variant": "patched"}, None)
    assert adv["binary_status"] == "ok"
    assert "patchset_status" not in adv


def test_scip_advice_patchset_ignored_for_stock() -> None:
    # No patch bundle on stock — patchset pinning doesn't apply to it.
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "stock"}, None)
    assert "patchset_status" not in adv


def test_scip_advice_patchset_ignored_when_version_differs() -> None:
    # A version mismatch already advises re-running setup; no second nag.
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.3.0", "variant": "patched", "patchset_version": 1},
        None,
    )
    assert adv["binary_status"] == "stale"
    assert "patchset_status" not in adv


# ---- graph patchset vs the installed binary: reindex advice + the update gate --
#
# A patchset bump changes scip-clang's output for EVERY translation unit (e.g.
# patchset 7 restored enclosing_range for macro-introduced definitions), so an
# existing graph built with an older patchset is stale for the installed binary
# in a way an incremental update can never fix — it needs a full re-index.


def test_scip_advice_reindex_on_graph_patchset_older_than_installed() -> None:
    graph = {"version": "0.4.0", "variant": "patched", "patchset": "6"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert adv.get("reindex_recommended") is True
    # The message says the GRAPH is stale, names both patchsets, and makes the
    # fix unambiguous: a FULL re-index, not an incremental update.
    assert "graph is stale" in adv["reindex_message"]
    assert "p6" in adv["reindex_message"] and "p7" in adv["reindex_message"]
    assert "FULL re-index" in adv["reindex_message"]
    assert "incremental update" in adv["reindex_message"]
    assert adv["graph_patchset"] == 6
    assert adv["installed_patchset"] == 7


def test_scip_advice_patchset_reindex_accepts_int_recorded_value() -> None:
    """Store meta values are strings, but a direct caller may pass the int —
    both parse."""
    graph = {"version": "0.4.0", "variant": "patched", "patchset": 6}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert adv.get("reindex_recommended") is True


def test_scip_advice_no_patchset_reindex_when_patchsets_equal() -> None:
    graph = {"version": "0.4.0", "variant": "patched", "patchset": "7"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert "reindex_recommended" not in adv


def test_scip_advice_no_patchset_reindex_when_installed_older() -> None:
    # The graph is NEWER than the installed binary: nothing to re-index for the
    # graph (the binary itself gets the patchset_status nag instead).
    graph = {"version": "0.4.0", "variant": "patched", "patchset": "7"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 6},
        graph,
    )
    assert "reindex_recommended" not in adv


def test_scip_advice_no_patchset_reindex_for_stock_graph() -> None:
    # A stock-built graph has no patch bundle — patchset comparison is meaningless.
    graph = {"version": "0.4.0", "variant": "stock", "patchset": "6"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert "reindex_recommended" not in adv


def test_scip_advice_no_patchset_reindex_for_stock_installed() -> None:
    # Same in the other direction: a stock installed binary can't be "newer
    # patchset" than anything.
    graph = {"version": "0.4.0", "variant": "patched", "patchset": "6"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "stock"},
        graph,
    )
    assert "reindex_recommended" not in adv


def test_scip_advice_no_patchset_reindex_when_graph_records_no_patchset() -> None:
    """A legacy graph predating patchset recording must not be guessed as any
    patchset — no advice, and `update` stays incremental (don't guess)."""
    graph = {"version": "0.4.0", "variant": "patched"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert "reindex_recommended" not in adv


def test_scip_advice_no_patchset_reindex_when_installed_unknown() -> None:
    graph = {"version": "0.4.0", "variant": "patched", "patchset": "6"}
    adv = updates.compute_scip_advice(_PIN, None, graph)
    assert "reindex_recommended" not in adv


def test_scip_advice_exposes_patchsets_informationally() -> None:
    """Both patchsets surface even with no gap (matching the CLI status line /
    MCP fields) — informational, like the variants."""
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        {"version": "0.4.0", "variant": "patched", "patchset": "7"},
    )
    assert adv["installed_patchset"] == 7
    assert adv["graph_patchset"] == 7
    assert "reindex_recommended" not in adv


def test_scip_advice_patchset_reindex_for_pre_rename_504_variant() -> None:
    # Pre-rename sidecar/meta spellings are the same patched family.
    graph = {"version": "0.4.0", "variant": "504", "patchset": "6"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert adv.get("reindex_recommended") is True


# ---- legacy-graph deduction ----------------------------------------------------
#
# `index_tool_patchset` recording was introduced in cppgraph 0.4.4 (alongside
# patchset 7 of the bundle), so EVERY graph built before that release — i.e.
# every graph that motivated this feature — carries no patchset. For those the
# patchset is DEDUCED from release history, not guessed: a patched-family graph
# built by cppgraph < 0.4.4 necessarily predates patchset recording, so its
# effective patchset is 1 and any newer installed patched binary raises the gap.


def test_patchset_gap_deduced_for_pre_recording_legacy_graph() -> None:
    gap = updates.patchset_gap(
        {"version": "0.4.0", "variant": "patched", "cppgraph_version": "0.4.2"},
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
    )
    assert gap == (1, 7)


def test_scip_advice_reindex_on_deduced_legacy_patchset() -> None:
    """End to end through `status`'s advice: the p6-era graph every existing
    user has (patched, pre-0.4.4, no recorded patchset) against a p7 binary."""
    graph = {"version": "0.4.0", "variant": "patched", "cppgraph_version": "0.4.2"}
    adv = updates.compute_scip_advice(
        _PIN,
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        graph,
    )
    assert adv.get("reindex_recommended") is True
    assert adv["graph_patchset"] == 1  # the deduced effective patchset
    assert "graph is stale" in adv["reindex_message"]


def test_patchset_gap_none_when_legacy_cppgraph_version_is_current() -> None:
    """At >= 0.4.4 a full build records the patchset, so a patched graph without
    one is genuinely unknowable (e.g. `build --scip` without the flag) — no
    guess, no gap."""
    graph = {"version": "0.4.0", "variant": "patched", "cppgraph_version": "0.4.4"}
    assert (
        updates.patchset_gap(
            graph,
            {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        )
        is None
    )
    graph["cppgraph_version"] = "0.5.0"
    assert (
        updates.patchset_gap(
            graph,
            {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        )
        is None
    )


def test_patchset_gap_none_when_cppgraph_version_unknown() -> None:
    graph = {"version": "0.4.0", "variant": "patched"}
    assert (
        updates.patchset_gap(
            graph,
            {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        )
        is None
    )


def test_patchset_gap_none_for_stock_legacy_graph() -> None:
    """A stock-built graph has no patch bundle, however old — no patchset is
    deduced for it."""
    graph = {"version": "0.4.0", "variant": "stock", "cppgraph_version": "0.4.2"}
    assert (
        updates.patchset_gap(
            graph,
            {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
        )
        is None
    )


# ---- patchset_gap: the pure detection `cppgraph update` gates on ---------------


def test_patchset_gap_detects_older_graph() -> None:
    gap = updates.patchset_gap(
        {"version": "0.4.0", "variant": "patched", "patchset": "6"},
        {"version": "0.4.0", "variant": "patched", "patchset_version": 7},
    )
    assert gap == (6, 7)


def test_patchset_gap_none_when_equal_or_newer() -> None:
    inst = {"version": "0.4.0", "variant": "patched", "patchset_version": 7}
    assert (
        updates.patchset_gap({"version": "0.4.0", "variant": "patched", "patchset": "7"}, inst)
        is None
    )
    assert (
        updates.patchset_gap({"version": "0.4.0", "variant": "patched", "patchset": "8"}, inst)
        is None
    )


def test_patchset_gap_none_for_stock_or_unrecorded_or_unknown() -> None:
    patched = {"version": "0.4.0", "variant": "patched", "patchset_version": 7}
    # stock graph / stock installed
    assert updates.patchset_gap({"version": "0.4.0", "variant": "stock"}, patched) is None
    assert updates.patchset_gap(patched, {"version": "0.4.0", "variant": "stock"}) is None
    # graph records no patchset (legacy) — never guessed
    assert updates.patchset_gap({"version": "0.4.0", "variant": "patched"}, patched) is None
    # installed sidecar missing entirely
    assert (
        updates.patchset_gap({"version": "0.4.0", "variant": "patched", "patchset": "6"}, None)
        is None
    )


def test_patchset_gap_installed_missing_field_counts_as_1() -> None:
    # The convention compute_scip_advice established: a patched sidecar from
    # before the field existed is patchset 1 — so a p1 graph isn't "behind" it.
    gap = updates.patchset_gap(
        {"version": "0.4.0", "variant": "patched", "patchset": "1"},
        {"version": "0.4.0", "variant": "patched"},
    )
    assert gap is None
