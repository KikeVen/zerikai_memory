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
import datetime
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

# Use deepseek-flash for general synthesis (fast, cheap, good cache hit rate).
# Served by DeepSeek-V4.1-Flash. NOTE: "deepseek-v4-flash" is a retired legacy
# alias still accepted but billed at Flash price — prefer the canonical name.
DEEPSEEK_MODEL_FAST = "deepseek-flash"
# Use deepseek-v4-pro for maximum reasoning on complex architectural
# queries (architecture, design, tradeoffs). Served by DeepSeek-V4-Pro-0813.
# NOTE: "deepseek-reasoner" is a legacy alias retiring July 24 2026.
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"

# Enable deepseek-v4-pro for complex queries (architecture, design, tradeoffs)
# If False, always uses deepseek-flash (cheaper — pro is ~4x the Flash rate)
ENABLE_DEEPSEEK_PRO = os.getenv(
    "ENABLE_DEEPSEEK_PRO", "false").lower() == "true"

# ─── DeepSeek Thinking Mode ──────────────────────────────────────────────────
# Docs: https://api-docs.deepseek.com/guides/thinking_mode/
# When no thinking parameter is sent, DeepSeek defaults to thinking ENABLED at
# effort "high". "disabled" skips chain-of-thought (right for extractive work:
# briefs and scan summaries); "enabled" keeps it and honours the matching
# DEEPSEEK_REASONING_EFFORT_* value. Each call path has its own toggle AND its
# own effort on purpose — briefs and query synthesis are separate pipelines.
_VALID_THINKING = {"enabled", "disabled"}
_VALID_EFFORT = {"low", "high", "max"}


def _read_thinking(env_name: str, default: str = "disabled") -> str:
    """Read and validate a thinking-mode env var.

    Returns 'enabled' or 'disabled'; falls back to `default` on unknown values.
    Pure — reads os.environ only.
    """
    value = os.getenv(env_name, default).strip().lower()
    return value if value in _VALID_THINKING else default


def _read_effort(env_name: str, default: str = "low") -> str:
    """Read and validate a reasoning-effort env var.

    Returns 'low', 'high', or 'max'; falls back to `default` on unknown values.
    Pure — reads os.environ only.
    """
    value = os.getenv(env_name, default).strip().lower()
    return value if value in _VALID_EFFORT else default


# Per-path DeepSeek thinking toggles (enabled | disabled) and matching reasoning
# effort (low | high | max). Each call path is independently tunable so that
# enabling one does not silently constrain the others.
DEEPSEEK_THINKING_BRIEF = _read_thinking("DEEPSEEK_THINKING_BRIEF", "disabled")
DEEPSEEK_THINKING_SCAN = _read_thinking("DEEPSEEK_THINKING_SCAN", "disabled")
DEEPSEEK_THINKING_QUERY = _read_thinking("DEEPSEEK_THINKING_QUERY", "disabled")
DEEPSEEK_REASONING_EFFORT_BRIEF = _read_effort("DEEPSEEK_REASONING_EFFORT_BRIEF", "low")
DEEPSEEK_REASONING_EFFORT_SCAN = _read_effort("DEEPSEEK_REASONING_EFFORT_SCAN", "low")
DEEPSEEK_REASONING_EFFORT_QUERY = _read_effort("DEEPSEEK_REASONING_EFFORT_QUERY", "low")


def deepseek_thinking_kwargs(mode: str, effort: str = "low") -> dict:
    """Return OpenAI-format thinking kwargs for a DeepSeek chat call.

    mode: one of DEEPSEEK_THINKING_BRIEF / _SCAN / _QUERY (an 'enabled'/'disabled'
    string). effort: the matching DEEPSEEK_REASONING_EFFORT_* value.
    disabled → {"extra_body": {"thinking": {"type": "disabled"}}};
    enabled → {"reasoning_effort": <effort>}.
    Spread into ds_client.chat.completions.create(**kwargs). Pure, deterministic.
    """
    if mode == "disabled":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": effort}


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

# Master switch for token and cost tracking. When true, query_memory and scan
# operations record per-call token usage and cost rows in zerikai.db; when false
# every tracking helper returns immediately and no rows are written.
# Valid values: true/false (case-insensitive). Reads ENABLE_TOKEN_TRACKING from
# the environment, defaulting to true. Consumed by main.py._init_db and the
# token-tracking helpers.
ENABLE_TOKEN_TRACKING = os.getenv(
    "ENABLE_TOKEN_TRACKING", "true").lower() == "true"
# Path to the persistent sqlite3 database for token tracking, workspace
# registry, and cost reporting. Created on first _init_db() call.
ZERIKAI_DB = DB_PATH / "zerikai.db"

# ─── DeepSeek Pricing (USD per 1M tokens) ────────────────────────────────────
# Source: https://api-docs.deepseek.com/quick_start/pricing
# Updated: 2026-09-14
#
# Peak hours (UTC): 01:00–04:00 and 06:00–10:00, Monday–Friday only.
# Weekends are always off-peak. Off-peak rates are exactly half of peak.
#
# ⚠️  Pricing is now TIME-DEPENDENT. Use get_deepseek_pricing() at call time
#     instead of referencing DEEPSEEK_PRICING directly.

# DeepSeek peak-pricing windows in UTC as half-open [start, end) hour tuples,
# Monday–Friday only — weekends are always off-peak. Each entry is
# (start_hour, end_hour) with hours in 0–23. Consumed by is_deepseek_peak_hour()
# to select the peak or off-peak tier in DEEPSEEK_PRICING.
DEEPSEEK_PEAK_WINDOWS_UTC = [
    (1, 4),   # 01:00–04:00 UTC
    (6, 10),  # 06:00–10:00 UTC
]

# DeepSeek API pricing in USD per 1M tokens, keyed by model ('v4-flash',
# 'v4-pro') with 'peak' and 'off_peak' tiers — off-peak is exactly half of
# peak. Source: https://api-docs.deepseek.com/quick_start/pricing.
# TIME-DEPENDENT: read via get_deepseek_pricing() at call time, not directly.
DEEPSEEK_PRICING = {
    # deepseek-flash / DeepSeek-V4.1-Flash (primary model for general synthesis)
    "v4-flash": {
        "peak": {
            "input":     0.30,
            "output":    1.20,
            "cache_hit": 0.006,
        },
        "off_peak": {
            "input":     0.15,
            "output":    0.60,
            "cache_hit": 0.003,
        },
    },
    # deepseek-v4-pro / DeepSeek-V4-Pro-0813 (complex architectural queries)
    "v4-pro": {
        "peak": {
            "input":     1.32,
            "output":    3.96,
            "cache_hit": 0.044,
        },
        "off_peak": {
            "input":     0.66,
            "output":    1.98,
            "cache_hit": 0.022,
        },
    },
}


def is_deepseek_peak_hour(
    utc_hour: int | None = None,
    utc_weekday: int | None = None,
) -> bool:
    """Return True if the given UTC time falls within a DeepSeek peak window.
    Defaults to current UTC time if not provided. Peak windows are
    01:00–04:00 and 06:00–10:00 UTC, Monday–Friday only — weekends are always
    off-peak. Use this to select the correct pricing tier at the moment of the
    API call. Pure, deterministic, no side effects.
    Args:
        utc_hour:    Override UTC hour (0–23). Defaults to now().
        utc_weekday: Override UTC weekday (0=Mon … 6=Sun). Defaults to now().
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if utc_hour is None:
        utc_hour = now.hour
    if utc_weekday is None:
        utc_weekday = now.weekday()
    if utc_weekday >= 5:  # Saturday or Sunday — always off-peak
        return False
    return any(start <= utc_hour < end for start, end in DEEPSEEK_PEAK_WINDOWS_UTC)


def get_deepseek_pricing(
    model_key: str,
    utc_hour: int | None = None,
    utc_weekday: int | None = None,
) -> dict:
    """Return the active pricing tier for a model based on current UTC time.
    Falls back to v4-flash pricing if model_key is not recognised.
    Args:
        model_key:   'v4-flash' or 'v4-pro'
        utc_hour:    Override UTC hour (0–23). Defaults to now(). Useful for
                     testing or when the caller already has the request timestamp.
        utc_weekday: Override UTC weekday (0=Mon … 6=Sun). Defaults to now().
    Returns:
        dict with keys: input, output, cache_hit  (USD per 1M tokens)
    """
    tier = "peak" if is_deepseek_peak_hour(utc_hour, utc_weekday) else "off_peak"
    return DEEPSEEK_PRICING.get(model_key, DEEPSEEK_PRICING["v4-flash"])[tier]


# Auto-routing threshold: queries with at least this many whitespace-separated
# words are escalated to DeepSeek cloud synthesis even in local/hybrid mode.
# Valid range: non-negative integer; default 40. Consumed by the query_memory
# router, checked only after CLOUD_ESCALATION_KEYWORDS misses.
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

# Query retrieval pool: documents fetched from ChromaDB before lexical
# reranking in query_memory. A wider pool lets reranking pull in keyword-
# relevant files that might be semantically distant. Does NOT control the final
# answer size — query_memory trims to a fixed top-5 after reranking. Query-only;
# project-brief synthesis uses BRIEF_FETCH_CAP below.
FETCH_CAP = int(os.getenv("FETCH_CAP", "75"))

# Candidate pool per section during project-brief synthesis. Each of the 9
# sections queries ChromaDB, lexically re-ranks locally, then trims to its
# per-section fetch_cap (20/25/30, defined in main.py). Decoupled from FETCH_CAP
# so a tight query pool does not starve the brief. Default 20.
# NOTE: a pool below a section's cap limits it — the 25/30-cap sections
# (Architecture, Dev & Testing, Roadmap) will only receive 20.
BRIEF_FETCH_CAP = int(os.getenv("BRIEF_FETCH_CAP", "20"))

# Local Ollama concurrency limit.
# Gates parallel brief synthesis sections to prevent local VRAM thrashing.
# Default 1 is recommended for 8GB cards; raise if you have more headroom.
OLLAMA_MAX_CONCURRENCY = int(os.getenv("OLLAMA_MAX_CONCURRENCY", "1"))

# Jev / TypeSafe feature gate and thresholds.
# Default OFF to preserve byte-identical behavior until explicitly enabled.
ENABLE_JEV = os.getenv("ENABLE_JEV", "false").lower() == "true"
# Enables Judge #1, the pre-retrieval query gate in Jev/TypeSafe. When true,
# assess_query runs before ChromaDB retrieval and may short-circuit with "I don't
# know"; when false Judge #1 is skipped and only Judge #2 (evidence) decides.
# Valid values: true/false (case-insensitive); default true. Ignored when
# ENABLE_JEV is false.
JEV_ENABLE_QUERY_JUDGE = os.getenv("JEV_ENABLE_QUERY_JUDGE", "true").lower() == "true"
# TypeSafe/Jev API key. Empty by default; must be set in .env for the Jev client
# to initialize. When empty, jev.client.get_client raises JevUnavailable and the
# pipeline fails open to the non-Jev path.
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "")
# TypeSafe/Jev model identifier passed to TypeSafeClient as the `model` argument.
# Default "jev-latest"; override to pin a specific Jev model version.
TYPESAFE_MODEL = os.getenv("TYPESAFE_MODEL", "jev-latest")
# Timeout in seconds for Jev/TypeSafe system_one network calls. Valid range:
# positive float; default 8 (reads JEV_TIMEOUT_SECONDS from the environment).
JEV_TIMEOUT_SECONDS = float(os.getenv("JEV_TIMEOUT_SECONDS", "8"))
# Maximum number of retrieval candidates forwarded to Judge #2 (evidence
# assessment), after filtering by JEV_DISTANCE_THRESHOLD and ranking.
# Valid range: positive integer; default 15; reads JEV_MAX_PASSAGES from the env.
JEV_MAX_PASSAGES = int(os.getenv("JEV_MAX_PASSAGES", "15"))
# L2 distance cap for candidates sent to Jev Judge #2, independent of
# QUERY_DISTANCE_THRESHOLD. ChromaDB L2 distances: 0 = identical, higher = less
# similar. Valid range: positive float; default 1.5.
JEV_DISTANCE_THRESHOLD = float(os.getenv("JEV_DISTANCE_THRESHOLD", "1.5"))
# Judge #1 gate: minimum in_domain Noul score required for the query to proceed
# to retrieval. Below it the query is rejected as out_of_domain. Range 0.0–1.0;
# default 0.40.
JEV_DOMAIN_MIN = float(os.getenv("JEV_DOMAIN_MIN", "0.40"))
# Judge #1 gate: maximum specificity Score (range 0–2) allowed before the query
# is rejected as too_vague. Higher values accept less specific queries; default 1.5.
JEV_SPEC_MAX = float(os.getenv("JEV_SPEC_MAX", "1.5"))
# Minimum answerability score (Noul). Used by Judge #1 as answerable_from_code and
# by Judge #2 as whole-set answerability; below it the result is marked partial or
# no_answer. Range 0.0–1.0; default 0.50.
JEV_ANSWERABILITY_MIN = float(os.getenv("JEV_ANSWERABILITY_MIN", "0.50"))
# Judge #2 gate: minimum relevance Noul score for a passage to be kept.
# Range 0.0–1.0; default 0.50.
JEV_RELEVANCE_MIN = float(os.getenv("JEV_RELEVANCE_MIN", "0.50"))
# Judge #2 gate: minimum evidence Noul score for a passage to be kept.
# Range 0.0–1.0; default 0.50.
JEV_EVIDENCE_MIN = float(os.getenv("JEV_EVIDENCE_MIN", "0.50"))
# Judge #2 gate: maximum prompt-injection score allowed; a passage at or above it
# is dropped. Range 0.0–1.0; default 0.50.
JEV_INJECTION_MAX = float(os.getenv("JEV_INJECTION_MAX", "0.50"))
# Judge #2 gate: minimum contradicts Noul score at which a passage is flagged as
# conflicting evidence. Range 0.0–1.0; default 0.50.
JEV_CONTRA_MIN = float(os.getenv("JEV_CONTRA_MIN", "0.50"))
# Maximum token budget for a rendered Jev report. Valid range: positive integer;
# default 300; reads JEV_REPORT_MAX_TOKENS from the environment.
JEV_REPORT_MAX_TOKENS = int(os.getenv("JEV_REPORT_MAX_TOKENS", "300"))
# When MEMORY_MODE is "local", Jev runs only if this is true. Valid values:
# true/false (case-insensitive); default false; reads JEV_ALLOW_IN_LOCAL from env.
JEV_ALLOW_IN_LOCAL = os.getenv("JEV_ALLOW_IN_LOCAL", "false").lower() == "true"
# Controls Jev error handling. When true, JevUnavailable and unexpected errors
# fall through to the existing non-Jev pipeline; when false they propagate.
# Valid values: true/false (case-insensitive); default true.
JEV_FAIL_OPEN = os.getenv("JEV_FAIL_OPEN", "true").lower() == "true"
