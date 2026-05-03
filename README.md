# Zerikai Memory: Documentation

A standalone local-only Python MCP server that gives any IDE persistent, workspace-isolated memory. Combines ChromaDB (local vector store), Ollama (free local summarisation), and DeepSeek (cloud synthesis) with automatic cost-aware routing.

---

## Table of Contents

1. [What Is Zerikai Memory?](#what-is-zerikai-memory)
2. [How It Works](#how-it-works)
3. [Cost Savings Explained](#cost-savings-explained)
4. [Project Structure](#project-structure)
5. [Prerequisites](#prerequisites)
6. **[Installation](#installation)** *
7. [IDE Registration](#ide-registration)
8. [First-Time Setup](#first-time-setup)
9. [Day-to-Day Usage](#day-to-day-usage)
10. [Auto-Routing Reference](#auto-routing-reference)
11. [Project Brief Structure](#project-brief-structure)
12. [MCP Tools Reference](#mcp-tools-reference)
13. [Monitoring & Logs](#monitoring--logs)
14. [Auxiliary Scripts](#auxiliary-scripts)
15. [DeepSeek KV Cache Optimisation](#deepseek-kv-cache-optimisation)
16. [Security & Data Privacy](#security--data-privacy)
17. [Troubleshooting](#troubleshooting)
18. [Disclaimer](#disclaimer)

---

## What Is Zerikai Memory?

Zerikai Memory is a **local MCP (Model Context Protocol) server** that provides persistent, workspace-isolated memory to your IDE's AI assistant. It solves a core problem with AI-assisted development: every new chat session starts cold, forcing you to re-explain your project context, decisions, and conventions, wasting tokens and time.

By storing compressed, semantically searchable summaries of your codebase and architectural decisions, Zerikai Memory enables your IDE's AI to:

- **Recall decisions** made in previous sessions instantly.
- **Understand your codebase** without raw file dumps into the chat window.
- **Share context across IDEs**, work in VS Code, then switch to Cursor, with no re-explanation.
- **Dramatically reduce API costs** through smart local/cloud routing and DeepSeek KV caching.

The server runs **entirely on your local machine**. Each IDE connects via STDIO to its own server process, with direct filesystem access for workspace scanning.

---

## How It Works

```
Your IDE  ──►  MCP Server (main.py)  ──►  ChromaDB (.brain/vector_db/)
                     │                         ↑ semantic retrieval
                     ├── Ollama (local)    ─── Used in Hybrid & Local modes
                     └── DeepSeek (cloud)  ─── Used in Hybrid & Cloud modes
```

When you ask your AI assistant a question:

1. The MCP server receives the query.
2. It performs a **vector search** against ChromaDB to retrieve the most relevant 3-sentence summaries from your codebase.
3. The auto-router decides whether to send the query to **Ollama** (local, free) or **DeepSeek** (cloud, billed).
4. The synthesised answer is returned to your IDE ; enriched with workspace context, without bloating your chat window.

You never call MCP tools directly. You speak to your AI assistant in natural language and it calls the tools on your behalf.

### Workspace Identity

You do not specify your project name or path in chat. Your IDE automatically attaches metadata about your currently active workspace to every message. The server maintains a **Workspace Registry** (SQLite) that maps each workspace folder to a persistent UUID and human-friendly display name.

The AI assistant can resolve any workspace identifier: UUID, short-UUID, or display name, so you never pass raw file paths for routine queries.

---

## Cost Savings Explained

Zerikai Memory is built around four specific cost-saving mechanisms.

### 1. DeepSeek KV Caching (50× Cheaper Reasoning)

Standard LLM calls re-process your entire project brief on every query. DeepSeek's KV Caching avoids this:

- The server places your stable **Project Brief** at the start of the system message.
- DeepSeek recognises the identical prefix and charges a **cache hit rate** of ~\$0.0028/M tokens instead of ~\$0.14/M (cache miss) for v4-flash.
- **Result:** After the first query of a session, a 1,000-token project brief costs virtually nothing, a **50× saving per query**.

### 2. Ollama-First Auto-Routing (70–80% Free Queries)

The auto-router acts as a financial gatekeeper:

- **Routine queries** (e.g., "What was the naming convention for our API routes?") → handled by **Ollama** at \$0.
- **Escalation** only occurs for complex queries (40+ words) or architectural keywords (`refactor`, `design`, `migration`, `audit`…).
- **Result:** 70–80% of daily memory lookups run locally for free.

### 3. Context Compression (Token Distillation)

Instead of attaching 10 full files to your chat window:

- The server performs a **vector search** and retrieves only the 3 most relevant sentence summaries.
- Your IDE receives ~300 tokens of targeted context instead of 5,000 tokens of raw code.
- **Result:** Faster responses, higher accuracy, and staying under model rate limits longer.

### 4. Cross-IDE Warm Start (No Context Tax)

Both your IDEs point to the same `.brain/` directory:

- Work refactored in VS Code via `save_to_memory` is instantly available in Cursor or Claude Desktop.
- **Result:** Zero re-explanation cost when switching tools.

---

## Project Structure

```
zerikai_memory/
├── .brain/                       # Created on first run: do NOT commit
│   ├── server.log                # Rotating log file (5 MB cap, 2 backups)
│   ├── zerikai.db                # SQLite: Workspace Registry & token tracking
│   ├── vector_db/                # ChromaDB: one sub-collection per workspace
│   └── contexts/                 # Per-workspace project briefs (.md files)
├── .env                          # API keys: never commit
├── .memignore                    # Files/dirs excluded from memory indexing
├── config.py                     # Configuration & routing thresholds
├── drop_memory.py                # Cleanup utility (registry + vectors + files)
├── main.py                       # MCP server entry point
└── requirements.txt
```

---

## Prerequisites

| Dependency | Purpose | Link |
|---|---|---|
| Python 3.11+ | Runtime | [python.org](https://python.org) |
| Ollama | Free local summarisation (hybrid/local modes) | [ollama.com](https://ollama.com) |
| DeepSeek API key | Cloud synthesis (hybrid/cloud modes) | [platform.deepseek.com](https://platform.deepseek.com) |

---

## Installation

### Step 1 — Clone and create the virtual environment

```bash
git clone https://github.com/your-username/zerikai_memory.git
cd zerikai_memory

# Python 3.13+
python -3.13 -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### Step 2 — Configure `.env`

Configure via `MEMORY_MODE` in your `.env` file.

| Mode | LLM Strategy | Best For |
|---|---|---|
| `local` | Ollama for everything | 100% privacy & $0 cost |
| `hybrid` | Ollama (scans/routine) + DeepSeek (architecture/briefs) | **Recommended.** Free local lookups, pro cloud reasoning. |
| `cloud` | DeepSeek for everything | Maximum accuracy and project brief quality |

**Recommendation:** Start with `hybrid` (You must have Ollama Installed). You get free lookups for the vast majority of queries and cloud-quality synthesis only when it matters.

```.env
DEEPSEEK_API_KEY=your_deepseek_key_here

# Memory Mode:
# - "cloud": DeepSeek for all operations (highest quality, tracked)
# - "hybrid": Ollama for scans, DeepSeek for briefs/escalated queries
# - "local": Ollama for everything (free, lower quality)
MEMORY_MODE=hybrid

# Enable token tracking and cost reporting
ENABLE_TOKEN_TRACKING=true

# Enable deepseek-v4-pro for complex queries (architecture, design, tradeoffs)
# v4-pro is 3x more expensive than v4-flash (6x after May 31, 2026)
# Recommended: keep false unless you need maximum reasoning quality
ENABLE_DEEPSEEK_PRO=false
```

> **Note:** `OLLAMA_HOST` is optional. If your system has `OLLAMA_HOST=0.0.0.0` set (common on server installs), the server automatically corrects it to `http://127.0.0.1:11434` for client connections.

### Step 3 — Pull a local Ollama model (hybrid/local mode only)

Download and install [Ollama](https://ollama.com) for your OS. Then pull a model:

```bash
ollama pull llama3.2
```

> Only required for `MEMORY_MODE=hybrid` or `MEMORY_MODE=local`.

### Step 4 — Verify the installation

Open a terminal in your project root (virtual environment activated) and run:

```bash
python -c "from main import scan_workspace, query_memory; print('OK')"
```

You should see the server startup banner followed by `OK`.

---

## IDE Registration

The server starts **once** when the IDE loads and stays running. Tool calls are messages to that process, there is no restart per call.

### Google Antigravity

Edit `mcp_config.json` directly:

```json
"universal-brain": {
  "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
  "args": [
    "C:\\path\\to\\zerikai_memory\\main.py"
  ],
  "disabled": false
}
```

> Replace `C:\\path\\to\\zerikai_memory` with the actual absolute path. Double backslashes are required for valid JSON on Windows.

### VS Code (Copilot / Cline)

1. Press `Ctrl+Shift+P` → **MCP: Add Local Server**
2. Choose **STDIO**
3. Set command to: `/path/to/zerikai_memory/venv/bin/python /path/to/zerikai_memory/main.py`

### Cursor

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

### Claude Desktop

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "universal-brain": {
      "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\zerikai_memory\\main.py"
      ]
    }
  }
}
```

> On macOS/Linux, use forward slashes: `/path/to/zerikai_memory/venv/bin/python`

---

## First-Time Setup

### 1. Setup the `.memignore` file

Works like `.gitignore`: one pattern per line. `scan_workspace` reads this file and skips matching paths.

Each project should have its own `.memignore` in its root directory. Forgetting to configure it before the first scan is the most common reason to use `drop_memory.py` and start fresh.

```gitignore
# Directories (trailing slash required)
.git/
node_modules/
venv/
__pycache__/
.brain/
dist/
build/

# File/Folder patterns
**/test/
**/tests/
.env
*.log
*.lock
*.pyc
```

### 2. Register a new project

Tell your assistant:

```
"Set up memory for this project"
```

The assistant calls `init_workspace`, which registers the folder and creates a pending brief file at:

```
.brain/contexts/<workspace_id>.md
```

> `init_workspace` is idempotent, running it multiple times is safe and returns the existing registration.

### 3. Scan and index the workspace

Tell your assistant:

```
"Scan and index the workspace."
```

The assistant calls `scan_workspace`. This triggers the **Post-Scan Auto-Briefing**:

1. Walks the directory (respecting `.memignore`) and saves compressed summaries of every readable file to ChromaDB.
2. Performs **iterative synthesis**: queries memory for 15 relevant results per section across 9 project brief sections.
3. Uses the auto-router (DeepSeek in Hybrid/Cloud modes) to synthesise a complete, accurate **Project Brief**.
4. Saves the brief to `.brain/contexts/<workspace_id>.md` and locks it to protect your DeepSeek KV cache prefix.

> **Cache Stability Policy:** Normal daily scans do **not** regenerate the project brief. The brief is only generated on the first scan or when explicitly forced.

### 4. Force a brief refresh (when needed)

If you make a major architectural pivot, tell your assistant:

```
"Rescan the workspace and force a refresh of the project brief."
```

The assistant calls `scan_workspace(force_refresh_brief=True)`.

---

## Day-to-Day Usage

### Scan

```
"Scan the workspace."
```

`scan_workspace` is **idempotent and self-cleaning**:

- Uses deterministic hashing to overwrite existing file records (no duplicates).
- Automatically **purges stale memories** for files deleted or added to `.memignore` since the last scan.
- Does **not** regenerate the project brief, preserving your KV cache.

### Common natural-language commands

| You say | What happens |
|---|---|
| *"Remember that we're using Redis for session caching"* | `save_to_memory` is called |
| *"What did we decide about auth?"* | `query_memory` → Ollama (local, instant) |
| *"Refactor the data layer, what are our constraints?"* | `query_memory` → DeepSeek (auto-escalated) |
| *"List what's in memory for this project"* | `list_memory` |
| *"What projects do you know about?"* | `list_workspaces` |
| *"Show me the project brief."* | `get_brief` → displays `.brain/contexts/<id>.md` |

### Retrieve the project brief

```
"Show me the project brief."
```

The assistant calls `get_brief`, which reads the `.md` file from `.brain/contexts/` and displays its content. If no brief exists, it suggests running `init_workspace` and `scan_workspace` first.

---

## Auto-Routing Reference

Routing is fully automatic based on query characteristics. You can override it explicitly.

| Condition | Engine | Cost |
|---|---|---|
| Short, specific query | Ollama | Free |
| Query ≥ 40 words | DeepSeek v4-flash | ~$0.028/M cached tokens |
| Contains architectural keywords (`refactor`, `architect`, `design`, `audit`…) | DeepSeek v4-pro | ~$0.028/M cached tokens |
| `use_cloud=True` (explicit override) | DeepSeek | — |
| `use_cloud=False` (explicit override) | Ollama | Free |

---

## Project Brief Structure

Each workspace gets an auto-generated project brief optimised for DeepSeek KV caching. The brief is 1,000–1,200 tokens, the sweet spot for cache stability and accuracy.

The brief is synthesised using **DeepSeek v4-flash** (or Ollama in local mode), generating 15 semantic search results per section for accuracy.

### 9-Section Structure

| # | Section | Content |
|---|---|---|
| 1 | **Overview** | High-level summary of type, purpose, and domain |
| 2 | **Technical Stack** | Backend, Database, API integrations, Libraries |
| 3 | **Core Architecture** | Frontend, Backend, Data/Processing layers |
| 4 | **Primary Conventions** | Code, docs, error handling, and schema rules |
| 5 | **Purpose** | Business problem solved and core objectives |
| 6 | **Key Files & Directories** | Entry points and routers with specific purposes |
| 7 | **Development & Testing** | Setup, running, testing, and deployment instructions |
| 8 | **Data Flow & Request Lifecycle** | Request trace from entry point to data layer |
| 9 | **Future Roadmap** | Planned features, improvements, and TODOs from code |

**Benefits:**

- 1,000–1,200 tokens → optimal cache stability.
- 10× cost savings via DeepSeek cache hits (identical prefix across queries).
- Semantic search friendly → accurate context retrieval.
- Human-readable → can be manually reviewed and edited.

---

## MCP Tools Reference

You never call these tools directly, your AI assistant calls them based on your natural language instructions. This reference is for understanding what the server can do.

### Workspace Management

| Tool | Description |
|---|---|
| `init_workspace` | Registers a project folder, assigns a UUID, and creates a pending brief file. Idempotent; safe to run multiple times. |
| `list_workspaces` | Lists all known workspaces that have a brief or stored memories. |
| `resolve_workspace` | Resolves a workspace identifier (UUID, short-UUID, or display name) to its filesystem path. |
| `merge_workspaces` | Consolidates duplicate workspace IDs into one. **Irreversible.** |
| `debug_workspace_id` | Diagnostic tool; shows what workspace ID would be generated from a given path. |

### Memory & Briefs

| Tool | Description |
|---|---|
| `scan_workspace` | Walks the directory, respects `.memignore`, and saves all readable text files to persistent memory. Idempotent and self-cleaning. |
| `save_to_memory` | Manually saves an architectural decision, fact, or technical note with an optional category tag. |
| `list_memory` | Lists stored memories for a workspace, optionally filtered by category. |
| `query_memory` | Retrieves relevant context via vector search and synthesises an answer via Ollama or DeepSeek (auto-routed). |
| `get_brief` | Retrieves the current project brief from `.brain/contexts/`. |
| `update_brief` | Manually updates the markdown content of a project brief. |

### Usage & Diagnostics

| Tool | Description |
|---|---|
| `get_token_usage` | Returns DeepSeek API token usage and cost statistics. |
| `get_cost_report` | Generates a cost breakdown by operation type. |
| `get_cache_stats` | Shows cache hit/miss rates by operation type. |
| `purge_usage_data` | Deletes historical token tracking records. |

---

## Monitoring & Logs

All server activity is written to **`.brain/server.log`** with a 5 MB rotating cap and 2 rolling backups.

### What is logged

| Event | Level |
|---|---|
| Server startup (DB path, model, mode) | `INFO` |
| Memory saved (workspace, category, preview) | `INFO` |
| Auto-route decision (reason) | `INFO` |
| DeepSeek cache hit / miss stats | `INFO` |
| `scan_workspace`: each file saved or skipped | `INFO` / `DEBUG` |
| Any tool failure | `ERROR` |

### Live tail

```powershell
# Windows PowerShell
Get-Content .brain\server.log -Wait -Tail 30
```

```bash
# macOS / Linux
tail -f .brain/server.log
```

### Filter errors only

```powershell
# Windows PowerShell
Select-String -Path .brain\server.log -Pattern "ERROR"
```

```bash
# macOS / Linux
grep "ERROR" .brain/server.log
```

---

## Auxiliary Scripts

### `drop_memory.py` — Wipe a workspace

Use this when you need to completely reset the AI's memory for a specific project, for example, if you forgot to configure `.memignore` before the first scan and the AI indexed a large `logs/` directory.

The script deletes:

- The ChromaDB vector collection for the workspace.
- The associated `.brain/contexts/<workspace_id>.md` brief file.
- The workspace registry entry in `zerikai.db`.

**Usage:**

```bash
# Windows
.\venv\Scripts\python.exe drop_memory.py "Workspace Name"
# or by UUID
.\venv\Scripts\python.exe drop_memory.py workspace-uuid

# macOS / Linux
venv/bin/python drop_memory.py "Workspace Name"
```

Find workspace names and IDs with `list_workspaces` or by listing `.brain/contexts/`.

After wiping, fix your `.memignore`, then re-run `init_workspace` and `scan_workspace`.

---

## DeepSeek KV Cache Optimisation

Caching is **automatic**, no flags required.

The server structures every API call to maximise hit rate:

- **System message** = fixed role instruction + stable project brief. This prefix is identical on every call for the same workspace → cached after the first call of a session.
- **User message** = retrieved context snippets + query. This changes every call → never cached (by design).

A well-populated 600-token project brief means paying **\$0.0028/M tokens** (cache hit) instead of **\$0.14/M** (cache miss) on your largest token block, a **50× saving** on every query after the first, using v4-flash pricing.

> **Cache Protection:** Do not force-refresh the project brief during normal development. The brief is intentionally locked after the first scan to keep the system message prefix identical and cache hits active.

---

## Security & Data Privacy

All memory data and API keys stay on your machine.

### `.gitignore` requirements

```gitignore
.env       # Contains DEEPSEEK_API_KEY
.brain/    # Contains local vector DB and project briefs
```

> **Warning:** Never commit your `.brain/` folder or `.env` file to version control.

### Key security properties

- **Workspace isolation:** Each project gets its own ChromaDB sub-collection, separate SQLite records, and a separate brief file. Queries for one workspace never return data from another.
- **Deterministic hashing:** File records use deterministic IDs, re-scanning overwrites existing records rather than duplicating them.
- **Local-first by default:** In `hybrid` and `local` modes, the majority of operations never leave your machine.

---

## Troubleshooting

### The server fails to start

**Symptoms:** IDE shows MCP connection error; no startup banner in logs.

**Check:**

1. Virtual environment is activated and `pip install -r requirements.txt` completed without errors.
2. `.env` exists with a valid `DEEPSEEK_API_KEY` (even in `local` mode, the file must exist).
3. Python version is 3.11+: `python --version`
4. Path to `main.py` in IDE config uses absolute paths and correct separators for your OS.

---

### Ollama not responding

**Symptoms:** `hybrid` or `local` mode queries fail or time out.

**Check:**

1. Ollama is running: open a browser to `http://127.0.0.1:11434`, you should see a response.
2. The model is pulled: `ollama list` should show `llama3.2` (or your configured model).
3. If your system has `OLLAMA_HOST=0.0.0.0` set as an environment variable, the server corrects this automatically. If issues persist, unset it or set it explicitly to `http://127.0.0.1:11434`.

---

### Memory is stale or contains irrelevant files

**Symptoms:** The AI recalls information from deleted files or ignored directories.

**Solution:** `scan_workspace` is self-cleaning, run it again and it will automatically purge stale records. If the problem is structural (wrong files indexed from the start), use `drop_memory.py` to wipe and re-index with a corrected `.memignore`.

---

### DeepSeek cache hit rate is low

**Symptoms:** `get_cache_stats` shows a high miss rate; costs are higher than expected.

**Causes:**

- The project brief was recently force-refreshed, resetting the cache prefix.
- `ENABLE_DEEPSEEK_PRO=true` is set: v4-pro and v4-flash have separate caches.
- The first query of a new session is always a miss (the cache warms on subsequent calls).

**Fix:** Avoid force-refreshing the brief unless you have made a major architectural change. Keep `ENABLE_DEEPSEEK_PRO=false` unless you specifically need pro-level reasoning.

---

### Duplicate workspace IDs

**Symptoms:** `list_workspaces` shows the same project registered under multiple UUIDs (common when the project is opened from different paths, e.g., absolute vs. relative).

**Fix:**

```
"Merge workspaces <source-uuid> into <target-uuid>"
```

The assistant calls `merge_workspaces`. This is **irreversible**; the source workspace is deleted after the merge.

---

## Disclaimer

This tool interacts directly with your local file system, vector databases, and LLM APIs. While every effort has been made to ensure safety, including strict workspace isolation and deterministic hashing, Zerikai is not responsible for any data loss, corruption, API charges, or unintended consequences resulting from the use of this software. Always maintain standard backups or version control for your codebase before scanning or dropping memory.

---

## License

MIT
