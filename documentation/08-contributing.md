# Contributing

## Code Conventions

### Naming

- Private functions and methods: underscore prefix — `_extract_markdown`, `_build_section`
- Module-level constants: `UPPER_CASE` with underscores — `MD_LANG`, `QUERY_DISTANCE_THRESHOLD`
- Classes: PascalCase — `LanguageConfig`, `CodeEntity`

### Docstring Style

Zerikai Memory uses the `embedding-docstring` standard for all Python, JavaScript,
TypeScript, and HTML files. This is not a style preference — it is a functional
requirement. Docstrings are indexed into ChromaDB and retrieved during queries. A
docstring that does not pass the density checklist produces a weaker embedding and
degrades retrieval for every query that touches that entity.

**Before submitting any change:**

1. Run the `embedding-docstring` skill on any file you modified:
   ```
   "Fix docstrings in <filename> using the embedding-docstring skill"
   ```
2. Approve fixes per the skill's entity-by-entity approval loop.

See [skills/02-embedding-docstring.md](skills/02-embedding-docstring.md) for the full
checklist and format reference.

### Key rules at a glance

- Prose body: 6 lines or 400 characters maximum, whichever is shorter.
- No blank lines anywhere in the docstring.
- Name every library explicitly — `"ChromaDB"` not `"vector store"`, `"tree-sitter"`
  not `"parser"`, `"DeepSeek v4-flash"` not `"LLM"`.
- Decorated functions must name the decorator in the docstring prose.
- Flask routes must state the HTTP method and path.
- Every module-level `UPPER_CASE` constant needs its own `#` comment block
  immediately above it.

---

## Pull Requests

> This project is provided for personal use and reference. Pull Requests and code
> contributions are not being accepted at this time. AI-generated PRs will be
> closed and users may be blocked.

Visit [zerikai.com](http://zerikai.com) for more.

---

## File Organization

```
zerikai_memory/
├── main.py              # MCP server entry point and tool definitions
├── code_indexer.py      # tree-sitter parsing, CodeEntity, LanguageConfig
├── config.py            # .env loader, DB_PATH, all configuration constants
├── drop_memory.py       # Workspace wipe utility
├── requirements.txt
├── .env.example
├── .memignore           # Exclusion list for scan_workspace and embedding-docstring
├── agent_rules/
│   └── ide_agent_rules.md
├── embedding-docstring/
│   └── SKILL.md         # Embedding-Docstring skill — do not rename or relocate
├── documentation/       # This directory
└── .brain/              # Runtime data — never commit
    ├── contexts/        # Per-workspace project briefs
    ├── zerikai.db       # SQLite workspace registry and token tracking
    └── server.log       # Rotating server log
```

---

## Environment

- Python 3.11+
- Virtual environment required (`venv/`) — do not install into system Python
- `DEEPSEEK_API_KEY` required in `.env` even in `local` mode
- `.brain/` and `.env` must be in `.gitignore` — never commit either
