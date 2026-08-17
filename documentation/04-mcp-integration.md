# MCP Integration

## How the MCP Server Works

Zerikai Memory runs as a **STDIO-based Model Context Protocol (MCP) server**. It is
launched once when the IDE starts and stays running for the entire session. The IDE's
AI agent communicates with it over standard input/output — no network port, no HTTP.

You never call MCP tools directly. You write natural language instructions to your
IDE agent and it calls the appropriate tool automatically. Workspace identity is
automatic: the IDE attaches the active workspace path to every message, and the
server maps it to a stable UUID via its SQLite workspace registry.

---

## IDE Registration

All paths must be **absolute**. Relative paths cause startup failures.

### VS Code (Copilot / Cline)

`Ctrl+Shift+P` → **MCP: Add Local Server** → STDIO

Command (Windows):
```
C:\path\to\zerikai_memory\venv\Scripts\python.exe C:\path\to\zerikai_memory\main.py
```

Command (macOS / Linux):
```
/path/to/zerikai_memory/venv/bin/python /path/to/zerikai_memory/main.py
```

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

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

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

### Google Antigravity

Edit `mcp_config.json` directly:
```json
"universal-brain": {
  "command": "C:\\path\\to\\zerikai_memory\\venv\\Scripts\\python.exe",
  "args": ["C:\\path\\to\\zerikai_memory\\main.py"],
  "disabled": false
}
```

---

## Agent Rules

> **Configure these immediately after IDE registration.** Without agent rules, your
> IDE's AI ignores `universal-brain` and falls back to raw file searches.

Add these two rules to your IDE's agent/system instructions. Full rule text:
[`agent_rules/ide_agent_rules.md`](../agent_rules/ide_agent_rules.md)

| Rule | What It Enforces |
|---|---|
| **Universal-Brain First** | Agent queries `query_memory` before any raw file search. Must state if it escalated beyond memory. |
| **Source Discipline** | Every answer surfaces `file.py:line` + confidence score. No fabricated answers — if memory has nothing, it says so. |

---

## MCP Tools Reference

You never call these directly. The agent calls them from your natural language.

### Workspace Management

| Tool | Description |
|---|---|
| `init_workspace` | Registers a project folder, assigns a stable UUID, creates a pending brief. Idempotent. |
| `list_workspaces` | Lists all known workspaces with a brief or stored memories. |
| `resolve_workspace` | Resolves UUID, short-UUID, or display name to filesystem path. |
| `merge_workspaces` | Consolidates duplicate workspace IDs. **Irreversible.** |
| `debug_workspace_id` | Diagnostic: shows what UUID a given path would generate. |

### Memory & Briefs

| Tool | Description |
|---|---|
| `scan_workspace` | Background scan. Returns immediately; poll `scan_status` to track progress. Idempotent and self-cleaning. |
| `scan_status` | Progress of a running or completed scan: files, entities, errors, elapsed, brief status. |
| `save_to_memory` | Manually saves a decision, fact, or note with an optional category tag. |
| `list_memory` | Lists stored memories, optionally filtered by category. |
| `query_memory` | Vector search + LLM synthesis (auto-routed). Returns plain-text answer with inline `#file:line | score` citations plus a `Sources:` block. |
| `get_brief` | Retrieves the current project brief from `.brain/contexts/`. |
| `update_brief` | Manually overwrites the markdown content of a project brief. No versioning. |

### Usage & Diagnostics

| Tool | Description |
|---|---|
| `get_token_usage` | DeepSeek token usage and cost statistics. |
| `get_cost_report` | Cost breakdown by operation type. |
| `get_cache_stats` | Cache hit/miss rates by operation type. |
| `purge_usage_data` | Deletes historical token tracking records. **Irreversible.** |

---

## Natural Language Command Reference

| You say | Tool called | Engine |
|---|---|---|
| *"Remember that we're using Redis for session caching"* | `save_to_memory` | Local |
| *"What did we decide about auth?"* | `query_memory` | Ollama (auto-routed) |
| *"Refactor the data layer, what are our constraints?"* | `query_memory` | DeepSeek (auto-escalated) |
| *"List what's in memory for this project"* | `list_memory` | Local |
| *"What projects do you know about?"* | `list_workspaces` | Local |
| *"Show me the project brief"* | `get_brief` | Local |
| *"Scan the workspace"* | `scan_workspace` | Background |

---

## Source Citations

Every `query_memory` response includes inline `#file:line | score L2 or rerank`
citations in the answer body plus a trailing `Sources:` block of
`file:line — score (label)` lines — plain text, cross-IDE compatible, clickable in
VS Code Copilot. This metadata is stored at scan time. No extra API cost.

---

## Logs

All activity is written to `.brain/server.log` — 5 MB rotating cap, 2 rolling backups.

```bash
# macOS / Linux — live tail
tail -f .brain/server.log

# macOS / Linux — errors only
grep "ERROR" .brain/server.log
```

```powershell
# Windows — live tail
Get-Content .brain\server.log -Wait -Tail 30

# Windows — errors only
Select-String -Path .brain\server.log -Pattern "ERROR"
```
