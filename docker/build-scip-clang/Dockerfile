# syntax=docker/dockerfile:1
#
# Build `scip-clang` v0.4.0 + the `enclosing_range` feature (PR #504) natively
# for the CPU architecture of the machine running the build.
#
#   * arm64 (aarch64) host -> native arm64-linux binary (upstream doesn't ship one)
#   * x86_64 host          -> x86_64-linux binary
#
# The binary is always a Linux binary of the host's CPU arch (it's built in a
# Linux container). No QEMU, no remote cache, no GCP auth.
#
# Usage: see build.sh (uses `docker build --output` to drop the binary on the host).

ARG BASE=ubuntu:22.04

FROM ${BASE} AS builder

# --- pinned inputs ----------------------------------------------------------
ARG SCIP_CLANG_TAG=v0.4.0
ARG BAZELISK_VERSION=v1.19.0
# JVM heap for Bazel. Tune down if the host has less RAM.
ARG BAZEL_JVM_HEAP=8g

ENV DEBIAN_FRONTEND=noninteractive

# Minimal system deps. Bazel fetches the whole C++/LLVM toolchain itself.
# libxml2 is a RUNTIME dep of the hermetic LLVM ld.lld (else the release link
# fails with "libxml2.so.2: cannot open shared object file").
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libc6-dev \
        libxml2 \
        python3 \
        unzip \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Bazelisk (arch-aware). It reads .bazelversion (7.5.0) and fetches that Bazel.
RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) b=amd64 ;; \
        arm64) b=arm64 ;; \
        *) echo "unsupported host arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/bazel \
        "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-${b}" \
    && chmod +x /usr/local/bin/bazel

WORKDIR /src
RUN git clone --depth 1 --branch "${SCIP_CLANG_TAG}" \
        https://github.com/sourcegraph/scip-clang.git .

# Apply PR #504 (enclosing_range), rebased onto the v0.4.0 tag.
COPY enclosing_range-on-v0.4.0.patch /tmp/enclosing_range.patch
RUN git apply --verbose /tmp/enclosing_range.patch \
    && grep -q 'enclosingRange' indexer/Indexer.cc  # fail fast if patch is a no-op

# Optimized release build (same config upstream uses for its linux binaries).
RUN echo "startup --host_jvm_args=-Xmx${BAZEL_JVM_HEAP}" > ci.bazelrc \
    && bazel build //indexer:scip-clang --config=release-linux \
    && cp bazel-bin/indexer/scip-clang /scip-clang \
    && /scip-clang --version

# --- export stage: ship only the binary ------------------------------------
FROM scratch AS export
COPY --from=builder /scip-clang /scip-clang
