# INSTALL — setting up cppgraph on a new machine

Verified on: macOS 15.7.7, arm64 (Apple Silicon), 2026-07-15. Commands that
differ for other platforms are noted inline.

## 1. Python environment (required, every machine)

Requires Python >= 3.13 (`pyproject.toml`). `uv` manages the venv.

```bash
uv venv
uv pip install -e ".[dev]"
```

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

`scip-clang` is a large external binary (~68 MB). It is never vendored in
git; each machine downloads its own copy into `scratch/bin/` (gitignored).

Verified version: **v0.4.0** from
https://github.com/sourcegraph/scip-clang (mirrors to `scip-code` releases
too — the GitHub API resolves either).

```bash
mkdir -p scratch/bin
gh release download v0.4.0 --repo sourcegraph/scip-clang \
  --pattern "scip-clang-arm64-darwin" --dir scratch/bin --clobber
chmod +x scratch/bin/scip-clang-arm64-darwin
mv scratch/bin/scip-clang-arm64-darwin scratch/bin/scip-clang
```

Asset name depends on platform — pick the matching one from the release:

| Platform            | Asset name                  |
|---------------------|------------------------------|
| macOS arm64          | `scip-clang-arm64-darwin`    |
| Linux x86_64         | `scip-clang-x86_64-linux`    |
| Linux x86_64 (dev)   | `scip-clang-dev-x86_64-linux`|

No Homebrew/apt package is needed for `scip-clang` itself — it's a
self-contained release binary.

Verify:

```bash
scratch/bin/scip-clang --version
# scip-clang 0.4.0
# Based on Clang/LLVM 2078da43e25a4623cab2d0d60decddf709aaea28
```

## 3. `protoc` (optional — only if regenerating SCIP protobuf bindings)

`src/cppgraph/proto/scip_pb2.py` and `scip_pb2.pyi` are **generated and committed**
to this repo specifically so that step 1 above is enough for normal
development — you do not need to install `protoc` just to build or run
cppgraph.

Only install `protoc` if `src/cppgraph/proto/scip.proto` changes (e.g. to
pick up a newer SCIP schema from upstream) and the bindings need
regenerating.

Verified version: **libprotoc 35.1**, installed via Homebrew (also installs
the `abseil` dependency):

```bash
brew install protobuf
protoc --version   # libprotoc 35.1
```

### Regenerating the bindings

1. Refresh the vendored schema (only if you intend to pick up upstream
   changes — otherwise skip and just re-run protoc on the existing file):

   ```bash
   curl -fsSL -o src/cppgraph/proto/scip.proto \
     https://raw.githubusercontent.com/scip-code/scip/main/scip.proto
   ```

   (`sourcegraph/scip` 301-redirects to `scip-code/scip` — same project,
   transferred to a dedicated org.)

2. Regenerate:

   ```bash
   protoc --proto_path=src/cppgraph/proto \
     --python_out=src/cppgraph/proto --pyi_out=src/cppgraph/proto \
     src/cppgraph/proto/scip.proto
   ```

3. Verify and commit:

   ```bash
   .venv/bin/python -c "from cppgraph.proto import scip_pb2; print(scip_pb2.Index())"
   git diff --stat src/cppgraph/proto/scip_pb2.py src/cppgraph/proto/scip_pb2.pyi
   ```

   Both generated files start with `# ... DO NOT EDIT!` / are marked
   generated — never hand-edit them; only regenerate via `protoc`.

## Summary: what's required vs. optional

| Tool                     | When needed                          | Committed to repo? |
|---------------------------|---------------------------------------|---------------------|
| Python 3.13+ / `uv`       | Always                                | N/A (tool)          |
| `scip-clang` binary       | Always (to produce a `.scip` index)   | No — `scratch/` (gitignored), fetched per machine |
| `protoc`                  | Only to regenerate `scip_pb2.py`/`.pyi` | No — one-off dev tool |
| `scip_pb2.py` / `.pyi`    | Always (imported by cppgraph)         | **Yes**, generated + committed (in `proto/`) |
| `scip.proto`              | Source of truth for the above          | Yes, vendored at `src/cppgraph/proto/scip.proto` |

## 4. Indexing a project (any C++ project with a compile_commands.json)

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

Outputs land under `scratch/<name>.{compdb.json,scip,graph.db}` —
gitignored, per-machine, never committed (see AGENTS.md "Large artifacts").
The `graph.db` is the interned SQLite store queried by `cppgraph
find/callers/callees/path/impact` (see DESIGN.md § Store).

Reference timings, indexing a large C++ codebase (~6000 TUs) on a 14-core
arm64 Mac with `scip-clang` v0.4.0:
- one subsystem — 519 TUs, ~151s, 0 errors, 23 MB `.scip`.
- the whole tree — 6004 TUs, ~1253s (indexing 1228s + merging 19s),
  0 errored TUs, 797 MB `.scip` → 323 MB `graph.db`
  (643,967 nodes, 2,735,021 edges; ~23s to build the store). The same graph
  as flat JSON was 1.19 GB — the interned SQLite store shrinks it 3.7× and
  makes queries hit a B-tree index instead of loading the whole file per
  query. See `DESIGN.md` § Store.

**Gotcha** (already handled by the script, documented here so it isn't
rediscovered on the next project): a build system's generated
`compile_commands.json` is not guaranteed to format the `file` field
uniformly — e.g. a Bazel-generated compdb can mix an absolute bazel-out path
for most entries with a bare relative path for a handful of the same kind of
location. A `SRC_FILTER` that requires a leading `/` would silently drop the
bare-relative ones. `scripts/reindex.sh` does a plain substring match, with no
anchoring, to stay robust to this on any project.
