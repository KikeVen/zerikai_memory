# Status - PLANNED (NOT IMPLEMENTED)

- [x] Add fetch_cap to the config.py and the .env file, and use it in the codebase to limit the number of documents fetched from ChromaDB for reranking.
- [ ] Diff preview in scan_status ("3 files changed since last scan") and a simple collection backup before scan are genuinely good ideas and easy to implement.
- [ ] Persist `last_scanned_at` timestamp per workspace in SQLite registry; expose via `scan_status` output
- [x] Test temperature 0 + explicit "return no answer if uncertain" prompt vs current behavior; measure hallucination rate on poorly documented codebases
- [x] Constant Upgrade: tree-sitter indexes UPPER_CASE constants (Python/JS/TS) with preceding comments as pseudo-docstrings, and markdown checkboxes as searchable entities — config files and todo lists now visible to query_memory.
- [ ] Investigate whether the embedding-docstring skill's 4-line/400-char prose body limit needs a complexity exception (e.g., orchestration/coordinator functions like run_council, run_chairman) without becoming project-specific — must stay generic to the skill (no hardcoded function names or thresholds tied to one codebase). Triggered by 3 size-limit violations found auditing a non-zerikai_memory project where trimming may force real information loss rather than just tightening prose.