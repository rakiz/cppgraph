# redwood_integration.md — handoff to Redwood (written from the cppgraph side)

Companion to redwood's `docs/cppgraph_integration.md` (their requirements
of us). This is the reverse half: what **cppgraph expects REDWOOD to do**
so the `log → code` step of their triage loop can go live. Same governance
as every integration in this workspace: redwood queries, cppgraph answers;
nobody edits the neighbor's tree; redwood adapts to cppgraph's contract —
never the reverse.

## 1. What cppgraph will supply (so redwood knows what it is building against)

An MCP tool **`log_id_lookup`** (CLI: `cppgraph log-id-lookup`), backed by
an id→symbol table extracted at INDEX time (a numeric-literal pass over the
already-scanned compilation: the 2nd argument of `LOG(...)`/`MONGO_LOGV2`
occurrences, mapped to the enclosing function's symbol). One batched call,
resolution entirely in code — zero LLM tokens spent on the lookup itself.

Sketch of the query surface (SHIPPED — the v1 contract is pinned below; the
sketch is kept for the history of the shape):

```
log_id_lookup(ids: [12983100, 12983101], repo: <checkout path>)
→ {
  "resolved_against": {"graph_source_commit": "...", "repo_head": "...",
                        "head_matches_graph": true},
  "ids": [
    {"id": 12983100,
     "found": true,
     "symbol": "mongo::secondary_lag_watchdog::check(...)",
     "scip": "scip-clang cxx . . mongo/…#check(…).",
     "location": "src/mongo/db/repl/secondary_lag_watchdog.cpp:140",
     "callers": ["…bounded list…"]}
  ]
}
```

**Contract v1, PINNED (the document redwood codes against — golden-file
tested; changes land as a new contract_version, never in place).** Full
detail in `src/cppgraph/redwood.py`'s docstring; the shape as shipped:

```
cppgraph log-id-lookup --ids <int> [<int> …] --repo <checkout> [--revision <githash>]
→ {"schema": 1, "command": "log-id-lookup", "contract_version": 1,
   "resolved_against": {"graph_source_commit": "<hex>", "repo_head": "<hex>",
                         "head_matches_graph": true, "revision": "<githash>|null"},
   "log_id_table": true,
   "ids": [{"id": 12983100, "found": true, "symbol": "<display name>",
            "scip": "<exact SCIP identity string>",
            "location": "<relative path>:<1-indexed line>",
            "callers": {"items": [{"symbol", "name", "file", "line"}, …],
                         "truncated": false, "total": 0}}
         | {"id": N, "found": false, "reason": "<why>"}]}
```

Deviations from the sketch above, pinned as v1: the envelope keys
`schema`/`command`/`contract_version`; per-id `scip` is the exact SCIP
identity string (the sketch's decorated `"scip-clang cxx . . …"` string is
masora's ANCHOR format, not SCIP's — cppgraph does not fabricate masora
anchors); `callers` is a bounded OBJECT (`items`/`truncated`/`total`), not a
flat list — bounding must be visible; `location` is 1-indexed; a found entry
carrying more than one table row for the id also carries `matches: N` (the
first row in (file, line) order answers; the duplicate id is shown, never
silently hidden). `resolved_against.revision` is the caller's `--revision`
ECHOED back — cppgraph never resolves it against anything it doesn't have
(no network, no fetch).

- **Revision guard is first-class, as redwood's doc demands**: the answer
  names the revision it was resolved against (`graph_source_commit` +
  `repo_head` + `head_matches_graph`). An answer computed on a checkout
  that does not contain the failing revision is delivered as-is but
  visibly mis-matched — redwood's policy (treat it as provenance-only)
  stays THEIRS to enforce; cppgraph never silently re-resolves.
- **Fail-closed**: an id with no matching literal occurrence, or no usable
  graph, returns `found: false` / an explicit unavailability — never a
  guessed symbol. Note the honest limit: the table maps ids to ENCLOSING
  symbols of occurrences indexed at build time; a log statement added
  after the index build is `not_found` even on the right revision.
- **Bounded**: `callers` is capped; the whole answer stays within
  redwood's ≤ 1500-token budget for ≤ 2 ids per call.

## 2. What REDWOOD must supply (their side of the deal)

1. **The hop split stays as they defined it**: redwood owns the texte hop
   (`id → file:line` over the checkout) and cppgraph the symbol hop. When
   both are available, redwood may cross-check, but the graph answer is
   the authoritative one; when only the texte hop answers, redwood must
   LABEL the result as text-only evidence — weaker than a graph answer
   (their contract already states this; it is repeated here because it is
   a cppgraph-side expectation too).
2. **Revision inputs**: redwood passes the failing revision's githash with
   the query (as their doc promises: error_ids + failing revision). The
   tool needs the checkout to be AT (or the graph to be indexed FROM) a
   revision redwood can name — the guard is only computable if the caller
   states the target.
3. **No coded dependency (P1/P2)**: redwood's skill and code must NOT
   require cppgraph. Optimistic call at step 8b: call once on first need;
   on failure (absent binary, timeout, non-zero exit) quarantine for the
   session — do not retry; skip silently and mark the step as skipped
   (their existing "skip silently" shape, now per-session instead of
   per-run).
4. **Adapt to the contract, not to our internals**: redwood must never
   read `graph.db`, the `.scip`, or any cppgraph store — the tool call is
   the only surface. When the final shape of `log_id_lookup` is pinned
   (v1 contract, golden-file tested like `masora facts`), redwood codes
   against THAT document.
5. **Budget discipline on their side**: ≤ 2 error_ids per investigation
   per their own contract; cppgraph enforces a hard cap server-side, but
   the caller-side discipline stays theirs.

## 3. Completeness of this list

The asks in §2 are EXHAUSTIVE: nothing else in cppgraph depends on any
redwood behavior. Every redwood feature not named in this document can be
changed, removed or rewritten without notice and without breaking cppgraph
(and §1 exists only so redwood knows what it is building against — it is
cppgraph's commitment, not a need).

## 4. Sequencing

- cppgraph ships the index-time extraction + the tool + the pinned v1
  contract (design first; the id-table costs one extra pass over scanned
  code and one store column/table).
- Redwood integrates in the same window: skill step 8b switches from
  "optional, skip silently" to "call optimistically, quarantine on
  failure", and their README's step 3 stops being aspirational the day
  the bridge is live.
- Until the v1 contract is pinned, redwood builds NOTHING against this
  document — it is a commitment of intent, not a shape to code against.
