# Memory and Scanning

## How `scan_workspace` Works

`scan_workspace` returns immediately and runs in the background. Poll `scan_status`
to track progress. Four concurrent workers process files in parallel.

**Background pipeline:**

1. Directory walk respecting `.memignore`. Every eligible file (`.py`, `.js`, `.ts`,
   `.html`) is picked up unless excluded.
2. Code files parsed by tree-sitter via 4 concurrent workers. Each file yields a list
   of `CodeEntity` objects (functions, methods, classes, constants, HTML boundary
   elements).
3. Entities batch-upserted to ChromaDB — up to 300 per call. Entity IDs are
   deterministic MD5 hashes of the file path + entity name. Re-scanning overwrites,
   never duplicates. Stale entities (from deleted or renamed files) are purged
   automatically.
4. Brief synthesis across 9 sections fires in parallel via `asyncio.gather`. Each
   section queries ChromaDB for up to 75 relevant nodes, then generates content via
   DeepSeek or Ollama.
5. Brief saved to `.brain/contexts/<workspace_id>.md` and locked to protect the
   DeepSeek KV cache prefix.

---

## `.memignore` Is the Sole Exclusion Source

Both `scan_workspace` and the `embedding-docstring` skill use `.memignore` as the
only source of truth for what to exclude. Neither tool falls back to any IDE default
exclusion list or hardcoded directory names.

**`scan_workspace` without `.memignore`** indexes `venv/`, `node_modules/`,
`__pycache__/`, test fixtures, migration history, build artifacts — anything present
in the workspace directory tree. The resulting ChromaDB collection contains noise that
degrades every query answer.

**Configure `.memignore` before the first scan.** There is no incremental fix for a
contaminated index short of wiping and re-indexing.

Recommended baseline — see [configuration-reference.md](configuration-reference.md).

---

## Workspace Identity

Each project is identified by a stable UUID derived deterministically from its
absolute filesystem path. The IDE attaches the active workspace path to every MCP
message — you never pass UUIDs or paths during queries.

The mapping is stored in `.brain/zerikai.db` (SQLite). Each workspace gets its own
ChromaDB sub-collection. Queries never cross workspace boundaries.

**Duplicate workspace IDs** occur when the same project is opened from different
paths (absolute vs. relative, symlinked directories). `list_workspaces` will show
duplicates. Resolve with:

```
"Merge workspaces <source-uuid> into <target-uuid>"
```

The agent calls `merge_workspaces`. **Irreversible** — the source workspace is
deleted after merge.

---

## Bare Files

Files where tree-sitter finds zero parseable entities are called **bare files**. By
default they are sent to the **Analysis Engine** (DeepSeek or Ollama depending on
mode) for LLM-based extraction (~$0.000167/file via Cloud).

To skip bare files instead of paying for LLM extraction:

```env
SKIP_BARE_FILES=['.py', '.html', '.md', '.css']
```

This skips those extensions when tree-sitter returns zero entities — it does not
skip all files of those types, only the ones tree-sitter cannot parse.

---

## The Project Brief

The brief is auto-generated on the first scan and locked until explicitly refreshed.
It is the fixed prefix on every DeepSeek API call and the foundation of every
`query_memory` answer.

Target size: **1,000–1,200 tokens.** Larger briefs destabilize the KV cache prefix
and inflate every API call. Smaller briefs lose architectural context.

The brief is synthesized across 9 parallel sections. Each section queries ChromaDB
for up to 75 relevant nodes using section-specific search terms, then generates
content via DeepSeek (cloud) or Ollama (local/hybrid). Generation time: ~20–30
seconds with cloud mode.

**Do not force-refresh the brief during normal development.** Force-refreshing resets
the DeepSeek KV cache. Every query pays full miss-rate pricing (~$0.14/M tokens)
until the cache warms again on subsequent calls. Only force a refresh after a major
architectural change:

```
"Rescan the workspace and force a refresh of the project brief."
```

---

## Wiping a Workspace — `drop_memory.py`

Use when `.memignore` was not configured before the first scan and the wrong
directories were indexed. Deletes the ChromaDB collection, the brief at
`.brain/contexts/<id>.md`, and the workspace registry entry.

```bash
# Windows
.\venv\Scripts\python.exe drop_memory.py "Workspace Name"
.\venv\Scripts\python.exe drop_memory.py workspace-uuid

# macOS / Linux
venv/bin/python drop_memory.py "Workspace Name"
venv/bin/python drop_memory.py workspace-uuid
```

After wiping: fix `.memignore` → run `embedding-docstring` skill → `init_workspace`
→ `scan_workspace`.

---

## Stale Memory

`scan_workspace` is self-cleaning. Running it again automatically purges stale
records for deleted or renamed files. No manual cleanup needed.

If the AI recalls deleted files or ignored directories:

1. Run `scan_workspace` again — stale records will be purged.
2. If wrong files were indexed from the start (missing `.memignore`), use
   `drop_memory.py` and re-index with a corrected exclusion list.
