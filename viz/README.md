# cppgraph viz

A tiny, self-contained viewer for the `graph.json` that `cppgraph export`
produces. Open the HTML, load a `graph.json`, and explore the neighbourhood.

This is **our own viewer** (MIT, same as the rest of cppgraph) — it does not use
or require graphify. The only third-party piece is the graph-drawing library
[vis-network](https://visjs.github.io/vis-network/), vendored locally so the
viewer works fully **offline**.

## Use

Find the symbol string first: `cppgraph find <name> --graph <graph.db>`.

### One-shot (easiest): `cppgraph view`

```sh
cppgraph view '<SCIP symbol>' --graph <graph.db> --depth 1
```

Builds the neighbourhood, writes a **self-contained** HTML (data + vis-network
inlined, no external references) to a temp dir, and opens it in your browser —
it renders immediately, no clicking. `--mode usage` gives the symbol→file usage
view (right for a type); `--no-open` just prints the path. The MCP server exposes
the same as the `visualize` tool, so an LLM can pop the graph open for you.

### Or: export a data file, then open the viewer

```sh
cppgraph export '<SCIP symbol>' --graph <graph.db> --depth 2 --out graph.json
```

Then open `viz/cppgraph-viz.html` and load `graph.json`:
- **file://** (double-click the HTML): use the file picker, or drag the
  `graph.json` onto the page. Browsers block `fetch()` of local files, so the
  `?graph=` shortcut does *not* work here — the picker/drop does.
- **served over http** (`python -m http.server`): `cppgraph-viz.html?graph=graph.json`
  auto-loads.

Either way, the page stays a full viewer: use the picker or drag another
`graph.json` onto it to load a different graph.

The full graph is far too large to render — always scope to a bounded
neighbourhood (depth 1-2), or you get an unreadable hairball.

Nodes are coloured by kind (blue = callable `…().`, purple = type `…#`, green =
file), edges by relation (`calls` / `inherits` / `implements` / `references`);
usage edges are thicker the more use sites a file has. Hover a node for its
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
