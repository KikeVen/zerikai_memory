## Future: Full hybrid search

* BM25 + vectors — needs a separate index (SQLite FTS5 or Whoosh)
* Cross-encoder re-ranker — feed (query, document) pairs to a small model
* Language filter — exclude results where meta["language"] ≠ query language
* Tiered scoring — hits >= 2 → stronger boost; hits == 0 → mild penalty
