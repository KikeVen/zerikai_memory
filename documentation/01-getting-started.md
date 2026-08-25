# Getting Started

## One-Time Installation

### 1 — Clone & Install

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

Python 3.11+ is required.

### 2 — Configure `.env`

Copy `.env.example` to `.env` and set your key:

```env
DEEPSEEK_API_KEY=your_deepseek_key_here

MEMORY_MODE=cloud
ENABLE_TOKEN_TRACKING=true
ENABLE_DEEPSEEK_PRO=false
QUERY_DISTANCE_THRESHOLD=1.0
ENABLE_LEXICAL_RERANK=true
LEXICAL_RERANK_WEIGHT=0.05
SKIP_BARE_FILES=['.py', '.html', '.md', '.css']
```

Start with `MEMORY_MODE=cloud`. No Ollama installation needed.
See [configuration-reference.md](configuration-reference.md) for all options.

### 3 — Register Your IDE

The MCP server runs as a STDIO process launched once when the IDE starts.
All paths must be **absolute** — relative paths cause startup failures.

**VS Code (Copilot / Cline)**
`Ctrl+Shift+P` → MCP: Add Local Server → STDIO

Command:

```
C:\path\to\zerikai_memory\venv\Scripts\python.exe C:\path\to\zerikai_memory\main.py
```

**Cursor** — add to `.cursor/mcp.json`:

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

**Claude Desktop**

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

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

### 4 — Configure Agent Rules

> Without agent rules, your IDE's AI ignores `universal-brain` and falls back to raw file searches.

Add these two rules to your IDE's agent/system instructions:

**Universal-Brain First** — the agent must call `query_memory` before any raw file search, and must state if it escalated beyond memory.

**Source Discipline** — every answer must surface `file.py:line` + confidence score. No fabricated answers — if memory has nothing, say so.

Full rule text: [`agent_rules/ide_agent_rules.md`](../agent_rules/ide_agent_rules.md)

### 5 — Verify

```bash
python -c "from main import scan_workspace, query_memory; print('OK')"
```

You should see the startup banner followed by `OK`.

---

## Upgrading

Upgrade **in place** — never copy, rename, clone, or move the project directory.
Your workspace UUID is derived from its absolute filesystem path, so changing the
path generates a new UUID and orphans your existing collections and briefs. Because
`.brain/` is gitignored, `git pull` never touches your memory.

The whole point is to **preserve your already-indexed `.brain/` data** — back it up,
upgrade the code, restore it, and you get your old memory back exactly as it was.
No re-indexing needed.

```bash
# 1. Stop the MCP server (close the IDE / kill main.py) — releases the ChromaDB lock

# 2. Back up your existing indexed data so you don't lose it
#    (do NOT rename the whole project — only back up .brain/)
#    Windows (PowerShell):  Copy-Item -Recurse .brain .brain.bak
#    macOS / Linux:         cp -r .brain .brain.bak

# 3. Pull the latest code
git pull

# 4. Reinstall dependencies only if requirements.txt changed
#    Windows:  .\venv\Scripts\python.exe -m pip install -r requirements.txt
#    macOS/Linux:  venv/bin/python -m pip install -r requirements.txt

# 5. Restore .brain/ (only needed if the upgrade replaced it), restart, and verify
#    the workspace still resolves to the same UUID — your old memory is back
```

See [09-upgrading.md](09-upgrading.md) for the full guide, including backup/restore
and recovery via `merge_workspaces` if you already moved the project.

---

## Per-Project Setup

Run this sequence for every new project, and again after any major refactor.
**Order matters — do not skip or reorder steps.**

```
1. Configure .memignore
2. Run the Embedding-Docstring Skill
3. Register the project  →  init_workspace
4. Scan & index          →  scan_workspace
```

### Step 1 — Configure `.memignore`

> **Do this before anything else.** Scanning without `.memignore` indexes the wrong
> directories. The only fix is `drop_memory.py` and a full re-index.

`.memignore` works like `.gitignore` — one pattern per line. `scan_workspace` reads
it and skips matching paths. The `embedding-docstring` skill also reads it and skips
matching files. Both tools share the same file as the single source of truth for
what is in scope.

Recommended baseline:

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

### Step 2 — Run the Embedding-Docstring Skill

> **Do this before `scan_workspace`.** tree-sitter indexes whatever docstrings exist
> at scan time. Fixing docstrings after indexing requires a full rescan to take effect.

The quality of every `query_memory` answer depends directly on the quality of the
docstrings indexed into ChromaDB. Vague docstrings, missing technology names, and
undocumented routing logic produce a silently degraded index — queries return weaker
answers with no indication that the source material was thin.

Tell your assistant:

```
"Audit and optimise docstrings across this project using the embedding-docstring skill,
respecting .memignore."
```

The skill flags violations with line numbers and proposes before/after diffs.
Nothing is written without your confirmation.

See [skills/02-embedding-docstring.md](skills/02-embedding-docstring.md) for the full
density checklist and format reference.

### Step 3 — Register the Project

```
"Set up memory for this project"
```

The assistant calls `init_workspace`. It registers the folder, assigns a stable UUID,
and creates a pending brief at `.brain/contexts/<workspace_id>.md`. Idempotent — safe
to run multiple times.

### Step 4 — Scan & Index

```
"Scan and index the workspace."
```

`scan_workspace` returns immediately and runs in the background. The assistant polls
`scan_status` to track progress. Four concurrent workers parse code entities via
tree-sitter, batch-upsert to ChromaDB in a single call, then synthesize the
9-section project brief.

### Force Brief Refresh (Only When Needed)

Normal scans do **not** regenerate the brief — this protects your DeepSeek KV cache
prefix. Only force a refresh after a major architectural change:

```
"Rescan the workspace and force a refresh of the project brief."
```

> Force-refreshing the brief resets the DeepSeek KV cache. Every query pays full
> miss-rate pricing until the cache warms again. Treat it like a schema migration.
