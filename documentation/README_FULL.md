<p align="center">
  <img width="908" height="510" src="zm_logo_70.png">
</p>

<p align="center">
  <strong>Never lose your AI context again.</strong><br/>
  Persistent, workspace-isolated memory for every IDE — local-first, cost-aware, instant.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/ChromaDB-Local_Vector_Store-FF6B35?style=for-the-badge" alt="ChromaDB">
  <img src="https://img.shields.io/badge/Ollama-Free_Local_LLM-000000?style=for-the-badge" alt="Ollama">
  <img src="https://img.shields.io/badge/DeepSeek-Cloud_Synthesis-1A73E8?style=for-the-badge" alt="DeepSeek">
  <img src="https://img.shields.io/badge/MCP-Model_Context_Protocol-6C3483?style=for-the-badge" alt="MCP">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="MIT License">
</p>

---

## Quick Setup

**One-time per IDE:**

1. [Clone & configure `.env`](#step-1--clone--install)
2. [Register your IDE](#step-3--register-your-ide)
3. [Configure Agent Rules](#step-3--register-your-ide)
4. [Verify installation](#step-4--verify)
5. [Register Embedding-Docstring Skill in your IDE](#step-3--register-your-ide)

**Per project (and after any major refactor):**

1. [Configure `.memignore`](#1--configure-memignore)
2. [Run Embedding-Docstring Skill](#2--run-the-embedding-docstring-skill)
3. [Register the project](#3--register-the-project) — `init_workspace`
4. [Scan & index](#4--scan--index) — `scan_workspace`



---

## Table of Contents

1. [Quick Setup](#quick-setup)
2. [The Problem & Solution](#the-problem--solution)
3. [How It Works](#how-it-works)
4. [Cost Model at a Glance](#cost-model-at-a-glance)
5. [Quick Start — 5 Steps](#quick-start--5-steps)
6. [Workspace Setup (Per Project)](#workspace-setup-per-project)
7. [Project Brief Structure](#project-brief-structure)
8. [Embedding-Docstring Skill](#embedding-docstring-skill)
9. [Day-to-Day Usage](#day-to-day-usage)
10. [Configuration Reference](#configuration-reference)
11. [MCP Tools Reference](#mcp-tools-reference)
12. [Auto-Routing Reference](#auto-routing-reference)
13. [Monitoring & Logs](#monitoring--logs)
14. [Auxiliary Scripts](#auxiliary-scripts)
15. [Security & Data Privacy](#security--data-privacy)
16. [Troubleshooting](#troubleshooting)
17. [Changelog](#changelog)
18. [Notice & License](#notice--license)

---

## The Problem & Solution

**Every new chat session starts cold.** You re-explain your stack. You re-paste your architecture. You repeat your decisions. Tokens burned. Time wasted.

**The core pain points:**

- AI assistants forget every decision the moment a chat ends.
- Re-pasting context inflates token costs on every query.
- Switching IDEs (VS Code → Cursor) means starting from scratch again.
- Raw file dumps into the chat window bloat context without precision.

**How Zerikai Memory fixes it:**

```
Your Codebase  ──►  tree-sitter (local parse)  ──►  ChromaDB (vector store)
                                                           │
Your Question  ──►  MCP Server  ──►  semantic search ──►  │
                         │                                 ▼
                         └──────────────────────►  Ollama / DeepSeek
                                                    (auto-routed synthesis)
                                                           │
                                                           ▼
                                                  Answer + #file:line citations
```

Zerikai Memory indexes your codebase once. Every subsequent question retrieves only the relevant context — no file dumps, no re-explanation, no duplicated tokens. Memory is shared across all your IDEs from a single `.brain/` directory.

---

## How It Works

The server runs locally as a **STDIO MCP process**, launched once when your IDE starts. You never call tools directly — your AI assistant calls them based on natural language.

### The 4-Stage Query Pipeline

```
1. Receive query
        │
        ▼
2. Vector search (ChromaDB)  ──►  top-N results by L2 distance
        │
        ▼
3. Lexical re-rank (optional)  ──►  boost on keyword hits in entity name + docstring
        │                            pure reorder, nothing dropped
        ▼
4. LLM synthesis  ──►  Ollama (free) or DeepSeek (cloud, auto-routed)
        │
        ▼
   Answer + inline #file:line (distance) citations
```

**Workspace identity is automatic.** Your IDE attaches the active workspace path to every message. The server maps it to a persistent UUID via a SQLite Workspace Registry — you never pass file paths during queries.

---

## Cost Model at a Glance

**The real cost isn't DeepSeek.** It's what you pay your IDE's AI every time you re-explain your codebase.

Every raw file dump, every re-pasted architecture doc, every repeated decision burns two things simultaneously: your monthly quota and your available context window. AI coding tools have moved to usage-based billing across the board — Copilot, Codex, Claude Code — and agentic workflows consume roughly 4–15× the tokens of a single chat. The context window fills up with re-explanation before real work begins. That's the Context Tax.

Zerikai Memory eliminates it on both sides:

| What gets taxed | Without Zerikai | With Zerikai |
|---|---|---|
| **Monthly quota** | Re-explaining stack, decisions, and conventions every session | Indexed once. Retrieved as compact snippets per query. |
| **Context window** | Raw file dumps shrink the window available for actual work | 1,000–1,200 token brief prefix. Window stays open for code. |
| **IDE switching** | Full re-explanation in every new tool | Shared `.brain/` directory. All IDEs draw from the same memory. |

**Zerikai's own running costs (cloud mode):**

| Operation | Engine | Estimated Cost |
|---|---|---|
| File scan (tree-sitter parseable) | Local only | **$0.00** |
| File scan (bare/non-parseable) | DeepSeek v4-flash | ~$0.000167 / file |
| Brief synthesis (9 sections) | DeepSeek v4-flash | ~$0.003 / full run |
| Routine query | Ollama (hybrid) or DeepSeek | $0 or ~$0.0028/M cached |
| Architectural query | DeepSeek v4-flash | ~$0.0028/M cached tokens |
| Repeated queries (cache hit) | DeepSeek KV cache | **50× cheaper** vs. miss |

The project brief is a fixed prefix on every API call. After the first query, DeepSeek caches it at **$0.0028/M tokens** (hit) vs. **$0.14/M** (miss). Keep `force_refresh_brief=False` during daily development to protect this prefix.

---

## Quick Start — 5 Steps

### Step 1 — Clone & Install

```bash
git clone https://github.com/your-username/zerikai_memory.git
cd zerikai_memory

# Create and activate a virtual environment (Python 3.11+)
python -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### Step 2 — Configure `.env`

Create a `.env` file in the project root:

```env
DEEPSEEK_API_KEY=your_deepseek_key_here    # Required. Get one at platform.deepseek.com

MEMORY_MODE=cloud                          # Start here. No Ollama needed.
ENABLE_TOKEN_TRACKING=true
ENABLE_DEEPSEEK_PRO=false                  # Keep false — v4-flash handles 99% of queries
QUERY_DISTANCE_THRESHOLD=1.0              # Lower = stricter matches
ENABLE_LEXICAL_RERANK=true                # Recommended: on
LEXICAL_RERANK_WEIGHT=0.05
SKIP_BARE_FILES=['.py', '.html', '.md', '.css']
```

> **Upgrading from an older version?** Replace `SKIP_BARE_PY_FILES=true` with `SKIP_BARE_FILES=['.py', '.html', '.md', '.css']`. The boolean toggle is no longer read.

See [Configuration Reference](#configuration-reference) for all options.

### Step 3 — Register Your IDE

The server starts **once** when the IDE loads and stays running. All paths must be **absolute** — relative paths cause startup failures.

**VS Code (Copilot / Cline)**

1. Press `Ctrl+Shift+P` → **MCP: Add Local Server**
2. Choose **STDIO**
3. Set command: `C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe C:\\path\\to\\zerikai_memory\\main.py`

> macOS/Linux use forward slashes: `/path/to/zerikai_memory/venv/bin/python`

<details>
<summary>Google Antigravity — click to expand</summary>

Edit `mcp_config.json` directly:

```json
"universal-brain": {
  "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\zerikai_memory\\main.py"],
  "disabled": false
}
```
</details>

<details>
<summary>Cursor — click to expand</summary>

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "universal-brain": {
      "command": "/path/to/zerikai_memory/venv/bin/python",
      "args": ["/path/to/zerikai_memory/main.py"]
    }
  }
}
```
</details>

<details>
<summary>Claude Desktop — click to expand</summary>

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "universal-brain": {
      "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\zerikai_memory\\main.py"]
    }
  }
}
```
</details>

**Then immediately configure Agent Rules — required before any scan:**

> Without agent rules your IDE's AI ignores `universal-brain` entirely and falls back to raw file searches. Full guide: [`agent_rules/ide_agent_rules.md`](agent_rules/ide_agent_rules.md)

| Rule | What It Enforces |
|---|---|
| **Universal-Brain First** | Agent queries `universal-brain` before any raw file search. Must state if it escalated beyond memory. |
| **Source Discipline** | Every answer surfaces `file.py:line` + confidence score. No fabricated answers — if memory has nothing, it says so. |

### Step 4 — Verify

```bash
python -c "from main import scan_workspace, query_memory; print('OK')"
```

You should see the startup banner followed by `OK`.

### Step 5 — Set Up Each Project

See [Workspace Setup (Per Project)](#workspace-setup-per-project).

---

## Workspace Setup (Per Project)

Each project requires a one-time setup before the first scan.

1. [Configure `.memignore`](#1--configure-memignore) — controls what gets indexed
2. [Run Embedding-Docstring Skill](#3--run-the-embedding-docstring-skill) — enriches docstrings before indexing; repeat after any major refactor
3. [Register the project](#4--register-the-project) — `init_workspace`
4. [Scan & index](#5--scan--index) — `scan_workspace`

---

### 1 — Configure `.memignore`

Works like `.gitignore` — one pattern per line. `scan_workspace` reads this file and skips matching paths. Forgetting to configure it before the first scan is the most common reason to use `drop_memory.py` and start fresh.

<details>
<summary>Sample .memignore — click to expand</summary>

```gitignore
# Directories
.git/
node_modules/
venv/
__pycache__/
.brain/
dist/
build/

# Patterns
**/tests/
.env
*.log
*.lock
*.pyc
```
</details>

### 2 — Run the Embedding-Docstring Skill

tree-sitter extracts code entities and their docstrings. If those docstrings are vague, missing technology names, or omit routing logic, the index is silently degraded and every query answer that follows is weaker.

Run this before the first scan, and again after any significant refactor.

Tell your assistant:

```
"Audit and optimise docstrings across this project using the embedding-docstring skill,
respecting .memignore."
```

The skill reads each file, flags violations with line numbers, and proposes before/after diffs for your approval. Nothing is written without confirmation.

See [Embedding-Docstring Skill](#embedding-docstring-skill) for full details on what it checks and enforces.

### 3 — Register the Project

```
"Set up memory for this project"
```

The assistant calls `init_workspace`. It registers the folder, assigns a UUID, and creates a pending brief at `.brain/contexts/<workspace_id>.md`. Idempotent — safe to run multiple times.

### 4 — Scan & Index

```
"Scan and index the workspace."
```

`scan_workspace` returns immediately and runs in background. The assistant polls `scan_status` to track progress.

**What happens in background:**
1. Directory walk respecting `.memignore`. Code files parsed by tree-sitter via 4 concurrent workers.
2. Entities batch-upserted to ChromaDB (up to 300 per call).
3. Brief synthesis across 9 sections — queries memory for up to 75 relevant nodes per section.
4. Brief saved to `.brain/contexts/<workspace_id>.md` and locked to protect your DeepSeek KV cache prefix.

### 5 — Force Brief Refresh (When Needed)

Normal scans do **not** regenerate the brief (to preserve KV cache). Only force a refresh after a major architectural pivot:

```
"Rescan the workspace and force a refresh of the project brief."
```

---

## Project Brief Structure

The project brief is the foundation of everything. It's what Zerikai synthesises from your codebase and uses as the fixed prefix on every API call. Without a well-formed brief, every query starts from a weaker position and your DeepSeek KV cache savings disappear.

Each workspace gets one auto-generated brief, optimised for DeepSeek KV caching. Target: **1,000–1,200 tokens** — the sweet spot between cache stability and retrieval accuracy. It is generated on first scan and locked until you explicitly force a refresh.

| # | Section | What It Captures |
|---|---|---|
| 1 | **Overview** | Type, purpose, and domain of the project |
| 2 | **Technical Stack** | Backend, database, API integrations, key libraries |
| 3 | **Core Architecture** | Frontend, backend, data/processing layers |
| 4 | **Primary Conventions** | Code style, error handling, schema rules |
| 5 | **Purpose** | Business problem and core objectives |
| 6 | **Key Files & Directories** | Entry points, routers, their specific roles |
| 7 | **Development & Testing** | Setup, run, test, deployment instructions |
| 8 | **Data Flow & Request Lifecycle** | Trace from entry point to data layer |
| 9 | **Future Roadmap** | Planned features and TODOs extracted from code |

**Why it must be treated carefully:** The brief is the stable prefix that DeepSeek caches across all queries. Force-refreshing it resets that cache, and every query pays full miss-rate pricing until the cache warms again. Treat it like a schema migration — only touch it when architecture changes justify the cost.

---

## Embedding-Docstring Skill

tree-sitter extracts code entities and their docstrings. The brief synthesis queries those entities to build the 9 sections above. If those docstrings are vague, use generic terms instead of technology names, or omit routing logic — the brief comes out thin, and every query answer that follows is degraded.

The Embedding-Docstring Skill (`skill/embedding-docstring/SKILL.md`) audits and rewrites docstrings across your codebase to be embedding-optimised **before** you run the first scan. It respects `.memignore` and only touches files that will be indexed.

### What it enforces

| Requirement | Why It Matters |
|---|---|
| **Technology names** | `"Uses Redis"` not `"key-value store"` — embeddings match words, not concepts |
| **Routing / branches** | Decision logic must be documented, not inferred |
| **Guarantees** | Idempotency, atomicity, ordering stated explicitly |
| **Side effects** | What the function writes, calls, or mutates beyond its return value |
| **Size limit** | Prose body capped at 4 lines or 400 characters above `Args:`/`Returns:` |

### How to run it

```
"Audit and optimise docstrings across this project using the embedding-docstring skill,
respecting .memignore."
```

For a single file or function:

```
"Audit docstrings in api_handler.py using the embedding-docstring skill"
"Optimise the docstring for authenticate_user for vector search"
```

The skill reads each file, flags violations with line numbers, and proposes before/after diffs for your approval. Nothing is written without confirmation.

> **Run this before `scan_workspace`.** Docstrings fixed after indexing require a rescan to take effect.

---

## Day-to-Day Usage

### Natural Language Command Reference

| You say | What the agent calls | Engine |
|---|---|---|
| *"Remember that we're using Redis for session caching"* | `save_to_memory` | Local |
| *"What did we decide about auth?"* | `query_memory` | Ollama (auto-routed) |
| *"Refactor the data layer, what are our constraints?"* | `query_memory` | DeepSeek (auto-escalated) |
| *"List what's in memory for this project"* | `list_memory` | Local |
| *"What projects do you know about?"* | `list_workspaces` | Local |
| *"Show me the project brief."* | `get_brief` | Local |
| *"Scan the workspace."* | `scan_workspace` | Background |

### Source Citations

Every `query_memory` response includes inline `#file:line (distance)` citations — plain text, cross-IDE compatible, clickable in VS Code Copilot. Zero extra API cost: this metadata is stored at scan time.

---

## Configuration Reference

Full `.env` options:

| Key | Default | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(required)* | API key from [platform.deepseek.com](https://platform.deepseek.com) |
| `MEMORY_MODE` | `cloud` | `cloud` / `hybrid` / `local` — see mode table below |
| `ENABLE_TOKEN_TRACKING` | `true` | Tracks usage in `.brain/zerikai.db` |
| `ENABLE_DEEPSEEK_PRO` | `false` | Enables v4-pro for architectural queries (6× more expensive post-May 31 2026) |
| `QUERY_DISTANCE_THRESHOLD` | `1.0` | L2 distance cutoff — lower = stricter. Watch `server.log` to calibrate. |
| `ENABLE_LEXICAL_RERANK` | `false` | Hybrid rerank: boosts results with keyword overlap in entity name + docstring |
| `LEXICAL_RERANK_WEIGHT` | `0.05` | Per-keyword boost weight. Keep below `0.156` to avoid overriding semantic results. |
| `SKIP_BARE_FILES` | `[]` | Extensions to skip when tree-sitter finds zero entities. E.g. `['.py', '.html', '.md', '.css']` |

### Memory Mode Comparison

| Mode | Scan Engine | Query Engine | Cost | Best For |
|---|---|---|---|---|
| `cloud` | DeepSeek | DeepSeek | Low | **Recommended.** No Ollama, best brief quality. |
| `hybrid` | Ollama | Ollama + DeepSeek (auto) | Lowest | Privacy-sensitive; free local lookups, cloud for architecture |
| `local` | Ollama | Ollama | $0 | Full privacy, lower brief quality |

---

## MCP Tools Reference

You never call these directly — your AI assistant calls them from your natural language instructions.

### Workspace Management

| Tool | Description |
|---|---|
| `init_workspace` | Registers a project folder, assigns UUID, creates pending brief. Idempotent. |
| `list_workspaces` | Lists all known workspaces with a brief or stored memories. |
| `resolve_workspace` | Resolves UUID, short-UUID, or display name to filesystem path. |
| `merge_workspaces` | Consolidates duplicate workspace IDs into one. **Irreversible.** |
| `debug_workspace_id` | Diagnostic: shows what UUID a given path would generate. |

### Memory & Briefs

| Tool | Description |
|---|---|
| `scan_workspace` | Background scan. Returns immediately; poll `scan_status` to track. Idempotent, self-cleaning, 4 concurrent workers. |
| `scan_status` | Progress of a running/completed scan: files, entities, errors, elapsed, brief status. |
| `save_to_memory` | Manually saves a decision, fact, or note with optional category tag. |
| `list_memory` | Lists stored memories, optionally filtered by category. |
| `query_memory` | Vector search + LLM synthesis (auto-routed). Returns inline `#file:line (distance)` citations. |
| `get_brief` | Retrieves the current project brief from `.brain/contexts/`. |
| `update_brief` | Manually updates the markdown content of a project brief. |

### Usage & Diagnostics

| Tool | Description |
|---|---|
| `get_token_usage` | DeepSeek token usage and cost statistics. |
| `get_cost_report` | Cost breakdown by operation type. |
| `get_cache_stats` | Cache hit/miss rates by operation type. |
| `purge_usage_data` | Deletes historical token tracking records. **Irreversible.** |

---

## Auto-Routing Reference

Routing is automatic based on query characteristics. Override it explicitly with `use_cloud=True/False`.

| Condition | Engine | Cost |
|---|---|---|
| Short, specific query | Ollama | Free |
| Query ≥ 40 words | DeepSeek v4-flash | ~$0.0028/M cached tokens |
| Contains `refactor`, `architect`, `design`, `audit`… | DeepSeek v4-pro | ~$0.003625/M cached tokens |
| `use_cloud=True` override | DeepSeek | Varies |
| `use_cloud=False` override | Ollama | Free |

---

## Monitoring & Logs

All activity is written to **`.brain/server.log`** — 5 MB rotating cap, 2 rolling backups.

| Event | Log Level |
|---|---|
| Server startup (DB path, model, mode) | `INFO` |
| Memory saved (workspace, category, preview) | `INFO` |
| Auto-route decision | `INFO` |
| DeepSeek cache hit / miss stats | `INFO` |
| `scan_workspace`: file saved or skipped | `INFO` / `DEBUG` |
| Any tool failure | `ERROR` |

```powershell
# Windows — live tail
Get-Content .brain\server.log -Wait -Tail 30

# Windows — errors only
Select-String -Path .brain\server.log -Pattern "ERROR"
```

```bash
# macOS / Linux — live tail
tail -f .brain/server.log

# macOS / Linux — errors only
grep "ERROR" .brain/server.log
```

---

## Auxiliary Scripts

### `drop_memory.py` — Wipe a Workspace

Use when you forgot to set up `.memignore` before the first scan and the AI indexed the wrong directories. Deletes the ChromaDB collection, the `.brain/contexts/<id>.md` brief, and the workspace registry entry.

```bash
# Windows
.\venv\Scripts\python.exe drop_memory.py "Workspace Name"
# or by UUID
.\venv\Scripts\python.exe drop_memory.py workspace-uuid

# macOS / Linux
venv/bin/python drop_memory.py "Workspace Name"
```

After wiping: fix `.memignore` → `init_workspace` → `scan_workspace`.

---

## Security & Data Privacy

All memory, vector data, and API keys stay on your machine.

### Required `.gitignore` entries

```gitignore
.env        # Contains DEEPSEEK_API_KEY
.brain/     # Contains local vector DB and project briefs
```

> **Warning:** Never commit `.brain/` or `.env` to version control.

### Key security properties

| Property | Behavior |
|---|---|
| **Workspace isolation** | Each project gets its own ChromaDB sub-collection and brief. Queries never cross workspaces. |
| **Deterministic hashing** | File records use deterministic IDs — re-scanning overwrites, never duplicates. |
| **Local-first by default** | In `hybrid` and `local` modes, the majority of operations never leave your machine. |

---

## Troubleshooting

### Server fails to start

**Symptoms:** IDE shows MCP connection error; no startup banner in logs.

1. Virtual environment activated and `pip install -r requirements.txt` completed cleanly.
2. `.env` exists with a valid `DEEPSEEK_API_KEY` (required even in `local` mode).
3. Python version is 3.11+: `python --version`
4. IDE config uses **absolute paths** with correct separators for your OS.

---

### Ollama not responding

**Symptoms:** `hybrid` or `local` mode queries fail or time out.

1. Ollama running: open `http://127.0.0.1:11434` in a browser — you should see a response.
2. Model is pulled: `ollama list` should show `llama3.2` (or your configured model).
3. If `OLLAMA_HOST=0.0.0.0` is set on your system, the server corrects it automatically. If issues persist, unset it or set it explicitly to `http://127.0.0.1:11434`.

---

### Memory is stale or contains irrelevant files

**Symptoms:** AI recalls deleted files or ignored directories.

`scan_workspace` is self-cleaning — run it again to purge stale records. If wrong files were indexed from the start, use `drop_memory.py` to wipe and re-index with a corrected `.memignore`.

---

### DeepSeek cache hit rate is low

**Symptoms:** `get_cache_stats` shows high miss rate; costs higher than expected.

- Brief was recently force-refreshed, resetting the cache prefix.
- `ENABLE_DEEPSEEK_PRO=true` is set: v4-pro and v4-flash use separate caches.
- The first query of a new session is always a miss — cache warms on subsequent calls.

**Fix:** Avoid force-refreshing the brief unless you've made a major architectural change.

---

### Duplicate workspace IDs

**Symptoms:** `list_workspaces` shows the same project under multiple UUIDs (common when opened from absolute vs. relative paths).

```
"Merge workspaces <source-uuid> into <target-uuid>"
```

The assistant calls `merge_workspaces`. **Irreversible** — the source workspace is deleted after the merge.

---

## Changelog

<details>
<summary>2026-06-10 — High-granularity brief synthesis & async scan</summary>

- **High-Granularity Brief Synthesis:** `_build_section` now fetches a wider re-ranking pool (75) with keyword matching across entity name, docstring, and `source_file`. Per-section `full_context` and adjustable `fetch_cap` trimming (default 20, 25 for Architecture).
- **Roadmap Extraction Fix:** Patched a critical bug in `_truncate_for_brief` that broke roadmap retrieval at the first blank line.
- **Async background scanning:** `scan_workspace` returns immediately. 4 concurrent workers, batch ChromaDB upserts (300 per call), LLM calls gated by `semaphore(2)`.
- **`scan_status` tool:** Tracks scan progress and brief synthesis independently.
- **`SKIP_BARE_FILES`:** Renamed from `SKIP_BARE_PY_FILES`; now accepts a configurable extension list.
</details>

<details>
<summary>2026-05-27 — IDE Agent Rules</summary>

- New `agent_rules/ide_agent_rules.md` with Universal-Brain First and Source Discipline rules.
- Per-IDE setup instructions for VS Code Copilot, pi.dev, Google Antigravity, and Claude Desktop.
</details>

<details>
<summary>2026-05-17 — Inline source citations</summary>

- Replaced `## Sources` Markdown tables with plain-text `#file:line (distance)` citations. Renders in every IDE; clickable in VS Code Copilot.
</details>

<details>
<summary>2026-05-13 — Lexical re-ranking & agent-aware tool descriptions</summary>

- Lexical re-ranking in `query_memory`: hybrid step between semantic retrieval and LLM synthesis. `ENABLE_LEXICAL_RERANK=true`.
- All 15 MCP tool docstrings tuned for AI agent consumption — priority directives, anti-pattern hints, "when not to use" guidance.
- Irreversible operations (`merge_workspaces`, `purge_usage_data`) carry explicit warnings.
</details>

<details>
<summary>2026-05-12 — Parallel brief synthesis & HTML comment indexing</summary>

- All 9 brief sections fire simultaneously via `asyncio.gather`. Generation time dropped from ~90s to ~20-30s.
- HTML comment indexing: `_extract_html` captures `<!-- -->` comments as docstrings.
- `SKIP_BARE_FILES` toggle added.
</details>

<details>
<summary>2026-05-11 — Structured evidence & fire-and-forget scanning</summary>

- `query_memory` now returns structured JSON with evidence metadata for IDE-native rendering.
- `scan_workspace` returns immediately; brief generates in background.
- Default `QUERY_DISTANCE_THRESHOLD=1.0` added to `.env`.
</details>

---

## Notice & License

> This project is provided "as-is" for personal use/reference. Pull Requests and code contributions are not being accepted at this time. AI-generated PRs will be closed and users may be blocked.

Visit [zerikai.com](http://zerikai.com) for more.

**MIT License**
