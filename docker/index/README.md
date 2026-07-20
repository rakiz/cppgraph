# index — run scip-clang to emit a SCIP index (ARM-Linux / Windows workaround)

An **x86_64** image whose only job is to run a prebuilt `scip-clang` and produce
a `.scip` index. cppgraph then builds the graph natively from that `.scip` (pure
Python, runs anywhere).

## Why this exists

scip-clang ships no **arm64-linux** binary. Indexing is the only step that needs
x86, so this image runs the x86_64 binary — **emulated** (QEMU/binfmt) on an ARM
host. Driven by
[`../../scripts/index-in-container.sh`](../../scripts/index-in-container.sh),
which builds this image (context: this directory) and runs it over your
`compile_commands.json`. See `../../INSTALL.md` § "ARM-Linux / Windows".

**Scope: small/medium projects only.** Under QEMU emulation scip-clang does not
parallelize — despite `-j 8` it runs effectively single-threaded (~13% total CPU
across 8 cores). On a large codebase (MongoDB on a Graviton `m6g.2xlarge`) this
is not merely slow: the run estimated **~11 h**, then the workers hit their
timeout and shut down before any `.scip` was written:

```
[error] worker N : timeout in worker; is the driver dead?... shutting down
```

So emulation is fine to *try* a subsystem, but for a real codebase on ARM-Linux
use the **native** binary instead: build one from source with the sibling
[`../build-scip-clang/`](../build-scip-clang) image (it drops the binary in the
per-machine data dir `scripts/index.sh` reads), and index natively with `scripts/index.sh` —
no container, full parallelism.

## Toolchain caveat

The image installs a stock `clang` toolchain so `compile_commands.json` entries
resolve their headers **inside** the container. If your project builds with a
custom/vendored toolchain (common — e.g. MongoDB), add it to the `Dockerfile`: a
`'X.h' file not found` during indexing almost always means the container is
missing that toolchain, not a scip-clang bug.

## Pins

`SCIP_CLANG_VERSION=v0.4.0` (build-arg, overridable), `BASE=ubuntu:24.04`.
