# Status - PLANNED (NOT IMPLEMENTED)

- [ ] Test Rule 3 (Citation-Strength Gating, `ide_agent_rules.md`) on VS Code/Copilot, pi.dev, and Claude Desktop — currently only confirmed working in Antigravity (Gemini 3.1 Pro).
- [ ] Run the "stale-but-confident document" test case from `memory-synthesis-reliability-spec.md` Section 6 — closest analog to the original import-extraction incident, not yet tested (existing tests only cover weak/missing citations, not a wrong-but-well-cited source).
- [x] Add fetch_cap to the config.py and the .env file, and use it in the codebase to limit the number of documents fetched from ChromaDB for reranking.
- [ ] Diff preview in scan_status ("3 files changed since last scan") and a simple collection backup before scan are genuinely good ideas and easy to implement.
- [ ] Persist `last_scanned_at` timestamp per workspace in SQLite registry; expose via `scan_status` output
- [ ] Test temperature 0 + explicit "return no answer if uncertain" prompt vs current behavior; measure hallucination rate on poorly documented codebases
- [ ] Full hybrid search, BM25 index + reciprocal rank fusion to replace keyword-overlap reranking.
