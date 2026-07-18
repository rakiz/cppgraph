"""Tests for the update/rebuild advice (`cppgraph.updates`).

The comparison logic is pure and tested directly against a fixture registry; the
network+cache layer is exercised only for its fail-soft behaviour (no real HTTP).
"""

from __future__ import annotations

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

_PIN = {"version": "0.4.0", "variant": "stock", "rebuild": "reindex"}


def test_scip_advice_no_pin_is_unchecked() -> None:
    assert updates.compute_scip_advice(None, {"version": "0.4.0"}, None)["checked"] is False


def test_scip_advice_binary_ok_when_matching_pin() -> None:
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "stock"}, None)
    assert adv["binary_status"] == "ok"
    assert adv["pinned"] == {"version": "0.4.0", "variant": "stock"}


def test_scip_advice_binary_stale_on_variant_mismatch() -> None:
    # pin wants a patched build; installed is stock -> stale, suggests building.
    pin = {"version": "0.4.0", "variant": "enclosing_range-504", "rebuild": "reindex"}
    adv = updates.compute_scip_advice(pin, {"version": "0.4.0", "variant": "stock"}, None)
    assert adv["binary_status"] == "stale"
    assert "--scip-source build" in adv["binary_message"]


def test_scip_advice_binary_stale_suggests_download_for_stock_pin() -> None:
    adv = updates.compute_scip_advice(
        _PIN, {"version": "0.3.0", "variant": "stock"}, None
    )
    assert adv["binary_status"] == "stale"
    assert "--scip-source download" in adv["binary_message"]


def test_scip_advice_binary_unknown_without_sidecar() -> None:
    adv = updates.compute_scip_advice(_PIN, None, None)
    assert adv["binary_status"] == "unknown"


def test_scip_advice_reindex_when_graph_variant_differs() -> None:
    pin = {"version": "0.4.0", "variant": "enclosing_range-504", "rebuild": "reindex"}
    graph = {"version": "0.4.0", "variant": "stock"}
    installed = {"version": "0.4.0", "variant": "enclosing_range-504"}
    adv = updates.compute_scip_advice(pin, installed, graph)
    assert adv.get("reindex_recommended") is True
    assert "re-index" in adv["reindex_message"]


def test_scip_advice_no_reindex_when_graph_matches_pin() -> None:
    graph = {"version": "0.4.0", "variant": "stock"}
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0", "variant": "stock"}, graph)
    assert "reindex_recommended" not in adv


def test_scip_advice_no_reindex_when_rebuild_none() -> None:
    # even a variant difference doesn't force a reindex if the pin says rebuild=none.
    pin = {"version": "0.4.0", "variant": "stock", "rebuild": "none"}
    graph = {"version": "0.3.0", "variant": "stock"}
    adv = updates.compute_scip_advice(pin, {"version": "0.4.0", "variant": "stock"}, graph)
    assert "reindex_recommended" not in adv


def test_scip_advice_defaults_missing_variant_to_stock() -> None:
    # a graph/sidecar without a variant field is treated as stock.
    adv = updates.compute_scip_advice(_PIN, {"version": "0.4.0"}, {"version": "0.4.0"})
    assert adv["binary_status"] == "ok"
    assert "reindex_recommended" not in adv
