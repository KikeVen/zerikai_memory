import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Memory Mode controls which LLM is used for operations:
# - "cloud": Use DeepSeek for all operations (scan, brief, queries)
# - "hybrid": Use Ollama for file scanning, DeepSeek for briefs and escalated queries
# - "local": Use Ollama for everything (free, but lower quality briefs)
MEMORY_MODE = os.getenv("MEMORY_MODE", "cloud")

# Aliases used by main.py
DEFAULT_MEMORY_MODE = MEMORY_MODE
SYNTHESIZE_WITH_CLOUD = MEMORY_MODE in ("cloud", "hybrid")

# DeepSeek — OpenAI-compatible
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Use v4-flash for general synthesis (fast, cheap, good cache hit rate).
# Swap to deepseek-v4-pro for maximum reasoning on complex architectural queries.
# NOTE: deepseek-chat / deepseek-reasoner are legacy aliases retiring July 24 2026.
DEEPSEEK_MODEL_FAST = "deepseek-v4-flash"
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"

# Enable deepseek-v4-pro for complex queries (architecture, design, tradeoffs)
# If False, always uses v4-flash (cheaper, 3x cost savings now, 6x after May 31 2026)
ENABLE_DEEPSEEK_PRO = os.getenv(
    "ENABLE_DEEPSEEK_PRO", "false").lower() == "true"

# Local Ollama model for summarisation (always free, always local)
OLLAMA_MODEL = "llama3.2:latest"
_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
if _host == "0.0.0.0":
    _host = "http://127.0.0.1:11434"
elif not _host.startswith("http"):
    _host = f"http://{_host}:11434"
OLLAMA_HOST = _host

# Storage root — all workspace data lives here
# Resolves to zerikai_memory/.brain/ — matches the project structure spec exactly.
# vector_db/ and contexts/ are sub-directories created on demand.
DB_PATH = Path(__file__).parent / ".brain"

# Database configuration
# zerikai.db stores token tracking, workspace registry, and other persistent state
ENABLE_TOKEN_TRACKING = os.getenv(
    "ENABLE_TOKEN_TRACKING", "true").lower() == "true"
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

# When True, .py files that produce zero tree-sitter entities (no functions or
# classes found) are skipped during scanning instead of falling through to LLM
# summarisation. Saves DeepSeek API calls on files like admin.py, urls.py,
# settings.py, wsgi.py that have only variable assignments and registration calls.
# Default: False (existing behaviour — all such files are LLM-summarised).
SKIP_BARE_PY_FILES = os.getenv("SKIP_BARE_PY_FILES", "false").lower() == "true"
