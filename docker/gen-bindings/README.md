# gen-bindings — regenerate the SCIP protobuf bindings with protoc, in a container

Regenerates `src/cppgraph/proto/scip_pb2.py` and `scip_pb2.pyi` from
`src/cppgraph/proto/scip.proto` using a **pinned `protoc` inside a container**,
so you never install `protoc` (or its `abseil` dependency) on the host.

## Why this exists

The bindings are generated *and committed*, so normal build/dev of cppgraph
needs no `protoc` — see [`../../src/cppgraph/proto/README.md`](../../src/cppgraph/proto).
The only time you regenerate is when `scip.proto` changes (picking up a newer
upstream SCIP schema). Nobody installs `protoc` on the host: this container
carries the exact compiler version the committed files were produced with, so
regeneration stays byte-reproducible whoever runs it.

## Use

```sh
docker/gen-bindings/gen.sh          # regenerates the two files in place
```

Then verify and commit:

```sh
.venv/bin/python -c "from cppgraph.proto import scip_pb2; print(scip_pb2.Index())"
git diff --stat src/cppgraph/proto/scip_pb2.py src/cppgraph/proto/scip_pb2.pyi
```

To refresh the schema from upstream first:

```sh
curl -fsSL -o src/cppgraph/proto/scip.proto \
  https://raw.githubusercontent.com/scip-code/scip/main/scip.proto
```

## Files

- `Dockerfile` — multi-stage: a `gen` stage downloads the pinned `protoc` and
  runs it (`--python_out` + `--pyi_out`); a `scratch` `export` stage carries only
  the two generated files. Build context is `src/cppgraph/proto/` (where
  `scip.proto` lives); `gen.sh` uses `docker build --output type=local` to write
  the results straight back into that dir — `scip.proto` and everything else are
  left untouched.
- `gen.sh` — host-side driver (docker or podman, auto-detected).

## Pins

`PROTOC_VERSION=35.1` (build-arg, overridable), `BASE=ubuntu:24.04`. protoc 35.1
is what the committed bindings were generated with — their header reads
`# Protobuf Python Version: 7.35.1`, and the runtime `protobuf` package pinned by
`pyproject.toml` matches. Bump `PROTOC_VERSION` only in lockstep with that
runtime dependency.
