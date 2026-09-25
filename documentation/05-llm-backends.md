# LLM Backends

Zerikai Memory routes between two LLM backends automatically based on query
characteristics. You can override routing explicitly on any call.

---

## The Two Backends

### Ollama (Local)

Runs entirely on your machine. Zero API cost. Lower brief synthesis quality than
DeepSeek on complex codebases — particularly for the Architecture, Data Flow, and
Roadmap sections. Suitable for short, specific queries and for privacy-sensitive
projects where no data should leave the machine.

Requires Ollama to be running locally with `mistral:7b` (the `OLLAMA_MODEL`
default) or your configured model
pulled. Verify: `http://127.0.0.1:11434` should respond in a browser.

If `OLLAMA_HOST=0.0.0.0` is set on your system, the server corrects it automatically.
If issues persist, unset it or set it explicitly to `http://127.0.0.1:11434`.

### DeepSeek (Cloud)

Two tiers:

- **deepseek-flash** — handles 99% of queries. Fast, cheap, high quality for
  synthesis. Served by DeepSeek-V4.1-Flash. Keep `ENABLE_DEEPSEEK_PRO=false`.
- **v4-pro** — reserved for major architectural queries when explicitly enabled.
  Served by DeepSeek-V4-Pro-0813. Maintains a separate KV cache from
  deepseek-flash — switching between them resets the cache and raises costs until
  it re-warms.

API key from [platform.deepseek.com](https://platform.deepseek.com). Required in
`.env` even in `local` mode.

#### Thinking Mode

DeepSeek's thinking mode (chain-of-thought) is **ON by default at effort `high`**
when no parameter is sent. In thinking mode the model emits reasoning tokens that
are billed as **output** tokens, and `temperature` / penalty parameters are
silently ignored. Docs:
<https://api-docs.deepseek.com/guides/thinking_mode/>.

Zerikai controls this per call path so extractive work does not burn its token
budget on reasoning:

| Setting | Applies to | Default |
|---|---|---|
| `DEEPSEEK_THINKING_BRIEF` | 9-section project brief generation | `disabled` |
| `DEEPSEEK_THINKING_SCAN` | per-file indexing summaries | `disabled` |
| `DEEPSEEK_THINKING_QUERY` | `query_memory` answer synthesis | `disabled` |
| `DEEPSEEK_REASONING_EFFORT_BRIEF` | brief path when `enabled`: `low` / `high` / `max` | `low` |
| `DEEPSEEK_REASONING_EFFORT_SCAN` | scan path when `enabled`: `low` / `high` / `max` | `low` |
| `DEEPSEEK_REASONING_EFFORT_QUERY` | query path when `enabled`: `low` / `high` / `max` | `low` |

Each path has its own toggle **and** its own effort, so enabling one path never
constrains another.

The toggle is sent in OpenAI format: `thinking: {"type": "disabled"}` inside
`extra_body` when off, or `reasoning_effort` when on. It is **DeepSeek-only** —
the Ollama path never receives it. Briefs and query synthesis are separate
pipelines, so their toggles are deliberately independent.

---

## Auto-Routing Logic

Routing runs automatically on every `query_memory` call. Override with
`use_cloud=True` or `use_cloud=False`.

| Condition | Engine | Cost |
|---|---|---|
| Query under 40 words | Ollama | Free |
| Query ≥ 40 words | DeepSeek deepseek-flash | ~$0.003–$0.006/M cached tokens (off-peak / peak) |
| Contains `refactor`, `architect`, `design`, `audit` | DeepSeek v4-pro | ~$0.022–$0.044/M cached tokens (off-peak / peak) |
| `use_cloud=True` override | DeepSeek | Varies |
| `use_cloud=False` override | Ollama | Free |

The threshold is controlled by `CLOUD_ESCALATION_WORD_COUNT` in `main.py`.

---

## Memory Mode and Backend Scope

> 💡 **Core Parsing:** Note that the heavy lifting for code indexing (Tree-Sitter) is **always local and deterministic**. The engines below handle architectural synthesis (Briefs) and non-code fallbacks.

| Mode | Analysis & Briefs | Query Engine |
|---|---|---|
| `cloud` | DeepSeek | DeepSeek (auto-routed) |
| `hybrid` | Ollama | Ollama + DeepSeek (auto-routed) |
| `local` | Ollama | Ollama only |

Set in `.env` as `MEMORY_MODE`. Start with `cloud` — no Ollama installation needed
and brief quality is highest.

---

## DeepSeek KV Cache & Pricing Tiers

DeepSeek uses **peak / off-peak pricing**. Peak hours (UTC): **01:00–04:00** and
**06:00–10:00, Monday–Friday only**. Weekends are always off-peak. Off-peak rates
are exactly half of peak.

| Tier | deepseek-flash cached | deepseek-flash miss | v4-pro cached | v4-pro miss |
|---|---|---|---|---|
| **Peak** | $0.006/M | $0.30/M | $0.044/M | $1.32/M |
| **Off-peak** | $0.003/M | $0.15/M | $0.022/M | $0.66/M |

The project brief is a fixed prefix on every DeepSeek API call. After the first
query of a session, DeepSeek caches this prefix at the active cached rate —
roughly 50–100× cheaper than a miss.

The first query of every new session is always a miss. The cache warms on
subsequent calls within the same session.

**What resets the cache:**

- Force-refreshing the project brief (`force_refresh_brief=True`)
- Switching between `ENABLE_DEEPSEEK_PRO=true` and `false` — deepseek-flash and
  v4-pro maintain separate caches
- Significant changes to the brief content itself

**Monitor cache health:**

```
"Show cache hit rates"
```

The agent calls `get_cache_stats`. A high miss rate with no recent force-refresh
usually means sessions are short or the brief is being regenerated too frequently.

---

## Running Cost Reference
,
> Monday–Friday only.

| Operation | Engine | Estimated Cost |
|---|---|---|
| File scan (tree-sitter parseable) | Local only | $0.00 |
| File scan (bare/non-parseable) | DeepSeek deepseek-flash | ~$0.000057–$0.000114 / file |
| Brief synthesis (9 sections) | DeepSeek deepseek-flash | ~$0.0010–$0.0021 / full run |
| Routine query (cache hit) | DeepSeek deepseek-flash | $0.003–$0.006/M tokens |
| Architectural query (cache hit) | DeepSeek v4-pro | $0.022–$0.044/M tokens |
| Repeated queries (cache hit vs miss) | DeepSeek KV cache | 50–1044/M tokens |
| Repeated queries (cache hit vs miss) | DeepSeek KV cache | 30–60× cheaper vs. miss |

The real cost is not DeepSeek. It is what your IDE's AI charges every time you
re-explain your codebase — raw file dumps, re-pasted architecture docs, repeated
decisions — burned from your monthly quota and context window simultaneously.
Zerikai eliminates that tax on both sides.
