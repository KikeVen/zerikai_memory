---
name: universal-brain-query
description: Use when querying universal-brain memory for any codebase question — architecture, functions, patterns, decisions, or prior session context. Trigger this skill whenever the user mentions universal-brain, asks about their codebase, wants to find a function, understand a module, trace a pipeline, or look up a past decision. Also trigger when the user opens a session with a project context and hasn't yet resolved a workspace — the skill's entry checks ensure the session is grounded before any query runs. Proactively invoke this rather than querying universal-brain cold.
---

# Universal-Brain Query Skill

This skill is **guidance**, not a script. You call the MCP tools directly — this tells you how to think about the sequence, the query shape, and what the responses mean. Exercise judgment on every call.

MCP tool name `universal-brain`

---

## On Entry — Three Checks Before Any Query

Before doing anything else, verify the session is ready. Do not skip these.

**1. MCP availability**
Attempt `list_workspaces()`. If the tool is not found or the call fails, stop and tell the user:
> "universal-brain MCP is not active — enable it in your MCP server settings and retry."
Do not proceed without it.

**2. Workspace resolution**
If the user named a workspace or project, pass it to `resolve_workspace(identifier)`. Echo the resolved path back in one line before running any queries:
> "Using workspace: gh_repo_traffic → d:/users/.../gh_repo_traffic"

If no workspace was named, call `list_workspaces()`, show the options, and ask the user which one to use. Never guess or assume a path.

**3. Workspace health**
Call `get_brief(workspace)` immediately after resolving. This reads a pre-synthesized architecture brief and often answers orientation questions without a vector search. Also call `scan_status(workspace)` — if no scan has run or a scan is in progress, tell the user before querying, since results may be incomplete or stale.

---

## Query Strategy — Natural Language, Not Keywords

This is the most important thing in this skill.

`query_memory` runs semantic vector search. It responds to *meaning*, not keyword density. Keyword dumps match on surface terms and return documentation noise — READMEs, skill files, schema comments. A natural question aimed at a named function or behavior pulls the actual implementation chunk.

**Don't do this:**

```
"views clones union intersection zero fill ingester"
```

**Do this:**

```
"How does sync_repository fetch and write daily traffic data?"
"What does is_snapshot_stale check for?"
"What are the main functions in the ingestion pipeline?"
```

Ask what a developer would ask a colleague who wrote the code.

---

## Drill-Down — Three Layers

Treat `universal-brain` like a knowledge graph. Navigate it in layers, using what each layer returns to form the next question. Never jump to Layer 3 without passing through Layer 1 and 2 — you will keyword-dump and miss.

**Layer 1 — Map:** Get entry points, pipeline stages, module names. Goal: function and file names to use in Layer 2.
> "What are the main functions in the ingestion pipeline?"
> "What modules make up the API layer?"

**Layer 2 — Locate:** Use names from Layer 1 to ask targeted questions about specific functions.
> "How does sync_repository write to the database?"
> "What does fetch_github_api return?"

**Layer 3 — Drill:** Ask about specific behavior, edge cases, or logic within a located function.
> "What happens when GitHub returns views but no clones for a date?"
> "How does the upsert handle a conflict on repo_id and date?"

Run multiple focused queries for complex topics — one query is rarely complete coverage.

---

## Reading the Response

Every result carries signal. Read it before citing anything.

| Signal | What it means |
| --- | --- |
| evidence ≥ 0.70, L2 ≤ 1.20 | Implementation chunk — cite directly with file:line |
| evidence 0.40–0.69 | Likely docs or README — use with caution, flag it |
| High relevance, evidence < 0.30 | Documentation gap in source — relay the file:line memory returned and send user to IDE. Nothing else to do. |
| evidence < 0.40, relevance low | Noise — requery with a better question |
| contradicts ≥ 0.50 | Memory conflicts with your premise — go to source |
| No result returned | Gap in index — report it explicitly |

**Memory silence is meaningful.** If a query returns nothing, say so. Silence is often the finding — the behavior may be undocumented, unscanned, or absent.

Always surface the file citation and line number: `ingester.py:110` (L2=0.96). Direct the user to their IDE for verbatim source — never reconstruct code from what memory returned.

---

## Audit vs. Query

`list_memory(workspace, category, limit)` lists raw indexed entries. Use it to audit what has been indexed — not to answer code questions. If `query_memory` results feel thin or wrong, `list_memory` tells you whether the relevant files were indexed at all.

If files are missing from the index, tell the user to run `scan_workspace(workspace)`. After significant documentation or docstring updates, use `scan_workspace(workspace, force_refresh_brief=True)` to rebuild both the index and the architecture brief.

---

## Saving Findings Back

If the session produced a durable finding — a confirmed bug, a design decision, an architectural note — save it before closing:

```
save_to_memory(
  content    = <finding>,
  workspace  = <resolved identifier>,
  category   = "decisions" | "architecture" | "research",
  source_id  = <file:line if applicable>
)
```

Save only what has value the next time someone asks about this codebase. Skip ephemeral session output.

---

## Troubleshooting

- **resolve_workspace fails:** Try `debug_workspace_id(path)` to check how the path is being normalized. Path casing or trailing slashes can cause ID mismatches.
- **Duplicate workspaces for the same project:** Use `merge_workspaces(source_id, target_id)` — irreversible, confirm with user first.
- **Results feel stale after code changes:** Ask the user to run `scan_workspace` with `force_refresh_brief=True`.
- **Queries keep returning SKILL.md or README:** The index is skewed toward documentation. Check `list_memory` to confirm implementation files were scanned. If not, rescan.
- **Cost awareness:** `get_token_usage(workspace)` and `get_cost_report(workspace)` are available if the user wants to understand query costs across sessions.
