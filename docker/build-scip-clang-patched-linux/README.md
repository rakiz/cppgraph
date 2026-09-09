# build-scip-clang-patched-linux — compile scip-clang natively, with our patches on top of v0.4.0

Builds a `scip-clang` binary **from source, for the host's own CPU architecture**,
carrying three patches stacked on the `v0.4.0` tag:

1. `enclosing_range` ([PR #504](https://github.com/sourcegraph/scip-clang/pull/504))
2. the `ForwardDefinition` bit on bodyless-declaration occurrences (our own fix,
   not yet upstreamed — see `forward-definition-on-v0.4.0.patch` and `TODO.md`'s
   scip-clang section). Fixes the declaration-site phantom-caller bug on
   **stock-shaped** graphs too (doesn't need #504's intervals for that fix).
3. the `ReadAccess`/`WriteAccess` syntactic classifier (our own fix, not yet
   upstreamed — see `read-write-access-on-v0.4.0.patch`). Tags `symbol_roles`
   with `WriteAccess` (and `ReadAccess` on read-modify-write sites) from the
   syntactic AST parent alone; plain reads stay untagged.

All three patches are bundled into one binary deliberately — there's no un-patched
variant shipped, so every consumer of the patched binary gets all three fixes.

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
./build.sh [output_dir]        # default: CPPGRAPH_BIN_DIR (see below)
```

The build compiles LLVM/Clang from source — **CPU-, RAM- *and* disk-heavy**
(Bazel; tune `BAZEL_JVM_HEAP` build-arg down on small hosts). `build.sh` uses
`docker build --output` to drop just the binary on the host (the build image is
discarded).

**Disk.** Budget **~30–40 GB free** on whatever filesystem backs the Docker
builder (LLVM/Clang sources + Bazel's build tree are large). A common failure is
a small root partition — the build dies with a disk-full error and produces no
binary. If `/` is tight, point Docker's data-root at a roomier partition (Docker
`data-root` / rootless `~/.local/share/docker`, or move `/var/lib/docker`), or
fall back to the emulated x86 container (`setup.sh --scip-source emulate`), which
needs almost no disk. This build is one-time per machine, so the space is
reclaimed once it's done (the image is discarded).

**Timing.** Measured **~32 min cold** (Docker cache purged first) on an AWS
Graviton `m6g.2xlarge` (Neoverse-N1, 8 vCPU, 30 GiB, ARM64). Where it goes:

| Step                                   | Time        |
| -------------------------------------- | ----------- |
| pull `ubuntu:22.04`                    | ~1 s        |
| `apt` system deps                      | ~16 s       |
| bazelisk download                      | <1 s        |
| `git clone` scip-clang v0.4.0          | <1 s        |
| apply PR #504 patch                    | <1 s        |
| apply ForwardDefinition patch           | <1 s        |
| apply ReadAccess/WriteAccess patch      | <1 s        |
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
(`${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin`) — exactly where `scripts/index.sh`
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
- `build.sh` — host-side driver. Build context is the **repo root** (not this
  dir), so the Dockerfile can `COPY` the shared `../../scip-clang-patches/`
  patch files (see that directory's own README) — not self-contained anymore
  since those patches also serve `scripts/build-scip-clang-patched-macos.sh`.
- `enclosing_range-on-v0.4.0.patch`, `forward-definition-on-v0.4.0.patch`,
  `read-write-access-on-v0.4.0.patch` —
  live in `../../scip-clang-patches/` (shared with the macOS build script), not
  in this directory. See that directory's own README for what each patch does,
  the build's apply order (not actually required — see that README's
  "Independence" section), and why `forward-definition-on-v0.4.0.patch` is
  ready for a standalone upstream PR despite being applied stacked here.

## Not yet upstreamed

Only `enclosing_range` (#504) is an upstream PR in progress. The
`ForwardDefinition` fix (`../../scip-clang-patches/forward-definition-on-v0.4.0.patch`)
and the `ReadAccess`/`WriteAccess` classifier
(`../../scip-clang-patches/read-write-access-on-v0.4.0.patch`) live only as our
patches for now, but each already applies to a clean `v0.4.0` on its own
(reduced diff context — see each patch's own header), so proposing either
upstream (independent of whether #504 or the other lands) needs no rework of
the patch itself, just the PR write-up. See `TODO.md`'s scip-clang section for
the planned path (validate end-to-end in cppgraph, publish our own binaries,
*then* propose upstream).

## Pins

`SCIP_CLANG_TAG=v0.4.0`, `BAZELISK_VERSION=v1.19.0`, `BASE=ubuntu:22.04`. Bazel
itself is pinned by scip-clang's `.bazelversion` (7.5.0), fetched by Bazelisk.
