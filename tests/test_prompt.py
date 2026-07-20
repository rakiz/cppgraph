"""Tests for the stdlib Prompter (the TUI path needs a terminal, not unit-tested)."""

from __future__ import annotations

from cppgraph.prompt import Prompter


def _p(answers):
    q = list(answers)
    out = []
    pr = Prompter(lambda _p: q.pop(0) if q else "", lambda *a: out.append(" ".join(map(str, a))))
    return pr, out


def test_text_returns_default_on_empty() -> None:
    p, _ = _p([""])
    assert p.text("Name", "fallback") == "fallback"


def test_confirm_parsing() -> None:
    assert _p(["y"])[0].confirm("ok?", False) is True
    assert _p(["n"])[0].confirm("ok?", True) is False
    assert _p([""])[0].confirm("ok?", True) is True  # empty -> default


def test_select_by_number_and_default() -> None:
    opts = [("a", "Apple"), ("b", "Banana"), ("c", "Cherry")]
    assert _p(["2"])[0].select("pick", opts, "a") == "b"
    assert _p([""])[0].select("pick", opts, "c") == "c"  # empty -> default
    assert _p(["x"])[0].select("pick", opts, "a") == "a"  # non-numeric -> default
    assert _p(["9"])[0].select("pick", opts, "a") == "a"  # out of range -> default
