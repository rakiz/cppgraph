# syntax=docker/dockerfile:1
#
# Regenerate the SCIP protobuf bindings (scip_pb2.py + scip_pb2.pyi) from
# scip.proto with a pinned protoc, in a container — so nobody has to install
# protoc on the host. Multi-stage: a `gen` stage runs protoc, an `export`
# (scratch) stage carries only the two generated files back out via
# `docker build --output`. Build context is src/cppgraph/proto/ (holds scip.proto).
ARG PROTOC_VERSION=35.1
ARG BASE=ubuntu:24.04

FROM ${BASE} AS gen
ARG PROTOC_VERSION
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# protoc ships prebuilt per-arch zips; map Docker's TARGETARCH to its naming.
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) pa=x86_64 ;; \
      arm64) pa=aarch_64 ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fL --retry 3 -o /tmp/protoc.zip \
      "https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/protoc-${PROTOC_VERSION}-linux-${pa}.zip"; \
    unzip -q /tmp/protoc.zip -d /usr/local; \
    protoc --version   # -> libprotoc ${PROTOC_VERSION}
WORKDIR /work
COPY scip.proto .
RUN mkdir -p /out \
 && protoc --proto_path=. --python_out=/out --pyi_out=/out scip.proto \
 && ls -l /out

# Only the generated files — `docker build --output type=local` writes this
# stage's rootfs onto the host (into src/cppgraph/proto/), leaving scip.proto
# and everything else untouched.
FROM scratch AS export
COPY --from=gen /out/ /
