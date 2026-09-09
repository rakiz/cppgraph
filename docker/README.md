# docker

Container images for the parts of cppgraph that need one. Each subdirectory is
self-contained (its own `Dockerfile` + `README.md`).

- [`index/`](index) — run a prebuilt scip-clang (x86_64, emulated on ARM) to emit
  a `.scip` index. The ARM-Linux / Windows indexing workaround; used by
  `scripts/index-in-container.sh`.
- [`build-scip-clang-patched-linux/`](build-scip-clang-patched-linux) — compile scip-clang from source for
  the host's native arch, carrying our patch bundle (PR #504 `enclosing_range`,
  ForwardDefinition, ReadAccess/WriteAccess). Slower to build, but no emulation
  and adds the exact-attribution feature.
- [`gen-bindings/`](gen-bindings) — regenerate the SCIP protobuf bindings
  (`scip_pb2.py`/`.pyi`) from `scip.proto` with a pinned `protoc`, so nobody
  installs `protoc` on the host. Only needed when `scip.proto` changes.

Rule of thumb: `index/` gets you indexing *today* (emulated); `build-scip-clang-patched-linux/`
gets you a *native* binary once, then index with `scripts/index.sh` and no container;
`gen-bindings/` is a dev-only, one-off when the vendored schema moves.
