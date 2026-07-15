import asyncio
import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import tiktoken
from chromadb import PersistentClient
from mcp.server.fastmcp import FastMCP
from ollama import Client
from openai import OpenAI

from code_indexer import extract_entities, get_supported_extensions
from config import (
    CLOUD_ESCALATION_KEYWORDS,
    CLOUD_ESCALATION_WORD_COUNT,
    DB_PATH,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL_FAST,
    DEEPSEEK_MODEL_PRO,
    DEEPSEEK_PRICING,
    DEFAULT_MEMORY_MODE,
    ENABLE_DEEPSEEK_PRO,
    ENABLE_LEXICAL_RERANK,
    ENABLE_TOKEN_TRACKING,
    FETCH_CAP,
    LEXICAL_RERANK_WEIGHT,
    OLLAMA_HOST,
    OLLAMA_MAX_CONCURRENCY,
    OLLAMA_MODEL,
    QUERY_DISTANCE_THRESHOLD,
    SKIP_BARE_FILES,
    SYNTHESIZE_WITH_CLOUD,
    ZERIKAI_DB,
)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
_log_dir = DB_PATH  # .brain/ — already in .memignore via *.log

# Ensure .brain/ exists before we try to write the log file
_log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        # Live output captured by the IDE / terminal
        logging.StreamHandler(),
        # Persistent file — 5 MB cap, 2 rolling backups
        RotatingFileHandler(
            filename=_log_dir / "server.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("universal-brain")
log.info("=" * 60)
log.info("Universal Brain MCP Server starting")
log.info("DB_PATH    : %s", DB_PATH.resolve())
log.info("Ollama host: %s", OLLAMA_HOST)
log.info("Ollama model: %s", OLLAMA_MODEL)
log.info("Default mode: %s", DEFAULT_MEMORY_MODE)
log.info("=" * 60)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP("UniversalBrain")

# ---------------------------------------------------------------------------
# ChromaDB — single client, collections are per-workspace (created on demand)
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()
_vector_db_path = DB_PATH / "vector_db"
_vector_db_path.mkdir(parents=True, exist_ok=True)
db_client = PersistentClient(path=str(_vector_db_path))

# ---------------------------------------------------------------------------
# DeepSeek client
# ---------------------------------------------------------------------------
ds_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ---------------------------------------------------------------------------
# Ollama client
# ---------------------------------------------------------------------------
ol_client = Client(host=OLLAMA_HOST)
# Local concurrency semaphore for local synthesis/briefs
ollama_semaphore = asyncio.Semaphore(OLLAMA_MAX_CONCURRENCY)


# ---------------------------------------------------------------------------
# Token usage tracking database
# ---------------------------------------------------------------------------
def _init_db():
    """Initialise zerikai.db via sqlite3: creates token_usage and
    workspace_registry tables (IF NOT EXISTS), auto-migrates missing
    columns (e.g. estimated_cost_usd), creates indices, and enables
    WAL mode. Skips entirely if ENABLE_TOKEN_TRACKING is disabled.
    Idempotent — safe to call on every startup.
    """
    if not ENABLE_TOKEN_TRACKING:
        return

    conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
    # allows concurrent reads during writes
    conn.execute("PRAGMA journal_mode=WAL")

    # Create token tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cache_hit_tokens INTEGER NOT NULL,
            cache_miss_tokens INTEGER NOT NULL,
            estimated_cost_usd REAL NOT NULL
        )
    """)

    # Migrate existing table: add estimated_cost_usd column if missing
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(token_usage)")
    columns = [row[1] for row in cursor.fetchall()]
    if "estimated_cost_usd" not in columns:
        log.info("Migrating token_usage table: adding estimated_cost_usd column")
        conn.execute(
            "ALTER TABLE token_usage ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspace_timestamp
        ON token_usage(workspace_id, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp
        ON token_usage(timestamp)
    """)

    # Create workspace registry table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace_registry (
            workspace_uuid TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_scanned TEXT,
            last_brief_update TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspace_path
        ON workspace_registry(workspace_path)
    """)

    conn.commit()
    conn.close()


def _track_token_usage(
    workspace_id: str,
    operation: str,
    model: str,
    usage: object,
):
    """Record DeepSeek API token usage and estimated cost to zerikai.db sqlite3.
    Best-effort: returns silently if ENABLE_TOKEN_TRACKING is disabled,
    usage is None, or insert fails. Routes pricing via model_key
    ('v4-pro' vs 'v4-flash') to DEEPSEEK_PRICING. Side effect: inserts
    one row per call into token_usage table.
    Args:
        workspace_id: The workspace identifier
        operation: Type of operation (query, brief_synthesis, etc.)
        model: Model name (deepseek-v4-flash, deepseek-v4-pro)
        usage: OpenAI usage object from API response
    """
    if not ENABLE_TOKEN_TRACKING or not usage:
        return

    try:
        # Extract token counts
        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", 0)
        cache_miss = getattr(usage, "prompt_cache_miss_tokens", 0)

        # Determine pricing tier
        model_key = "v4-pro" if "pro" in model.lower() else "v4-flash"
        pricing = DEEPSEEK_PRICING.get(model_key, DEEPSEEK_PRICING["v4-flash"])

        # Calculate cost: cache hits + cache misses + output
        cost = (
            (cache_hit / 1_000_000) * pricing["cache_hit"] +
            (cache_miss / 1_000_000) * pricing["input"] +
            (completion_tokens / 1_000_000) * pricing["output"]
        )

        # Store in database
        with sqlite3.connect(str(ZERIKAI_DB), timeout=10) as conn:
            conn.execute("""
                INSERT INTO token_usage (
                    timestamp, workspace_id, operation, model,
                    prompt_tokens, completion_tokens,
                    cache_hit_tokens, cache_miss_tokens, estimated_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                workspace_id,
                operation,
                model,
                prompt_tokens,
                completion_tokens,
                cache_hit,
                cache_miss,
                cost,
            ))

        log.info(
            "Token tracking | workspace=%s | operation=%s | cost=$%.6f",
            workspace_id, operation, cost,
        )

    except Exception as exc:
        log.error("Token tracking failed: %s", exc)


# Initialize database (token tracking + workspace registry) on startup
_init_db()


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _derive_workspace_id(workspace_path: str) -> tuple[str, str]:
    """Derive a stable workspace UUID from a filesystem path via zerikai.db sqlite3.
    Normalizes the path (case, separators, trailing slashes), then looks
    up or creates a workspace_registry entry. Subsequent calls with the
    same path return the same UUID. Deterministic per path. Side effect:
    inserts a new row on first call for each unique path.
    Args:
        workspace_path: Filesystem path to the workspace
    Returns:
        tuple: (workspace_uuid, display_name)
    """
    if not workspace_path:
        return ("default", "default")

    # Aggressive normalization to prevent duplicate workspace IDs due to:
    # - Case differences (d:\ vs D:\)
    # - Separator differences (/ vs \)
    # - Relative vs absolute paths
    # - Trailing slashes
    # - Symlinks/junctions (on Windows)

    # Step 1: Strip trailing slashes/backslashes
    workspace_path = workspace_path.rstrip('/\\')

    # Step 2: Convert to absolute path using os.path.abspath
    if not os.path.isabs(workspace_path):
        workspace_path = os.path.abspath(workspace_path)

    # Step 3: Normalize path separators and case
    # os.path.normcase handles platform-specific case-sensitivity correctly
    normalized_path = os.path.normcase(os.path.normpath(workspace_path))

    # Step 4: Use forward slashes as canonical separator
    normalized_path = normalized_path.replace('\\', '/')

    # Step 5: Extract folder name for display name
    folder_name = os.path.basename(normalized_path)
    display_name = re.sub(r"[^a-z0-9]+", "_", folder_name.lower()).strip("_")

    # Step 6: Look up or create workspace registry entry
    try:
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Try to find existing workspace by normalized path
        cursor.execute(
            "SELECT workspace_uuid, display_name FROM workspace_registry WHERE workspace_path = ?",
            (normalized_path,)
        )
        row = cursor.fetchone()

        if row:
            # Existing workspace found
            workspace_uuid = row["workspace_uuid"]
            stored_display_name = row["display_name"]
            conn.close()
            log.debug(
                f"Workspace ID lookup: '{workspace_path}' → '{normalized_path}' → {workspace_uuid} ({stored_display_name})"
            )
            return (workspace_uuid, stored_display_name)

        # No existing workspace - create new UUID and register it
        workspace_uuid = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        cursor.execute("""
            INSERT INTO workspace_registry (workspace_uuid, workspace_path, display_name, created_at)
            VALUES (?, ?, ?, ?)
        """, (workspace_uuid, normalized_path, display_name, created_at))

        conn.commit()
        conn.close()

        log.info(
            f"New workspace registered: '{workspace_path}' → '{normalized_path}' → {workspace_uuid} ({display_name})"
        )
        return (workspace_uuid, display_name)

    except Exception as exc:
        log.error(
            f"Workspace ID derivation failed for '{workspace_path}': {exc}")
        # Fallback: generate ephemeral UUID (won't persist, but allows operation to continue)
        return (str(uuid4()), display_name)


def _resolve_workspace(identifier: str) -> tuple[str, str, str]:
    """Resolve a workspace identifier via zerikai.db sqlite3 to (uuid, name, path).
    Three-tier routing: exact UUID match → short UUID (first 8+ chars)
    LIKE match → display_name exact match. Reads from workspace_registry
    table. Pure read — no side effects.
    Args:
        identifier: Full UUID, short UUID (first 8+ chars), or display name
    Returns:
        tuple: (workspace_uuid, display_name, workspace_path)
    Raises:
        ValueError: If no workspace matches the identifier
    """
    try:
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Try exact UUID match
        cursor.execute(
            "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE workspace_uuid = ?",
            (identifier,)
        )
        row = cursor.fetchone()

        # Try short UUID match (first 8+ chars)
        if not row and len(identifier) >= 8:
            cursor.execute(
                "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE workspace_uuid LIKE ?",
                (f"{identifier}%",)
            )
            row = cursor.fetchone()

        # Try display name match
        if not row:
            cursor.execute(
                "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE display_name = ?",
                (identifier,)
            )
            row = cursor.fetchone()

        conn.close()

        if not row:
            raise ValueError(
                f"No workspace found matching '{identifier}'. "
                f"Run `list_workspaces` to see available workspaces."
            )

        return (row["workspace_uuid"], row["display_name"], row["workspace_path"])

    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        log.error(f"Workspace resolution failed for '{identifier}': {exc}")
        raise ValueError(f"Could not resolve workspace '{identifier}': {exc}")


# Placeholder string inserted into .brain/contexts/<id>.md after init_workspace.
# Detected by _background_scan to trigger first-time brief synthesis via
# DeepSeek or Ollama after scan completes. Replaced by the generated brief.
UNINITIALIZED_MARKER = "<!-- ZERIKAI_PENDING_SYNTHESIS -->"


def _truncate_for_brief(doc: str) -> str:
    """Truncate a docstring to its first sentence for cheap brief synthesis.
    Skips leading blank lines, joins remaining text, then splits at the
    first period+space found after position 20. Used by _build_section
    to keep DeepSeek/Ollama prompt context compact. Pure, deterministic.
    """
    lines = doc.strip().split("\n")
    meaningful = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        meaningful.append(stripped)
    result = " ".join(meaningful)
    dot = result.find(". ")
    if dot > 20:
        result = result[:dot + 1]
    return result


async def _build_section(
    section: dict,
    collection,
    display_name: str,
    use_cloud: bool,
    workspace_id: str,
) -> tuple[str, str]:
    """Build one brief section: queries ChromaDB, lexically re-ranks,
    and synthesizes via DeepSeek or Ollama. Runs in parallel via
    asyncio.gather across all 9 sections. Lexical re-ranking boosts
    results by keyword overlap in entity name, docstring, and
    source_file. Trims to per-section fetch_cap before LLM call.
    Side effect: writes token usage to zerikai.db sqlite3.
    Args:
        section: Dict with query, prompt_template, heading, optional
                 fetch_cap (default 20) and full_context (bool).
        collection: ChromaDB collection for this workspace.
        display_name: Project name for prompt formatting.
        use_cloud: True → DeepSeek, False → Ollama.
        workspace_id: UUID for _track_token_usage logging.
    Returns:
        (heading, content) on success, (heading, error) on failure.
    """
    heading = section["heading"]
    log.info("_synthesize_deep_brief | Generating: %s", heading)

    try:
        with _db_lock:
            total_docs = collection.count()
            # Fetch a wide pool (up to 75) for re-ranking, then trim to
            # the per-section fetch_cap before sending to the LLM.
            # This lets the re-rank pull in semantically-distant but
            # keyword-relevant files (e.g. todo.md, ROADMAP.md).
            pool_size = min(FETCH_CAP, total_docs) if total_docs > 0 else 1
            results = collection.query(
                query_texts=[section["query"]],
                n_results=pool_size,
                where={"category": "codebase"},
                include=["documents", "metadatas", "distances"],
            )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        if not docs:
            with _db_lock:
                fallback = collection.get(
                    where={"category": "codebase"}, limit=pool_size)
            docs = fallback.get("documents", [])
            metas = fallback.get("metadatas", [])
            distances = [1.0] * len(docs)

        # ── Lexical re-rank: boost results whose filename, entity name, or
        # content share keywords with the section query.  Same scoring
        # formula used by query_memory, extended with source_file so that
        # files named todo.md, ROADMAP.md, CHANGELOG.md surface naturally.
        query_terms = set(section["query"].lower().split())
        scored = []
        for doc, meta, dist in zip(docs, metas or [{}] * len(docs), distances):
            if (meta or {}).get("source_type") == "manual":
                continue
            name = (meta or {}).get("name", "").lower()
            text = doc.lower()
            src_file = (meta or {}).get("source_file", "").lower()
            hits = sum(
                1 for t in query_terms
                if t in name or t in text or t in src_file
            )
            score = (1 / dist) + (hits * LEXICAL_RERANK_WEIGHT)
            scored.append((score, doc, meta))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Trim to per-section cap for LLM cost control
        llm_cap = section.get("fetch_cap", 20)
        scored = scored[:llm_cap]

        context_parts = []
        for _score, doc, meta in scored:
            src = (meta or {}).get("source_file", "")
            header = f"### {src}\n" if src else ""
            if section.get("full_context"):
                context_parts.append(f"{header}{doc}")
            else:
                context_parts.append(f"{header}{_truncate_for_brief(doc)}")
        context = "\n\n".join(context_parts)
        prompt = section["prompt_template"].format(context=context)

        if use_cloud:
            response = await asyncio.to_thread(
                ds_client.chat.completions.create,
                model=DEEPSEEK_MODEL_FAST,
                messages=[
                    {"role": "system",
                        "content": "You are a senior software architect."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                max_tokens=2048,
            )
            content = response.choices[0].message.content.strip()
            usage = getattr(response, "usage", None)
            if usage:
                _track_token_usage(
                    workspace_id, "brief_synthesis", DEEPSEEK_MODEL_FAST, usage)
        else:
            result = await asyncio.to_thread(
                ol_client.generate,
                model=OLLAMA_MODEL,
                prompt=prompt,
                options={"temperature": 0},
            )
            content = result["response"].strip()

        log.info("_synthesize_deep_brief | \u2713 %s complete", heading)
        return (heading, content)

    except Exception as exc:
        log.error("_synthesize_deep_brief | Failed on %s: %s", heading, exc)
        return (heading, f"(Section generation failed: {exc})")


async def _synthesize_deep_brief(workspace_id: str, display_name: str, use_cloud: bool = True) -> str:
    """Build a 9-section project brief via parallel ChromaDB queries.
    Fires all section-specific queries via asyncio.gather, delegating
    each to _build_section. Routes to DeepSeek or Ollama based on
    SYNTHESIZE_WITH_CLOUD. Side effect: saves assembled markdown to
    .brain/contexts/<id>.md. Overwrites existing brief.
    Args:
        workspace_id: The workspace UUID (for collection access)
        display_name: Human-readable project name (for brief title and prompts)
        use_cloud: If True, uses DeepSeek for higher quality (small cost).
                   If False, uses Ollama (free, may include noise).
    """
    log.info("_synthesize_deep_brief | Starting iterative synthesis for %s (%s)",
             display_name, workspace_id)
    collection = _get_collection(workspace_id)

    # Check if we have any codebase data at all
    with _db_lock:
        check = collection.get(where={"category": "codebase"}, limit=1)
    if not check.get("documents"):
        return f"# Project Brief: {display_name}\n\nNo codebase files found during scan."

    # Section definitions with semantic queries and format-guided prompts
    sections = [
        {
            "heading": "## Overview",
            "query": "What is this project's purpose, main features, external services it integrates with, who it is designed for, and what does the README say about the project overview?",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, write the Overview section. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                "Use this format:\n"
                f"`{display_name}` is a [type] system designed to [purpose]. "
                "State the key technologies used (parsing, storage, LLMs, protocols). "
                "Name the external services or APIs it integrates with and their role. "
                "State who it is designed for and in what context it operates.\n\n"
                f"Do not add new sections or headings — this is one continuous paragraph.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Write the Overview section:"
            ),
        },
        {
            "heading": "## Technical Stack",
            "query": "What are the primary dependencies, libraries, frameworks, and databases used in this project? List the language, frameworks, data storage, interfaces (API, CLI, web, MCP), and key libraries.",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, list the Technical Stack. "
                "Be concise and direct. Start directly with 'Listing only primary libraries, max 5:' — no other introductory text.\n\n"
                "IMPORTANT: Only list the 5-10 most important PRIMARY dependencies. "
                "Omit transitive dependencies, low-level utilities, and standard library modules. "
                "Focus on frameworks, databases, APIs, and major integrations that define the project's architecture.\n\n"
                "Use this format:\n\n"
                "* **Language:** [Python, JavaScript, TypeScript, etc.]\n"
                "* **Frameworks:** [Server, web, MCP frameworks — omit if none]\n"
                "* **Data Storage:** [Database, vector store, file-based, etc.]\n"
                "* **Interfaces:** [API, CLI, web, MCP — omit any that do not apply]\n"
                "* **Libraries:**\n"
                "  * [Category]: [Library names]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List the Technical Stack:"
            ),
        },
        {
            "heading": "## Core Architecture",
            "query": "How is this project structured? Describe the architectural layers — entry points, processing pipeline, data storage, code indexing, and LLM integration.",
            "full_context": True,
            "fetch_cap": 25,
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, describe the Core Architecture. "
                "Be concise and direct. Start directly with 'The application consists of the following layers:' — no other introductory text.\n\n"
                "Use this format:\n\n"
                "The application consists of the following layers:\n\n"
                "1. **[Layer Name]:** [Technology and what it handles]\n"
                "2. **[Layer Name]:** [Technology and what it handles]\n\n"
                "Name each layer based on what you find in the summaries. "
                "Omit any layer that does not exist in the codebase.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe the Core Architecture:"
            ),
        },
        {
            "heading": "## Primary Conventions",
            "query": "What conventions, patterns, and standards does this project follow? Describe the code organization, naming conventions, error handling, docstring style, file ignore rules, and any other conventions evident in the codebase.",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, list the Primary Conventions. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format for any sections that apply:\n"
                "* **Code Organization:** [How code is structured into directories/modules]\n"
                "* **Naming Conventions:** [Prefix patterns like _private, UPPER_CASE constants]\n"
                "* **File Ignore Rules:** [How .memignore or similar patterns are handled]\n"
                "* **Docstring Style:** [Convention used]\n"
                "* **Error Handling & Logging:** [Method and mechanism]\n"
                "* **Database Schema:** [Where defined and how updated]\n\n"
                "Omit any section that does not apply. Only include categories evident "
                "in the codebase. Do not add any other sections.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List the Primary Conventions:"
            ),
        },
        {
            "heading": "## Purpose",
            "query": "What problem does this project solve? What is its goal, who is it for, and what technologies does it use to achieve that goal?",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, explain the Purpose. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                "Use this format:\n"
                f"`{display_name}` aims to [goal] using [technologies] to solve [problem]. "
                "It is designed for [audience].\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Explain the Purpose:"
            ),
        },
        {
            "heading": "## Key Files & Directories",
            "query": "What are the key files and directories in this project? List the entry point, configuration, core modules, documentation, and storage directories with their purposes.",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, identify Key Files & Directories. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format:\n"
                "* **`path/to/file.ext`** - [Brief purpose]\n"
                "* **`directory/`** - [What this directory contains]\n\n"
                "Focus on entry points, configuration, core modules, key directories, "
                "and project documentation. Omit test files, CI configs, and generic items.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List Key Files & Directories:"
            ),
        },
        {
            "heading": "## Development & Testing",
            "query": "How do you set up, run, test, and deploy this project? What are the installation steps, startup commands, test framework, and build or deployment process?",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, describe Development & Testing setup. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format:\n"
                "* **Setup:** [How to install dependencies and prepare environment]\n"
                "* **Running Locally:** [Command or method to start the project]\n"
                "* **Testing:** [Test framework and command to run tests — omit if none]\n"
                "* **Build/Deploy:** [Build process or containerization — omit if none]\n\n"
                "If a category has no information in the summaries, omit it. "
                "Do not fabricate details.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe Development & Testing:"
            ),
        },
        {
            "full_context": True,
            "fetch_cap": 25,
            "heading": "## Data Flow & Request Lifecycle",
            "query": "How does a request flow through this project? Describe the entry point, processing pipeline, data access, response generation, and authentication if present.",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, describe the Data Flow & Request Lifecycle. "
                "Be concise and direct. Start directly with 'A typical request flows through:' — no other introductory text.\n\n"
                "Use this format:\n"
                "A typical request flows through:\n\n"
                "1. **[Entry Point]:** [What happens first]\n"
                "2. **[Processing Layer]:** [How request is processed]\n"
                "3. **[Data Layer]:** [How data is accessed/modified]\n"
                "4. **[Response]:** [How response is generated]\n\n"
                "Include authentication flow only if present in the summaries. "
                "Do not fabricate details.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe Data Flow & Request Lifecycle:"
            ),
        },
        {
            "heading": "## Future Roadmap",
            "query": "What planned features, TODOs, FIXMEs, roadmap items, or future improvements are documented in this codebase?",
            "full_context": True,
            "fetch_cap": 30,
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase (look for TODOs, FIXME, comments about future changes, documented roadmaps, or explicit plans), "
                "describe the Future Roadmap. "
                "Be concise and direct. Start directly with the first planned item or milestone — no introductory text.\n\n"
                "Use this format:\n"
                "1. **Phase/Feature Title:** [Description of planned improvement]\n"
                "2. **[Next Title]:** [Description...]\n\n"
                "IMPORTANT: If no clear future plans, TODOs, or roadmap items are found in the code or documentation, "
                "respond ONLY with: 'No future roadmap specified in the codebase.'\n\n"
                "DO NOT suggest or infer plans. Only report what is explicitly documented.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe the Future Roadmap:"
            ),
        },
    ]

    async def _build_section_safe(s: dict):
        """Wrapper to gate Ollama calls via semaphore during local synthesis."""
        if not use_cloud:
            async with ollama_semaphore:
                return await _build_section(s, collection, display_name, use_cloud, workspace_id)
        return await _build_section(s, collection, display_name, use_cloud, workspace_id)

    tasks = [
        _build_section_safe(s)
        for s in sections
    ]
    results = await asyncio.gather(*tasks)

    brief_parts = [f"# Project Brief: {display_name}\n"]
    for heading, content in results:
        brief_parts.append(f"\n{heading}\n\n{content}\n")

    final_brief = "".join(brief_parts)
    log.info("_synthesize_deep_brief | Complete for %s", workspace_id)
    return final_brief


# ---------------------------------------------------------------------------
# Background scan progress tracking
# ---------------------------------------------------------------------------

@dataclass
class ScanProgress:
    """Tracks progress of a background workspace scan. Written by
    _background_scan during file processing and _background_brief_synthesis
    for brief_status transitions (pending → running → Complete/Failed).
    Read by scan_status for user-facing progress reports. Plain dataclass
    — no methods, no side effects beyond field mutation by callers.
    """
    workspace_id: str
    display_name: str
    total_files: int
    scanned: int = 0
    entities: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: float = field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp())
    completed: bool = False
    brief_status: str = "pending"  # pending, running, complete, failed


# Module-level registry of active/recent scans, keyed by workspace_id
_scans: dict[str, ScanProgress] = {}
_scan_tasks: dict[str, asyncio.Task] = {}


async def _background_brief_synthesis(
    workspace_id: str,
    display_name: str,
    context_file: Path,
    progress: ScanProgress | None = None,
) -> None:
    """Fire-and-forget brief synthesis after scan to avoid MCP timeouts.
    Delegates to _synthesize_deep_brief (cloud/local via
    SYNTHESIZE_WITH_CLOUD). Creates/overwrites .brain/contexts/<id>.md.
    Updates progress.brief_status to 'Complete' or 'Failed'. Launched
    via asyncio.create_task — no await, no return value.
    """
    try:
        new_brief = await _synthesize_deep_brief(workspace_id, display_name, use_cloud=SYNTHESIZE_WITH_CLOUD)
        context_file.write_text(new_brief, encoding="utf-8")
        if progress:
            progress.brief_status = "Complete"
        log.info("_background_brief_synthesis | brief saved for %s", display_name)
    except Exception as exc:
        if progress:
            progress.brief_status = "Failed"
        log.error("_background_brief_synthesis | failed for %s: %s",
                  display_name, exc)


def _get_collection(workspace_id: str):
    """Return the ChromaDB PersistentClient collection for a workspace.
    Uses db_client.get_or_create_collection with name `memory_{id}`.
    Idempotent — creates on first call, reuses thereafter. Thread-safe
    when callers hold _db_lock. No other side effects.
    """
    return db_client.get_or_create_collection(f"memory_{workspace_id}")


def _load_project_context(workspace_id: str) -> str:
    """Load the per-workspace project brief from .brain/contexts/<id>.md.
    Used as the stable prefix for DeepSeek KV cache optimisation via
    _build_system_message. Creates contexts/ directory on first call.
    Falls back to a placeholder string if no brief file exists. Pure
    read — no writes beyond mkdir.
    """
    context_dir = Path(DB_PATH) / "contexts"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_file = context_dir / f"{workspace_id}.md"

    if context_file.exists():
        return context_file.read_text(encoding="utf-8").strip()

    # Placeholder — functional but won't produce meaningful cache hits
    # until you populate the file with real project context.
    return (
        f"Project workspace: {workspace_id}\n"
        "No project brief found. Run the `init_workspace` tool to create one."
    )


def _get_score_tuple(evidence_item: dict) -> tuple[float | None, str]:
    """Return score and score label from evidence item."""
    score = evidence_item.get("rerank_score")
    score_label = "rerank"
    if score is None:
        score = evidence_item.get("l2_distance")
        score_label = "L2"
    return score, score_label


def _build_system_message(workspace_id: str) -> str:
    """Assemble the DeepSeek system message with tiktoken for KV cache optimisation.
    Concatenates a fixed role instruction with the per-workspace project
    brief from _load_project_context. The identical prefix maximises
    DeepSeek KV cache hits across calls (best-effort, no guarantees).
    Logs token count via tiktoken's cl100k_base encoding. Pure read-only.
    """
    role_instruction = (
        "You are a project memory assistant. "
        "Your role is to synthesize retrieved project context and answer "
        "the developer's query accurately and concisely. "
        "Prioritise specifics over generalities. "
        "Do not repeat the retrieved context verbatim.\n\n"
        "=== STRICT ATTRIBUTION RULES ===\n"
        "1. GROUNDING: Base your answer EXCLUSIVELY on the provided context. If the information is not present, say 'I don't have this information'.\n"
        "2. SIGNATURE TRUTH: When explaining a function or class, use only the signature and logic provided in its specific context block. Do not attribute logic from helper functions (e.g., _extract_*) to the top-level caller unless explicitly stated in that caller's block.\n"
        "3. NO HALLUCINATION: Do not invent parameters, return types, or implementation details. Verify every claim against the context.\n"
        "4. INLINE CITATIONS: When you state a fact drawn from a specific source, cite it inline immediately "
        "after the claim using the format: #file:line | score L2 or rerank\n"
        "   Example: \"The brief is loaded via _load_project_context (#main.py:810 | 0.72 L2).\"\n"
        "   Only cite sources that are present in the provided context. Do not fabricate file paths, line numbers, or scores.\n\n"
        "=== PROJECT BRIEF ===\n"
    )
    project_context = _load_project_context(workspace_id)
    full_message = role_instruction + project_context

    # Per https://api-docs.deepseek.com/guides/kv_cache:
    # - Cache persists at request boundaries and detects common prefixes automatically
    # - Prefix matching works from token 0
    # - Cache units are created at fixed intervals for long inputs
    # - No explicit minimum length requirement; cache works on "best-effort" basis
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(full_message))
        log.debug("_build_system_message | System message: %d tokens", token_count)
    except Exception as exc:
        log.warning("_build_system_message | Token count failed: %s", exc)

    return full_message


# ---------------------------------------------------------------------------
# .memignore helpers
# ---------------------------------------------------------------------------

# Text extensions we are willing to read and summarise.
_TEXT_EXTENSIONS = {
    ".py", ".pyw", ".js", ".ts", ".jsx", ".tsx",
    ".md", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".sql", ".sh", ".env",
    ".java", ".go", ".rs", ".c", ".cpp", ".h",
}

# Never read files larger than this (bytes).
_MAX_FILE_BYTES = 200_000  # Increased from 100KB to 200KB to include main.py

# Files larger than this (in lines) are split into chunks before indexing.
# Prevents DeepSeek from truncating structured extraction on large files.
_CHUNK_LINE_THRESHOLD = 300   # lines — files above this get chunked
_CHUNK_SIZE_LINES = 250   # lines per chunk (with overlap)
_CHUNK_OVERLAP_LINES = 20    # overlap between chunks for context continuity


def _load_memignore(workspace_path: str) -> list[str]:
    """Read .memignore from the workspace root via fnmatch-compatible parsing.
    Lines starting with # and blank lines are ignored. Returns empty list
    if no .memignore file exists. Pure read — no side effects.
    """
    memignore = Path(workspace_path) / ".memignore"
    if not memignore.exists():
        return []
    patterns = []
    for line in memignore.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _is_ignored(file_path: Path, workspace_root: Path, patterns: list[str]) -> bool:
    """Check if a file matches .memignore patterns via fnmatch.
    Two-strategy routing: matches any single path component (directory
    names), then matches full relative posix path. Returns False on
    path resolution error. Deterministic for a given pattern set.
    """
    try:
        rel = file_path.relative_to(workspace_root)
    except ValueError:
        return False

    rel_posix = rel.as_posix()
    parts = rel.parts

    for pattern in patterns:
        p = pattern.rstrip("/")
        # 1. Match against any part of the path (covers directory names without slashes)
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
        # 2. Match against full relative path or filename
        if fnmatch.fnmatch(rel_posix, pattern) or fnmatch.fnmatch(file_path.name, pattern):
            return True

    return False


def _chunk_file_content(
    content: str,
    chunk_size: int = _CHUNK_SIZE_LINES,
    overlap: int = _CHUNK_OVERLAP_LINES,
) -> list[str]:
    """Split file content into overlapping line-based chunks for LLM indexing.
    Single-chunk files pass through as-is. Multi-chunk files use
    configurable overlap (default 20 lines) for context continuity.
    Always returns list[str] with at least one element. Deterministic
    for given inputs — no side effects beyond string allocation.
    """
    lines = content.splitlines(keepends=True)
    if len(lines) <= chunk_size:
        return [content]

    chunks = []
    start = 0
    while start < len(lines):
        end = min(start + chunk_size, len(lines))
        chunks.append("".join(lines[start:end]))
        if end == len(lines):
            break
        start = end - overlap  # step back by overlap for continuity
    return chunks


# ---------------------------------------------------------------------------
# Auto-routing logic
# ---------------------------------------------------------------------------

def _should_use_cloud(user_query: str, use_cloud: bool | None) -> bool:
    """Route queries to DeepSeek or Ollama via a 4-step priority chain.
    Priority: explicit use_cloud override → CLOUD_ESCALATION_KEYWORDS
    keyword match → CLOUD_ESCALATION_WORD_COUNT length threshold →
    DEFAULT_MEMORY_MODE fallback. Pure, deterministic, no side effects.
    """
    if use_cloud is not None:
        return use_cloud

    words = user_query.lower().split()

    # Keyword check
    if any(w in CLOUD_ESCALATION_KEYWORDS for w in words):
        log.info("Auto-route → cloud (keyword match)")
        return True

    # Length check
    if len(words) >= CLOUD_ESCALATION_WORD_COUNT:
        log.info("Auto-route → cloud (query length %d words)", len(words))
        return True

    return DEFAULT_MEMORY_MODE == "cloud"


def _select_model(user_query: str) -> str:
    """Select DEEPSEEK_MODEL_FAST vs DEEPSEEK_MODEL_PRO based on query keywords.
    Pro is reserved for queries containing architectural trigger words
    (architect, design, tradeoff, audit). If ENABLE_DEEPSEEK_PRO is False
    in .env, always falls back to FAST. Pure, deterministic.
    """
    if not ENABLE_DEEPSEEK_PRO:
        return DEEPSEEK_MODEL_FAST

    pro_triggers = {"architect", "architecture",
                    "design", "tradeoff", "trade-off", "audit"}
    if any(w in pro_triggers for w in user_query.lower().split()):
        log.info("Model → deepseek-v4-pro (reasoning query)")
        return DEEPSEEK_MODEL_PRO
    return DEEPSEEK_MODEL_FAST


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tool: init_workspace
# ---------------------------------------------------------------------------

@mcp.tool()
async def init_workspace(workspace_path: str) -> str:
    """Initialize a workspace via zerikai.db sqlite3 registry and ChromaDB.
    Derives stable UUID via _derive_workspace_id, creates
    .brain/contexts/<id>.md with placeholder brief. Idempotent —
    safe to re-call, returns existing info if already registered.
    Args:
        workspace_path: Absolute path to the project root.
    """
    workspace_id, display_name = _derive_workspace_id(workspace_path)
    context_dir = Path(DB_PATH) / "contexts"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_file = context_dir / f"{workspace_id}.md"

    if context_file.exists():
        return (
            f"Workspace already registered: `{display_name}`\n"
            f"Workspace ID: `{workspace_id[:8]}`\n\n"
            f"Use this ID with other tools:\n"
            f"  scan_workspace(workspace=\"{workspace_id[:8]}\")\n"
            f"  query_memory(workspace=\"{workspace_id[:8]}\", user_query=\"...\")"
        )

    template = f"{UNINITIALIZED_MARKER}\n# Project Brief — {display_name}\n\n(Waiting for initial scan... run `scan_workspace` to auto-generate the architecture brief. Generation takes about 20 seconds.)"
    context_file.write_text(template, encoding="utf-8")

    return (
        f"Workspace registered: `{display_name}`\n"
        f"Workspace ID: `{workspace_id[:8]}`\n\n"
        f"Next step — copy/paste this into your chat to start scanning:\n"
        f"  scan_workspace(workspace=\"{workspace_id[:8]}\")"
    )


# ---------------------------------------------------------------------------
# Tool: save_to_memory
# ---------------------------------------------------------------------------

@mcp.tool()
async def save_to_memory(
    content: str,
    workspace: str,
    category: str = "general",
    source_id: str | None = None,
    last_modified: str | None = None,
) -> str:
    """Save content to persistent vector memory in ChromaDB via tree-sitter or LLM.
    Routes by file extension: supported extensions (.py, .js, .ts, .css,
    .html, .md) use tree-sitter entity extraction; other formats fall
    through to DeepSeek/Ollama LLM summarisation. Uses deterministic
    md5 IDs so re-scans overwrite duplicates. Side effect: upserts to
    ChromaDB collection and logs token usage to zerikai.db sqlite3.
    Args:
        content:   The raw content to remember.
        workspace: Workspace identifier (UUID, short UUID, or display name).
        category:  Tag for filtering (e.g. 'architecture', 'api', 'decision').
        source_id: Optional unique identifier (like a file path) to prevent duplicates on re-scans.
    """
    try:
        workspace_id, display_name, workspace_path = _resolve_workspace(
            workspace)

        # -------------------------------------------------------------------
        # tree-sitter code extraction (deterministic, zero API cost)
        # Replaces the .py/.js/.ts/.tsx LLM branches below for supported langs.
        # -------------------------------------------------------------------
        ext = Path(source_id or "").suffix.lower()
        if ext in get_supported_extensions():
            try:
                entities = extract_entities(content, source_id or "")
            except Exception as exc:
                log.warning("tree-sitter parse failed for %s: %s",
                            source_id, exc)
                entities = []

            if entities:
                collection = _get_collection(workspace_id)
                saved_count = 0
                for entity in entities:
                    doc_id = hashlib.md5(
                        f"{workspace_id}:{source_id or 'snippet'}:{entity.name}:{entity.lineno}"
                        .encode()
                    ).hexdigest()
                    is_manual = (source_id or "").startswith("chat/")

                    meta = {
                        "category": category,
                        "workspace": workspace_id,
                        "source_file": source_id or "",
                        "source_type": "manual" if is_manual else "",
                        "language": entity.language,
                        "entity_type": entity.entity_type,
                        "name": entity.name,
                        "lineno": entity.lineno,
                        "end_lineno": entity.end_lineno,
                        "parent_class": entity.parent_class or "",
                        "return_type": entity.return_type or "",
                        "has_docstring": entity.docstring is not None,
                        "params_count": len(entity.params),
                        "last_modified": last_modified or "",
                    }
                    if entity.decorators:
                        meta["decorators"] = entity.decorators

                    with _db_lock:
                        collection.upsert(
                            documents=[entity.document_text],
                            metadatas=[meta],
                            ids=[doc_id],
                        )
                    saved_count += 1
                return (
                    f"[{workspace_id}] Archived {saved_count} entities "
                    f"({category}): {entities[0].signature[:100]}..."
                )
            # If tree-sitter found nothing (empty file, unsupported constructs),
            # skip bare .py files when configured to avoid DeepSeek calls.
            if ext in SKIP_BARE_FILES:
                return f"[{workspace_id}] Skipped bare {ext} (no entities): {source_id}"
            # fall through to the generic summary path below.

        # ---------------------------------------------------------------------------
        # File-type-aware prompt dispatch (non-code files only now)
        # ---------------------------------------------------------------------------
        # Select prompt + max_tokens based on file type so agents get precise,
        # actionable indexes instead of generic prose summaries.
        # ---------------------------------------------------------------------------

        src = (source_id or "").lower()
        src_name = Path(src).name  # bare filename for exact-name checks

        if src.endswith(".py") or "::chunk_" in (source_id or ""):
            # Structured index: imports, classes, functions with full signatures.
            # Chunk-aware: if this is a partial chunk, tell DeepSeek to extract only what is visible.
            is_chunk = "::chunk_" in (source_id or "")
            chunk_note = (
                "This is a partial chunk of a larger file. "
                "Extract only what is visible in this chunk.\n\n"
                if is_chunk else ""
            )
            index_prompt = (
                f"You are a code indexer. Extract a structured index from the Python "
                f"source below.\n\n"
                f"{chunk_note}"
                f"Respond ONLY in this exact format — no prose, no explanation:\n\n"
                f"description: <one sentence in plain English describing what this file does overall>\n"
                f"file: <filename>\n"
                f"imports: <comma-separated top-level external imports>\n\n"
                f"classes:\n"
                f"  - <ClassName>: <one-line docstring or purpose>\n\n"
                f"functions:\n"
                f"  - <function_name>(<param: type = default>, ...) -> <return_type>\n"
                f"      <one-line docstring or purpose>\n\n"
                f"Include every public function and method visible in this chunk. "
                f"Use exact parameter names, type annotations, and default values "
                f"as they appear in the source.\n\n"
                f"Source file: {source_id}\n\n"
                f"{content}"
            )
            max_tok = 800
        elif src_name == "requirements.txt":
            # Exact package list — no summarisation
            index_prompt = (
                f"Extract the exact package list from the requirements file below.\n\n"
                f"Respond ONLY in this exact format — no prose, no explanation:\n\n"
                f"description: Python dependencies for this project.\n"
                f"file: <filename>\n"
                f"packages: <comma-separated package names, no version pins>\n\n"
                f"Source file: {source_id}\n\n"
                f"{content}"
            )
            max_tok = 100
        elif src_name in (".env.example", ".env.template") or src.endswith(".env"):
            # Key names only — never values
            index_prompt = (
                f"Extract only the environment variable KEY NAMES from the file below. "
                f"Never include values.\n\n"
                f"Respond ONLY in this exact format — no prose, no explanation:\n\n"
                f"description: Environment variable configuration for this project.\n"
                f"file: <filename>\n"
                f"environment_variables: <comma-separated KEY names>\n\n"
                f"Source file: {source_id}\n\n"
                f"{content}"
            )
            max_tok = 100
        elif src.endswith(".md"):
            # Heading structure only — skip prose
            index_prompt = (
                f"Extract the heading structure AND checklist items from the Markdown file below. "
                f"Headings are lines starting with #, ##, ###, or ####. "
                f"Extract all checkbox items, preserving their completion status "
                f"[ ] or [x] and the accompanying text. "
                f"Group items under their nearest preceding heading.\n\n"
                f"Respond ONLY in this exact format — no prose, no explanation:\n\n"
                f"description: <one sentence in plain English describing what this document covers>\n"
                f"file: <filename>\n"
                f"headings:\n"
                f"  - <heading line>\n"
                f"checklists:\n"
                f"  - [x] <item text> (under: <heading>)\n\n"
                f"Source file: {source_id}\n\n"
                f"{content}"
            )
            max_tok = 150
        else:
            # Generic fallback — 2-3 sentence summary
            index_prompt = (
                f"Summarise the following for long-term technical memory "
                f"in 2–3 concise sentences:\n\n"
                f"{content}"
            )
            max_tok = 150

        # ---------------------------------------------------------------------------
        # Model dispatch: cloud vs local
        # File scanning uses cloud ONLY in "cloud" mode, not "hybrid"
        # "hybrid" mode uses Ollama for scanning, DeepSeek for briefs/queries
        # ---------------------------------------------------------------------------
        if DEFAULT_MEMORY_MODE == "cloud":
            response = await asyncio.to_thread(
                ds_client.chat.completions.create,
                model=DEEPSEEK_MODEL_FAST,
                messages=[{"role": "user", "content": index_prompt}],
                max_tokens=max_tok,
            )
            summary = response.choices[0].message.content.strip()

            # Track token usage
            usage = getattr(response, "usage", None)
            if usage:
                _track_token_usage(workspace_id, "file_scan",
                                   DEEPSEEK_MODEL_FAST, usage)
        else:
            # Use Ollama for "hybrid" and "local" modes
            result = await asyncio.to_thread(
                ol_client.generate,
                model=OLLAMA_MODEL,
                prompt=index_prompt,
            )
            summary = result["response"].strip()

        # Use a deterministic ID if source_id is provided so re-scans overwrite instead of duplicate
        if source_id:
            doc_id = hashlib.md5(
                f"{workspace_id}:{source_id}".encode()).hexdigest()
        else:
            doc_id = str(uuid4())

        with _db_lock:
            collection = _get_collection(workspace_id)
            collection.upsert(
                documents=[summary],
                metadatas=[{
                    "category": category,
                    "workspace": workspace_id,
                    "source_file": source_id or "",   # preserves filename through Ollama summarisation
                    "line_count": content.count("\n"),
                    "last_modified": last_modified or "",
                }],
                ids=[doc_id],
            )

        log.info(
            "Memory saved | workspace=%s | category=%s | preview=%.60s",
            workspace_id, category, summary,
        )
        return f"[{workspace_id}] Archived ({category}): {summary[:100]}..."

    except Exception as exc:
        log.error("save_to_memory failed: %s", exc)
        return f"ERROR: Could not save memory — {exc}"


# ---------------------------------------------------------------------------
# Tool: query_memory
# ---------------------------------------------------------------------------

@mcp.tool()
async def query_memory(
    user_query: str,
    workspace: str,
    category: str | None = None,
    use_cloud: bool | None = None,
) -> str:
    """Query ChromaDB codebase memory for this workspace with LLM synthesis.
    Retrieves top FETCH_CAP results from ChromaDB, filters by
    QUERY_DISTANCE_THRESHOLD (L2 distance), optionally re-ranks via
    ENABLE_LEXICAL_RERANK, trims to top 5, then synthesizes answer
    via DeepSeek or Ollama. Auto-routes via _should_use_cloud.
    Returns a JSON string with 'answer' and 'evidence' keys.
    Args:
        user_query: The question or topic to look up.
        workspace:  Workspace identifier (UUID, short UUID, or display name).
        category:   Optional filter to scope results by tag.
        use_cloud:  True = force DeepSeek. False = force Ollama.
                    None = auto-route (recommended).
    """
    if not user_query or not user_query.strip():
        return "Query cannot be empty. What would you like to know about the codebase?"

    try:
        workspace_id, display_name, workspace_path = _resolve_workspace(
            workspace)
        collection = _get_collection(workspace_id)

        # 1. Semantic retrieval — scoped to this workspace's collection
        # Strip source-table request phrases from the search query so DeepSeek
        # doesn't try to acknowledge/deny a table it can't see (we prepend it).
        import re as _re
        search_query = _re.sub(
            r'([. ]*[Ss]how (me )?(the )?([Ss]ources?( table| chart)?)[. ]*)',
            '', user_query
        ).strip() or user_query
        where = {"category": category} if category else None
        results = collection.query(
            query_texts=[search_query],
            n_results=FETCH_CAP,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        # Check if anything was retrieved
        if not docs:
            log.info("query_memory | no documents retrieved for workspace=%s query=%r",
                     workspace_id, user_query)
            context = "No specific code snippets found in memory."
            relevant_for_evidence = []
        else:
            # Distance threshold — ChromaDB returns L2 distances; filter out results
            # that are too dissimilar. Tune via QUERY_DISTANCE_THRESHOLD in .env.
            # (0 = identical, higher = less similar; >1.5 is typically noise)
            relevant = [
                (doc, meta, dist)
                for doc, meta, dist in zip(docs, metas, distances)
                if dist <= QUERY_DISTANCE_THRESHOLD
            ]

            if not relevant:
                best = min(distances)
                log.info(
                    "query_memory | no results below threshold (best dist=%.3f) workspace=%s query=%r",
                    best, workspace_id, user_query,
                )
                context = "No specific code snippets found below distance threshold."
                relevant_for_evidence = []
            else:
                log.info(
                    "query_memory | %d/%d results passed threshold for workspace=%s",
                    len(relevant), len(docs), workspace_id,
                )

                # Lexical re-ranking — reorder by keyword-overlap boost.
                # Pure reorder: nothing is dropped. Weight is tuned to nudge
                # within the ~0.156 1/dist valid-hit spread without overriding
                # genuinely closer semantic results.
                if ENABLE_LEXICAL_RERANK:
                    query_terms = set(user_query.lower().split())
                    
                    reranked_relevant = []
                    for doc, meta, dist in relevant:
                        name = (meta or {}).get("name", "").lower()
                        text = doc.lower()
                        hits = sum(
                            1 for t in query_terms if t in name or t in text)
                        # Guard against divide-by-zero for exact vector matches (dist=0)
                        inv_dist = 1.0 / dist if dist > 1e-6 else 1000000.0
                        rerank_score = inv_dist + (hits * LEXICAL_RERANK_WEIGHT)
                        reranked_relevant.append((doc, meta, dist, rerank_score))

                    reranked_relevant = sorted(
                        reranked_relevant, key=lambda item: item[3], reverse=True)
                    log.info(
                        "query_memory | lexical re-rank applied, top result: %s",
                        (reranked_relevant[0][1] or {}).get("name", "unknown"),
                    )
                    relevant_for_evidence = reranked_relevant
                else:
                    # Add a placeholder for the rerank score when it's disabled
                    relevant_for_evidence = [(doc, meta, dist, None) for doc, meta, dist in relevant]


                # Final number of reranked results passed to synthesis — kept separate from
                # FETCH_CAP (which only controls the pre-rerank candidate pool size) to cap
                # answer scope and cost regardless of how wide FETCH_CAP is set.
                relevant_for_evidence = relevant_for_evidence[:5]

                # Build location-tagged context and sources list
                context_parts = []
                evidence_list = []
                for doc, meta, dist, rerank_score in relevant_for_evidence:
                    meta = meta or {}
                    src_file = meta.get("source_file", "")
                    lineno = meta.get("lineno", "")
                    name = meta.get("name", "")
                    entity_type = meta.get("entity_type", "")
                    parent = meta.get("parent_class", "")

                    evidence_item = {
                        "source_file": src_file,
                        "lineno": lineno,
                        "name": name,
                        "entity_type": entity_type,
                        "l2_distance": dist,
                    }
                    if rerank_score is not None:
                        evidence_item["rerank_score"] = rerank_score
                    evidence_list.append(evidence_item)

                    score, score_label = _get_score_tuple(evidence_item)
                    score_str = f"{score:.2f} {score_label}" if score is not None else "no score"

                    location_label = ""
                    if src_file and lineno:
                        location = f"{src_file}:{lineno}"
                        if name and entity_type:
                            label = f"{name} ({entity_type})"
                            if parent:
                                label += f" in {parent}"
                            location_label = f"[{location}] {label} — score: {score_str}"
                        else:
                            location_label = f"[{location}] — score: {score_str}"

                    if location_label:
                        context_parts.append(f"{location_label}\n{doc}")
                    else:
                        context_parts.append(doc)

                context = "\n\n".join(context_parts)

        # 2. Route and synthesise
        if _should_use_cloud(user_query, use_cloud):
            answer = await _query_deepseek(context, search_query, workspace_id)
        else:
            answer = await _query_ollama(context, search_query, workspace_id)

        def _format_sources_block(evidence_list: list[dict]) -> str:
            if not evidence_list:
                return ""
            lines = ["\n\nSources:"]
            for ev in evidence_list:
                score, score_label = _get_score_tuple(ev)
                loc = ev.get("source_file", "unknown")
                if ev.get("lineno") not in (None, ""):
                    loc += f":{ev['lineno']}"
                line = f"* {loc} — {score:.2f} ({score_label})" if isinstance(score, (int, float)) else f"* {loc} (no score)"
                note = ev.get("note")
                if note:
                    line += f" — {note}"
                lines.append(line)
            return "\n".join(lines)

        # Final return — plain string again, no JSON
        return answer + _format_sources_block(evidence_list if 'evidence_list' in locals() else [])

    except Exception as exc:
        log.error("query_memory failed: %s", exc)
        return f"Memory query failed — {exc}"


async def _query_deepseek(context: str, user_query: str, workspace_id: str) -> str:
    """Call DeepSeek via OpenAI client with cache-optimised message structure.
    System message (fixed role + project brief from _build_system_message)
    is stable across calls for KV cache prefix matching. Retrieved context
    goes in the user turn. Logs cache hit/miss rates and tracks token
    usage to zerikai.db. Side effect: writes to token_usage table.
    """
    system_message = _build_system_message(workspace_id)
    model = _select_model(user_query)

    messages = [
        {"role": "system", "content": system_message},
        {
            "role": "user",
            "content": (
                f"Retrieved context from project memory:\n{context}\n\n"
                f"Query: {user_query}"
            ),
        },
    ]

    response = await asyncio.to_thread(
        ds_client.chat.completions.create,
        model=model,
        messages=messages,
        max_tokens=1024,
    )

    choice = response.choices[0]
    content = choice.message.content

    # Fallback for reasoning-enabled models (e.g. v4-pro) that populate
    # reasoning_content instead of (or in addition to) content.
    if not content:
        content = getattr(choice.message, "reasoning_content", None)

    if not content:
        log.warning(
            "_query_deepseek | empty response | model=%s | finish_reason=%s",
            model, choice.finish_reason
        )

    # Log cache performance — watch this to verify prefix stability is working
    usage = getattr(response, "usage", None)
    if usage:
        hit = getattr(usage, "prompt_cache_hit_tokens",  0)
        miss = getattr(usage, "prompt_cache_miss_tokens", 0)
        total = hit + miss
        hit_pct = round(hit / total * 100) if total else 0
        log.info(
            "DeepSeek cache | workspace=%s | model=%s | hit=%d | miss=%d | hit_rate=%d%%",
            workspace_id, model, hit, miss, hit_pct,
        )

        # Track token usage to SQLite
        _track_token_usage(workspace_id, "query", model, usage)

    return content or ""


async def _query_ollama(context: str, user_query: str, workspace_id: str) -> str:
    """Synthesize an answer via local Ollama using ol_client.generate.
    Combines project brief from _load_project_context with retrieved
    ChromaDB context. Runs on OLLAMA_MODEL — zero cost, zero network
    latency. Pure read — no side effects beyond the API call.
    """
    brief = _load_project_context(workspace_id)
    prompt = (
        "You are a project memory assistant. Answer concisely and technically.\n\n"
        "=== STRICT ATTRIBUTION RULES ===\n"
        "1. GROUNDING: Base your answer EXCLUSIVELY on the provided context. If information is not present, say 'I don't have this information'.\n"
        "2. SIGNATURE TRUTH: When explaining a function/class, use only the signature and logic provided in its specific context block. Do not attribute logic from helpers (e.g. _extract_*) to the top-level caller unless explicitly stated.\n"
        "3. NO HALLUCINATION: Do not invent details; verify every claim against the context.\n\n"
        f"Project Brief:\n{brief}\n\n"
        f"Retrieved Context:\n{context}\n\n"
        f"Query: {user_query}\n\n"
        "Final Synthesis (Answer):"
    )
    result = await asyncio.to_thread(
        ol_client.generate,
        model=OLLAMA_MODEL,
        prompt=prompt,
    )
    return result["response"].strip()


# ---------------------------------------------------------------------------
# Tool: list_memory
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_memory(
    workspace: str,
    category: str | None = None,
    limit: int = 10,
) -> str:
    """List raw ChromaDB memory entries for this workspace.
    Reads from the ChromaDB collection via collection.get with optional
    category filter. Use for auditing what has been indexed — not for
    answering code questions (use query_memory for that). Pure read,
    no side effects.
    Args:
        workspace: Workspace identifier (UUID, short UUID, or display name).
        category:  Optional tag filter.
        limit:     Max entries to return (default 10).
    """
    try:
        workspace_id, display_name, _ = _resolve_workspace(workspace)
        collection = _get_collection(workspace_id)

        where = {"category": category} if category else None
        results = collection.get(where=where, limit=limit)

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])

        if not docs:
            return f"[{workspace_id}] No memories stored yet."

        lines = [f"Workspace: {workspace_id}\n"]
        for doc, meta in zip(docs, metas):
            tag = meta.get("category", "general")
            lines.append(f"  [{tag}] {doc[:120]}...")

        return "\n".join(lines)

    except Exception as exc:
        log.error("list_memory failed: %s", exc)
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Tool: list_workspaces
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_workspaces() -> str:
    """List all known workspaces from zerikai.db and ChromaDB.
    Scans .brain/contexts/*.md brief files and ChromaDB memory_*
    collections, cross-referencing workspace_registry for display
    names. Reports brief and memory presence per workspace. Pure
    read — no side effects.
    """
    try:
        context_dir = Path(DB_PATH) / "contexts"
        context_dir.mkdir(parents=True, exist_ok=True)

        briefs = list(context_dir.glob("*.md"))
        collections = [c.name for c in db_client.list_collections()]

        if not briefs and not collections:
            return "No workspaces initialised yet. Run `init_workspace` to get started."

        # Get all workspace UUIDs from briefs and collections
        workspace_ids = {f.stem for f in briefs} | {
            c.replace("memory_", "") for c in collections if c.startswith("memory_")
        }

        # Query workspace registry for display names
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Build lookup map of UUID -> display_name
        uuid_to_name = {}
        for wid in workspace_ids:
            cursor.execute(
                "SELECT display_name FROM workspace_registry WHERE workspace_uuid = ?", (wid,))
            row = cursor.fetchone()
            if row:
                uuid_to_name[wid] = row["display_name"]
            else:
                # Fallback for old workspaces not in registry yet
                uuid_to_name[wid] = wid

        conn.close()

        lines = ["Known workspaces:\n"]
        for wid in sorted(workspace_ids, key=lambda w: uuid_to_name.get(w, w)):
            has_brief = (context_dir / f"{wid}.md").exists()
            has_collection = f"memory_{wid}" in collections
            display_name = uuid_to_name.get(wid, wid)

            # Show display name with short UUID hint if it's a UUID
            if len(wid) > 16 and "-" in wid:  # Likely a UUID
                id_display = f"{display_name} ({wid[:8]})"
            else:
                id_display = display_name

            lines.append(
                f"  {id_display}  "
                f"[brief={'Y' if has_brief else 'N'}]  "
                f"[memory={'Y' if has_collection else 'N'}]"
            )

        return "\n".join(lines)

    except Exception as exc:
        log.error("list_workspaces failed: %s", exc)
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Tool: resolve_workspace
# ---------------------------------------------------------------------------

@mcp.tool()
async def resolve_workspace(identifier: str) -> str:
    """Resolve a workspace identifier to its filesystem path via zerikai.db sqlite3.
    Three-tier routing: exact UUID → short UUID LIKE → display_name.
    Query workspace_registry table. Helper for agents without filesystem
    context. Pure read — no side effects.
    Args:
        identifier: Workspace UUID (full or first 8 chars), or display name
    Returns:
        The absolute filesystem path to use with other workspace tools
    Example:
        resolve_workspace("b2e5077c") → "d:/users/kike/projects/zerikai_memory"
        resolve_workspace("zerikai_memory") → "d:/users/kike/projects/zerikai_memory"
    """
    try:
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Try exact UUID match first
        cursor.execute(
            "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE workspace_uuid = ?",
            (identifier,)
        )
        row = cursor.fetchone()

        # Try short UUID match (first 8 chars)
        if not row and len(identifier) >= 8:
            cursor.execute(
                "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE workspace_uuid LIKE ?",
                (f"{identifier}%",)
            )
            row = cursor.fetchone()

        # Try display name match
        if not row:
            cursor.execute(
                "SELECT workspace_uuid, display_name, workspace_path FROM workspace_registry WHERE display_name = ?",
                (identifier,)
            )
            row = cursor.fetchone()

        conn.close()

        if not row:
            return (
                f"No workspace found matching '{identifier}'.\n"
                f"Run `list_workspaces` to see available workspaces."
            )

        return (
            f"Workspace: {row['display_name']}\n"
            f"UUID: {row['workspace_uuid']}\n"
            f"Path: {row['workspace_path']}\n\n"
            f"Use this path with other tools:\n"
            f'  query_memory(workspace_path="{row["workspace_path"]}", ...)\n'
            f'  scan_workspace(workspace_path="{row["workspace_path"]}", ...)'
        )

    except Exception as exc:
        log.error("resolve_workspace failed: %s", exc)
        return f"ERROR: Could not resolve workspace — {exc}"


# ---------------------------------------------------------------------------
# Tool: update_brief
# ---------------------------------------------------------------------------

@mcp.tool()
async def update_brief(workspace: str, new_content: str) -> str:
    """Replace the project brief in .brain/contexts/<id>.md with new markdown.
    Resolves workspace via zerikai.db, then overwrites the brief file.
    Use after significant architectural changes. Side effect: writes to
    filesystem. No versioning — overwrites existing content.
    Args:
        workspace:   Workspace identifier (UUID, short UUID, or display name).
        new_content: The full markdown content for the new brief.
    """
    try:
        workspace_id, display_name, _ = _resolve_workspace(workspace)
        context_dir = Path(DB_PATH) / "contexts"
        context_file = context_dir / f"{workspace_id}.md"

        context_file.write_text(new_content, encoding="utf-8")
        return f"Brief updated for workspace `{display_name}`."
    except Exception as exc:
        log.error("update_brief failed: %s", exc)
        return f"ERROR: Could not update brief — {exc}"


# ---------------------------------------------------------------------------
# Tool: get_brief
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_brief(workspace: str) -> str:
    """Retrieve the current project brief from .brain/contexts/<id>.md.
    Resolves workspace via zerikai.db, then reads the brief file.
    Returns guidance on init_workspace + scan_workspace if no brief
    exists. Pure read — no side effects.
    Args:
        workspace: Workspace identifier (UUID, short UUID, or display name).
    """
    try:
        workspace_id, display_name, workspace_path = _resolve_workspace(
            workspace)
        context_dir = Path(DB_PATH) / "contexts"
        context_file = context_dir / f"{workspace_id}.md"

        if not context_file.exists():
            return (
                f"No brief found for workspace `{display_name}`.\n"
                f"Run `init_workspace` followed by `scan_workspace` to generate one."
            )

        brief_content = context_file.read_text(encoding="utf-8")
        return brief_content
    except Exception as exc:
        log.error("get_brief failed: %s", exc)
        return f"ERROR: Could not retrieve brief — {exc}"


# ---------------------------------------------------------------------------
# Background scan worker
# ---------------------------------------------------------------------------

async def _background_scan(
    workspace_id: str,
    display_name: str,
    workspace_root: Path,
    patterns: list[str],
    collection,
    old_ids: set[str],
    category: str,
    progress: ScanProgress,
    force_refresh_brief: bool,
) -> None:
    """Run the full 5-phase scan loop in background with asyncio concurrency.
    Phase 1: Collect eligible files (respects .memignore, _TEXT_EXTENSIONS,
    _MAX_FILE_BYTES). Phase 2: Concurrent processing via Semaphore(4) for
    tree-sitter parsing + Semaphore(2) for LLM summarization. Phase 3:
    Aggregate results and batch-upsert to ChromaDB via collection.upsert.
    Phase 4: Purge stale memories (old_ids - scanned_ids). Phase 5:
    Fire-and-forget brief synthesis via _background_brief_synthesis.
    Errors are logged, not propagated. Updates ScanProgress in _scans.
    """
    try:
        # ── Phase 1: Collect eligible files ──────────────────────────
        files: list[Path] = []
        skipped = 0
        for fp in sorted(workspace_root.rglob("*")):
            if not fp.is_file():
                continue
            if _is_ignored(fp, workspace_root, patterns):
                skipped += 1
                continue
            if fp.suffix.lower() not in _TEXT_EXTENSIONS:
                skipped += 1
                continue
            if fp.stat().st_size > _MAX_FILE_BYTES:
                skipped += 1
                log.info("_background_scan | too large, skipping: %s", fp)
                continue
            files.append(fp)

        progress.total_files = len(files)
        progress.skipped = skipped
        log.info(
            "_background_scan | %d files queued, %d skipped by filters",
            len(files), skipped,
        )

        # ── Phase 2: Concurrent processing ───────────────────────────
        _parse_sem = asyncio.Semaphore(4)  # limit concurrent file parsing
        _llm_sem = asyncio.Semaphore(2)    # limit concurrent LLM calls

        # Per-worker result: entities to batch-write, or saved/skipped/error status
        EntityBatch = tuple[list[str], list[str],
                            list[dict]]  # ids, docs, metas
        WorkerResult = tuple[Path, str, int, EntityBatch | None]
        #                           rel_path, status, entity_count, optional batch

        results: list[WorkerResult] = []
        scanned_ids: set[str] = set()

        async def process_file(fp: Path) -> WorkerResult:
            """Process one file: tree-sitter entity extraction or chunk+LLM summarization.
            Routes by file extension: supported extensions go through tree-sitter
            (_extract_python/_extract_js_like), others via chunking + DeepSeek/Ollama.
            Upserts entity batches directly to ChromaDB collection via batch lists.
            Side effect: calls save_to_memory for chunked files, populates scanned_ids."""
            rel_path = fp.relative_to(workspace_root).as_posix()
            last_modified_ts = datetime.fromtimestamp(
                fp.stat().st_mtime, timezone.utc
            ).isoformat()
            ext = fp.suffix.lower()

            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
                if not content.strip():
                    return (fp, "skipped", 0, None)

                # Tree-sitter extraction
                if ext in get_supported_extensions():
                    try:
                        entities = extract_entities(content, rel_path)
                    except Exception as exc:
                        log.warning(
                            "_background_scan | tree-sitter parse failed for %s: %s",
                            rel_path, exc,
                        )
                        entities = []

                    if entities:
                        batch_ids: list[str] = []
                        batch_docs: list[str] = []
                        batch_metas: list[dict] = []
                        for entity in entities:
                            doc_id = hashlib.md5(
                                f"{workspace_id}:{rel_path}:{entity.name}:{entity.lineno}"
                                .encode()
                            ).hexdigest()
                            meta = {
                                "category": category,
                                "workspace": workspace_id,
                                "source_file": rel_path,
                                "source_type": "",
                                "language": entity.language,
                                "entity_type": entity.entity_type,
                                "name": entity.name,
                                "lineno": entity.lineno,
                                "end_lineno": entity.end_lineno,
                                "parent_class": entity.parent_class or "",
                                "return_type": entity.return_type or "",
                                "has_docstring": entity.docstring is not None,
                                "params_count": len(entity.params),
                                "last_modified": last_modified_ts,
                            }
                            if entity.decorators:
                                meta["decorators"] = entity.decorators
                            batch_ids.append(doc_id)
                            batch_docs.append(entity.document_text)
                            batch_metas.append(meta)
                        return (fp, "entities", len(entities), (batch_ids, batch_docs, batch_metas))

                # Skip bare files when configured
                if ext in SKIP_BARE_FILES:
                    log.info(
                        "_background_scan | skipped bare %s (no entities): %s",
                        ext, rel_path,
                    )
                    return (fp, "skipped", 0, None)

                # Non-code files: chunk + LLM summarization
                chunks = _chunk_file_content(content)
                total_chunks = len(chunks)
                for chunk_idx, chunk_text in enumerate(chunks):
                    if total_chunks > 1:
                        chunk_header = (
                            f"### {rel_path} "
                            f"[chunk {chunk_idx + 1}/{total_chunks}]\n"
                        )
                    else:
                        chunk_header = f"### {rel_path}\n"
                    labelled_chunk = f"{chunk_header}{chunk_text}"
                    if total_chunks > 1:
                        chunk_source_id = f"{rel_path}::chunk_{chunk_idx + 1}"
                    else:
                        chunk_source_id = rel_path
                    async with _llm_sem:
                        await save_to_memory(
                            content=labelled_chunk,
                            workspace=workspace_id,
                            category=category,
                            source_id=chunk_source_id,
                            last_modified=last_modified_ts,
                        )
                        doc_id = hashlib.md5(
                            f"{workspace_id}:{chunk_source_id}".encode()
                        ).hexdigest()
                        scanned_ids.add(doc_id)
                    log.info(
                        "_background_scan | saved: %s [chunk %d/%d]",
                        rel_path, chunk_idx + 1, total_chunks,
                    )
                return (fp, "saved", 0, None)

            except Exception as exc:
                log.error("_background_scan | error reading %s: %s", fp, exc)
                return (fp, "error", 0, None)

        # Run workers with semaphore
        async def worker(fp: Path) -> WorkerResult:
            """Semaphore-guarded wrapper around process_file for concurrent scanning.
            Limits concurrent file processing to _parse_sem (4). Forwards all
            results unchanged. Pure passthrough — no side effects."""
            async with _parse_sem:
                return await process_file(fp)

        results = list(await asyncio.gather(*[worker(f) for f in files]))

        # ── Phase 3: Aggregate results, batch-write entities ─────────
        saved = 0
        entity_count = 0
        errors = 0
        all_ids: list[str] = []
        all_docs: list[str] = []
        all_metas: list[dict] = []

        # Deduplicate to prevent ChromaDB upsert collisions
        unique_entities: dict[str, tuple[str, dict]] = {}

        for fp, status, count, batch in results:
            if status == "entities":
                saved += 1
                entity_count += count
                if batch:
                    ids, docs, metas = batch
                    for i in range(len(ids)):
                        unique_entities[ids[i]] = (docs[i], metas[i])
            elif status == "saved":
                saved += 1
            elif status == "error":
                errors += 1
            # "skipped" — counted during Phase 1

        for doc_id, (doc_text, meta) in unique_entities.items():
            all_ids.append(doc_id)
            all_docs.append(doc_text)
            all_metas.append(meta)
            scanned_ids.add(doc_id)

        if all_ids:
            with _db_lock:
                collection.upsert(
                    documents=all_docs,
                    metadatas=all_metas,
                    ids=all_ids,
                )
            log.info(
                "_background_scan | batch upserted %d entities from %d files",
                len(all_ids), saved,
            )

        progress.scanned = saved + errors
        progress.entities = entity_count
        progress.errors = errors

        # ── Phase 4: Purge stale ─────────────────────────────────────
        stale_ids = list(old_ids - scanned_ids)
        if stale_ids:
            with _db_lock:
                collection.delete(ids=stale_ids)
            log.info("_background_scan | purged %d stale memories for %s",
                     len(stale_ids), workspace_id)

        # ── Phase 5: Brief synthesis ──────────────────────────────────
        context_dir = Path(DB_PATH) / "contexts"
        context_file = context_dir / f"{workspace_id}.md"
        brief_status = "No (Cache Stable)"
        trigger_brief = False

        if context_file.exists():
            current_text = context_file.read_text(
                encoding="utf-8", errors="ignore")
            if force_refresh_brief or (UNINITIALIZED_MARKER in current_text):
                trigger_brief = True
                brief_status = "In progress (background, about 20 seconds)"
        elif saved > 0:
            trigger_brief = True
            brief_status = "In progress (background, about 20 seconds)"

        if trigger_brief:
            context_dir.mkdir(parents=True, exist_ok=True)
            progress.brief_status = "In progress (background, about 20 seconds)"
            log.info(
                "_background_scan | triggering brief synthesis for %s", display_name)
            asyncio.create_task(_background_brief_synthesis(
                workspace_id, display_name, context_file, progress))

        progress.completed = True
        progress.brief_status = brief_status

        log.info(
            "_background_scan | complete for %s — saved=%d skipped=%d errors=%d",
            display_name, saved, progress.skipped, errors,
        )

    except Exception as exc:
        log.error("_background_scan | fatal error for %s: %s",
                  display_name, exc)
        progress.completed = True
        progress.brief_status = "failed"
        progress.errors += 1


# ---------------------------------------------------------------------------
# Tool: scan_workspace
# ---------------------------------------------------------------------------

@mcp.tool()
async def scan_workspace(
    workspace: str,
    category: str = "codebase",
    force_refresh_brief: bool = False,
) -> str:
    """Start a background scan of the workspace via _background_scan task.
    Returns immediately; use scan_status() to track progress. Respects
    .memignore patterns and _TEXT_EXTENSIONS. Idempotent: overwrites
    existing files with deterministic md5 IDs, automatically purges
    stale memories. Re-scanning cancels any in-progress scan. Side
    effect: launches asyncio.create_task for background processing.
    Args:
        workspace:  Workspace identifier (UUID, short UUID, or display name).
        category:   Tag applied to every saved memory (default 'codebase').
        force_refresh_brief: If True, forces a new brief synthesis after
                             scanning. Use when architecture has changed.
    """
    workspace_id, display_name, workspace_path = _resolve_workspace(workspace)
    workspace_root = Path(workspace_path)
    if not workspace_root.is_dir():
        return f"ERROR: {workspace_path} is not a directory."

    patterns = _load_memignore(workspace_path)
    log.info(
        "scan_workspace | root=%s | memignore patterns=%d",
        workspace_path, len(patterns),
    )

    collection = _get_collection(workspace_id)
    with _db_lock:
        existing = collection.get(where={"category": category})
        old_ids = set(existing.get("ids", []))

    # Cancel any in-progress scan for this workspace
    old_task = _scan_tasks.pop(workspace_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
        log.info("scan_workspace | cancelled previous scan for %s", display_name)

    total_files = sum(1 for _ in workspace_root.rglob("*") if _.is_file())

    progress = ScanProgress(
        workspace_id=workspace_id,
        display_name=display_name,
        total_files=total_files,
    )
    _scans[workspace_id] = progress

    task = asyncio.create_task(_background_scan(
        workspace_id=workspace_id,
        display_name=display_name,
        workspace_root=workspace_root,
        patterns=patterns,
        collection=collection,
        old_ids=old_ids,
        category=category,
        progress=progress,
        force_refresh_brief=force_refresh_brief,
    ))
    _scan_tasks[workspace_id] = task

    return (
        f"Background scan started for `{display_name}`\n"
        f"Workspace ID: `{workspace_id[:8]}`\n"
        f"- Files queued: {total_files}\n"
        f"- To check progress, copy/paste this into your chat:\n"
        f"  scan_status(workspace=\"{workspace_id[:8]}\")\n"
        f"- When scan finishes, the brief auto-generates — then query_memory as usual."
    )


# ---------------------------------------------------------------------------
# Tool: get_token_usage
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_token_usage(
    workspace: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Return DeepSeek API token usage and cost from zerikai.db sqlite3.
    Queries token_usage table with optional workspace and date range
    filters. Reports call count, token totals, cache hit rate, and
    total cost in USD. Pure read — no side effects.
    Args:
        workspace:  Optional workspace identifier (UUID, short UUID, or display name). If None, shows all workspaces.
        start_date: Optional ISO date string (YYYY-MM-DD) for filtering. Defaults to beginning of time.
        end_date:   Optional ISO date string (YYYY-MM-DD) for filtering. Defaults to now.
    """
    if not ENABLE_TOKEN_TRACKING:
        return "Token tracking is disabled. Set ENABLE_TOKEN_TRACKING=true in config to enable."

    try:
        conn = sqlite3.connect(str(ZERIKAI_DB))
        conn.row_factory = sqlite3.Row

        # Build query with optional filters
        conditions = []
        params = []
        workspace_display_name = None

        if workspace:
            workspace_id, workspace_display_name, _ = _resolve_workspace(
                workspace)
            conditions.append("workspace_id = ?")
            params.append(workspace_id)

        if start_date:
            conditions.append("timestamp >= ?")
            params.append(f"{start_date}T00:00:00")

        if end_date:
            conditions.append("timestamp < ?")
            # Add 1 day to include the entire end_date
            end_dt = datetime.fromisoformat(end_date) + timedelta(days=1)
            params.append(end_dt.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        query = f"""
            SELECT
                COUNT(*) as call_count,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                SUM(cache_hit_tokens) as total_cache_hits,
                SUM(cache_miss_tokens) as total_cache_misses,
                SUM(estimated_cost_usd) as total_cost
            FROM token_usage
            WHERE {where_clause}
        """

        result = conn.execute(query, params).fetchone()
        conn.close()

        if not result or result["call_count"] == 0:
            return "No token usage data found for the specified criteria."

        total_cache = result["total_cache_hits"] + result["total_cache_misses"]
        cache_hit_rate = (result["total_cache_hits"] /
                          total_cache * 100) if total_cache > 0 else 0

        scope = f"Workspace: {workspace_display_name}" if workspace_display_name else "All Workspaces"
        date_range = []
        if start_date:
            date_range.append(f"from {start_date}")
        if end_date:
            date_range.append(f"to {end_date}")
        date_info = " ".join(date_range) if date_range else "all time"

        return (
            f"Token Usage Report\n"
            f"{scope} ({date_info})\n\n"
            f"API Calls: {result['call_count']}\n"
            f"Prompt Tokens: {result['total_prompt_tokens']:,}\n"
            f"Completion Tokens: {result['total_completion_tokens']:,}\n"
            f"Cache Hits: {result['total_cache_hits']:,}\n"
            f"Cache Misses: {result['total_cache_misses']:,}\n"
            f"Cache Hit Rate: {cache_hit_rate:.1f}%\n"
            f"Total Cost: ${result['total_cost']:.4f} USD"
        )

    except Exception as exc:
        log.error("get_token_usage failed: %s", exc)
        return f"ERROR: Could not retrieve token usage — {exc}"


# ---------------------------------------------------------------------------
# Tool: get_cache_stats
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_cache_stats(workspace: str | None = None) -> str:
    """Show DeepSeek cache hit/miss rates by operation from zerikai.db sqlite3.
    Groups token_usage rows by operation, reporting call count, total
    hits/misses, and average hit rate per operation type. Pure read.
    Args:
        workspace: Optional workspace identifier (UUID, short UUID, or display name). If None, shows all workspaces.
    """
    if not ENABLE_TOKEN_TRACKING:
        return "Token tracking is disabled. Set ENABLE_TOKEN_TRACKING=true in config to enable."

    try:
        conn = sqlite3.connect(str(ZERIKAI_DB))
        conn.row_factory = sqlite3.Row

        conditions = []
        params = []
        workspace_display_name = None

        if workspace:
            workspace_id, workspace_display_name, _ = _resolve_workspace(
                workspace)
            conditions.append("workspace_id = ?")
            params.append(workspace_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        query = f"""
            SELECT
                operation,
                COUNT(*) as call_count,
                SUM(cache_hit_tokens) as total_hits,
                SUM(cache_miss_tokens) as total_misses,
                AVG(CAST(cache_hit_tokens AS FLOAT) /
                    (cache_hit_tokens + cache_miss_tokens) * 100) as avg_hit_rate
            FROM token_usage
            WHERE {where_clause}
            GROUP BY operation
            ORDER BY call_count DESC
        """

        results = conn.execute(query, params).fetchall()
        conn.close()

        if not results:
            return "No cache statistics available."

        scope = f"Workspace: {workspace_display_name}" if workspace_display_name else "All Workspaces"

        lines = [
            f"Cache Statistics\n{scope}\n",
            f"{'Operation':<20} {'Calls':<8} {'Hit Rate':<12} {'Hits':<12} {'Misses':<12}",
            "-" * 70,
        ]

        for row in results:
            hit_rate = row["avg_hit_rate"] if row["avg_hit_rate"] else 0
            lines.append(
                f"{row['operation']:<20} "
                f"{row['call_count']:<8} "
                f"{hit_rate:>6.1f}%     "
                f"{row['total_hits']:>10,}  "
                f"{row['total_misses']:>10,}"
            )

        return "\n".join(lines)

    except Exception as exc:
        log.error("get_cache_stats failed: %s", exc)
        return f"ERROR: Could not retrieve cache stats — {exc}"


# ---------------------------------------------------------------------------
# Tool: get_cost_report
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_cost_report(
    workspace: str | None = None,
    period: str = "all",
) -> str:
    """Generate DeepSeek cost breakdown by operation from zerikai.db sqlite3.
    Groups token_usage rows by operation and model. Supports period
    filtering (today, week, month, all). Pure read — no side effects.
    Args:
        workspace: Optional workspace identifier (UUID, short UUID, or display name). If None, shows all workspaces.
        period:    Time period filter: "today", "week", "month", or "all" (default).
    """
    if not ENABLE_TOKEN_TRACKING:
        return "Token tracking is disabled. Set ENABLE_TOKEN_TRACKING=true in config to enable."

    try:
        conn = sqlite3.connect(str(ZERIKAI_DB))
        conn.row_factory = sqlite3.Row

        # Calculate date range based on period
        now = datetime.now(timezone.utc)
        start_date = None

        if period == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start_date = now - timedelta(days=7)
        elif period == "month":
            start_date = now - timedelta(days=30)

        conditions = []
        params = []
        workspace_display_name = None

        if workspace:
            workspace_id, workspace_display_name, _ = _resolve_workspace(
                workspace)
            conditions.append("workspace_id = ?")
            params.append(workspace_id)

        if start_date:
            conditions.append("timestamp >= ?")
            params.append(start_date.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        query = f"""
            SELECT
                operation,
                model,
                COUNT(*) as call_count,
                SUM(estimated_cost_usd) as total_cost,
                AVG(estimated_cost_usd) as avg_cost_per_call
            FROM token_usage
            WHERE {where_clause}
            GROUP BY operation, model
            ORDER BY total_cost DESC
        """

        results = conn.execute(query, params).fetchall()

        # Get overall totals
        total_query = f"""
            SELECT
                COUNT(*) as total_calls,
                SUM(estimated_cost_usd) as grand_total
            FROM token_usage
            WHERE {where_clause}
        """

        totals = conn.execute(total_query, params).fetchone()
        conn.close()

        if not results:
            return "No cost data available for the specified criteria."

        scope = f"Workspace: {workspace_display_name}" if workspace_display_name else "All Workspaces"
        period_label = period.capitalize() if period != "all" else "All Time"

        lines = [
            f"Cost Report\n{scope} — {period_label}\n",
            f"{'Operation':<20} {'Model':<20} {'Calls':<8} {'Total Cost':<15} {'Avg/Call'}",
            "-" * 85,
        ]

        for row in results:
            lines.append(
                f"{row['operation']:<20} "
                f"{row['model']:<20} "
                f"{row['call_count']:<8} "
                f"${row['total_cost']:>10.4f}     "
                f"${row['avg_cost_per_call']:>8.6f}"
            )

        lines.append("-" * 85)
        lines.append(
            f"{'TOTAL':<20} {'':<20} "
            f"{totals['total_calls']:<8} "
            f"${totals['grand_total']:>10.4f}"
        )

        return "\n".join(lines)

    except Exception as exc:
        log.error("get_cost_report failed: %s", exc)
        return f"ERROR: Could not generate cost report — {exc}"


# ---------------------------------------------------------------------------
# Tool: purge_usage_data
# ---------------------------------------------------------------------------

@mcp.tool()
async def purge_usage_data(before_date: str) -> str:
    """Delete token tracking records from zerikai.db sqlite3 before a date.
    Validates date format, counts matching records, then executes DELETE
    on token_usage table. Irreversible — cannot be undone. Side effect:
    permanently deletes rows from the database.
    Args:
        before_date: ISO date string (YYYY-MM-DD). Records before this date will be deleted.
    """
    if not ENABLE_TOKEN_TRACKING:
        return "Token tracking is disabled. Set ENABLE_TOKEN_TRACKING=true in config to enable."

    try:
        # Validate date format
        datetime.fromisoformat(before_date)

        conn = sqlite3.connect(str(ZERIKAI_DB))

        # Count records to be deleted
        count_query = "SELECT COUNT(*) as count FROM token_usage WHERE timestamp < ?"
        count = conn.execute(
            count_query, (f"{before_date}T00:00:00",)).fetchone()[0]

        if count == 0:
            conn.close()
            return f"No records found before {before_date}."

        # Delete records
        conn.execute("DELETE FROM token_usage WHERE timestamp < ?",
                     (f"{before_date}T00:00:00",))
        conn.commit()
        conn.close()

        log.info("Purged %d token usage records before %s", count, before_date)
        return f"Successfully deleted {count} token usage records before {before_date}."

    except ValueError:
        return f"ERROR: Invalid date format '{before_date}'. Use YYYY-MM-DD format."
    except Exception as exc:
        log.error("purge_usage_data failed: %s", exc)
        return f"ERROR: Could not purge usage data — {exc}"


@mcp.tool()
async def debug_workspace_id(test_path: str) -> str:
    """Show what workspace ID _derive_workspace_id would generate from a path.
    Normalizes the path (case, separators, trailing slashes) and displays
    the resulting UUID and display name. Useful for debugging path
    normalization issues. Pure read — no side effects.
    Args:
        test_path: The workspace path to test
    """
    try:
        # Show the normalized path used for hashing (matches _derive_workspace_id logic)
        test_path_stripped = test_path.rstrip('/\\')

        # Convert to absolute if needed
        if not os.path.isabs(test_path_stripped):
            test_path_stripped = os.path.abspath(test_path_stripped)

        # Normalize using os.path.normcase (respects platform case-sensitivity)
        normalized_path = os.path.normcase(
            os.path.normpath(test_path_stripped))
        normalized_path = normalized_path.replace('\\', '/')

        # Get the actual workspace UUID and display name
        workspace_uuid, display_name = _derive_workspace_id(test_path)

        return (
            f"Test path: {test_path}\n"
            f"Normalized: {normalized_path}\n"
            f"Workspace UUID: {workspace_uuid}\n"
            f"Display name: {display_name}"
        )
    except Exception as exc:
        log.error("debug_workspace_id failed: %s", exc)
        return f"ERROR: {exc}"


@mcp.tool()
async def merge_workspaces(source_workspace_id: str, target_workspace_id: str) -> str:
    """Merge ChromaDB collections from source into target workspace, then delete source.
    Consolidates duplicate workspace IDs from path variations. Uses
    collection.upsert to move data, then deletes source via
    db_client.delete_collection. Irreversible — cannot be undone.
    Side effect: permanently deletes source collection.
    Args:
        source_workspace_id: The workspace ID to merge FROM (will be deleted after merge)
        target_workspace_id: The workspace ID to merge INTO (will receive all data)
    """
    try:
        # Use the global db_client singleton (DB_PATH/vector_db/) instead of
        # instantiating a local PersistentClient, which would point at DB_PATH
        # and open a second chroma.sqlite3 in the wrong directory, causing every
        # collection lookup to fail silently.

        # Check if both workspaces exist
        all_collections = {c.name for c in db_client.list_collections()}

        if source_workspace_id not in all_collections:
            return f"ERROR: Source workspace '{source_workspace_id}' not found."

        if target_workspace_id not in all_collections:
            return f"ERROR: Target workspace '{target_workspace_id}' not found."

        if source_workspace_id == target_workspace_id:
            return "ERROR: Source and target workspace IDs must be different."

        source_col = db_client.get_collection(source_workspace_id)
        target_col = db_client.get_collection(target_workspace_id)

        # Get all data from source
        source_data = source_col.get(
            include=["metadatas", "documents", "embeddings"])

        if not source_data["ids"]:
            log.info("Source workspace '%s' is empty, deleting it",
                     source_workspace_id)
            db_client.delete_collection(source_workspace_id)
            return f"Source workspace '{source_workspace_id}' was empty and has been deleted."

        # Add all source data to target
        # Note: If there are ID conflicts, this will overwrite the target data
        target_col.upsert(
            ids=source_data["ids"],
            documents=source_data["documents"],
            metadatas=source_data["metadatas"],
            embeddings=source_data["embeddings"] if source_data["embeddings"] else None
        )

        # Delete source workspace
        db_client.delete_collection(source_workspace_id)

        count = len(source_data["ids"])
        log.info(
            "Merged %d items from workspace '%s' into '%s'",
            count, source_workspace_id, target_workspace_id
        )
        return (
            f"Successfully merged {count} items from '{source_workspace_id}' "
            f"into '{target_workspace_id}'. Source workspace deleted."
        )

    except Exception as exc:
        log.error("merge_workspaces failed: %s", exc)
        return f"ERROR: Could not merge workspaces — {exc}"


# ---------------------------------------------------------------------------
# Tool: scan_status
# ---------------------------------------------------------------------------

@mcp.tool()
async def scan_status(workspace: str) -> str:
    """Return progress of a running or completed background scan from _scans.
    Reads ScanProgress for the workspace: files scanned/skipped/errored,
    entities indexed, brief synthesis status, elapsed time, and ETA.
    Pure read — no side effects.
    Args:
        workspace: Workspace identifier (UUID, short UUID, or display name).
    """
    try:
        workspace_id, display_name, _ = _resolve_workspace(workspace)
        progress = _scans.get(workspace_id)
        if not progress:
            return (
                f"No active or recent scan for `{display_name}`.\n"
                f"To start one, ask your agent: scan_workspace(workspace=\"{display_name}\")"
            )

        elapsed = datetime.now(timezone.utc).timestamp() - progress.started_at

        if progress.completed:
            parts = [
                f"Scan complete for `{display_name}`",
                f"- Files: {progress.scanned} scanned, {progress.skipped} skipped, {progress.errors} errors",
                f"- Entities: {progress.entities} indexed",
                f"- Brief: {progress.brief_status}",
                f"- Duration: {elapsed:.0f}s",
            ]
            return "\n".join(parts)

        # Estimate remaining time
        if progress.scanned > 0:
            rate = progress.scanned / elapsed if elapsed > 0 else 0
            remaining = (progress.total_files - progress.scanned) / \
                rate if rate > 0 else 0
            eta = f"~{remaining:.0f}s remaining"
        else:
            eta = "estimating..."

        return (
            f"Scan in progress for `{display_name}`\n"
            f"- Files: {progress.scanned}/{progress.total_files} | "
            f"{progress.errors} errors\n"
            f"- Entities: {progress.entities} indexed\n"
            f"- Brief: {progress.brief_status}\n"
            f"- Elapsed: {elapsed:.0f}s | {eta}"
        )

    except Exception as exc:
        log.error("scan_status failed: %s", exc)
        return f"ERROR: Could not retrieve scan status — {exc}"


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    # If run with --sse (like in our Docker container), serve over the network
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="0.0.0.0", port=8200)
    else:
        # Standard local IDE execution
        mcp.run()
