# Configuration Reference

## Required Setup Sequence

Two steps must happen **before** `scan_workspace` runs. Skipping or reordering them
produces a silently degraded index that is difficult to recover from without wiping
and re-indexing the entire workspace.

```
1.  venv activated + pip install -r requirements.txt
          │
          ▼
2.  .memignore configured
          │
          ▼
3.  Embedding-Docstring Skill run across the workspace
          │
          ▼
4.  init_workspace
          │
          ▼
5.  scan_workspace
```

### Why `.memignore` Must Come First

`scan_workspace` reads `.memignore` as the sole source of truth for what to index.
The `embedding-docstring` skill's Python discovery script reads the same file to
determine which files to audit. Both tools share one exclusion list — but only if
it exists before either runs.

Scanning without `.memignore` indexes `venv/`, `node_modules/`, `__pycache__/`, test
fixtures, and any other noise in your workspace. The only recovery is `drop_memory.py`
followed by a full re-index.

### Why the Embedding-Docstring Skill Must Come Before the Scan

tree-sitter indexes whatever docstrings exist at scan time. The quality of every
`query_memory` answer, and the accuracy of all 9 project brief sections, depends
directly on what those docstrings contain. Docstrings that:

- use generic terms (`"key-value store"`) instead of technology names (`"Redis"`)
- omit routing logic, branching conditions, or decorator context
- miss side effects or guarantee statements
- are absent entirely

...produce embeddings that fail to surface the entity during semantic search. The
retrieval result is degraded silently — no error is raised, answers simply come back
weaker or incomplete.

**Run the skill first. Then scan.** Docstrings fixed after indexing require a full
rescan to take effect.

See [skills/embedding-docstring.md](skills/embedding-docstring.md) for the full
checklist, format reference, and invocation patterns.

---

## `.env` Variables

| Key | Default | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(required)* | API key from [platform.deepseek.com](https://platform.deepseek.com). Required even in `local` mode. |
| `MEMORY_MODE` | `cloud` | `cloud` / `hybrid` / `local` — see mode table below. |
| `ENABLE_TOKEN_TRACKING` | `true` | Tracks usage and cost in `.brain/zerikai.db`. |
| `ENABLE_DEEPSEEK_PRO` | `false` | Enables v4-pro for architectural queries. 6× more expensive — keep `false` unless needed. |
| `QUERY_DISTANCE_THRESHOLD` | `1.0` | L2 distance cutoff for retrieval. Lower = stricter matches. Watch `server.log` to calibrate. |
| `ENABLE_LEXICAL_RERANK` | `false` | Hybrid rerank: boosts results with keyword overlap in entity name + docstring. |
| `LEXICAL_RERANK_WEIGHT` | `0.05` | Per-keyword boost weight. Keep below `0.156` to avoid overriding semantic results. |
| `SKIP_BARE_FILES` | `[]` | Extensions to skip when tree-sitter finds zero entities. E.g. `['.py', '.html', '.md', '.css']`. |

> **Upgrading from an older version?** Replace `SKIP_BARE_PY_FILES=true` with
> `SKIP_BARE_FILES=['.py', '.html', '.md', '.css']`. The boolean toggle is no longer read.

---

## Memory Mode Comparison

> 💡 **Deterministic First:** All high-resolution code parsing (functions, classes, methods) is performed **locally and deterministically** using Tree-Sitter for $0 cost. The engines below are only used for text-file fallbacks and generating the architectural Project Brief.

| Mode | Analysis & Briefs | Query Engine | Cost | Best For |
|---|---|---|---|---|
| `cloud` | DeepSeek | DeepSeek | Low | **Recommended.** No Ollama required. best brief quality. |
| `hybrid` | Ollama | Ollama + DeepSeek (auto) | Lowest | Privacy-sensitive projects. Free local lookups, cloud for architecture. |
| `local` | Ollama | Ollama | $0 | Full local privacy. Lower brief quality. |

---

## `.memignore`

Works like `.gitignore` — one pattern per line, comments with `#`. Both
`scan_workspace` and the `embedding-docstring` skill respect it. Without this file,
both tools run with zero exclusions across the entire workspace.

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

Pattern matching rules (applied by the skill's discovery script):

- Matches against the full relative path and the basename
- Trailing `/` matches directory prefixes (`venv/` excludes everything under `venv/`)
- `fnmatch` glob patterns supported (`*.pyc`, `**/tests/`)
- `.memignore` exclusions override explicit user requests unless the user says
  `"override .memignore for this file"`

---

## Auto-Routing Reference

Routing is automatic. Override explicitly with `use_cloud=True` or `use_cloud=False`.

| Condition | Engine | Cost |
|---|---|---|
| Short, specific query (under 40 words) | Ollama | Free |
| Query ≥ 40 words | DeepSeek v4-flash | ~$0.0028/M cached tokens |
| Contains `refactor`, `architect`, `design`, `audit` | DeepSeek v4-pro | ~$0.003625/M cached tokens |
| `use_cloud=True` override | DeepSeek | Varies |
| `use_cloud=False` override | Ollama | Free |

---

## DeepSeek KV Cache

The project brief is a fixed prefix on every DeepSeek API call. After the first query
it caches at **$0.0028/M tokens** (hit) vs **$0.14/M** (miss) — roughly 50× cheaper.

To protect this prefix:

- Keep `force_refresh_brief=False` during daily development.
- Do not switch between `ENABLE_DEEPSEEK_PRO=true` and `false` unnecessarily —
  v4-pro and v4-flash maintain separate caches.
- The first query of a new session is always a miss. Cache warms on subsequent calls.
- If `get_cache_stats` shows a high miss rate, check whether the brief was recently
  force-refreshed.

---

## Security

```gitignore
# Required .gitignore entries
.env        # Contains DEEPSEEK_API_KEY
.brain/     # Contains local vector DB and project briefs
```

Never commit `.brain/` or `.env`.

All memory, vector data, and API keys stay on your machine. Each project gets its
own isolated ChromaDB sub-collection — queries never cross workspaces.
