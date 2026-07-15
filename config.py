"""Configuration hub for the zerikai_memory MCP server.
Defines all environment-variable-driven settings via python-dotenv:
DeepSeek API credentials (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL), Ollama
connection (OLLAMA_HOST, OLLAMA_MODEL), pricing tables (DEEPSEEK_PRICING),
routing thresholds (CLOUD_ESCALATION_WORD_COUNT), ChromaDB settings
(QUERY_DISTANCE_THRESHOLD, FETCH_CAP), and lexical re-rank configuration.
Loaded once at import time. Side effect: reads os.environ and creates
DB_PATH directory.
"""

import ast
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Controls which LLM is used for memory operations: "cloud" uses DeepSeek
# for everything, "hybrid" uses Ollama for scanning with DeepSeek for
# briefs/queries, "local" uses Ollama only. Reads from environment with
# fallback to "cloud" when not set.
MEMORY_MODE = os.getenv("MEMORY_MODE", "cloud")

# DEFAULT_MEMORY_MODE mirrors MEMORY_MODE env var. Controls whether
# query_memory auto-routes to DeepSeek cloud. Read-only after import.
DEFAULT_MEMORY_MODE = MEMORY_MODE
# SYNTHESIZE_WITH_CLOUD gates brief synthesis to DeepSeek when True.
# True for cloud and hybrid modes; hybrid scanning still uses Ollama.
SYNTHESIZE_WITH_CLOUD = MEMORY_MODE in ("cloud", "hybrid")

# DeepSeek — OpenAI-compatible API key. Reads from DEEPSEEK_API_KEY env
# var. Required for cloud synthesis via ds_client.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
# DeepSeek OpenAI-compatible REST API endpoint. Points to
# https://api.deepseek.com. Used by ds_client for all cloud calls.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Use v4-flash for general synthesis (fast, cheap, good cache hit rate).
# NOTE: deepseek-chat is a legacy alias retiring July 24 2026.
DEEPSEEK_MODEL_FAST = "deepseek-v4-flash"
# Use deepseek-v4-pro for maximum reasoning on complex architectural
# queries (architecture, design, tradeoffs). 75% off until May 31 2026.
# NOTE: deepseek-reasoner is a legacy alias retiring July 24 2026.
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"

# Enable deepseek-v4-pro for complex queries (architecture, design, tradeoffs)
# If False, always uses v4-flash (cheaper, 3x cost savings now, 6x after May 31 2026)
ENABLE_DEEPSEEK_PRO = os.getenv(
    "ENABLE_DEEPSEEK_PRO", "false").lower() == "true"

# Local Ollama model for summarisation (always free, always local)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")
_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
if _host == "0.0.0.0":
    _host = "http://127.0.0.1:11434"
elif not _host.startswith("http"):
    _host = f"http://{_host}:11434"
# Normalised Ollama server URL. Handles bare hostnames (adds http://
# and :11434), maps 0.0.0.0 to 127.0.0.1 for local-only binding.
# Falls back to http://127.0.0.1:11434 when OLLAMA_HOST is unset.
OLLAMA_HOST = _host

# Storage root — all workspace data lives here
# Resolves to zerikai_memory/.brain/ — matches the project structure spec exactly.
# vector_db/ and contexts/ are sub-directories created on demand.
DB_PATH = Path(__file__).parent / ".brain"

# Database configuration
# zerikai.db stores token tracking, workspace registry, and other persistent state
ENABLE_TOKEN_TRACKING = os.getenv(
    "ENABLE_TOKEN_TRACKING", "true").lower() == "true"
# Path to the persistent sqlite3 database for token tracking, workspace
# registry, and cost reporting. Created on first _init_db() call.
ZERIKAI_DB = DB_PATH / "zerikai.db"

# DeepSeek pricing (USD per 1M tokens) - verified May 1, 2026
# Source: https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_PRICING = {
    # deepseek-v4-flash (primary model for general synthesis)
    "v4-flash": {
        "input": 0.14,       # Cache miss
        "output": 0.28,
        "cache_hit": 0.0028,  # 50x cheaper than cache miss (reduced 4/26/2026)
    },
    # deepseek-v4-pro (complex architectural queries)
    "v4-pro": {
        "input": 0.435,      # Currently 75% off until 2026/05/31, then $1.74
        "output": 0.87,      # Currently 75% off until 2026/05/31, then $3.48
        "cache_hit": 0.003625,  # Currently 75% off until 2026/05/31, then $0.0145
    },
    # NOTE: v4-pro pricing increases ~4x after May 31, 2026 when discount expires
}


# Auto-routing thresholds
# Queries over this word count are escalated to cloud
CLOUD_ESCALATION_WORD_COUNT = 40

# Keywords that always trigger cloud synthesis regardless of length
CLOUD_ESCALATION_KEYWORDS = {
    "refactor", "architect", "architecture", "design", "redesign",
    "migrate", "migration", "strategy", "tradeoff", "trade-off",
    "structure", "pattern", "review", "audit", "compare", "alternative",
}

# Semantic search relevance threshold for query_memory.
# ChromaDB returns L2 distances: 0 = identical, higher = less similar.
# Results above this threshold are considered too dissimilar and dropped.
# If ALL results are dropped, the tool returns "I don't know" instead of
# hallucinating an answer from model priors.
# Tune this by watching "best dist=X.XX" in server.log.
# Typical ranges: <0.8 strong match, 0.8-1.5 related, >1.5 noise.
QUERY_DISTANCE_THRESHOLD = float(os.getenv("QUERY_DISTANCE_THRESHOLD", "1.5"))

# File extensions to skip during scanning when tree-sitter produces zero
# entities. Saves API calls on files with no extractable functions, classes,
# or structural elements. Format: ['.py', '.html', '.md', '.css']
# Default: [] (empty — no extensions skipped)
SKIP_BARE_FILES = set(ast.literal_eval(os.getenv("SKIP_BARE_FILES", "[]")))

# Lexical re-ranking in query_memory
# When enabled, results are reordered by: (1/dist) + (hits * weight).
# Pure reorder — nothing below threshold is dropped.
ENABLE_LEXICAL_RERANK = os.getenv(
    "ENABLE_LEXICAL_RERANK", "false").lower() == "true"
# Weight per keyword-match hit in the lexical re-rank formula.
# Default 0.05 — small enough to nudge within the 1/dist spread
# without overriding semantic distance. Reads from env.
LEXICAL_RERANK_WEIGHT = float(os.getenv("LEXICAL_RERANK_WEIGHT", "0.05"))

# Maximum number of documents to fetch from ChromaDB before applying lexical reranking.
# A wider pool allows reranking to pull in keyword-relevant files that might be semantically distant.
# Does NOT control the final answer size — see the fixed top-5 (`relevant = relevant[:5]`) cutoff applied after reranking in main.py.
FETCH_CAP = int(os.getenv("FETCH_CAP", "75"))

# Local Ollama concurrency limit.
# Gates parallel brief synthesis sections to prevent local VRAM thrashing.
# Default 1 is recommended for 8GB cards; raise if you have more headroom.
OLLAMA_MAX_CONCURRENCY = int(os.getenv("OLLAMA_MAX_CONCURRENCY", "1"))
