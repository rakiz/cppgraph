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
