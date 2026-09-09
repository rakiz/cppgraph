# Scripts

The scripts in this directory are the operational entry points for cppgraph:
obtaining the tool and its `scip-clang` dependency, indexing projects, measuring
the cost of graph queries, and uninstalling the installation. Querying a graph
and building its SQLite store are `cppgraph` CLI operations; this directory
coordinates the lifecycle around them.

The normal user path is `setup.sh`, followed by `index.sh` for each project.
The two `scip-clang` maintainer scripts are separate: they are manual tools,
never called by cppgraph's own code and never run in CI.

## `setup.sh`

Sets up cppgraph on the machine, then hands off to `cppgraph setup`. The
launcher creates or reuses the repository's `.venv`, installs the editable tool
with its development, MCP, and TUI dependencies, and then lets the CLI obtain
`scip-clang`, register the MCP server, and optionally index the current project.
This is the main entry point because it ensures the Python environment exists
before any Python-based setup can run.

```sh
scripts/setup.sh
```

Useful options:

- `--list-sources` prints the `scip-clang` sources valid for this host and exits.
- `--scip-source download-patched|download|build|emulate` selects a source without
  prompting; this is required for non-interactive runs. `download-patched` fetches this
  project's own prebuilt patched binary — enclosing_range + ForwardDefinition + ReadAccess/WriteAccess
  (macOS arm64 / Linux aarch64 today);
  `build` is the Linux Docker build; `emulate` indexes through an x86 container
  when no native binary exists.
- `--version VERSION`, `--branch BRANCH`, or `--nightly` selects the cppgraph
  checkout/ref to install.
- `--from-scratch` re-walks setup stages; `--no-index` stops after machine setup;
  `-y`/`--yes` keeps existing binaries and MCP registration.

It requires `uv`. The installed tool and per-machine binary live below
`${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph`; `CPPGRAPH_BIN_DIR` can override
the binary location. The Linux Docker build used by the `build` source is the
sibling [`docker/build-scip-clang-patched-linux/`](../docker/build-scip-clang-patched-linux/) implementation.

## `index.sh`

Runs the installed `cppgraph index` wizard from a project directory. It finds
the compilation database, presents scope/test/attribution choices, and reuses
existing `.scip` and `.graph.db` artifacts unless asked to recompute them. This
keeps the expensive compiler indexing step explicit and avoids overwriting a
potentially hours-long index by surprise.

```sh
scripts/index.sh
scripts/index.sh --from-scratch
```

The tool environment must already have been created by `setup.sh`. All index
wizard flags are passed through, including `--plan-json` for inspecting choices
and `--filter`, `--no-tests`, `--attributed-refs`, `--run`, and `-y` for a
non-interactive run. Outputs are stored under the target project's
`.cppgraph/` directory.

## `index-in-container.sh`

Builds only the `.scip` portion of an index with an x86_64 `scip-clang` image on
hosts that cannot run the indexer natively, such as ARM-Linux or Windows. It
then builds the graph natively with cppgraph, because parsing SCIP is portable
and does not need to remain in the container. This split avoids emulating the
Python graph-building step and prints a native build command if it cannot find
the local tool environment.

```sh
scripts/index-in-container.sh COMPDB [SRC_FILTER] [OUT_NAME] [PROJECT_ROOT]
```

The container engine is auto-detected as Docker or Podman. Set
`CPPGRAPH_CONTAINER` to choose one, `CPPGRAPH_INDEX_IMAGE` to change the image
tag (default `cppgraph-index:latest`), and `SCIP_CLANG_VERSION` to change the
image build argument. `CPPGRAPH_INDEX_NO_BUILD=1` stops after writing the `.scip`
file. On non-x86 Linux, QEMU binfmt must be registered first. The script also
requires a usable `compile_commands.json`; it does not create one.

## `uninstall.sh`

The safe, interactive counterpart to `setup.sh`. It offers removal of the user
MCP registration, the per-machine `scip-clang` binary, the cppgraph checkout and
venv, and the current project's `.cppgraph/` data. Project graph data is kept
by default because a `.scip` file can take hours to rebuild; other projects are
not touched.

```sh
scripts/uninstall.sh
scripts/uninstall.sh --dry-run
scripts/uninstall.sh --yes
scripts/uninstall.sh --purge
```

`--dry-run` makes no changes. `--yes` removes the MCP registration, binary, and
tool while keeping project data. `--purge` (or `--all`) also removes the current
project's data. `${XDG_DATA_HOME:-$HOME/.local/share}` selects the data root and
`CPPGRAPH_BIN_DIR` overrides the binary directory.

## `measure_tokens.py`

Measures what an LLM would receive when answering “who calls this name?” with
whole-tree grep, known-subtree grep, and cppgraph's MCP JSON. It reports the
cost of raw matches and of the context needed to disambiguate declarations,
comments, and same-named symbols. Token counts are deliberately rough
character-based estimates, not tokenizer output; the suite includes both
grep-favorable and cppgraph-favorable cases.

```sh
scripts/measure_tokens.py NAME SRC_ROOT GRAPH_DB [SUBTREE] [TARGET_SUBSTR]
scripts/measure_tokens.py --suite SRC_ROOT GRAPH_DB
```

Run it with the repository's Python environment so it can import cppgraph. The
graph database must already exist. `TARGET_SUBSTR` selects which resolved symbol
to benchmark; otherwise the symbol with the most callers is selected.

## `gen-cli-reference.py`

Rewrites the per-command tables in [`CLI_REFERENCE.md`](../CLI_REFERENCE.md)
from the CLI's own argparse subparsers, so the reference page cannot drift from
the real `--help` text. The grouping, section prose, and examples are
hand-written in the markdown; only the command/purpose/argument rows are
generated, between `cppgraph-gen` markers.

```sh
scripts/gen-cli-reference.py
```

Run it with the repository's Python environment. It exits non-zero when a
subcommand exists in the CLI but is placed in no section of the doc (or a marker
names a command that no longer exists) — the prompt to curate the newcomer's
place, not a silent omission. `tests/test_gen_cli_reference.py` checks the
checked-in page is always fresh.

## `build-scip-clang-patched-macos.sh`

Builds a patched `scip-clang` binary (v0.4.0 + `enclosing_range` (#504) +
ForwardDefinition + ReadAccess/WriteAccess) natively for
the current Mac architecture. Docker cannot produce a native macOS binary, so
this script builds LLVM/Clang on the host with Bazel, patches the pinned source,
verifies the result, and writes the binary plus provenance sidecar.

This is a **manual maintainer tool**: never run it from CI or expect cppgraph to
call it. It requires macOS, Xcode Command Line Tools, git, and python3; Bazel or
Bazelisk is used, with Bazelisk downloaded locally when needed. It takes an
optional output directory:

```sh
scripts/build-scip-clang-patched-macos.sh [OUTPUT_DIR]
```

`CPPGRAPH_BUILD_SRC_DIR` selects the reusable source/build checkout,
`CPPGRAPH_BIN_DIR` and `XDG_DATA_HOME` select the default binary destination,
`CPPGRAPH_MIN_BUILD_GB` changes the advisory disk-space threshold, and
`BAZEL_JVM_HEAP` changes the Bazel heap setting. Expect roughly 30–60 minutes
and 30–40 GB of disk. Unlike the Docker build, whose container is discarded
afterwards, Bazel's own build cache is left on disk (in its default
per-workspace `output_base`, outside any of this repo's directories) — after a
successful build the script measures its actual size and, at an interactive
terminal, offers to reclaim it with `bazel clean --expunge`; a non-interactive
run always skips this and just prints the size and the manual command. The
resulting binary is the input to `publish-scip-clang-patched.sh`.

For Linux, the equivalent Docker-based maintainer build is
[`docker/build-scip-clang-patched-linux/`](../docker/build-scip-clang-patched-linux/); its own README
documents that path and its disk/timing trade-offs.

## `publish-scip-clang-patched.sh`

Publishes a locally built patched binary and its SHA-256 checksum as assets on a
GitHub release. It creates or reuses the release tag
`scip-clang-patched-vVERSION-pPATCHSET`, allowing assets for multiple platforms
to share one release (PATCHSET is this repo's patch-bundle version,
`scip_clang.patchset_version` in `versions.json` — bumping it starts a fresh
release even on the same upstream version). Existing assets are replaced only
after an interactive confirmation. This publishes the binary only;
`cppgraph setup`'s `download-patched` source consumes exactly these assets.

This is also a **manual maintainer tool**, never called by cppgraph and never
run in CI. It requires an authenticated `gh`, python3, and `sha256sum` or
`shasum`:

```sh
scripts/publish-scip-clang-patched.sh BINARY PLATFORM [VERSION] [PATCHSET]
```

`BINARY` may be the executable or a directory containing `scip-clang`.
`PLATFORM` must describe where it was built, not where the command runs:
`aarch64-linux`, `x86_64-linux`, or `arm64-darwin`. `VERSION` defaults to the
`scip_clang.version` pin and `PATCHSET` to the `scip_clang.patchset_version`
pin in `versions.json`. The script sanity-checks binaries
that can run on the current host, but still supports publishing a foreign-arch
binary and always computes its checksum.

The intended handoff is: build with `build-scip-clang-patched-macos.sh` (or the sibling
Docker build), then publish with this script. Publishing does not change the
normal setup/index path.
