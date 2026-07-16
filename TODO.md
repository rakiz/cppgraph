# TODO

Only open items live here. Completed work is in `CHANGELOG.md`; design detail in
`DESIGN.md`.

## Packaging / open-source

- Contributing notes, CI (lint + pytest), publish.

## Blocked on scip-clang `enclosing_range` (PR #504)

Both need exact reference→enclosing-symbol attribution — the nearest-preceding
proxy can't give it in class bodies, so until #504 we stay locations-only
(exact, zero heuristic). See `DESIGN.md` § Graph model.

- Attributed reference **edges** (opt-in at indexing, since they're large):
  approach "A" (type→type) and "B" (all references) — symbol→symbol and
  traversable, exact via containment.
- `usage` view at **symbol** granularity (type → the functions that use it)
  instead of file granularity, for `export --mode usage` / the `visualize` tool.
