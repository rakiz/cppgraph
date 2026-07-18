#!/usr/bin/env bash
# Regenerate src/cppgraph/proto/scip_pb2.py + scip_pb2.pyi from scip.proto using
# a pinned protoc in a container (no host protoc needed). Writes the two files
# back in place via `docker build --output`. Run after changing scip.proto.
#
#   docker/gen-bindings/gen.sh
#
# Env: PROTOC_VERSION (default 35.1 — matches the committed bindings' header),
#      CPPGRAPH_CONTAINER (docker|podman; auto-detected).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PROTO_DIR="$REPO/src/cppgraph/proto"
PROTOC_VERSION="${PROTOC_VERSION:-35.1}"

ENGINE="${CPPGRAPH_CONTAINER:-}"
if [ -z "$ENGINE" ]; then
  for e in docker podman; do
    if command -v "$e" >/dev/null 2>&1; then ENGINE="$e"; break; fi
  done
fi
[ -n "$ENGINE" ] || { echo "error: no container engine (docker/podman) found" >&2; exit 1; }

[ -f "$PROTO_DIR/scip.proto" ] || { echo "error: $PROTO_DIR/scip.proto not found" >&2; exit 1; }

echo "==> Regenerating scip_pb2.py/.pyi with protoc ${PROTOC_VERSION} (engine: $ENGINE)" >&2
DOCKER_BUILDKIT=1 "$ENGINE" build \
  --build-arg "PROTOC_VERSION=${PROTOC_VERSION}" \
  -f "$HERE/Dockerfile" \
  --target export \
  --output "type=local,dest=${PROTO_DIR}" \
  "$PROTO_DIR"

echo "==> Written in place: $PROTO_DIR/scip_pb2.py, scip_pb2.pyi" >&2
echo "    Verify:  .venv/bin/python -c 'from cppgraph.proto import scip_pb2; print(scip_pb2.Index())'" >&2
echo "    Diff:    git diff --stat $PROTO_DIR/scip_pb2.py $PROTO_DIR/scip_pb2.pyi" >&2
