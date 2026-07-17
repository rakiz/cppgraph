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

## Query quality — targeted for 0.2.0

Surfaced by the MongoDB change-stream benchmark (2026-07-17). Ranked by impact.

- **`impact_of` on a type returns 0, silently misleading.** `impact_of
  ResumeTokenData (kind=calls)` → `total: 0` even though `find_references` shows
  68 sites — a type has no call-graph callers, so the blast radius question must
  be asked via references. Detect when the resolved symbol is a type (not a
  callable) and either redirect to `find_references` or return an explicit notice
  ("type has no callers — N reference sites; use find_references"), never a bare 0.
- **`find` is substring-only, no multi-term AND.** `find "buildPipeline
  changeStream"` → 0 hits while `buildPipeline` alone → 11. Support an AND of
  tokens (all terms present, order-free) so multi-word queries work.
- **Overloads aren't grouped.** `ResumeToken::parse` has two signatures (distinct
  SCIP hashes, `.h`/`.cpp`); querying the wrong one silently misses callers.
  Group signatures sharing a qualified name under one entry, hashes as sub-lines.
- **Callee noise (opt-in filter).** `what_it_calls` buries real edges under
  `operator==`/`tassert`/`makeStatus`/`source_location`. Add an opt-in
  `hide_trivial` filter (mirror the existing test filtering), and revisit the
  default `limit` (25 can push real edges out — 40 was needed to see all stages).
- **`path` across runtime dispatch returns a bare `found: false`.** Plan→exec
  boundary (`DocumentSourceX` built by `buildPipeline` → `XStage::doGetNext`) is a
  registered-factory / virtual dispatch hop with no static edge, so end-to-end
  paths break. Can't be fully resolved without indexing the factory registry;
  short term, when `path` fails, hint that the flow may cross virtual dispatch /
  a factory rather than implying no relationship exists.

## Later: a build container that compiles scip-clang native to its host arch

Today's ARM-Linux workaround (`scripts/index-in-container.sh`) runs the x86_64
scip-clang **emulated**, so indexing is slow. The better long-term option: a
Docker image that carries the whole build toolchain (Bazel + LLVM deps) and
compiles a **vanilla** scip-clang for whatever arch the container runs on
(native, e.g. `linux/arm64` on an ARM host — no emulation), dropping the binary
into `scratch/bin/` for normal *native* indexing afterwards. Keeps the heavy
build toolchain off the host, and there's **nothing to host/maintain** — each
machine that lacks a prebuilt binary builds its own once. Needs scip-clang's
real Bazel build recipe (pin the version; build is CPU/RAM-heavy, tens of
minutes). Distinct from item above: build-and-use-locally, not build-and-publish.

Bonus tie-in: **if we build scip-clang ourselves we could carry the
`enclosing_range` patch (#504)** — but that turns "vanilla, build locally" into
"own the whole distributed matrix", so keep it a separate, later decision.

## Blocked on scip-clang `enclosing_range` (PR #504)

Both need exact reference→enclosing-symbol attribution — the nearest-preceding
proxy can't give it in class bodies, so until #504 we stay locations-only
(exact, zero heuristic). See `DESIGN.md` § Graph model.

- Attributed reference **edges** (opt-in at indexing, since they're large):
  approach "A" (type→type) and "B" (all references) — symbol→symbol and
  traversable, exact via containment.
- `usage` view at **symbol** granularity (type → the functions that use it)
  instead of file granularity, for `export --mode usage` / the `visualize` tool.
