"""Tests for the self-contained HTML export (viz_html)."""

from __future__ import annotations

from pathlib import Path

from cppgraph.viz_html import render_standalone, standalone_html, write_temp_html

_TEMPLATE = (
    "<html><body>"
    '<script src="./vendor/vis-network.min.js"></script>'
    "<script>if (window.GRAPH) render(window.GRAPH);</script>"
    "</body></html>"
)


def test_standalone_inlines_data_and_library() -> None:
    g = {"nodes": [{"id": "A"}], "links": []}
    html = standalone_html(g, _TEMPLATE, "VIS_LIB_SOURCE")

    # data is inlined as window.GRAPH ...
    assert "window.GRAPH = " in html
    assert '"id": "A"' in html or '"id":"A"' in html
    # ... the library is inlined ...
    assert "VIS_LIB_SOURCE" in html
    # ... and there is no external reference left.
    assert 'src="./vendor/vis-network.min.js"' not in html


def test_render_standalone_uses_bundled_assets() -> None:
    html = render_standalone({"nodes": [], "links": []})
    assert "vis-network" in html  # the real vendored lib got inlined
    assert 'src="./vendor' not in html


def test_write_temp_html_creates_a_self_contained_file(tmp_path: Path) -> None:
    p = write_temp_html({"nodes": [{"id": "X"}], "links": []})
    assert p.exists() and p.suffix == ".html"
    text = p.read_text(encoding="utf-8")
    assert "window.GRAPH" in text and 'src="./vendor' not in text


def test_standalone_html_escapes_script_breakout() -> None:
    """A payload string containing `</script>` (e.g. a doc comment carried in a
    node's label) must not appear raw in the HTML — it would close the inline
    <script> tag and let the rest be parsed as markup. `<`, `>`, `&` (and
    U+2028/2029) are escaped as \\uXXXX in the JSON; the payload still
    round-trips exactly through json.loads."""
    import json

    evil = "</script><b>hello & <i>world</i></b>\u2028\u2029"
    g = {"nodes": [{"id": "n", "label": evil}], "links": []}
    html = standalone_html(g, _TEMPLATE, "VIS_LIB_SOURCE")

    assert "</script><b>" not in html  # nothing breaks out of the tag
    assert "\\u003c/script\\u003e" in html  # escaped, inside the JSON string
    assert "\u2028" not in html and "\u2029" not in html

    embedded = html.split("window.GRAPH = ", 1)[1].split(";</script>", 1)[0]
    assert json.loads(embedded) == g  # round-trips: escaping is transparent


def test_standalone_html_roundtrips_clean_payload_unchanged() -> None:
    """A payload with nothing to escape still round-trips (the escaping is a
    no-op on ordinary data)."""
    import json

    g = {"nodes": [{"id": "A", "file": "a.cpp"}], "links": [{"source": "A", "target": "A"}]}
    html = standalone_html(g, _TEMPLATE, "VIS_LIB_SOURCE")
    embedded = html.split("window.GRAPH = ", 1)[1].split(";</script>", 1)[0]
    assert json.loads(embedded) == g
