# build-scip-clang — compile scip-clang natively, with `enclosing_range` (#504)

Builds a `scip-clang` binary **from source, for the host's own CPU architecture**,
carrying the `enclosing_range` feature ([PR #504](https://github.com/sourcegraph/scip-clang/pull/504))
on top of the `v0.4.0` tag.

## Why this exists

Upstream ships prebuilt scip-clang only for **x86_64-linux** and **arm64-darwin**
— there is no **arm64-linux** binary. On an ARM-Linux host the sibling
[`../index/`](../index) image works around that by running the x86_64 binary
*emulated* (QEMU), which is correct but slow. This image instead **compiles**
scip-clang for whatever arch the build runs on, so ARM hosts get a **native**
binary — no emulation — and, as a bonus, the `enclosing_range` patch cppgraph
wants for exact reference→enclosing-symbol attribution (see `../../DESIGN.md`).

Build-and-use-locally: nothing is hosted or maintained centrally — each machine
that lacks a prebuilt binary builds its own once.

## Use

```sh
./build.sh [output_dir]        # default output_dir: ./out
```

The build compiles LLVM-based code from source — CPU/RAM-heavy (Bazel; tune
`BAZEL_JVM_HEAP` build-arg down on small hosts). `build.sh` uses
`docker build --output` to drop just the binary on the host (the build image is
discarded).

**Timing.** Measured **~32 min cold** (Docker cache purged first) on an AWS
Graviton `m6g.2xlarge` (Neoverse-N1, 8 vCPU, 30 GiB, ARM64). Where it goes:

| Step                                   | Time        |
| -------------------------------------- | ----------- |
| pull `ubuntu:22.04`                    | ~1 s        |
| `apt` system deps                      | ~16 s       |
| bazelisk download                      | <1 s        |
| `git clone` scip-clang v0.4.0          | <1 s        |
| apply PR #504 patch                    | <1 s        |
| **Bazel compile (LLVM+Clang) + LTO link** | **~31 min** |

So **~99 % is the Bazel compile** — scip-clang embeds Clang as a library, so it
builds a large chunk of LLVM/Clang from source; the annex steps are noise. The
final `-flto=thin` link is the slow serial tail.

It is **CPU-bound and parallel**: on this run Bazel's critical path was ~148 s
but wall time ~1879 s — the gap is the 8 cores saturating. **More cores → much
faster** (a 32-core host finishes in a handful of minutes); fewer → proportionally
longer. Budget by core count, not a fixed number.

> Not to be confused with the **~11 h** figure elsewhere in the docs — that is
> *emulated* (QEMU) indexing of a large codebase, a different operation entirely.
> This native build (~30 min, one-time) is precisely what lets an ARM host skip
> that emulated path.

By default the binary lands in the per-machine data dir
(`${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin`) — exactly where `reindex.sh`
looks — so it's used for **native** indexing with no further wiring. It's a
persistent location (not a cache), so this long build won't be wiped by a cache
cleaner:

```sh
./build.sh                        # -> ~/.local/share/cppgraph/bin/scip-clang
./build.sh ./out                  # or output elsewhere, then move it yourself
```

## Files

- `Dockerfile` — multi-stage: a `builder` stage (Bazelisk → `.bazelversion`'s
  Bazel → `bazel build //indexer:scip-clang --config=release-linux`), then a
  `scratch` `export` stage carrying only the binary.
- `build.sh` — host-side driver (self-contained: its own dir is the build
  context).
- `enclosing_range-on-v0.4.0.patch` — the #504 change **rebased onto the
  `v0.4.0` tag**, so it applies cleanly. The Dockerfile still fails fast as a
  guard (`git apply --verbose`, then `grep -q enclosingRange indexer/Indexer.cc`);
  if a future tag bump breaks it, rebase the PR again and replace this file.

## Pins

`SCIP_CLANG_TAG=v0.4.0`, `BAZELISK_VERSION=v1.19.0`, `BASE=ubuntu:22.04`. Bazel
itself is pinned by scip-clang's `.bazelversion` (7.5.0), fetched by Bazelisk.
