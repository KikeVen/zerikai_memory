# Zerikai Memory — Hybrid Universal Memory Bridge

> A standalone Python MCP server that gives any IDE persistent, workspace-isolated memory.
> Combines **ChromaDB** (local vector store), **Ollama** (free local summarisation), and **DeepSeek** (cloud synthesis) with automatic cost-aware routing.

This implementation plan creates a highly efficient "Memory Layer" that fundamentally changes how you use tokens in Google Antigravity and VS Code Copilot. By moving the heavy lifting of context management out of the IDE chat and into your local Python MCP server, you achieve three specific savings:

## 1. 10x Cheaper Reasoning via DeepSeek KV Caching

DeepSeek's KV Caching is the core "credit saver" in this setup.

* The Problem: Standard LLM calls re-process your entire project brief (stack, rules, patterns) every time you ask a question.
* The Fix: Your _build_system_message puts the stable "Project Brief" first. DeepSeek recognizes this identical prefix and only charges you ~$0.028/M tokens (Cache Hit) instead of ~$0.28/M (Cache Miss).
* Result: You can feed the AI a massive 1,000-token project manual for virtually zero cost after the first query of the session.

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

```
Your IDE  ──►  MCP Server (main.py)  ──►  ChromaDB (.brain/vector_db/)
                     │                         ↑ semantic retrieval
                     ├── Ollama (local)    ─── short / routine queries
                     └── DeepSeek (cloud)  ─── architectural / long queries
```

One server process handles **multiple projects simultaneously**. Each project gets its own isolated ChromaDB collection and project brief, keyed by workspace path.

---

## Project Structure

```
zerikai_memory/
├── .brain/                       # Created on first run — do NOT commit
│   ├── server.log                # Rotating log file (5 MB cap)
│   ├── vector_db/                # ChromaDB — one sub-collection per workspace
│   └── contexts/                 # Per-workspace project briefs
├── .env                          # API keys (never commit)
├── .memignore                    # Files to exclude from memory indexing
├── config.py                     # Configuration & routing thresholds
├── main.py                       # MCP server entry point
└── requirements.txt
```

> **Add `.brain/` and `.env` to your `.gitignore`.**

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

# Fallback when auto-routing is inconclusive. Options: "local" | "cloud"
DEFAULT_MEMORY_MODE=local
```

> `OLLAMA_HOST` is optional. If your system has `OLLAMA_HOST=0.0.0.0` set (common on server installs), the server automatically corrects it to `http://127.0.0.1:11434` for client connections.

### 3. Pull a local Ollama model

```bash
ollama pull llama3.2
```

### 4. Verify the installation

```bash
python -c "from main import scan_workspace, query_memory; print('OK')"
```

You should see the server startup banner followed by `OK`.

### 5. Remote / Docker Deployment (Optional)

You can run the server remotely (e.g., on a Linux laptop on your LAN) using the provided `Dockerfile` and `docker-compose.yml`.

1. **Build and start the container:** Mounts your `.brain` directory to persist vectors.

   ```bash
   docker compose up -d --build
   ```

2. **Network Mode:** The container runs `main.py --sse` automatically, which starts the FastMCP server over HTTP on port `8200` instead of using standard local STDIO.
3. **Ollama Routing:** Because the server runs remotely but you want to use your powerful local Windows machine for Ollama summaries, pass `OLLAMA_HOST=your-local-ip` in your `.env` file on the remote machine. The server will route summary processing back to your local machine.

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

### Remote (SSE) Registration

If you deployed the server via Docker to a remote machine, you do **not** use the STDIO setup above. Instead, configure your IDE to connect via **SSE** and point it to the remote HTTP endpoint:

* **URL:** `http://your-remote-server-ip:8200/sse`

> The server starts **once** when the IDE loads and stays running. Tool calls are messages to that process — there is no restart per call.

---

## Usage

You never call tools directly. You speak to your AI assistant in natural language and it calls the tools on your behalf.

### How does it know what workspace you are in?

You do not need to specify your project name or path in the chat. Your IDE (Antigravity/Cursor/VS Code) automatically attaches metadata about your currently active workspace (the folder you have open) to every message you send.

The AI assistant reads this hidden metadata and automatically passes the exact `workspace_path` to the MCP tools. The MCP server then securely hashes this path to completely isolate your project's memory from all others.

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
2. Once the scan is complete, it reads the top 50 file summaries.
3. It feeds those summaries to Ollama to **synthesize a definitive, highly-accurate Project Brief**.
4. The brief is saved to the `.md` file, and the file is **locked** to protect your DeepSeek Cache.

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

> **Self-Cleaning Sync:** The `scan_workspace` tool is idempotent. It uses deterministic hashing to overwrite existing file records. Additionally, it **automatically purges** any stale memories from your database if the corresponding files were deleted or added to `.memignore` since the last scan. Your memory always perfectly mirrors your codebase, without breaking the cache prefix.

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

### 6. Retrieve the Project Brief

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

# File patterns
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

| Tool | Description |
|---|---|
| `init_workspace(workspace_path)` | Prepares the workspace for its initial deep scan |
| `scan_workspace(..., force_refresh_brief?)` | Syncs code to memory and auto-synthesizes the brief |
| `save_to_memory(content, workspace_path, category?, source_id?)` | Summarises and stores a single fact or decision |
| `query_memory(user_query, workspace_path, category?, use_cloud?)` | Retrieves and synthesises an answer |
| `list_memory(workspace_path, category?, limit?)` | Lists stored memories for a workspace |
| `list_workspaces()` | Shows all known workspaces |
| `update_brief(workspace_path, new_content)` | Replaces the project brief |

---

## Auxiliary Scripts

### Wiping Workspace Memory (`drop_memory.py`)

If you misconfigured your `.memignore` or want to completely reset the AI's memory for a specific project, you can use the included `drop_memory.py` script. This script safely deletes the ChromaDB vector collection and the associated `.md` context file for a given workspace.

**Scenario: Starting Fresh**
You ran the initial scan on a new workspace, but realized the AI indexed a massive `logs/` directory because you forgot to add it to `.memignore`. Instead of dealing with duplicate embeddings or irrelevant context, you can wipe the workspace memory completely and start from scratch.

**Usage:**
Run the script from the root of `zerikai_memory`, passing the workspace ID you want to wipe:

```bash
# Windows
.\venv\Scripts\python.exe drop_memory.py the_workspace_id

# macOS / Linux
venv/bin/python drop_memory.py the_workspace_id
```

*(You can find the workspace ID by asking your AI assistant "What workspaces do you know about?" or by checking the `.brain/contexts/` folder).*

---

## DeepSeek KV Cache Optimisation

Caching is automatic — no flags required. The server is structured to maximise hit rate:

* The **system message** = fixed role instruction + stable project brief (identical every call for the same workspace → cached after the first call)
* The **user message** = retrieved context + query (changes every call → never cached, that's fine)

A well-populated 600-token project brief means paying **\$0.028/M** instead of **\$0.28/M** on your largest token block — a 10× saving on every query after the first.

---

## Disclaimer

This tool interacts directly with your local file system, vector databases, and LLM APIs. While every effort has been made to ensure safety (such as strict workspace isolation and deterministic hashing), the authors (Zerikai) are not responsible for any data loss, corruption, API charges, or unintended consequences resulting from the use of this software. Always ensure you have standard backups or version control for your codebase before scanning or dropping memory.

---

## License

MIT
