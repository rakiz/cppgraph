---
name: dev-workflow
description: "Multi-step work cycle of a software project managed by TODO.md, INPROGRESS.md, CHANGELOG.md, DESIGNS.md. Triggers on: starting a new task or phase tracked in TODO.md/INPROGRESS.md, finishing a task, updating progress state, creating or updating a source file's .md companion, preparing or verifying a commit (pre-commit hook, dev-workflow.json), suggesting a /compact compaction at a task boundary."
---

# Dev workflow (TODO → INPROGRESS → CHANGELOG; DESIGNS archive)

## 1. TODO/INPROGRESS → CHANGELOG cycle

(`TODO.md`, `INPROGRESS.md` and `CHANGELOG.md` are the default filenames — configurable via `workflow.todo` / `workflow.inprogress` / `workflow.changelog` / `workflow.designs` in `dev-workflow.json`.)

- **Roles**: `TODO.md` is the high-level roadmap — phases made of task checkboxes, no detail; `INPROGRESS.md` tracks exactly **one** task at a time and holds the active task's detail. Stable design decisions go to `SPEC.md` (if `workflow.spec` of `dev-workflow.json` points to an existing file), not to `INPROGRESS.md`. `DESIGNS.md` (if `workflow.designs` points to an existing file) is the **archive of designs as implemented** — one section per completed task, newest first; it is non-normative history, never a second progress tracker or roadmap: `SPEC.md` stays the owner of current decisions, and when SPEC is absent or not configured, durable design decisions live in DESIGNS entries plus `TODO.md`'s "Context" notes.
- **At task start** (before executing):
  - **Resume**: read `INPROGRESS.md` — if it holds an unfinished task, resume exactly where marked (first unticked `[ ]` step) — plus `TODO.md` if it exists (roadmap) and `SPEC.md` if configured.
  - **Design step (mandatory, every task)**: write the task header immediately (name + objective + reference to the matching `TODO.md` section) together with an unticked `[ ]` design/planning step, then detail the steps progressively as the design converges — the STEP is always there, the ARTIFACTS are proportional (a trivial task's design can be a few lines: scope, steps, acceptance criterion) — so the design itself stays traceable across a lost session (the first unticked `[ ]` step is always the current position).
  - **Routing** — the design's depth follows the task's class: **mechanical** (well-understood, single-file-scope transformation) → the orchestrator writes the minimal plan itself, no design agent; **standard** (ordinary feature/refactor on established patterns) → the orchestrator designs, delegating to a design producer agent is optional.
  - **Architectural tasks** (new/changed contract, migration, concurrency, security boundary, costly-to-reverse): delegate design to a producer agent (`cheap-design`/`strong-design` per escalation rules) **and** run a mandatory independent design review (fresh instance, read-only) before implementation. No design agent available in the session → stop and ask the user how to satisfy the gate. Never silently self-design an architectural task.
  - **SPEC reconciliation**: re-reading the sections of `SPEC.md` (and `TODO.md`) the task touches is not just a consistency check — the orchestrator **re-evaluates the global design at task granularity** (do the touched sections still hold given this task's detail?); a global-vs-local disagreement is flagged before writing code, not mid-way; the local decision wins and the SPEC update follows.
  - **Scope**: the step list bounds the session — anything identified but outside it is explicitly deferred (a new `TODO.md` entry or `SPEC.md`'s "Out of scope"), never silently absorbed; if the scope must grow mid-task, revalidate with the user. Explicit user approval **before implementation** is required when: the objective changes relative to the `TODO.md` task; the scope would grow (crossing the "Out of scope" boundary — merely referencing it is not a trigger); the design conflicts with a SPEC requirement or non-goal; the action is destructive or costly to reverse; a user-facing choice is unresolved. Otherwise a clear request to perform a task authorizes its in-scope plan — delegating the design to a producer agent is by itself not a confirmation trigger; a question, a partial answer or an ambiguous assent does not authorize an unresolved task or scope — clarify before executing.
  - **Batch questions before asking**: when the design leaves several open questions, investigate repo facts first, then ask the user once — a single batch of the independent, consequential questions, each with a recommended answer. Do not leak one question at a time, and never pad the batch with trivia resolvable from the repo.
- **One task at a time**: if a second task is requested while `INPROGRESS.md` holds an unfinished one, ask the user first (finish, park, or switch) before touching the file.
- **While working**: tick `[x]` steps as they complete and record blockers as soon as they appear — never only before commit. The first unticked `[ ]` step is the current position; this is what makes a lost session resumable.
- **Task complete** (every step ticked, or explicitly dropped with a note) — one atomic pass, performed by the orchestrator once all delegated steps are done or dropped (delegated agents maintain INPROGRESS.md as they work but never close the task unless the brief explicitly delegates it):
  1. Add an entry to `CHANGELOG.md` (Keep a Changelog format — entry template at the top of that file; for versionless projects, a date + summary line is enough). Task entries always land under `## [Unreleased]` and describe:
     - the net change relative to the previous released version (the last `## [X.Y.Z] - date` entry below it) — not relative to the state before this task;
     - a change undone within the same unreleased version: not mentioned — edit the earlier Unreleased entry so the section describes only the net effect;
     - the revert of an already-released change: mentioned, as part of the new version's entry.
  2. Tick the corresponding task checkbox in `TODO.md` — only if every `INPROGRESS.md` step is ticked or explicitly dropped; if the task was re-scoped or split along the way, update `TODO.md` accordingly instead of ticking it.
  3. If `workflow.designs` of `dev-workflow.json` points to an existing file (e.g. `DESIGNS.md`), add a DESIGNS entry — one section per completed task, newest first: the design **as implemented**, direction changes (planned X → did Y, why), rejected alternatives worth remembering, and pointers to the relevant `SPEC.md` sections (link, don't duplicate normative content). Write it **before** the reset: resetting `INPROGRESS.md` erases the task intent the commit review needs, so the finalized contract — acceptance criteria plus the design decisions taken — is captured in this entry first; after closure, the commit review's task-intent sources include the latest DESIGNS entry (§6).
  4. Reset `INPROGRESS.md` to the clean skeleton with a pointer to the next `TODO.md` task — a clean skeleton between two tasks is normal, not stale.

## 2. .md companions

If `dev-workflow.json` has `companions.enabled: true`:

- Every **newly created** source file gets its companion `.md` (same folder, same name) created **in the same pass**: role of the file, invariants, pitfalls. Not after the fact. Enforcement scope: the hook enforces this mechanically **only** for the extensions listed in the companions check of `dev-workflow.json` (default `["cpp", "h", "ts", "tsx"]`) — keep that list aligned with the project's languages; for any other extension the rule is the skill's, not the hook's.
- An existing source file **significantly modified** → its `.md` companion (if it exists) is updated in the same pass.

On an existing codebase you don't want to pollute with companions, set `companions.enabled: false` in dev-workflow.json.

## 3. Coding conventions

If `workflow.conventions` of `dev-workflow.json` points to an existing file (e.g. `CONVENTIONS.md`), read it before writing or editing code and follow its rules.

Two baseline rules apply **always** — even if the conventions file does not exist or has not been customized:

1. **Comments describe intent, not code.** A comment explains WHY the code does something (a decision, a constraint, a non-obvious tradeoff) — it never restates WHAT the next line already says in code.
2. **Comments describe the current state, not its history.** Outside `CHANGELOG.md`, code comments never narrate evolution — no history-narrating phrasing ("previously", "used to be", "changed from"…). A past decision that matters belongs in `CHANGELOG.md` or a commit message, not in a comment describing what the code does today.

## 4. Safety rules

If `workflow.rules` of `dev-workflow.json` points to an existing file (e.g. `RULES.md`), read it before writing or editing code and follow its rules. Unlike the conventions, a rule violation is never silent: flag it per that file's deviation protocol.

## 5. Spec/design compliance

If `workflow.spec` of `dev-workflow.json` points to an existing file (e.g. `SPEC.md`), re-read it before committing a change that could deviate from it. If a deviation is necessary, **flag it explicitly to the user** — do not decide alone.

## 6. Before proposing a commit

Two layers, both required:

1. **Deterministic hook.** Verify (or remind the user) that the pre-commit hook passes: checks are defined in `precommit.checks[]` of `dev-workflow.json` (the template ships a disabled `tests` check plus the companions builtin — only checks with a non-empty `cmd` run). If the hook is absent or inactive (e.g. an existing hook was parked as a sibling), run the configured `precommit.checks[]` checks manually before proposing the commit.
2. **Semantic check (LLM).** A review-agent pass (`review`, or `cheap-review` from the global roster if installed) on the commit, not the worktree (`git show`, or the staged diff before one exists), plus the task intent (INPROGRESS.md, TODO.md, SPEC.md — for an already-closed task, also the latest DESIGNS.md entry, which captured the contract before the reset), with one question: *what might be missing?*
   - stale or missing companion `.md`;
   - missing CHANGELOG entry;
   - a stale, missing or divergent `DESIGNS.md` entry when `workflow.designs` is enabled (it must reflect the design as actually implemented, direction changes included);
   - touched-but-unstaged file, forgotten test, dead code left behind.

   Totals claimed in a commit message (tests added, suites passing) must be reproducible — run the test suite against the commit, not the worktree. Each finding is rated by importance (blocking / worth fixing / ignorable).

The orchestrator summarizes the findings and makes a recommendation — the **user decides**: fix first, or commit as-is. The semantic check never blocks the commit mechanically; the hook does.

Committing is the user's call too: at task end — or when a validated wave could be committed — the orchestrator asks how to proceed (one commit per validated wave, a single bulk commit, or wait), recommends one option with a one-line rationale grounded in the task's actual content (e.g. "3 independent waves → 3 commits"), judged on bisectability and risk, not a generic formula, and follows the user's decision — it never commits autonomously.

Once a commit is made (user-approved), a quick post-commit gate applies to every commit: `git show --stat HEAD` must match the implementation agent's reported file list file by file — any gap is blocking; the test totals must equal the pre-commit baseline plus the wave's contribution (run post-commit); and the commit message states only verifiable claims.

Releases are proposed the same way: when `## [Unreleased]` accumulates releasable changes, propose a release as a commit is proposed — recommend timing and version with a grounded rationale, and follow the user's decision. Approval of the proposal is not release authorization: after approval, verify the notes, the version, the checks, the tag and the push target separately — never tag or push from the proposal alone. If checks fail, the tag already exists, or a push partly succeeds: abort before pushing, fix, and retry from a clean state — an already-published tag is handled explicitly with the user, never implied automatic rollback.

NEVER suggest `--no-verify` unless the user explicitly asks for it.

## 7. Memory / compaction

- Suggest `/compact` (never run it automatically) only at **natural boundaries**: phase done and committed, completely different subject starting. Never in the middle of an active debug/refactor.
- The suggested message talks about the **next** task: objective + existing reusable base. Never a summary of what was just done (already in git/CHANGELOG).
- **Agent instance — resume or start fresh**: per the global AGENTS.md rule (same `task_id` instance for a small iteration on its own output; fresh instance with a complete self-contained brief for anything large or unrelated to what it did before).
- **Reviews — always a fresh instance**: a review (code or design), first pass or re-review, never reuses an instance that authored or already judged the work; the orchestrator starts fresh and hands it the **contract** (acceptance criteria from TODO/INPROGRESS/SPEC in the orchestrator's own words, frozen decisions, rules/conventions pointers, `RULE-DEVIATION` lines), the **artifact** (diff range or doc path) and **raw evidence** (test output verbatim) — never the author's reasoning or transcript. On a re-review the brief adds the prior findings list plus the fix delta: each finding closed by `file:line`, and the delta reviewed as new code. One task = one instance for implementers and designers: resumed only for that task's own fix rounds, never across tasks.
- **Empty report — a recovery decision, not a verdict**: a subagent that returns completed with an empty or evidence-free report is a recovery decision, not a verdict on the work — inspect the worktree and task state first; if the instance is resumable, request a factual report from it; verify the result independently; if recovery fails, escalate rather than blindly rerunning side-effecting work.

## 8. Sub-agents & git

- **The staging area is the agent's report, file by file.** Commit from the implementation agent's reported file list (`git add <reported files>`), never a directory deduction — a `git add <dir>` is how tests get missed. After the commit, compare `git show --stat HEAD` against the report: any gap is blocking (see §6 post-commit gate).
- **Dirty worktree is stashed, never discarded.** Before any checkout, reset or rebase, `git status` must be clean; if dirty, `git stash` (recoverable, logged) — never `git checkout -- .` or any other discard.
- **One writing agent per worktree.** Run parallel implementation sub-agents only with disjoint file sets and no shared artifacts (build directory, test suite, stash); otherwise serialize. Implementation agents never mutate git state — no stash, checkout, reset or commit; every git operation routes through the orchestrator.
- **Git mutations stay inside the launch repo.** An agent instance — sub-agent or orchestrator — adds, commits, stashes, checks out or resets only in the repository it was launched in (its starting directory / its own worktree); mutating git state in any other repo (a dependency, a sibling checkout, a scratch dir reached via `cd`) is forbidden — a cross-repo change is surfaced to the user instead.
- **Lost work that was rewritten is documented, not hidden.** Restoring lost work gets a dedicated restoration commit stating what was lost, the faulty original commit and the totals discrepancy — never silently amend or rewrite history; the history must tell the incident.

## 9. Periodic coherence review

- When a phase completes (all its tasks ticked in TODO.md) — or whenever the user asks — PROPOSE a read-only coherence & simplification review of the phase's commits (never run it automatically).
- The review checks: docs/instructions vs actual behavior (config, hook, agents), stale or missing companions, cross-file contradictions introduced along the way, and simplification opportunities (fewer instructions at equal meaning — nothing may be lost).
- Report findings ranked by importance with file:line; the user decides what to apply.
