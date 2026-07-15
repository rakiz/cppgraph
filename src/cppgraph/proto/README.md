# `cppgraph.proto` — vendored SCIP schema

## What's in here

- `scip.proto` — vendored verbatim from
  [`scip-code/scip`](https://github.com/scip-code/scip/blob/main/scip.proto)
  (the SCIP protocol; the project was transferred from `sourcegraph/scip`,
  which now 301-redirects there). This is the schema `scip-clang` emits
  indexes against.
- `scip_pb2.py`, `scip_pb2.pyi` — **generated** by `protoc` from `scip.proto`.
  Both files self-mark `DO NOT EDIT!` at the top. They are committed to this
  repo so that running/developing cppgraph never requires installing
  `protoc` — only regenerating these files after a `scip.proto` change does.

## Regenerating

Only needed if `scip.proto` changes (e.g. picking up a newer upstream SCIP
schema). Requires `protoc` (`brew install protobuf` — see repo root
`INSTALL.md`).

```bash
# 1. (optional) refresh the vendored schema from upstream
curl -fsSL -o src/cppgraph/proto/scip.proto \
  https://raw.githubusercontent.com/scip-code/scip/main/scip.proto

# 2. regenerate the bindings, in place
protoc --proto_path=src/cppgraph/proto \
  --python_out=src/cppgraph/proto --pyi_out=src/cppgraph/proto \
  src/cppgraph/proto/scip.proto

# 3. verify and commit
.venv/bin/python -c "from cppgraph.proto import scip_pb2; print(scip_pb2.Index())"
git diff --stat src/cppgraph/proto/scip_pb2.py src/cppgraph/proto/scip_pb2.pyi
```

Never hand-edit `scip_pb2.py`/`.pyi` — only regenerate via `protoc`.
