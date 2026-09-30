---
name: dev-workflow
description: "Multi-step work cycle of a software project managed by TODO.md, INPROGRESS.md, CHANGELOG.md. Triggers on: starting a new task or phase tracked in TODO.md/INPROGRESS.md, finishing a task, updating progress state, creating or updating a source file's .md companion, preparing or verifying a commit (pre-commit hook, dev-workflow.json), suggesting a /compact compaction at a task boundary."
---

# Dev workflow (TODO → INPROGRESS → CHANGELOG)

## 1. TODO/INPROGRESS → CHANGELOG cycle

(`TODO.md`, `INPROGRESS.md` and `CHANGELOG.md` are the default filenames — configurable via `workflow.todo` / `workflow.inprogress` / `workflow.changelog` in `dev-workflow.json`.)

- **Roles**: `TODO.md` is the high-level roadmap — phases made of task checkboxes, no detail; `INPROGRESS.md` tracks exactly **one** task at a time and holds the active task's detail. Stable design decisions go to `SPEC.md` (if `workflow.spec` of `dev-workflow.json` points to an existing file), not to `INPROGRESS.md`.
- **At task start** (before executing): read `INPROGRESS.md` — if it holds an unfinished task, resume exactly where marked (first unticked `[ ]` step) — plus `TODO.md` if it exists (roadmap) and `SPEC.md` if configured. A task begins with a design step of variable length: write the task header immediately (name + objective + reference to the matching `TODO.md` section), then detail the steps progressively as the design converges — so the design itself stays traceable across a lost session. The step list bounds the session: anything identified but outside it is explicitly deferred (a new `TODO.md` entry or `SPEC.md`'s "Out of scope"), never silently absorbed; if the scope must grow mid-task, revalidate with the user. During that same convergence, re-read the sections of `SPEC.md` (and `TODO.md`) the task touches: an inconsistency or a gap is flagged immediately — before writing code, not mid-way. A clear request to perform a task authorizes its start; a question, a partial answer or an ambiguous assent does not authorize an unresolved task or scope — clarify before executing.
- **One task at a time**: if a second task is requested while `INPROGRESS.md` holds an unfinished one, ask the user first (finish, park, or switch) before touching the file.
- **While working**: tick `[x]` steps as they complete and record blockers as soon as they appear — never only before commit. The first unticked `[ ]` step is the current position; this is what makes a lost session resumable.
- **Task complete** (every step ticked, or explicitly dropped with a note) — one atomic pass, performed by the orchestrator once all delegated steps are done or dropped (delegated agents maintain INPROGRESS.md as they work but never close the task unless the brief explicitly delegates it):
  1. Add an entry to `CHANGELOG.md` (Keep a Changelog format — entry template at the top of that file; for versionless projects, a date + summary line is enough). Task entries always land under `## [Unreleased]` and describe:
     - the net change relative to the previous released version (the last `## [X.Y.Z] - date` entry below it) — not relative to the state before this task;
     - a change undone within the same unreleased version: not mentioned — edit the earlier Unreleased entry so the section describes only the net effect;
     - the revert of an already-released change: mentioned, as part of the new version's entry.
  2. Tick the corresponding task checkbox in `TODO.md` — only if every `INPROGRESS.md` step is ticked or explicitly dropped; if the task was re-scoped or split along the way, update `TODO.md` accordingly instead of ticking it.
  3. Reset `INPROGRESS.md` to the clean skeleton with a pointer to the next `TODO.md` task — a clean skeleton between two tasks is normal, not stale.

## 2. .md companions

If `dev-workflow.json` has `companions.enabled: true`:

- Every **newly created** source file gets its companion `.md` (same folder, same name) created **in the same pass**: role of the file, invariants, pitfalls. Not after the fact. Enforcement scope: the hook enforces this mechanically **only** for the extensions listed in the companions check of `dev-workflow.json` (default `["cpp", "h", "ts", "tsx"]`) — keep that list aligned with the project's languages; for any other extension the rule is the skill's, not the hook's.
- An existing source file **significantly modified** → its `.md` companion (if it exists) is updated in the same pass.

On an existing codebase you don't want to pollute with companions, set `companions.enabled: false` in dev-workflow.json — no companion is created and the hook check is skipped.

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
2. **Semantic check (LLM).** A review-agent pass (`review`, or `cheap-review` from the global roster if installed) on the staged diff plus the task intent (INPROGRESS.md, TODO.md, SPEC.md) with one question: *what might be missing?* — stale or missing companion `.md`, missing CHANGELOG entry, touched-but-unstaged file, forgotten test, dead code left behind. Each finding is rated by importance (blocking / worth fixing / ignorable).

The orchestrator summarizes the findings and makes a recommendation — the **user decides**: fix first, or commit as-is. The semantic check never blocks the commit mechanically; the hook does.

Committing is the user's call too: at task end — or when a validated wave could be committed — the orchestrator asks how to proceed (one commit per validated wave, a single bulk commit, or wait), recommends one option with a one-line rationale grounded in the task's actual content (e.g. "3 independent waves → 3 commits"), judged on bisectability and risk, not a generic formula, and follows the user's decision — it never commits autonomously.

Releases are proposed the same way: when `## [Unreleased]` accumulates releasable changes, propose a release as a commit is proposed — recommend timing and version with a grounded rationale, and follow the user's decision. Approval of the proposal is not release authorization: after approval, verify the notes, the version, the checks, the tag and the push target separately — never tag or push from the proposal alone. If checks fail, the tag already exists, or a push partly succeeds: abort before pushing, fix, and retry from a clean state — an already-published tag is handled explicitly with the user, never implied automatic rollback.

NEVER suggest `--no-verify` unless the user explicitly asks for it.

## 7. Memory / compaction

- Suggest `/compact` (never run it automatically) only at **natural boundaries**: phase done and committed, completely different subject starting. Never in the middle of an active debug/refactor.
- The suggested message talks about the **next** task: objective + existing reusable base. Never a summary of what was just done (already in git/CHANGELOG).
- **Agent instance — resume or start fresh**: reuse the same agent instance (its `task_id`) for a small iteration on **its own** output — the author's fix round, a short follow-up: it already holds the files it read, the code it wrote, the tests it ran; a fresh instance re-derives all of it from scratch. Start a fresh instance for anything large or unrelated to what it did before, with a complete self-contained brief — the brief replaces the accumulated context, so nothing is lost; resuming instead means the instance re-reads its whole past on every turn and the context cost balloons for no benefit. The test: would a new instance have to rediscover anything the old one already knows? If yes — resume; if the past context adds nothing — start fresh.
- **Reviews — always a fresh instance**: a review (code or design), first pass or re-review, never reuses an instance that authored or already judged the work; the orchestrator starts fresh and hands it the **contract** (acceptance criteria from TODO/INPROGRESS/SPEC in the orchestrator's own words, frozen decisions, rules/conventions pointers, `RULE-DEVIATION` lines), the **artifact** (diff range or doc path) and **raw evidence** (test output verbatim) — never the author's reasoning or transcript. On a re-review the brief adds the prior findings list plus the fix delta: each finding closed by `file:line`, and the delta reviewed as new code. The author's report is an index of claims to verify — its "points of attention" are a hint, not the review's scope. One task = one instance for implementers and designers: resumed only for that task's own fix rounds, never across tasks.
- **Empty report — a recovery decision, not a verdict**: a subagent that returns completed with an empty or evidence-free report is a recovery decision, not a verdict on the work — inspect the worktree and task state first; if the instance is resumable, request a factual report from it; verify the result independently; if recovery fails, escalate rather than blindly rerunning side-effecting work.

## 8. Periodic coherence review

- When a phase completes (all its tasks ticked in TODO.md) — or whenever the user asks — PROPOSE a read-only coherence & simplification review of the phase's commits (never run it automatically).
- The review checks: docs/instructions vs actual behavior (config, hook, agents), stale or missing companions, cross-file contradictions introduced along the way, and simplification opportunities (fewer instructions at equal meaning — nothing may be lost).
- Report findings ranked by importance with file:line; the user decides what to apply.
