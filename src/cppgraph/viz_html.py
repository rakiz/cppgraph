"""Produce a single self-contained HTML file that renders a graph.json.

The bundled viewer (`viz/cppgraph-viz.html`) normally loads its data via a file
picker or `?graph=` fetch — but browsers block `fetch()` of local files under
`file://`, so "just open the file" wouldn't auto-render. A *standalone* export
sidesteps that entirely: it inlines both the graph data (as `window.GRAPH`) and
the vis-network library into one HTML file with no external references, so
opening it shows the graph immediately, anywhere, offline.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# The exact tag in the template that pulls the vendored library; we swap it for
# an inline copy so the standalone file has zero external references.
_VENDOR_TAG = '<script src="./vendor/vis-network.min.js"></script>'


def standalone_html(graph_json: dict, template_html: str, vendor_js: str) -> str:
    """Inline `graph_json` and the vis-network source into the viewer template.

    Pure (no IO) so it's unit-testable. The result references nothing external.
    """
    data_tag = "<script>window.GRAPH = " + json.dumps(graph_json) + ";</script>"
    inlined_vendor = "<script>\n" + vendor_js + "\n</script>"
    # Put the data before the (now inlined) library; the template's bootstrap
    # runs render(window.GRAPH) once both are defined.
    return template_html.replace(_VENDOR_TAG, data_tag + "\n" + inlined_vendor)


def _viz_dir() -> Path:
    """Locate the bundled `viz/` assets (repo layout: <repo>/viz, package at
    <repo>/src/cppgraph)."""
    here = Path(__file__).resolve()
    candidate = here.parents[2] / "viz"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"could not locate the viz/ assets (looked in {candidate}); "
        "run from a source checkout"
    )


def render_standalone(graph_json: dict) -> str:
    """Read the bundled template + vendored library and inline `graph_json`."""
    viz = _viz_dir()
    template = (viz / "cppgraph-viz.html").read_text(encoding="utf-8")
    vendor = (viz / "vendor" / "vis-network.min.js").read_text(encoding="utf-8")
    return standalone_html(graph_json, template, vendor)


def write_temp_html(graph_json: dict, prefix: str = "cppgraph-") -> Path:
    """Render a standalone HTML for `graph_json` into a fresh temp dir; return it."""
    html = render_standalone(graph_json)
    out = Path(tempfile.mkdtemp(prefix=prefix)) / "graph.html"
    out.write_text(html, encoding="utf-8")
    return out


def open_in_browser(path: Path | str) -> tuple[bool, str]:
    """Open `path` in the OS default browser. Returns (launched, command_used).

    Best-effort: on an unknown platform (or if the opener isn't found) we return
    (False, <suggested command>) so the caller can just print it for the user.
    """
    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        subprocess.Popen(
            [opener, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True, opener
    except OSError:
        return False, opener
