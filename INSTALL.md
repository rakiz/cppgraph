# INSTALL — setting up cppgraph on a new machine

Verified on: macOS 15.7.7, arm64 (Apple Silicon), 2026-07-15. Commands that
differ for other platforms are noted inline.

## 1. Python environment (required, every machine)

Clone into the per-machine tool dir — the same `${XDG_DATA_HOME:-~/.local/share}/cppgraph/`
where §2 puts the `scip-clang` binary (`bin/`), so the whole tool sits in one
stable, persistent place. The global MCP registration points at this checkout's
`.venv`, so it must not move:

```bash
git clone https://github.com/rakiz/cppgraph "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
cd "${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/repo"
```

Requires Python >= 3.13 (`pyproject.toml`). `uv` manages the venv — and fetches
a 3.13 automatically if the system Python is older (e.g. Ubuntu 22.04 ships 3.10),
so no `deadsnakes`/`pyenv` needed. Prereqs: `uv` and `curl` (`setup.sh` assumes
both; install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`).
`scripts/setup.sh` does the two steps below for you:

```bash
uv venv
uv pip install -e ".[dev]"
```

### Choosing a version

cppgraph is pure Python, so a version is just a **git tag** — no build step,
checking out the tag and installing editable is all there is. `scripts/setup.sh`
wraps this (it also fetches `scip-clang`, see §2):

```bash
scripts/setup.sh                 # current checkout as-is; if clean and a stable
                                 # release exists, check out that tag first
scripts/setup.sh --version 0.2.0 # pin to a released version (tag v0.2.0)
scripts/setup.sh --nightly       # track main (bleeding edge)
scripts/setup.sh --branch foo    # an arbitrary branch (rarely needed)
```

The installed version is reported by `cppgraph status` (the `tool` section) and
comes from `git describe`, so it always reflects the tag you have checked out —
no reinstall needed after a `git checkout`. Until the first release is tagged,
`versions.json` has no `latest`, so the default simply installs `main`.

Verify:

```bash
.venv/bin/python -c "from cppgraph.proto import scip_pb2; print(scip_pb2.Index())"
.venv/bin/python -m pytest --version
```

This installs the committed, pre-generated protobuf bindings' runtime
dependency (`protobuf`) — you do **not** need `protoc` for this step. See
§3 for when `protoc` actually is needed.

To also run the MCP server (`cppgraph-mcp`, exposes the graph to an LLM),
install the optional `mcp` extra — it's not needed for the core build/query CLI:

```bash
uv pip install -e ".[dev,mcp]"
# then, pointed at a built graph:
.venv/bin/cppgraph-mcp --graph scratch/myproject.graph.db --root /path/to/checkout
```

## 2. `scip-clang` (required, every machine — NOT committed to this repo)

`scip-clang` is a large external binary (~68 MB). It is never vendored in git.
It's a **per-machine** artifact — one per CPU arch, shared by this checkout and
every project you index — so each machine keeps a single copy in the persistent
user data dir, `${XDG_DATA_HOME:-~/.local/share}/cppgraph/bin/scip-clang`
(override with `CPPGRAPH_BIN_DIR`). It goes in the data dir, **not a cache**: a
self-built binary (ARM-Linux, PR #504) costs 30-60 min to rebuild and can't be
re-downloaded, so it must survive cache cleaners. Not under `scratch/` or any
project's `.cppgraph/`.

Verified version: **v0.4.0** from
https://github.com/sourcegraph/scip-clang (mirrors to `scip-code` releases
too — the GitHub API resolves either).

**Where it comes from — `setup.sh` picks a source:**

| `--scip-source` | what it does | when |
|---|---|---|
| `download` | fetch the prebuilt release binary (no PR #504) | macOS arm64, Linux x86_64 |
| `build` | compile it locally with `enclosing_range`/PR #504 (`docker/build-scip-clang/`, ~30-60 min, Docker, **Linux host only** — produces a Linux binary) | ARM-Linux, or anyone wanting #504 |
| `emulate` | install no host binary; index through an x86 container | ARM-Linux without building, Intel Mac, Windows |

Also settable via `CPPGRAPH_SCIP_SOURCE` (the flag wins). If you pass neither,
`setup.sh` **prompts on a terminal**; otherwise it auto-picks — `download` where
a prebuilt binary exists, else `emulate` — and never starts a long build
unattended. (A `build` on macOS is rejected: the container emits a *Linux*
binary, unusable on the host.)

**Pinned version + staleness.** scip-clang is pinned by **version only** in
`versions.json` (`scip_clang`). `setup.sh` reads it and writes a provenance
sidecar (`scip-clang.json`) next to the binary recording what it installed —
including the **variant** (`stock` vs a patched build like `enclosing_range-504`
from PR #504). `cppgraph status` flags **"update the binary"** / **"re-index"**
only on a *version* change. The **variant is not pinned**: `stock` and `#504` are
two valid capability levels, and a graph's variant is independent of the local
binary (a #504-indexed store can be copied to a stock-only machine), so `status`
reports the variant for information rather than nagging. Whether a given graph has
the richer symbol-granularity attribution is shown by its `usage_view`, not by a
variant match — get it with a #504 index + `--attributed-refs`, or `enrich-refs`.

Normally you don't do this by hand — `scripts/setup.sh` downloads the right
asset with `curl` into that data dir. To fetch it manually (only `curl` needed,
no `gh`), pick the asset for your platform and save it there:

```bash
BIN_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin"
mkdir -p "$BIN_DIR"
curl -fL --retry 3 -o "$BIN_DIR/scip-clang" \
  https://github.com/sourcegraph/scip-clang/releases/download/v0.4.0/scip-clang-arm64-darwin
chmod +x "$BIN_DIR/scip-clang"
```

Asset name depends on platform — pick the matching one from the release:

| Platform            | Asset name                  |
|---------------------|------------------------------|
| macOS arm64          | `scip-clang-arm64-darwin`    |
| Linux x86_64         | `scip-clang-x86_64-linux`    |
| Linux x86_64 (dev)   | `scip-clang-dev-x86_64-linux`|

Use the plain `scip-clang-x86_64-linux`. The `-dev-` asset is a debug build
(assertions on, slower) — you only want it if you're diagnosing a scip-clang
crash, not for normal indexing.

No Homebrew/apt package is needed for `scip-clang` itself — it's a
self-contained release binary.

Verify:

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph/bin/scip-clang" --version
# scip-clang 0.4.0
# Based on Clang/LLVM 2078da43e25a4623cab2d0d60decddf709aaea28
```

### ARM-Linux / Windows: index via a container (Docker or Podman)

`scip-clang` ships **no ARM-Linux (aarch64) binary** — only `x86_64-linux` and
`arm64-darwin` — and nothing for Windows. But indexing is the **only** step that
needs x86: cppgraph builds the graph and serves queries in pure Python, natively,
on any platform. `scripts/setup.sh` reflects this — it installs the tool (venv)
on *every* platform and simply skips the native indexer where none exists,
pointing you here. So on an ARM-Linux workstation (or Intel Mac / Windows), run
scip-clang in an x86_64 container, then build the graph natively.

> **Large codebase on ARM-Linux? Build a native binary instead.** Emulated
> scip-clang doesn't parallelize (effectively single-threaded under QEMU) and on
> a big project (e.g. MongoDB on a Graviton `m6g.2xlarge`) the run can estimate
> **~11 h** and then die with worker timeouts before writing any `.scip`. The
> container path below is fine for a subsystem or a small/medium project; for a
> real ARM-Linux indexing workflow, compile a native scip-clang once with
> [`docker/build-scip-clang/`](docker/build-scip-clang) and index with
> `reindex.sh` (no container). See that directory's README.

```bash
# 1. produce the .scip in an x86_64 container (emulated on ARM via qemu). Uses
#    docker or podman (auto-detected; CPPGRAPH_CONTAINER to force one). Same args
#    as reindex.sh; writes <project>/.cppgraph/<name>.scip and prints the exact
#    build command to run next.
scripts/index-in-container.sh /path/to/project/compile_commands.json src/ myproject

# 2. build the graph natively (no container) — the command above prints this:
.venv/bin/cppgraph build \
  --scip /path/to/project/.cppgraph/myproject.scip \
  --out  /path/to/project/.cppgraph/myproject.graph.db
```

`reindex.sh` also picks this up automatically: on a platform without a native
scip-clang, if a matching `<name>.scip` already sits in `<project>/.cppgraph/`
(from the container step, or copied from another machine that indexed the same
checkout), it **skips indexing and builds straight from it** — so the workflow is
"generate the `.scip` once, then `reindex.sh` as usual". (Incremental `--update`
still needs a native scip-clang.)

Requires **Docker or Podman** with `linux/amd64` emulation — the script
auto-detects either (force one with `CPPGRAPH_CONTAINER=podman`). Podman is
daemonless, rootless and fully FOSS. Neither is assumed to be present; if you have
no container engine yet, install one (Ubuntu):

```bash
sudo apt-get install -y docker.io      # Docker Engine
# or, rootless/daemonless:  sudo apt-get install -y podman
```

Three gotchas:

- **amd64 emulation must be registered** (native-Linux ARM hosts — e.g. Ubuntu
  arm64 — do *not* get it automatically; only Docker Desktop does). Register it
  once, and use `tonistiigi/binfmt`, **not** `qemu-user-static`: the latter often
  registers without the `F` (fix-binary) flag, so emulation "exists" but dies
  inside the build with `exec /bin/sh: exec format error`.
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install amd64
  docker run --rm --platform linux/amd64 alpine uname -m   # must print: x86_64
  ```
  The script preflights this and stops with the fix if it's missing. If `docker`
  itself needs `sudo`, either prefix the commands or join the group once:
  `sudo usermod -aG docker $USER` (then re-login).
- **Paths must match.** `compile_commands.json` holds absolute paths; the wrapper
  bind-mounts the project at its *same* absolute path in the container so they
  resolve. Keep the source tree where it was built.
- **Toolchain headers.** If your project builds with a custom/vendored compiler,
  add it to `docker/index/Dockerfile` — a `'X.h' file not found` during indexing
  means the container lacks that toolchain, not a scip-clang bug.

Alternatively, index on any x86_64 machine/CI and copy the resulting
`<name>.graph.db` into `<project>/.cppgraph/` on the ARM host — the MCP server
auto-discovers it and everything downstream is platform-independent.

## 3. Regenerating the SCIP protobuf bindings (optional, dev-only)

`src/cppgraph/proto/scip_pb2.py` and `scip_pb2.pyi` are **generated and committed**
to this repo specifically so that step 1 above is enough for normal
development — you never install `protoc` on the host; the one time you
regenerate, a pinned `protoc` runs in a container.

Only regenerate if `src/cppgraph/proto/scip.proto` changes (e.g. to pick up a
newer SCIP schema from upstream).

### Regenerating the bindings

No host `protoc` needed: [`docker/gen-bindings/`](docker/gen-bindings) runs the
pinned compiler (protoc **35.1**, matching the committed header) in a container
and writes both files back in place — the only supported way, so the compiler
version stays fixed and regeneration is reproducible.

1. (optional) refresh the vendored schema — `sourcegraph/scip` 301-redirects to
   `scip-code/scip` (same project, moved to a dedicated org):

   ```bash
   curl -fsSL -o src/cppgraph/proto/scip.proto \
     https://raw.githubusercontent.com/scip-code/scip/main/scip.proto
   ```

2. Regenerate (needs docker or podman):

   ```bash
   docker/gen-bindings/gen.sh
   ```

3. Verify and commit:

   ```bash
   .venv/bin/python -c "from cppgraph.proto import scip_pb2; print(scip_pb2.Index())"
   git diff --stat src/cppgraph/proto/scip_pb2.py src/cppgraph/proto/scip_pb2.pyi
   ```

   Both generated files self-mark `DO NOT EDIT!` — never hand-edit them, only
   regenerate.

## Summary: what's required vs. optional

| Tool                     | When needed                          | Committed to repo? |
|---------------------------|---------------------------------------|---------------------|
| Python 3.13+ / `uv`       | Always                                | N/A (tool)          |
| `scip-clang` binary       | Always (to produce a `.scip` index)   | No — per-machine data dir (`~/.local/share/cppgraph/bin`), fetched per machine |
| `protoc`                  | Only to regenerate `scip_pb2.py`/`.pyi` | No — runs in a container (`docker/gen-bindings/`), never on the host |
| `scip_pb2.py` / `.pyi`    | Always (imported by cppgraph)         | **Yes**, generated + committed (in `proto/`) |
| `scip.proto`              | Source of truth for the above          | Yes, vendored at `src/cppgraph/proto/scip.proto` |

## 4. Indexing a project (any C++ project with a compile_commands.json)

**Guided path: `cppgraph init`.** From the project directory it locates the
`compile_commands.json`, shows what's indexable, asks the scope questions
(subtree / tests / attribution) in order — each with the info to choose well —
then runs the pipeline below. Re-run it to resume/update. It's a thin front-end
over `reindex.sh`; drop to the raw script when you want to script or fine-tune.

`scripts/reindex.sh` wraps the whole compdb-filter → scip-clang →
cppgraph-build pipeline in one command. It is **generic** — cppgraph works on
any project that provides a `compile_commands.json` (see `AGENTS.md`); the
script takes that path as its first argument, with no project-specific
defaults baked in. This is also the script to hand to an LLM/agent if you
want it to redo or adjust an indexing run, since it embeds the one
non-obvious gotcha inline (see below) instead of relying on the agent to
rediscover it.

```bash
scripts/reindex.sh COMPDB_PATH [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
```

Example invocations (no project is baked into the tool or the script):

```bash
# A project's source subtree (filter to skip third_party/vendored code):
scripts/reindex.sh /path/to/project/compile_commands.json src/ myproject

# One subsystem instead (fast, good for iterating):
scripts/reindex.sh /path/to/project/compile_commands.json src/subsystem/ subsystem

# Everything in the compdb (no filter):
scripts/reindex.sh /path/to/project/compile_commands.json
```

Outputs land in the target project's own `.cppgraph/<name>.{compdb.json,scip,graph.db}`
— next to the code they describe (like `.vscode/`), gitignored, per-machine, never
committed (see AGENTS.md "Large artifacts"). The `graph.db` is the interned SQLite
store queried by `cppgraph find/callers/callees/path/impact` (see DESIGN.md § Store).

Reference timings and store sizes on a large C++ codebase (~6000 TUs): see
`DESIGN.md` § Store. (On ARM via the emulated container, indexing is far slower
and may not complete at all on a large codebase — see the callout in § 2.)

**Gotcha** (already handled by the script, documented here so it isn't
rediscovered on the next project): a build system's generated
`compile_commands.json` is not guaranteed to format the `file` field
uniformly — e.g. a Bazel-generated compdb can mix an absolute bazel-out path
for most entries with a bare relative path for a handful of the same kind of
location. A `SRC_FILTER` that requires a leading `/` would silently drop the
bare-relative ones. `scripts/reindex.sh` does a plain substring match, with no
anchoring, to stay robust to this on any project.
