# masora_integration.md — handoff to Masora (written from the cppgraph side)

This document states what **cppgraph expects MASORA to do** so the
integration can move from a hard coupling to a contract. It mirrors the
governance of Masora's `docs/CPPGRAPH_INTEGRATION.md`: each repo owns its
side; nobody edits the neighbor's tree; surface changes land as versioned
contracts, never in place. Masora is the consumer here and adapts to
cppgraph's query contract — never the reverse.

> Note: the `scip-clang cxx …` symbol examples in this document and in
> Masora's `docs/CPPGRAPH_INTEGRATION.md` are masora's ANCHOR format —
> not the form cppgraph's `scip`-bearing surfaces emit (the plain,
> scheme-prefixed `cxx …` identity the store carries; see §6).

## 1. What masora must STOP doing

Read cppgraph's SQLite store (`<repo>/.cppgraph/*.graph.db`) directly.
Today `masora/providers.py` re-implements queries over the internal schema
(`files`/`symbols`/`edges`/`meta`, gated on `schema_version` +
`symbol_format`). That schema is NOT a public contract: it changes with any
store migration (it is at v6, it will move), and every bump silently
degrades masora's code anchors to `unknown` until masora re-reads our
internals and re-gates. The gates make the coupling fail-safe, not clean.

## 2. What masora must USE instead — the fingerprint surface

cppgraph will expose the exact data masora's provider computes today, as a
spawned-command contract (same shape as `masora facts`, reversed roles):

```
cppgraph fingerprints --repo <path> [--symbol <scip-string> ...]
```

One JSON document on stdout:

```json
{
  "contract_version": 1,
  "repo_root": "/path/to/checkout",
  "graph_source_commit": "0e5f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f",
  "repo_head": "1a1a8e1f4e5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c",
  "graph_behind_head": false,
  "symbol_format": 1,
  "symbols": [
    {
      "symbol": "scip-clang cxx . . mongo/ResumeTokenData#makeResumeToken().",
      "found": true,
      "definition_fingerprint": "sha256-of-normalized-definition-range",
      "edge_snapshot": {
        "edges": "edge-set-hash",
        "neighbours": {
          "scip-clang cxx . . mongo/Engine#commitShard().": "edge-set-hash"
        }
      }
    }
  ]
}
```

Field semantics pinned for v1:

- `definition_fingerprint` — sha256 of the normalized definition-range
  source (`symbols.line`..`symbols.end_line` inclusive, 0-indexed; a store
  without body extents hashes the definition line alone) — byte-for-byte
  what `providers.py:definition_fingerprint` computes today.
- `edge_snapshot` — the symbol's edge-set hash plus each 1-hop `calls`
  neighbour's hash, self excluded — what `edge_snapshot_of` computes.
- `found: false` — the identity matched nothing (`not_found`, §6.2 of
  Masora's FORMAT); no fingerprint is guessed.
- `graph_behind_head` — whether the store's indexed commit is an ancestor
  of, or equal to, the checkout HEAD (masora's §5.2 behind-HEAD policy);
  masora keeps the refusal decision, cppgraph supplies the fact.
- `symbol_format` — the store meta row, so masora's format gate rides the
  contract instead of reading meta directly.
- Matching is verbatim on exact SCIP identity strings, repeatable
  `--symbol` for batch. No fuzzy resolution in v1.

## 3. What cppgraph supplies (its side of the deal)

- The command, fail-closed: non-zero exit or timeout ⇒ masora treats it as
  provider-unavailable (`unknown`), never a guessed fingerprint. Budget:
  2 s wall-clock, same discipline as `masora facts`.
- A versioned contract (this §2 shape): changes land as a new
  `contract_version`, never in place; masora rejects unknown major versions
  like it already does for its own facts reader.
- Golden-file tests pinning the §2 shape, so a cppgraph release cannot
  drift the surface silently.
- `symbol_format` governance unchanged (masora's existing gate keeps its
  meaning, carried by the field above).

## 4. Completeness of this list

The asks in §2 and §4 are EXHAUSTIVE: nothing else in cppgraph depends on
any masora behavior. Every masora feature not named in this document can be
changed, removed or rewritten without notice and without breaking cppgraph.
Sections §1 and §3 exist only as context (what problem this solves, what
cppgraph commits on its side) — they are not needs.

## 5. Migration masora must perform

1. Adopt the spawn behind masora's existing `CppgraphRegistry` interface
   (the callables `fingerprints`/`edges` map 1:1 to the §2 fields) — the
   rest of masora does not change.
2. Drop the direct SQLite reads and the `schema_version`/`symbol_format`
   SQLite gates once the spawn is in place (the contract carries
   `symbol_format`; schema versioning stops being masora's business at
   all — that is the point).
3. Keep the behind-HEAD refusal: masora still decides policy, from
   `graph_behind_head` / `graph_source_commit`.
4. Delete the store-schema coupling gates (`schema_version` parsing) in the
   same release that flips the default to the spawn.

Timeline is masora's to pick; until the flip, cppgraph changes nothing on
its side and the current gates keep the direct reads fail-safe.

## 6. Handoff — masora's exact-identity predicate vs the store's SCIP form

Found during the redwood E2E of the three-tool loop (2026-10): masora's
`_is_exact_identity` (`masora/cppgraph.py`) treats a ref as an exact identity
only when it starts with `scip` — i.e. the `scip-clang cxx …` ANCHOR form.
The identity cppgraph's store (and therefore every cppgraph surface —
`find`'s `unique_symbol`, `log-id-lookup`'s `scip`) carries is the plain,
scheme-prefixed SCIP string (`cxx …`): the `scip-clang ` prefix names the
INDEXER, is never store data, and `cppgraph fingerprints` matches verbatim —
a `scip-clang`-prefixed query answers `found: false` (pinned test:
`tests/test_redwood.py::test_masora_anchor_is_a_prefix_derivation_of_the_scip_field`).

So masora-side anchor resolution from a cppgraph-supplied identity only works
today by coincidence (the bare `cxx …` form falls through to find-fragment
resolution). **Masora's owner should change `_is_exact_identity` to accept
the bare scheme-prefixed SCIP form** (e.g. also `ref.startswith("cxx ")` —
or, cleaner, match on "contains a space and a known SCIP scheme token"),
so `cxx …` identities resolve exactly without the fragment hop. cppgraph
does NOT emit the `scip-clang`-prefixed form (that would fabricate a prefix
the store does not carry and would break fingerprints' verbatim round-trip);
the masora ANCHOR form remains derivable as `"scip-clang " + <scip>` —
redwood_integration.md §1 pins this derivation for redwood.
