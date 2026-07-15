# cppgraph viz

A tiny, self-contained viewer for the `graph.json` that `cppgraph export`
produces. Open the HTML, load a `graph.json`, and explore the neighbourhood.

This is **our own viewer** (MIT, same as the rest of cppgraph) — it does not use
or require graphify. The only third-party piece is the graph-drawing library
[vis-network](https://visjs.github.io/vis-network/), vendored locally so the
viewer works fully **offline**.

## Use

1. Export a viewable subgraph around a symbol (the full graph is far too large
   to render — a bounded neighbourhood is the unit you actually look at):

   ```sh
   cppgraph export '<SCIP symbol>' --graph <graph.db> --depth 2 --out graph.json
   # find the symbol string first with:  cppgraph find <name> --graph <graph.db>
   ```

2. Open `viz/cppgraph-viz.html` in a browser and load the `graph.json`:
   - **file://** (just double-click the HTML): use the file picker, or drag the
     `graph.json` onto the page. Browsers block `fetch()` of local files, so the
     `?graph=` shortcut does *not* work here — the picker/drop does.
   - **served over http** (e.g. `python -m http.server` in the repo): you can
     use `cppgraph-viz.html?graph=graph.json` to auto-load.

Nodes are coloured by kind (blue = callable `…().`, purple = type `…#`), edges by
relation (`calls` / `inherits` / `implements`). Hover a node for its
`file:line`.

## The `graph.json` format

`cppgraph export` writes the **graphify-compatible** schema on purpose:

```json
{ "nodes": [{ "id": "<scip symbol>", "label": "...", "source_file": "...", "source_location": "L123" }],
  "links": [{ "source": "<id>", "target": "<id>", "relation": "calls" }] }
```

Node ids are the SCIP symbol strings — globally unique and stable, so
`source`/`target` line up with `id` exactly. Because the container is graphify's
schema, the same file can *also* be opened in
[graphify](https://github.com/Graphify-Labs/graphify) if you prefer its
clustering/report views — but the edges themselves are cppgraph's
compiler-exact ones, not graphify's by-name approximation. See
[`../COMPARISON.md`](../COMPARISON.md) for why that distinction matters.

## Third-party notices

- **vis-network** `9.1.6` — `vendor/vis-network.min.js`, © vis.js contributors,
  dual-licensed MIT / Apache-2.0. Upstream:
  <https://github.com/visjs/vis-network>. Unmodified; vendored for offline use.
