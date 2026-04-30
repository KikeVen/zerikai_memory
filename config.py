import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Fallback when auto-router doesn't escalate to cloud
DEFAULT_MEMORY_MODE = os.getenv("DEFAULT_MEMORY_MODE", "local")

# DeepSeek — OpenAI-compatible
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Use v4-flash for general synthesis (fast, cheap, good cache hit rate).
# Swap to deepseek-v4-pro for maximum reasoning on complex architectural queries.
# NOTE: deepseek-chat / deepseek-reasoner are legacy aliases retiring July 24 2026.
DEEPSEEK_MODEL_FAST = "deepseek-v4-flash"
DEEPSEEK_MODEL_PRO  = "deepseek-v4-pro"

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


# Auto-routing thresholds
# Queries over this word count are escalated to cloud
CLOUD_ESCALATION_WORD_COUNT = 40

# Keywords that always trigger cloud synthesis regardless of length
CLOUD_ESCALATION_KEYWORDS = {
    "refactor", "architect", "architecture", "design", "redesign",
    "migrate", "migration", "strategy", "tradeoff", "trade-off",
    "structure", "pattern", "review", "audit", "compare", "alternative",
}
