# Status - PLANNED (NOT IMPLEMENTED)

- [ ] Lightweight Structural Layer review: embed graph properties directly into ChromaDB's metadata dictionary and track file-level metadata using existing SQLite database (zerikai.db).
- [ ] Local judgement layer with with `jeb:4b` through `Ollaya` test via `test_jev_local_vs_cloud.py`
- [ ] Post-synthesis faithfulness guard (Answer Verification): Planned as `Choice` per citation {verified, unsupported, contradicted, fabricated} plus a `Score` for `grounded`. in `jev\docs\ARCHITECTURE_PROPOSAL.md`
- [ ] Persist `last_scanned_at` timestamp per workspace in SQLite registry; expose via `scan_status` output
- [ ] Investigate whether the embedding-docstring skill's 4-line/400-char prose body limit needs a complexity exception (e.g., orchestration/coordinator functions like run_council, run_chairman) without becoming project-specific — must stay generic to the skill (no hardcoded function names or thresholds tied to one codebase). Triggered by 3 size-limit violations found auditing a non-zerikai_memory project where trimming may force real information loss rather than just tightening prose.
- [ ] Diff preview in scan_status ("3 files changed since last scan") and a simple collection backup before scan are genuinely good ideas and easy to implement.