# Architecture Overview

## What Zerikai Memory Does

Indexes your codebase once into a local vector store. Every subsequent query retrieves
only the relevant context — no file dumps, no re-explanation, no duplicated tokens.
Memory is shared across all IDEs from a single `.brain/` directory.

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

---

## System Layers

### 1 — MCP Interface Layer (`main.py`)

STDIO-based Model Context Protocol server. Launched once when the IDE starts and
stays running. Routes natural language commands from the IDE agent to the internal
pipeline. You never call tools directly — the agent calls them based on your
natural language.

### 2 — Code Parsing & Indexing Layer (`code_indexer.py`)

tree-sitter grammars extract functions, classes, constants, and their docstrings
from source files into structured `CodeEntity` objects. Supports Python, JavaScript,
TypeScript, and HTML. Entities with no parseable structure (bare files) are routed
to the **Analysis Engine** (defined by `MEMORY_MODE`) for LLM-based extraction if 
`SKIP_BARE_FILES` does not exclude their extension.

### 3 — Vector Storage Layer (`.brain/`)

Local ChromaDB vector store. Each workspace gets its own sub-collection, isolated
by a deterministic UUID derived from the workspace path. Workspace identity is
automatic — the IDE attaches the active path to every message; the server maps it
to the UUID via a SQLite workspace registry (`zerikai.db`). Re-scanning is
idempotent: deterministic MD5 IDs mean re-scanned entities overwrite, never
duplicate.

### 4 — Retrieval & Reranking Layer

Hybrid search pipeline:

1. ChromaDB L2 distance retrieval — top-N candidates by vector similarity.
2. Distance threshold filter (`QUERY_DISTANCE_THRESHOLD`) — drops results beyond
   the configured cutoff.
3. Optional lexical rerank (`ENABLE_LEXICAL_RERANK`) — boosts candidates with
   keyword overlap in entity name + docstring. Pure reorder — nothing is dropped.
   Keep `LEXICAL_RERANK_WEIGHT` below `0.156` to avoid overriding semantic results.

### 5 — LLM Synthesis Layer

Auto-routes between Ollama (local, free) and DeepSeek (cloud, paid):

- Queries under 40 words → Ollama via `_query_ollama()`
- Queries 40+ words → DeepSeek v4-flash
- Queries containing `refactor`, `architect`, `design`, `audit` keywords → DeepSeek
  v4-pro (only if `ENABLE_DEEPSEEK_PRO=true`)
- Override anytime with `use_cloud=True` or `use_cloud=False`

---

## Key Files & Directories

| Path | Role |
|---|---|
| `main.py` | MCP server entry point. Workspace lifecycle, tool definitions, query routing. |
| `code_indexer.py` | Core parsing. `LanguageConfig` and `CodeEntity` classes. tree-sitter extraction for all supported languages. |
| `config.py` | Configuration loader. Sets `DB_PATH` to `.brain/`. Reads all `.env` variables. |
| `.brain/` | Runtime data directory. ChromaDB collections, project briefs, workspace registry, logs. Never commit. |
| `.brain/contexts/<id>.md` | Auto-generated project brief for each workspace. Fixed prefix on every DeepSeek API call. |
| `.brain/zerikai.db` | SQLite registry. Maps workspace paths to UUIDs. Tracks token usage and cost. |
| `.brain/server.log` | Rotating log (5 MB cap, 2 backups). All events, auto-route decisions, errors. |
| `.memignore` | Single source of truth for what to exclude from indexing and docstring audits. |
| `drop_memory.py` | Wipes a workspace's ChromaDB collection, brief, and registry entry. Irreversible. |
| `agent_rules/ide_agent_rules.md` | Universal-Brain First and Source Discipline rules for IDE agent configuration. |
| `documentation/` | This directory. Topic-specific reference docs for contributors and operators. |

---

## Project Brief

The brief is the foundation of every query. It is synthesized from the indexed codebase
across 9 sections in parallel via `asyncio.gather`, using up to 75 ChromaDB nodes per
section. Target size: **1,000–1,200 tokens** — the sweet spot between cache stability
and retrieval accuracy.

| # | Section | Content |
|---|---|---|
| 1 | Overview | Project type, purpose, domain |
| 2 | Technical Stack | Backend, database, integrations, libraries |
| 3 | Core Architecture | Frontend, backend, data/processing layers |
| 4 | Primary Conventions | Code style, error handling, schema rules |
| 5 | Purpose | Business problem and objectives |
| 6 | Key Files & Directories | Entry points, routers, their roles |
| 7 | Development & Testing | Setup, run, test, deployment |
| 8 | Data Flow & Request Lifecycle | Entry point to data layer trace |
| 9 | Future Roadmap | Planned features and TODOs from code |

The brief is the stable prefix that DeepSeek caches across all queries. After the
first query it caches at **$0.0028/M tokens** (hit) vs. **$0.14/M** (miss).
Force-refreshing resets that cache — treat it like a schema migration.

---

## The 4-Stage Query Pipeline

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

Citations are stored at scan time — no extra API cost.
