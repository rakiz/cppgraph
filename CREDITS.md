# Credits & third-party dependencies

cppgraph itself is MIT-licensed (see `LICENSE`). It stands on other people's
work — this file records what we depend on, with links, in fairness to the
projects that make cppgraph possible. Licenses are listed as a courtesy; each
project's own license text is authoritative.

## Indexing toolchain (external — run, not bundled)

- **scip-clang** — [sourcegraph/scip-clang](https://github.com/sourcegraph/scip-clang)
  (Apache-2.0). The compiler-exact C++ indexer cppgraph is built around: it reads
  `compile_commands.json` and emits the SCIP index we turn into a graph. Pinned by
  version (see `versions.json`).
- **`enclosing_range` patch (PR #504)** —
  [sourcegraph/scip-clang#504](https://github.com/sourcegraph/scip-clang/pull/504).
  Enclosing ranges are **not** in the official scip-clang yet (the PR is in progress
  upstream). Until it lands, cppgraph vendors the patch at
  `docker/build-scip-clang/enclosing_range-on-v0.4.0.patch` and applies it when
  building the binary; a stock (unpatched) binary works too, at file — rather than
  symbol — granularity.
- **LLVM / Clang** — [llvm/llvm-project](https://github.com/llvm/llvm-project)
  (Apache-2.0 with LLVM exceptions). scip-clang is a Clang LibTooling program; the
  build compiles LLVM/Clang from source (see `docker/build-scip-clang/`).

## Schema (vendored)

- **SCIP protocol** — [scip-code/scip](https://github.com/scip-code/scip)
  (Apache-2.0; the project was transferred from `sourcegraph/scip`). `scip.proto` is
  vendored verbatim at `src/cppgraph/proto/scip.proto`; `scip_pb2.py`/`.pyi` are
  generated from it with `protoc`. Provenance details in `src/cppgraph/proto/README.md`.

## Runtime (Python)

- **protobuf** — [protocolbuffers/protobuf](https://github.com/protocolbuffers/protobuf)
  (BSD-3-Clause). The Python runtime for the generated SCIP bindings (`protobuf>=5.0`).

## Visualization (vendored — redistributed in the repo)

- **vis-network** — [visjs/vis-network](https://github.com/visjs/vis-network)
  (dual MIT / Apache-2.0, © vis.js contributors). Bundled as
  `viz/vendor/vis-network.min.js` and used by `viz/cppgraph-viz.html` to render the
  exported graph.

  *Not a dependency:* the optional `graph.json` export is **graphify-compatible** in
  format only — no graphify code is used or required. graphify
  ([COMPARISON.md](COMPARISON.md)) is referenced solely as a comparison point.

## Development only

- **pytest** — [pytest-dev/pytest](https://github.com/pytest-dev/pytest) (MIT).
- **ruff** — [astral-sh/ruff](https://github.com/astral-sh/ruff) (MIT).
