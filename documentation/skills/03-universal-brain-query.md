# Universal-Brain Query Skill

**Location:** `universal-brain-query/SKILL.md`

Queries the project memory layer to answer architecture, implementation, and historical-decision questions using semantic retrieval instead of keyword guessing. This skill is used when you need to find a function, trace a pipeline, locate an edge case, or confirm how the codebase behaves in practice.

> **Do not query memory cold.** Resolve the workspace, read the brief, and confirm scan health before asking the semantic search for implementation answers.

---

## Prerequisites

Before invoking the skill, confirm:

1. The `universal-brain` MCP is active and available in the IDE.
2. The target workspace is resolved explicitly — do not assume the current folder is correct.
3. The workspace brief is available and the scan has been run recently enough to be trustworthy.
4. The user has not asked for a broad grep dump. This skill is for targeted semantic discovery.

---

## Entry Checks

The skill follows a strict startup sequence.

### 1. MCP availability

Attempt `list_workspaces()`. If the tool is unavailable or the call fails, stop and tell the user:

> "universal-brain MCP is not active — enable it in your MCP server settings and retry."

Do not continue with a query.

### 2. Workspace resolution

If the user named a workspace or project, pass it to `resolve_workspace(identifier)`. Echo the resolved path back once before running any queries:

> "Using workspace: gh_repo_traffic → d:/users/.../gh_repo_traffic"

If no workspace was named, call `list_workspaces()`, show the options, and ask the user which one to use. Never guess or assume a path.

### 3. Workspace health

Call `get_brief(workspace)` immediately after resolving. This reads the synthesized architecture brief and often answers orientation questions without a vector search.

Then call `scan_status(workspace)`. If no scan has run or a scan is in progress, tell the user before querying — stale or partial results can hide the real implementation path.

---

## Query Strategy

This skill is built around natural-language reasoning, not keyword packing.

`query_memory` is a semantic search over embedded code and documentation chunks. It responds to meaning, not to a dense pile of symbols.

**Avoid this style of query:**

```text
"views clones union intersection zero fill ingester"
```

**Prefer this style instead:**

```text
"How does sync_repository fetch and write daily traffic data?"
"What does is_snapshot_stale check for?"
"What are the main functions in the ingestion pipeline?"
```

Ask what a developer would ask a colleague who wrote the code. Good questions map to behavior, not raw token lists.

---

## Drill-Down Flow

Treat memory like a layered knowledge graph. Move through it in stages rather than jumping directly to a single answer.

### Layer 1 — Map

Goal: identify entry points, pipeline stages, or module names.

Example prompts:

```text
"What are the main functions in the ingestion pipeline?"
"What modules make up the API layer?"
```

### Layer 2 — Locate

Goal: find the function or file that owns a specific behavior.

Example prompts:

```text
"How does sync_repository write to the database?"
"What does fetch_github_api return?"
```

### Layer 3 — Drill

Goal: inspect edge cases, branching logic, or failure modes inside the targeted function.

Example prompts:

```text
"What happens when GitHub returns views but no clones for a date?"
"How does the upsert handle a conflict on repo_id and date?"
```

Run multiple focused queries for complex topics. One query usually misses part of the story.

---

## Reading the Response

Every query result carries signal. Read it before citing anything.

| Signal | Meaning |
| --- | --- |
| evidence ≥ 0.70, L2 ≤ 1.20 | Strong implementation chunk — cite it directly with file and line. |
| evidence 0.40–0.69 | Probably documentation or a partial summary — use with caution. |
| high relevance, evidence < 0.30 | Likely a source gap; relay the result and send the user to the IDE. |
| evidence < 0.40, low relevance | Noise — rewrite the question more precisely. |
| contradiction score ≥ 0.50 | The memory conflicts with the premise; go back to source. |
| no result returned | Missing index coverage or missing source material. |

Memory silence is meaningful. If a query returns no useful chunk, say so explicitly. The index may not contain the relevant files, the scan may be stale, or the code may simply not be documented well enough.

When citing results, surface the file and line number with the score. Example:

```text
ingester.py:110 (L2=0.96)
```

Direct the user to the IDE for the verbatim source. Do not reconstruct code from memory alone.

---

## Audit vs. Query

Use `list_memory(workspace, category, limit)` to inspect what was indexed. Use it as an audit step, not as the primary answer tool.

If a query feels thin or wrong, `list_memory` shows whether the relevant implementation files were indexed at all.

If missing files are the issue, tell the user to run:

```text
scan_workspace(workspace)
```

If there were significant documentation or docstring updates, rebuild the brief as well:

```text
scan_workspace(workspace, force_refresh_brief=True)
```

---

## Saving Durable Findings

If a session produces a durable finding — a confirmed bug, a design decision, or a meaningful architectural note — save it back to memory before closing.

```python
save_to_memory(
    content    = <finding>,
    workspace  = <resolved identifier>,
    category   = "decisions" | "architecture" | "research",
    source_id  = <file:line if applicable>
)
```

Only save findings that are likely to help the next person who asks the same question. Skip ephemeral debugging noise.

---

## Example Invocation Patterns

### Architectural question

```text
"What are the main functions in the ingestion pipeline and how do they connect?"
```

### Specific behavior

```text
"How does the updater decide whether a repository record is stale?"
```

### Historical decision lookup

```text
"Why was the sync process designed to batch repository traffic by day instead of per request?"
```

### File-level grounding

```text
"Trace the code path from repo fetch to database upsert for the GitHub traffic job."
```

---

## Troubleshooting

- **Workspace resolution fails:** Try `debug_workspace_id(path)` to check path normalization, case sensitivity, or trailing slash mismatches.
- **Duplicate workspaces are detected:** Use `merge_workspaces(source_id, target_id)` only after confirming the merge with the user.
- **Results feel stale after code changes:** Ask the user to rerun `scan_workspace` with `force_refresh_brief=True`.
- **Queries keep returning `SKILL.md` or README material:** The index is skewed toward documentation. Check `list_memory` and rescan implementation files if needed.
- **Cost questions:** Use `get_token_usage(workspace)` and `get_cost_report(workspace)` when the user wants a breakdown of semantic-query costs.

---

## Output Standard

The assistant should treat the returned memory as evidence, not as the final truth. The answer should be data-backed, cite real implementation references, and direct the user to the exact source file when a technical claim needs verification.

This skill is most effective when used in a workflow like:

1. resolve workspace,
2. read brief,
3. check scan health,
4. ask a natural-language semantic question,
5. locate the implementation file,
6. verify the answer against source code.

See [01-overview.md](01-overview.md) for the broader skill system and [02-embedding-docstring.md](02-embedding-docstring.md) for the other major skill in this documentation set.
