# Zerikai Memory — Hybrid Universal Memory Bridge

> A standalone **local-only** Python MCP server that gives any IDE persistent, workspace-isolated memory.
> Combines **ChromaDB** (local vector store), **Ollama** (free local summarisation), and **DeepSeek** (cloud synthesis) with automatic cost-aware routing.

> **Note:** This server runs locally on your development machine. Each IDE connects via STDIO to its own server process, with direct filesystem access for workspace scanning.

This implementation plan creates a highly efficient "Memory Layer" that fundamentally changes how you use tokens in Google Antigravity and VS Code Copilot. By moving the heavy lifting of context management out of the IDE chat and into your local Python MCP server, you achieve three specific savings:

## 1. 10x Cheaper Reasoning via DeepSeek KV Caching

DeepSeek's KV Caching is the core "credit saver" in this setup.

* The Problem: Standard LLM calls re-process your entire project brief (stack, rules, patterns) every time you ask a question.
* The Fix: Your _build_system_message puts the stable "Project Brief" first. DeepSeek recognizes this identical prefix and only charges you ~$0.0028/M tokens (Cache Hit) instead of ~$0.14/M (Cache Miss) for v4-flash.
* Result: You can feed the AI a massive 1,000-token project manual for virtually zero cost after the first query of the session—cache hits are **50x cheaper** than cache misses.

------------------------------

## 2. Auto-Routing: The "Ollama First" Policy

The _should_use_cloud logic acts as a financial gatekeeper.

* Routine Queries: Questions like "What was the naming convention for our API routes?" or "Retrieve the last decision on auth" are handled by Ollama (local). These cost $0 and use zero cloud tokens.
* Selective Escalation: You only spend DeepSeek credits when the query is complex (40+ words) or contains "Architectural" keywords (refactor, design, migration).
* Result: 70–80% of your daily "memory lookups" happen on your local machine for free.

------------------------------

## 3. Context Compression (Token Distillation)

Instead of attaching 10 full files to your chat window—which bloats the context window and slows down response time—this system uses Semantic Retrieval.

* Vector Search: When you ask a question, the MCP server finds only the relevant 3-sentence summaries stored in ChromaDB.
* Distilled Context: Instead of 5,000 tokens of raw code, the IDE only receives ~300 tokens of highly relevant "memory snippets."
* Result: You stay under the rate limits of your favorite models longer and get faster, more accurate answers because the AI isn't "hallucinating" through irrelevant code noise.

------------------------------

## 4. Cross-IDE "Warm Start"

Since both Antigravity and VS Code point to the same .brain directory:

* No Re-Explanation: If you refactor a component in VS Code and save_to_memory, Antigravity instantly "knows" it. You don't have to waste 500 tokens re-explaining the change to a new chat window in a different IDE.
* Result: You avoid the "Context Tax" of switching tools.

------------------------------

## 🚀 Immediate Next Step

To maximize these savings, you need to populate the Project Brief for each workspace. Have you run the init_workspace tool yet to generate the .md template?

---

## How it works

The server operates in one of three **Memory Modes** (configured in `.env`), allowing you to balance cost, privacy, and reasoning quality:

```
Your IDE  ──►  MCP Server (main.py)  ──►  ChromaDB (.brain/vector_db/)
                     │                         ↑ semantic retrieval
                     ├── Ollama (local)    ─── Used in Hybrid & Local modes
                     └── DeepSeek (cloud)  ─── Used in Hybrid & Cloud modes
```

### 🧠 Memory Modes

| Mode | LLM Strategy | Best For |
|---|---|---|
| **`local`** | **Ollama** for everything | 100% Privacy & $0 cost. |
| **`hybrid`** | **Ollama** (Scans/Routine) + **DeepSeek** (Architecture/Briefs) | **Best Balance.** Free local lookups, pro cloud reasoning. |
| **`cloud`** | **DeepSeek** for everything | Maximum accuracy and project brief quality. |

The server uses a **Workspace Registry** to manage projects. Each workspace is assigned a persistent UUID and a human-friendly display name.

The AI assistant automatically resolves your workspace identifier (UUID, short-ID, or name) so you never have to pass complex file paths manually for routine queries.

---

## Project Structure

```
zerikai_memory/
├── .brain/                       # Created on first run — do NOT commit
│   ├── server.log                # Rotating log file (5 MB cap)
│   ├── zerikai.db                # SQLite database for Registry & Token tracking
│   ├── vector_db/                # ChromaDB — one sub-collection per workspace
│   └── contexts/                 # Per-workspace project briefs
├── .env                          # API keys (never commit)
├── .memignore                    # Files to exclude from memory indexing
├── config.py                     # Configuration & routing thresholds
├── drop_memory.py                # Cleanup utility (registry + vectors + files)
├── main.py                       # MCP server entry point
└── requirements.txt
```

### Security & Data Privacy

To protect your sensitive credentials and local memory data, ensure your `.gitignore` includes the following:

```gitignore
.env       # Contains DEEPSEEK_API_KEY
.brain/    # Contains local vector DB and project briefs
```

> **Warning:** Never commit your `.brain/` folder or `.env` file to version control.

---

## Project Brief Structure

Each workspace gets an auto-generated project brief (`.brain/contexts/<workspace_id>.md`) with an 8-section structure optimized for DeepSeek KV caching. These sections are synthesized automatically during a scan:

1. **Overview** — High-level summary of [type], [purpose], and [domain].
2. **Technical Stack** — Concise list of Backend, Database, API integrations, and Libraries.
3. **Core Architecture** — Layered description (Frontend, Backend, Data/Processing layers).
4. **Primary Conventions** — Organizational rules for code, docs, error handling, and schema.
5. **Purpose** — In-depth explanation of the business problem solved and core objectives.
6. **Key Files & Directories** — Curated list of entry points and routers with their specific purposes.
7. **Development & Testing** — Verified instructions for setup, running, testing, and deployment.
8. **Data Flow & Request Lifecycle** — Trace of a request from Entry Point through to the Data Layer.
9. **Future Roadmap** — Planned features, architectural improvements, and TODOs extracted from code.

**Benefits:**

* **1000-1200 tokens** per brief → optimal cache stability.
* **10x cost savings** via DeepSeek cache hits (identical prefix across queries).
* **Semantic search friendly** → accurate context retrieval.
* **Human-readable** → can be manually reviewed/edited.

The brief is synthesized using **DeepSeek v4-flash** (or Ollama in local mode) with section-by-section generation (15 semantic search results per section) for accuracy.

---

## Prerequisites

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.11+ | Runtime | [python.org](https://python.org) |
| Ollama | Free local summarisation | [ollama.com](https://ollama.com) |
| DeepSeek API key | Cloud synthesis | [platform.deepseek.com](https://platform.deepseek.com) |

---

## Installation

### 1. Clone and set up the virtual environment

```bash
git clone https://github.com/your-username/zerikai_memory.git
cd zerikai_memory

python -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure `.env`

```ini
DEEPSEEK_API_KEY=your_deepseek_key_here

# Memory Mode controls which LLM is used:
# - "cloud": DeepSeek for all operations (highest quality, tracked)
# - "hybrid": Ollama for scans, DeepSeek for briefs/escalated queries
# - "local": Ollama for everything (free, lower quality)
MEMORY_MODE=cloud

# Enable token tracking and cost reporting
ENABLE_TOKEN_TRACKING=true

# Enable deepseek-v4-pro for complex queries (architecture, design, tradeoffs)
# v4-pro is 3x more expensive than v4-flash (6x after May 31, 2026)
# Recommended: keep false unless you need maximum reasoning
ENABLE_DEEPSEEK_PRO=false
```

> **Note:** `OLLAMA_HOST` is optional. If your system has `OLLAMA_HOST=0.0.0.0` set (common on server installs), the server automatically corrects it to `http://127.0.0.1:11434` for client connections.

### 3. Pull a local Ollama model (for hybrid/local mode)

Download and install the [Ollama](https://ollama.com) version for your operating system. Once installed and running, open your terminal and run:

```bash
ollama pull llama3.2
```

> **Note:** Only required if using `MEMORY_MODE=hybrid` or `MEMORY_MODE=local`.

### 4. Verify the installation

Open a new terminal in your IDE, ensure your virtual environment is activated (from Step 1), and run:

```bash
python -c "from main import scan_workspace, query_memory; print('OK')"
```

You should see the server startup banner followed by `OK`.

---

## IDE Registration

### Google Antigravity

If you are editing the `mcp_config.json` file directly, you can use the following pattern:

```json
    "universal-brain": {
      "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\zerikai_memory\\main.py"
      ],
      "disabled": false
    }
```

*(Remember to replace `C:\\path\\to\\zerikai_memory` with the actual absolute path to your cloned repository. Double your backslashes for valid JSON on Windows).*

### VS Code (Copilot / Cline)

1. `Ctrl+Shift+P` → **MCP: Add Local Server**
2. Choose **STDIO**
3. Command: `/path/to/zerikai_memory/venv/bin/python /path/to/zerikai_memory/main.py`

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

Add to your Claude Desktop configuration file:

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

*(On macOS/Linux, use forward slashes and the path to your venv's python: `/path/to/zerikai_memory/venv/bin/python`)*

> The server starts **once** when the IDE loads and stays running. Tool calls are messages to that process — there is no restart per call.

---

## Usage

You never call tools directly. You speak to your AI assistant in natural language and it calls the tools on your behalf.

### How does it know what workspace you are in?

You do not need to specify your project name or path in the chat. Your IDE (Antigravity/Cursor/VS Code) automatically attaches metadata about your currently active workspace (the folder you have open) to every message you send.

The system uses a **Workspace Registry** to manage projects.

1. The user (or IDE) runs `init_workspace(path)` to register the project.
   - **Note:** In a practical sense, it means that `init_workspace` acts as a "Get or Create" command. This tool is idempotent—running it multiple times is safe and will simply return the existing registration.
2. The AI assistant then uses the assigned **UUID** or **Display Name** for all subsequent tool calls.
3. The MCP server identifies the correct context and isolation based on this identifier.

### First-time setup for a new project

Tell your assistant:

```text
"Set up memory for this project"
```

The assistant calls `init_workspace`, which creates a pending file at:

```text
.brain/contexts/<workspace_id>.md
```

Then tell your assistant:

```text
"Scan and index the workspace."
```

The assistant calls `scan_workspace`. This triggers the **Post-Scan Auto-Briefing**:

1. It walks the directory and saves summaries of every readable file to the vector database.
2. The server then performs an **iterative synthesis**, querying the memory for 15 relevant results for each of the 9 project brief sections.
3. It uses the auto-router (DeepSeek by default in Hybrid/Cloud modes) to **synthesize a definitive, highly-accurate Project Brief**.
4. The brief is saved to the `.md` file, and the synthesis marker is removed, effectively **locking** the brief to protect your DeepSeek KV cache.

> **Cache Stability Policy:** To ensure your DeepSeek KV cache prefix stays identical (which gives you the 10x cost savings), the system will **never** overwrite your Project Brief during normal daily scans. It is only generated on the very first scan.

### How to Force a Brief Refresh

If you make a massive architectural pivot (e.g., migrating from React to Vue) and you *want* the AI to rewrite the brief and reset the cache, tell your assistant:

```text
"Rescan the workspace and force a refresh of the project brief."
```

*(The assistant will run `scan_workspace(force_refresh_brief=True)`).*

---

### Day-to-day

Tell your assistant:

```text
"Scan the workspace."
```

> **Self-Cleaning Sync:** The `scan_workspace` tool is idempotent. It uses deterministic hashing to overwrite existing file records. Additionally, it **automatically purges** any stale memories from your database if the corresponding files were deleted or added to `.memignore` since the last scan. Your memory always perfectly mirrors your codebase.
>
> **Cache Protection:** Normal scans do NOT regenerate the project brief—this preserves your DeepSeek KV cache prefix for 10× cost savings. The brief is only generated on the first scan or when explicitly forced with `force_refresh_brief=True`.

| You say | What happens |
|---|---|
| *"Remember that we're using Redis for session caching"* | `save_to_memory` is called |
| *"What did we decide about auth?"* | `query_memory` → Ollama (local, instant) |
| *"Refactor the data layer — what are our constraints?"* | `query_memory` → DeepSeek (auto-escalated) |
| *"List what's in memory for this project"* | `list_memory` |
| *"What projects do you know about?"* | `list_workspaces` |

### Auto-routing

| Condition | Engine | Cost |
|---|---|---|
| Short, specific query | Ollama | Free |
| Query ≥ 40 words | DeepSeek v4-flash | ~$0.028/M cached tokens |
| Contains: *refactor, architect, design, audit…* | DeepSeek v4-pro | ~$0.028/M cached tokens |
| `use_cloud=True` (explicit override) | DeepSeek | — |
| `use_cloud=False` (explicit override) | Ollama | Free |

---

### 5. Retrieve the Project Brief

You can now directly retrieve the current project brief for a workspace. This is useful for reviewing the synthesized project context and architecture overview before making further queries or updates.

**Command:**

```text
"Show me the project brief."
```

The assistant will call `get_brief`, which retrieves the `.md` file from `.brain/contexts/` and displays its content. If no brief exists, it will suggest running `init_workspace` and `scan_workspace` to generate one.

---

## .memignore

Works like `.gitignore` — one pattern per line. The `scan_workspace` tool reads this file and skips any matching path.

```
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

Each project should have its own `.memignore` in its root.

---

## Monitoring & Error Inspection

All server activity is written to **`.brain/server.log`** (5 MB cap, 2 rolling backups).

### Live tail

```powershell
# Windows PowerShell
Get-Content .brain\server.log -Wait -Tail 30
```

```bash
# macOS / Linux
tail -f .brain/server.log
```

### Errors only

```powershell
Select-String -Path .brain\server.log -Pattern "ERROR"
```

```bash
grep "ERROR" .brain/server.log
```

### What is logged

All server activity is recorded in **`.brain/server.log`** (located in the project root). The server uses a rotating file handler to track architectural decisions, retrieval performance, and operational errors, providing essential transparency into model routing and cache efficiency.

| Event | Level |
|---|---|
| Server startup (DB path, model, mode) | `INFO` |
| Memory saved (workspace, category, preview) | `INFO` |
| Auto-route decision (reason) | `INFO` |
| DeepSeek cache hit / miss stats | `INFO` |
| `scan_workspace` — each file saved or skipped | `INFO` / `DEBUG` |
| Any tool failure | `ERROR` |

---

## Available MCP Tools

### Workspace Management

* **`init_workspace`**: Scaffolds a project brief file for a new workspace (idempotent "Get or Create" command).
* **`list_workspaces`**: Lists all known workspaces that have a brief or stored memories.
* **`resolve_workspace`**: Resolves a workspace identifier (UUID or name) to its filesystem path.
* **`merge_workspaces`**: Consolidates duplicate workspace IDs into one (irreversible).

### Memory & Briefs

* **`scan_workspace`**: Walks the directory and saves all readable text files to persistent memory.
* **`save_to_memory`**: Manually saves architectural decisions, facts, or technical notes.
* **`list_memory`**: Lists stored memories for a workspace, optionally filtered by category.
* **`query_memory`**: Retrieves relevant context and synthesizes an answer via Ollama or DeepSeek.
* **`get_brief`**: Retrieves the current project brief/architecture overview.
* **`update_brief`**: Manually updates the markdown content of a project brief.

### Usage & Diagnostics

* **`get_token_usage`**: Returns DeepSeek API token usage and cost statistics.
* **`get_cost_report`**: Generates a cost breakdown by operation.
* **`get_cache_stats`**: Shows cache hit/miss rates by operation type.
* **`purge_usage_data`**: Deletes historical token tracking records.
* **`debug_workspace_id`**: Diagnostic tool to see what workspace ID would be generated from a path.

---

## Auxiliary Scripts

### Wiping Workspace Memory (`drop_memory.py`)

If you misconfigured your `.memignore` or want to completely reset the AI's memory for a specific project, you can use the included `drop_memory.py` script. This script safely deletes the ChromaDB vector collection, the associated `.md` context file, and the workspace registry entry for a given workspace.

**Scenario: Starting Fresh**
You ran the initial scan on a new workspace, but realized the AI indexed a massive `logs/` directory because you forgot to add it to `.memignore`. Instead of dealing with duplicate embeddings or irrelevant context, you can wipe the workspace memory completely and start from scratch.

**Usage:**
Run the script from the root of `zerikai_memory`, passing the **workspace name or UUID** you want to wipe:

```bash
# Windows
.\venv\Scripts\python.exe drop_memory.py "Workspace Name"
# OR
.\venv\Scripts\python.exe drop_memory.py workspace-uuid

# macOS / Linux
venv/bin/python drop_memory.py "Workspace Name"
```

*(You can find the workspace name or ID by using the `list_workspaces` tool or by checking the `.brain/contexts/` folder).*

---

## DeepSeek KV Cache Optimisation

Caching is automatic — no flags required. The server is structured to maximise hit rate:

* The **system message** = fixed role instruction + stable project brief (identical every call for the same workspace → cached after the first call)
* The **user message** = retrieved context + query (changes every call → never cached, that's fine)

A well-populated 600-token project brief means paying **\$0.0028/M** (cache hit) instead of **\$0.14/M** (cache miss) on your largest token block—a **50× saving** on every query after the first (v4-flash pricing).

---

## Disclaimer

This tool interacts directly with your local file system, vector databases, and LLM APIs. While every effort has been made to ensure safety (such as strict workspace isolation and deterministic hashing), the authors (Zerikai) are not responsible for any data loss, corruption, API charges, or unintended consequences resulting from the use of this software. Always ensure you have standard backups or version control for your codebase before scanning or dropping memory.

---

## License

MIT
