# cppgraph viz

A tiny, self-contained viewer for the `graph.json` that `cppgraph export`
produces. Open the HTML, load a `graph.json`, and explore the neighbourhood.

This is **our own viewer** (MIT, same as the rest of cppgraph) — it does not use
or require graphify. The only third-party piece is the graph-drawing library
[vis-network](https://visjs.github.io/vis-network/), vendored locally so the
viewer works fully **offline**.

## Use

`view`/`export` take a plain **name** (resolved via `find`) or an exact SCIP
symbol string — you don't have to look the string up first. Run `cppgraph find
<name>` only when a name is ambiguous and you want to pick the exact symbol.

### One-shot (easiest): `cppgraph view`

```sh
cppgraph view <name-or-SCIP-symbol> --graph <graph.db> --depth 1
```

Builds the neighbourhood, writes a **self-contained** HTML (data + vis-network
inlined, no external references) to a temp dir, and opens it in your browser —
it renders immediately, no clicking. `--mode usage` gives the symbol→file usage
view (right for a type); `--no-open` just prints the path. The MCP server exposes
the same as the `visualize` tool, so an LLM can pop the graph open for you.

### Or: export a data file, then open the viewer

```sh
cppgraph export <name-or-SCIP-symbol> --graph <graph.db> --depth 2 --out graph.json
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

### Beyond the neighbourhood: `--mode path` and `--mode cycle`

`view`/`export` (and the MCP `visualize`) also answer two structural questions
directly, producing the same self-contained HTML / `graph.json` as above:

- **`--mode path --dst <other>`** — "how do these two connect?": the shortest
  `calls` chain between the symbol and `--dst` (required in this mode; a plain
  name or exact SCIP string, resolved like the main symbol). `--expand-paths`
  widens it to the **corridor**: every node/edge lying on *some* call path to
  `--dst` (the chain is always inside it; siblings that don't reach `--dst` are
  not). `--depth N` adds N context hops around every chain/corridor node
  (default 0 — the pure answer, vs deps mode's 2-hop radius); `--limit N`
  (default 40) caps the node count, chain/corridor nodes kept first.
- **`--mode cycle`** — the multi-member **call cycle** containing the symbol
  (the strongly-connected component of the `calls` graph): members as nodes,
  every `calls` edge induced on them as links — the visual counterpart of
  `cppgraph strongly-connected-components`. `--dst` is not used in this mode;
  `--depth`/`--limit` behave exactly as in path mode.

When there is no static chain, or the symbol sits in no multi-member cycle,
nothing is rendered — the tools say so (`found: false` + a hint) instead of
opening a near-empty picture, since the real flow may cross a virtual call or
registered factory the static graph can't link.

### Layering violations: `boundary-violations --out`

`cppgraph boundary-violations --rule FROM:FORBIDDEN --out graph.json` checks
your declared layering rules and, next to the stdout table, writes the
violating edges as a `graph.json` for this viewer: nodes are every distinct
endpoint of a violating edge, edges are the violations themselves. The MCP
server exposes the same as the `visualize_boundary_violations` tool (rules as
`[from, forbidden]` prefix pairs, e.g. `[["common/", "platform/"]]`; `limit`
caps the list), so an LLM can pop the picture open for you. With **zero**
violations nothing is written or opened — 0 is a clean outcome, not an error.

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
