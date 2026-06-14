# Status - PLANNED (NOT IMPLEMENTED)

- [ ] Persist `last_scanned_at` timestamp per workspace in SQLite registry; expose via `scan_status` output
- [ ] Add fetch_cap to the config.py and the .env file, and use it in the codebase to limit the number of documents fetched from ChromaDB for reranking.
- [ ] Test temperature 0 + explicit "return no answer if uncertain" prompt vs current behavior; measure hallucination rate on poorly documented codebases
- [ ] Diff preview in scan_status ("3 files changed since last scan") and a simple collection backup before scan are genuinely good ideas and easy to implement.
- [ ] Full hybrid search, BM25 index + reciprocal rank fusion to replace keyword-overlap reranking.