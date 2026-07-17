# TODO

Only open items live here. Completed work is in `CHANGELOG.md`; design detail in
`DESIGN.md`.

## Packaging / open-source

- Contributing notes, CI (lint + pytest), publish.
- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`requires_rebuild`, one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.
- **Version for non-git installs.** `current_version` falls back to the static
  `pyproject`/`__version__` when the source isn't a git checkout (tarball/PyPI).
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`) so those installs report the truth too.

## Blocked on scip-clang `enclosing_range` (PR #504)

Both need exact reference→enclosing-symbol attribution — the nearest-preceding
proxy can't give it in class bodies, so until #504 we stay locations-only
(exact, zero heuristic). See `DESIGN.md` § Graph model.

- Attributed reference **edges** (opt-in at indexing, since they're large):
  approach "A" (type→type) and "B" (all references) — symbol→symbol and
  traversable, exact via containment.
- `usage` view at **symbol** granularity (type → the functions that use it)
  instead of file granularity, for `export --mode usage` / the `visualize` tool.
