# Proposal: an "agent pack" to simplify install/update for Claude Code + opencode

**Status: parked, not started.** Design work is done and its open questions are
resolved (verified empirically — see § Verified facts). Referenced from
`TODO.md`'s "Package as a Claude Code plugin" item. Read this before picking that
item up.

## Goal

Reduce today's two-phase manual install ritual (script + agent-driven interview,
Claude-only MCP registration, hand-copied skill files) to something closer to a
single point of entry, for **at least Claude Code and opencode**, without
pretending away the parts that must stay scripts (obtaining the native
`scip-clang` binary, indexing a project — both are heavy, platform-specific,
non-declarative operations).

## The core idea: one folder, read natively by both hosts

**`~/.claude/skills/cppgraph/`** is the single artifact both hosts read, for
different reasons, without conflict:

- **Claude Code** loads any folder under a skills directory that contains
  `.claude-plugin/plugin.json` as a plugin named `<name>@skills-dir` — **discovered
  in place, no marketplace, no install step.** It then honors `SKILL.md` (root),
  `.mcp.json`, `hooks/hooks.json`, `commands/`, `agents/` inside that same folder.
- **opencode** globs `SKILL.md` under `~/.claude/skills/<name>/` as part of its
  "external, Claude-compatible" skill discovery — and simply ignores every other
  file in that folder (`.claude-plugin/`, `.mcp.json`, `hooks/`, `commands/`).
  **Verified empirically**: `opencode debug skill` still lists the skill
  correctly with all those sibling files present.

Call this folder **the agent pack**. Everything below is built around
generating and refreshing it, rather than hand-installing pieces into
per-host locations.

## Why now (the diagnostic)

`setup.sh`/`setup_cmd.py` today fuse three layers of different nature into one
sequential run:

| Layer | Content | Cost | Nature |
|---|---|---|---|
| **L1 — runtime** | checkout + venv + `scip-clang` binary | 1 min–60 min | irreducibly a script |
| **L2 — wiring** | MCP registration, skill, hooks, slash commands | milliseconds | should be declarative |
| **L3 — project data** | `.scip` + `graph.db` in `<project>/.cppgraph/` | minutes–hours | irreducibly a script |

L2 has **no version axis today**: `install_skill()` copies a file once at setup
time and never revisits it. Bumping the tool (`setup.sh --version`) does not
update the skill or re-register anything. The agent has to orchestrate the
right order across all three layers, which is exactly the kind of manual
procedure that produced the real incident this design responds to (an agent
`pip install`-ed cppgraph into an unrelated project's venv, causing a `protobuf`
version conflict — latent, not yet materialized into breakage, but a direct
consequence of install being "a procedure an agent executes", not something
structurally hard to get wrong).

## Proposed architecture

### Pack layout

```
~/.claude/skills/cppgraph/              ← written by the runtime, read by both hosts
├── SKILL.md                            ← read by Claude Code AND opencode
├── .claude-plugin/plugin.json          ← makes the folder a Claude Code plugin
│                                          (opencode ignores it)
├── .mcp.json                           ← MCP declaration (Claude Code only)
├── hooks/hooks.json                    ← SessionStart, PreToolUse
├── hooks-handlers/                     ← bash scripts the hooks call
├── commands/
│   ├── cppgraph-index.md
│   ├── cppgraph-update.md
│   └── cppgraph-doctor.md
└── bin/
    ├── cppgraph-mcp                    ← shim
    └── cppgraph                        ← shim
```

`.mcp.json`:
```json
{
  "mcpServers": {
    "cppgraph": { "command": "${CLAUDE_PLUGIN_ROOT}/bin/cppgraph-mcp" }
  }
}
```
`${CLAUDE_PLUGIN_ROOT}` substitution in `.mcp.json`/`hooks.json` `command`
fields is **officially documented and used as-is in Claude Code's own
examples** — no uncertainty here.

`.claude-plugin/plugin.json`:
```json
{
  "name": "cppgraph",
  "version": "0.4.1",
  "description": "Compiler-exact C++ code graph (SCIP).",
  "skills": ["./"]
}
```
`version` is stamped by the runtime = the tool's own version, so the pack's
version tracks the tool's, instead of drifting independently.

### The shim

`bin/cppgraph-mcp` (~10 lines of bash):
```bash
#!/usr/bin/env bash
HOME_DIR="${CPPGRAPH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/cppgraph}"
BIN="$HOME_DIR/repo/.venv/bin/cppgraph-mcp"
if [ ! -x "$BIN" ]; then
  echo "cppgraph runtime not installed at $HOME_DIR — run /cppgraph-doctor" >&2
  exit 1
fi
exec "$BIN" "$@"
```
Needed because the actual venv binary lives **outside** the plugin folder (in
the persistent per-machine checkout), so `${CLAUDE_PLUGIN_ROOT}` alone can't
reach it. The shim: (1) fails cleanly with a clear message instead of not
existing, (2) lets the runtime move (`CPPGRAPH_HOME`) without rewriting the
pack. On stdio, an error can't go to stdout (would corrupt the MCP protocol)
— hence `>&2` + non-zero exit; the channel that actually talks to the user is
the `SessionStart` hook, which runs independently of MCP health.

### Per-host registration

| Host | MCP | Skill | Hooks / commands |
|---|---|---|---|
| **Claude Code** | declarative — the pack's `.mcp.json`. `claude mcp add` goes away. | the pack's `SKILL.md` | declarative |
| **opencode** | `opencode mcp add cppgraph -- <shim>` (one-shot, idempotent — **verified**: always writes the global config regardless of a project `opencode.json`, cleanly overwrites by key on re-run, no duplication) | the same `SKILL.md`, no copy | n/a — no equivalent mechanism |

Single conceptual entry point: `cppgraph agent-pack sync`. It writes the
folder and, if `opencode` is detected, runs the `opencode mcp add`. The
mechanical asymmetry between hosts is absorbed by one command, not exposed to
the user.

> **Mandatory migration for existing installs**: the current
> `claude mcp add cppgraph --scope user` registration must be **removed** when
> the pack lands, or both coexist. **Verified empirically**: `claude mcp list`
> shows `plugin:cppgraph:cppgraph` (auto-loaded from the pack) *and*
> `cppgraph` (the old user-scope registration) as two separate entries when
> both are present. `agent-pack sync` must do this removal, not the docs.

## Addressing the polluted-venv incident, structurally

Four defenses, decreasing in strength:

1. **Take the agent out of the L2 install path.** The pack isn't installed by
   hand, it's written by the runtime. What isn't installed by a procedure
   can't be mis-installed.
2. **A `PreToolUse` hook on `Bash`, in `deny` mode**, matching
   `(uv )?pip install.*cppgraph` (cover both plain and `-e` forms — the exact
   command from the actual incident isn't known, so the pattern should catch
   both spellings). This is the only defense that fires *at the moment* of the
   error. Unlike the advisory grep-steering hook already in TODO.md (grep on
   comments/literals stays legitimate, so that one must stay advisory), this
   one should be a hard deny: there is no legitimate reason to `pip install
   cppgraph` into a project venv.
3. **Escalate the existing warning to a refusal.** `setup.sh` today just prints
   a note ignoring an active `VIRTUAL_ENV`; that should become a hard failure
   requiring an explicit `--force`.
4. **`cppgraph doctor`** — one command reporting the state of all three layers
   (`--json` for hooks/scripts) — the safe target to redirect an improvising
   agent to.

## Update — three axes, one of which doesn't exist today

| Axis | Detection today | Application today |
|---|---|---|
| **A — the cppgraph tool itself** | `updates.compute_advice` (already good: classifies cost as `none`/`store`/`reindex`) | manual, `setup.sh --version` |
| **B — the scip-clang binary** | `updates.compute_scip_advice` (version + `patchset_version`) | manual, `setup.sh` |
| **C — the agent pack** | **none** | **none** |

`updates.py` already does the hard work, including warning that a version
bump will cost a reindex. The actual gap: **that text only surfaces if
someone calls `status`**, which an agent rarely does unprompted.

Proposal:
1. **Eliminate axis C by deriving it from A.** The pack becomes an artifact
   *generated* by the runtime (`cppgraph agent-pack sync`), stamped with the
   tool's own version. No more hand-copied file, no more drift.
2. **Self-heal at `SessionStart`.** The hook calls
   `cppgraph agent-pack sync --quiet`: if the runtime's version is newer than
   the pack's stamp, the pack is rewritten. Bumping L1 propagates to L2 at the
   next session, no user action needed. (Note: per Claude Code's own docs,
   changes to a skill-dir plugin's `.mcp.json`/`hooks/` only take effect after
   `/reload-plugins` or a restart — only `SKILL.md` is live-reloaded. This
   matches the "next session" framing above; it does not work mid-session.)
3. **Notify at `SessionStart`.** The same hook emits the line from
   `updates.update_advice()` (reuse, don't rewrite) — same for axis B via
   `scip_update_advice`.
4. **Apply: `cppgraph self-update`**, a real subcommand (not a script). `git
   fetch` → checkout the tag → `uv pip install -e .` → re-obtain scip-clang iff
   the pin moved → `agent-pack sync`. Today this logic is scattered between
   `ref_mode` handling in `setup.sh` and `obtain_scip_clang`. A slash command
   `/cppgraph-update` is just a discoverable façade over it — **not** a real
   script (Claude Code slash commands are prompt templates, like skills, not
   arbitrary executables — confirmed via docs).

## What stays a script vs. what becomes declarative

| Item | Becomes | Why |
|---|---|---|
| obtaining `scip-clang` | **script** (unchanged) | native per-platform binary, 4 sources, up to 60 min, checksum |
| indexing a project | **script** (unchanged) | hours of compilation, depends on `compile_commands.json` |
| venv + checkout | **script** (`install.sh`) | must exist before any Python runs |
| Claude Code MCP registration | **declarative** | the pack's `.mcp.json` |
| opencode MCP registration | **one-shot script** | no declarative mechanism on this host; `opencode mcp add` is idempotent and scriptable, that's enough |
| skill | **declarative** (pack file) | read directly by both hosts |
| hooks, slash commands | **declarative** | Claude Code only; opencode has no equivalent (its JS plugins could, see "explicitly not doing") |
| update detection | **automatic** (hook) | the logic already exists, only the channel was missing |

## Migration of each current piece

| Today | Becomes |
|---|---|
| `scripts/setup.sh` | **`scripts/install.sh`** (the name `versions.json`'s comments already assume). Shrinks to L1 only: platform, checkout, venv, `exec cppgraph install`. Loses MCP + skill. `curl`-able. |
| `setup_cmd.obtain_scip_clang` | unchanged — legitimately scripted |
| `setup_cmd.register_mcp` | → **`hosts.py`**, one registrar per host. The Claude case becomes a no-op (the pack handles it) + de-registration of the old user-scope entry |
| `setup_cmd.install_skill` | → **`agent_pack.py::sync()`**, writes a folder instead of a file. The `~/.config/opencode/skills/` write disappears (redundant — opencode reads the same `~/.claude/skills/` copy) |
| `scripts/index.sh` | unchanged, + `/cppgraph-index` façade |
| `scripts/uninstall.sh` | + a 5th item (the pack + the opencode entry); item 1 becomes "deregister from every detected host". Keeps the "don't touch `.cppgraph/`" default. |
| `versions.json` | unchanged — shape is already right |
| `updates.py` | unchanged — just consumed by one more channel |

**The move that matters most**: the two-phase indexing protocol currently
lives in `AGENTS.md` — but that file is only loaded when an agent works
*inside the cppgraph repo itself*. An agent working on, say, MongoDB never
sees it. That protocol belongs in `commands/cppgraph-index.md` inside the
pack, which loads everywhere. This is half of the "manual ritual" the whole
proposal is trying to remove.

## Sequencing (no big-bang)

| Phase | Content | Payoff | Risk |
|---|---|---|---|
| **0 — foundation** | `cppgraph doctor`, `agent_pack.py`, `hosts.py`. No visible change. Tests. | — | none |
| **1 — the pack** ⭐ | Pack generation/write; `.mcp.json`; migration removing `claude mcp add`; `opencode mcp add`. | **Install goes from "script + two-phase interview" to one command.** | MCP collision if migration is buggy → test that first |
| **2 — updates** | `SessionStart` hook (sync + advice), `cppgraph self-update`, `/cppgraph-update`. | axis C disappears; A and B become visible without calling `status` | low |
| **3 — safety & steering** | `PreToolUse` deny on `pip install cppgraph`; `PreToolUse` advisory on grep; `SubagentStart` replay (existing TODO items). | closes the incident + the subagent coverage gap | independent, any order |
| **4 — bonus** | marketplace entry for discoverability. | visibility | real tension, see below |

### The phase-4 tension (don't gloss over it)

Marketplace distribution **copies** the plugin into
`~/.claude/plugins/cache/` — so its `SKILL.md` is no longer under
`~/.claude/skills/` and **opencode stops seeing it**. That would lose exactly
the property the whole design leans on. And writing to
`~/.claude/skills/cppgraph/` *as well* creates two same-named plugins (one
loaded, one reported "not loaded").

Clean resolution if discoverability is ever prioritized: publish a
marketplace containing a **separate** `cppgraph-installer` plugin, whose only
content is `/cppgraph-setup` + a hook that bootstraps L1 then writes the real
pack to `~/.claude/skills/cppgraph/`. The marketplace becomes a distribution
vehicle for the installer, never for the durable wiring itself.

## Explicitly NOT recommended

- **A native opencode JS plugin with custom tools** wrapping the CLI. Doable
  (the project's CLI/MCP parity would make it easy) but creates a second
  surface to maintain for a host that already consumes the MCP server just
  fine. Only worth it if MCP proves insufficient — it isn't today.
- **Publishing to PyPI / switching to a wheel install.** Hard blocker:
  `setup_cmd._repo_root()` is `Path(__file__).parents[2]` and reads
  `versions.json`, `skills/`, `docker/build.sh`, `.venv/bin/`;
  `updates.current_version()` does a `git describe`. A wheel breaks all of
  this **silently** (the path would resolve inside `site-packages`). The
  checkout isn't legacy, it's the distribution mechanism. Minor improvement
  worth doing regardless: make `_repo_root()` fail loudly if it can't find
  `versions.json`.

## Verified facts (empirical + doc, resolving every open question from the design pass)

All done in an isolated `/tmp` sandbox (`HOME`/`XDG_CONFIG_HOME` overridden),
zero contact with real configs. Cleaned up after.

1. **Keystone**: opencode's skill loader (`opencode debug skill`) correctly
   lists a skill whose folder also contains `.claude-plugin/plugin.json`,
   `.mcp.json`, `hooks/hooks.json`, `commands/` — no interference. ✅
2. `opencode mcp add <name> -- <command...>` (note the `--` separator syntax,
   mirroring `claude mcp add`) **always writes to the global config**
   (`~/.config/opencode/opencode.jsonc`), even when a project-level
   `opencode.json` exists in the cwd. ✅
3. Re-running `opencode mcp add` with a different command **cleanly
   overwrites** the existing entry by key — no duplication, no error. ✅
4. A Claude Code slash command is a markdown prompt template (same mechanism
   as a skill/command file) — **not** an arbitrary executable. The
   deterministic channel for automation must be a hook, not a slash command.
   Confirmed via official docs. ✅
5. `${CLAUDE_PLUGIN_ROOT}` substitution is officially documented and used
   as-is in Claude Code's own `.mcp.json`/`hooks.json` examples (quoted form:
   `"${CLAUDE_PLUGIN_ROOT}/servers/db-server"`). No shim needed for this
   specific purpose — the shim in this design exists only because the real
   binary lives *outside* the plugin folder, not because of a substitution
   limitation. ✅
6. **MCP duplication is real**, confirmed by direct observation: with both an
   old-style `claude mcp add cppgraph --scope user` registration *and* a
   skills-dir plugin exposing `.mcp.json`, `claude mcp list` shows **two**
   distinct entries — `cppgraph` and `plugin:cppgraph:cppgraph`. Migration
   step (removing the old registration) is mandatory, not optional polish. ✅
7. The exact command behind the real incident (`pip install cppgraph` vs.
   `pip install -e <path>`) is unknowable in hindsight — not a fact to
   discover, a design choice: the deny-hook pattern should cover both
   spellings. N/A — resolved as "not blocking".

Bonus facts surfaced during verification (not required, worth keeping in mind
for later implementation):
- `OPENCODE_DISABLE_EXTERNAL_SKILLS` / `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`
  env vars exist as opencode-side opt-outs of the whole `~/.claude/` skill
  scan — worth mentioning in docs for a user who wants to disable this
  mechanism entirely.
- For a Claude Code skills-directory plugin, only `SKILL.md` edits apply
  live in the current session; changes to `.mcp.json`/`hooks/`/`agents/`
  need `/reload-plugins` or a restart. Relevant to how "self-heal at
  `SessionStart`" is framed above (it's a next-session guarantee, not
  mid-session).

## Next steps when this is picked back up

Start at Phase 0/1 as scoped above. No further research needed before
implementation — every open question from the design pass has a verified
answer in this document.
