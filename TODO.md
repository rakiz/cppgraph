# TODO

Only open items live here. This is the current state of open work (the project
isn't versioned yet, so it's a snapshot, not a log). Design detail is in
`DESIGN.md`; shipped-feature summary in `CHANGELOG.md`.

## Packaging / open-source

- **Release blocker (0.1.0): re-measure the token numbers.** `README.md` and
  `COMPARISON.md` quote token counts that predate `DEFAULT_LIMIT = 40`
  (`mcp_server.py:56`). Re-run the measurement on the mongo graph (workstation)
  and update both docs before tagging, so the published figures match what the
  tool actually emits.
- Contributing notes, CI (lint + pytest), publish.
- **Cut the actual releases.** The plumbing is in place — `scripts/setup.sh`
  installs by tag (`--version`/`--nightly`/`--branch`), `current_version` derives
  from `git describe`, and `cppgraph status` reads `versions.json` for the
  "update available / rebuild needed" advice — but no release exists yet. Per
  release: tag `vX.Y.Z`, then bump `latest` in `versions.json` and append a
  `releases` entry (`rebuild` level `none`/`store`/`reindex` — what the release
  invalidates in the index stack — plus one-line `notes`, `url`). The advice only
  becomes meaningful once at least one tag exists.
- **Version for non-git installs.** `current_version` falls back to the static
  `pyproject`/`__version__` when the source isn't a git checkout (tarball/PyPI).
  If we ever publish that way, wire a build-time version from the tag
  (`hatch-vcs`/`setuptools-scm`) so those installs report the truth too.

## Native scip-clang build container

`docker/build-scip-clang/` compiles scip-clang from source (with the #504
`enclosing_range` patch) for the host arch — built and timed on ARM64 already.
Open:

- **Decide the distribution stance.** Building #504 ourselves means owning the
  build for every arch that needs it — a native binary per host, built once
  locally. Fine as build-and-use-locally; revisit if it grows into a hosted
  matrix.
- **Wire the `download` path for ARM-Linux once upstream publishes it.** If
  scip-clang ships a `scip-clang-aarch64-linux` asset (see the arm64-linux issue
  draft in `docker/build-scip-clang/`), add the `Linux/aarch64` case in
  `setup.sh` (a commented one-liner is already there) so `--scip-source download`
  works on ARM-Linux — the stock binary (no #504); build stays the #504 route.

## scip-clang `enclosing_range` (PR #504)

The field is consumed (exact caller attribution + the opt-in symbol-granularity
usage view). Open:

- **Report the crash upstream** (sourcegraph/scip-clang PR #504): the missing
  same-file guard is a bug in the PR itself. Draft ready in
  `docker/build-scip-clang/PR504-COMMENT.draft.md`; post it on the PR.
- **Re-validate attribution on real data.** Two consumption bugs were fixed;
  neither has yet been confirmed on the mongo `.scip`:
  - *Wrong source of `enclosing_range`.* The first `enrich-refs` run attributed
    **0** references — the builder read `enclosing_range` off the *reference*
    occurrence, but per the SCIP spec it is emitted on **definitions** (their body
    extent), not references (0 of 13.7M reference occurrences carry one). Fixed to
    attribute by **containment** (each use → the innermost definition whose
    interval contains its line).
  - *Perf: O(refs × defs).* The containment lookup then scanned every preceding
    interval per use site → a use at file scope (namespace/global, of which there
    are many) walked the whole file's definitions before giving up. Measured
    blocking on mongo: `enrich-refs` ran **8.5 h at 99.9% CPU without finishing**.
    Fixed to a single per-document line **sweep** with a stack of open intervals
    (`_attribute_containment`), O((refs+defs)·log). Regression test:
    `test_reference_outside_every_body_is_unattributed`.
  - *Perf: enrich write re-scanned per UPDATE.* A second, independent bottleneck:
    `enrich_references` UPDATEs `refs` filtering on `(symbol_id, file_id, line)`,
    but the only index was `ix_refs(symbol_id)` — each UPDATE scanned every row of
    that symbol (thousands on hub symbols like `ResumeToken`), x millions of
    updates → 36 min and climbing. Fixed by building a temporary composite index
    `ix_refs_enrich(symbol_id, file_id, line)` before the `executemany` (dropped
    after — write-only), making each UPDATE an O(log n) seek.
  The data is already in the `.scip` (929k enclosing ranges, 99.3% of `src/mongo`
  files), so **no re-index needed** — re-run `enrich-refs` on the stored `.scip`
  to confirm it now finishes quickly, the real "attributed X of Y references"
  coverage, and the symbol-granularity usage view (feeds the cost measurement
  below).
- **Auto-enrich after a #504 re-index?** `reindex.sh --attributed-refs` is an
  explicit opt-in today; decide whether a #504 re-index should enrich by default.
- **Attributed reference edges** as first-class graph edges (traversable
  symbol→symbol, distinct `kind`) for `impact`/`path`, beyond the usage view.
- **Quantify the cost.** Measure the real store-size delta of `--attributed-refs`
  on the mongo graph and put a number in the flag help / DESIGN (currently just
  "larger store").

## Synthetic factory-registry edges (reconnect plan→exec across dispatch)

`path` today only *hints* that a missing static chain may cross a runtime
boundary; it can't rebuild the edge. In codebases like MongoDB the plan→exec
flow hops through a factory table keyed by a string
(`REGISTER_DOCUMENT_SOURCE("$match", DocumentSourceMatch::createFromBson)`) then
through virtual dispatch, so there is no static edge from `buildPipeline` to
`DocumentSourceMatch::doGetNext` even though they're linked at runtime.

To close it: parse the registration macros to learn `"$match" → createFromBson`
and inject a synthetic edge into the graph, so end-to-end paths resolve.

Blocked on / costs: the registration macros are codebase-specific (each project
has its own), so this needs per-codebase pattern support, and a synthetic edge
departs from the graph's otherwise exact, heuristic-free model — decide how to
mark such edges (e.g. a distinct `kind`) before adding them.
