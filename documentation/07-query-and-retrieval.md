# Query and Retrieval

## How `query_memory` Works

Every query runs through a 4-stage pipeline:

```
1. Receive query
        │
        ▼
2. Vector search (ChromaDB)  ──►  top-N results by L2 distance
        │
        ▼
3. Lexical re-rank (optional)  ──►  boost on keyword hits in entity name + docstring
        │                            pure reorder — nothing is dropped
        ▼
4. LLM synthesis  ──►  Ollama (free) or DeepSeek (cloud, auto-routed)
        │
        ▼
   Answer + inline #file:line | score citations
```

---

## Stage 2 — Vector Search

ChromaDB performs an L2 (Euclidean) distance search over all indexed entities for
the workspace. Returns the top-N candidates ranked by distance — lower distance
means higher similarity to the query.

`QUERY_DISTANCE_THRESHOLD` (default `1.5`) filters out candidates beyond the
configured cutoff. Lower values are stricter. If you see answers citing loosely
related code, lower the threshold. If relevant entities are being missed, raise it.

Calibrate by watching `.brain/server.log` — the log records the distance of every
retrieved entity.

---

## Stage 3 — Lexical Rerank

When `ENABLE_LEXICAL_RERANK=true`, a second pass boosts candidates with keyword
overlap between the query terms and the entity name + docstring text. This is a
pure reorder — no candidates are dropped, and the fetch cap remains the same.

`LEXICAL_RERANK_WEIGHT` (default `0.05`) controls the per-keyword boost. Keep it
below `0.156` to avoid the lexical signal overriding the semantic L2 ranking. At
`0.05`, keyword overlap provides a meaningful tie-break without distorting results
for queries that have no exact keyword matches.

---

## Stage 4 — LLM Synthesis

The top results after retrieval and reranking are passed to the synthesis layer.
The project brief is prepended as a fixed context prefix. The LLM generates a
synthesized answer citing the retrieved entities.

Auto-routing selects the engine based on query length and keywords. See
[llm-backends.md](llm-backends.md) for the full routing table.

---

## Source Citations

Every `query_memory` response includes inline `#file:line | score L2 or rerank`
citations in plain text format — cross-IDE compatible, clickable in VS Code
Copilot. This metadata is stored at scan time and carries no extra API cost.

The distance score in parentheses indicates how closely the retrieved entity matched
the query. Lower distance = stronger match. Use these scores to calibrate
`QUERY_DISTANCE_THRESHOLD` — if cited entities have high distances and are only
loosely related, tighten the threshold.

---

## Why Retrieval Quality Depends on Docstring Quality

The vector search embeds the query and compares it to the stored embeddings of every
indexed entity. Those entity embeddings were generated from the docstring text at scan
time.

A query for `"JWT token validation"` can only surface a function if that function's
docstring contains the words `"JWT"` and `"validation"` (or semantically close terms).
If the docstring says `"Checks the user's credentials"`, the semantic distance is
larger and the entity may fall below the threshold.

This is why the `embedding-docstring` skill and `.memignore` are prerequisites before
the first scan. The pipeline cannot compensate for missing or vague source material.
See [skills/02-embedding-docstring.md](skills/02-embedding-docstring.md).

---

## Saving Manual Context

For decisions, conventions, or facts that are not captured in code, use
`save_to_memory` directly:

```
"Remember that we're using Redis for session caching, not PostgreSQL"
"Save that the auth service requires a JWT with RS256 signing"
```

Use optional category tags to organize:
```
"Save to memory under 'architecture': the API gateway handles rate limiting at the edge"
```

List what's stored:
```
"List what's in memory for this project"
"List memories tagged 'architecture'"
```

---

## Troubleshooting Retrieval

**Relevant entities not surfacing:**
- `QUERY_DISTANCE_THRESHOLD` may be too strict — try raising it slightly.
- The entity's docstring may be vague or missing technology names. Run the
  `embedding-docstring` skill on that file, then rescan.

**Loosely related entities appearing in answers:**
- `QUERY_DISTANCE_THRESHOLD` may be too permissive — try lowering it.
- Check the distance scores in citations — consistently high scores indicate
  the indexed content is thin.

**Stale entities in answers (deleted files, renamed functions):**
- Run `scan_workspace` again — it is self-cleaning and will purge stale records.

**Answers citing wrong workspace:**
- Run `list_workspaces` to check for duplicate UUIDs. Merge with `merge_workspaces`.
