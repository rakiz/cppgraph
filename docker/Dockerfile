# syntax=docker/dockerfile:1
#
# x86_64 image whose only job is to run scip-clang and emit a SCIP index.
# Reason: scip-clang ships no ARM-Linux (aarch64) binary — only x86_64-linux and
# arm64-darwin. Indexing is the *only* step that needs x86; cppgraph then builds
# the graph natively from the .scip (pure Python, runs anywhere). See INSTALL.md
# § "ARM-Linux / Windows: index via a container".
#
# Works with docker or podman. On an ARM host it runs under emulation
# (linux/amd64 + qemu/binfmt): slower than native, but correct. Driven by
# scripts/index-in-container.sh.
FROM --platform=linux/amd64 ubuntu:24.04

ARG SCIP_CLANG_VERSION=v0.4.0

# A clang toolchain so that compile_commands.json entries referencing the system
# compiler resolve their headers *inside* the container. IMPORTANT: if your
# project builds with a custom/vendored toolchain (a common case — e.g. MongoDB),
# add it here; "fatal error: 'X.h' file not found" during indexing almost always
# means the container is missing that toolchain, not a scip-clang bug.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl clang libc++-dev libc++abi-dev build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fL --retry 3 -o /usr/local/bin/scip-clang \
      "https://github.com/sourcegraph/scip-clang/releases/download/${SCIP_CLANG_VERSION}/scip-clang-x86_64-linux" \
 && chmod +x /usr/local/bin/scip-clang \
 && /usr/local/bin/scip-clang --version

# Args (compdb / output / -j) are appended by the caller; cwd is set with -w.
ENTRYPOINT ["scip-clang", "--no-progress-report"]
