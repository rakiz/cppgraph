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
        {"version": "0.1.0", "requires_rebuild": False},
        {"version": "0.2.0", "requires_rebuild": True},   # schema/extraction change
        {"version": "0.3.0", "requires_rebuild": False},
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
            {"version": "2.0.0", "requires_rebuild": True},
            {"version": "10.0.0", "requires_rebuild": False},
        ],
    }
    # on 3.0.0 -> jump to 10.0.0 must NOT re-include the older 2.0.0 boundary
    adv = updates.compute_advice(reg, current="3.0.0", graph_built_with="3.0.0")
    assert adv["update_available"] is True
    assert adv.get("update_requires_rebuild") is False


def test_update_available_flags_rebuild_when_crossing_boundary() -> None:
    # on 0.1.0, latest 0.3.0 -> the 0.2.0 rebuild boundary is in (0.1.0, 0.3.0]
    adv = updates.compute_advice(REGISTRY, current="0.1.0", graph_built_with="0.1.0")
    assert adv["update_available"] is True
    assert adv["update_requires_rebuild"] is True
    assert adv["rebuild_required_at"] == ["0.2.0"]  # names the boundary version
    assert "0.2.0" in adv["update_message"]


def test_rebuild_boundary_detected_when_several_versions_behind() -> None:
    # installed 0.0.5, way behind: the jump to 0.3.0 spans 0.1.0/0.2.0/0.3.0,
    # and 0.2.0 needs a rebuild -> flagged even though it's a middle version.
    reg = {
        "latest": "0.3.0",
        "releases": [
            {"version": "0.1.0", "requires_rebuild": False},
            {"version": "0.2.0", "requires_rebuild": True},
            {"version": "0.3.0", "requires_rebuild": False},
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
    assert "no graph rebuild" in adv["update_message"].lower()


def test_no_update_when_current_is_latest() -> None:
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.3.0")
    assert adv["update_available"] is False
    assert "update_message" not in adv


def test_rebuild_now_when_binary_ahead_of_graph_across_boundary() -> None:
    # graph built with 0.1.0, binary already upgraded to 0.3.0 -> crossed 0.2.0
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.1.0")
    assert adv["update_available"] is False           # already on latest
    assert adv["rebuild_recommended"] is True
    assert "rebuild" in adv["rebuild_message"].lower()


def test_no_rebuild_now_when_graph_built_after_boundary() -> None:
    adv = updates.compute_advice(REGISTRY, current="0.3.0", graph_built_with="0.2.0")
    assert "rebuild_recommended" not in adv


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
