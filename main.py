import asyncio
import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import threading
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
    ENABLE_TOKEN_TRACKING,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    QUERY_DISTANCE_THRESHOLD,
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


# ---------------------------------------------------------------------------
# Token usage tracking database
# ---------------------------------------------------------------------------
def _init_db():
    """Initialize SQLite database for token tracking and workspace registry."""
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
    """
    Records DeepSeek API token usage to SQLite.

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
    """
    Derives a stable workspace UUID from a filesystem path.

    Returns a tuple of (workspace_uuid, display_name).

    The UUID is generated once per unique normalized path and stored in the workspace_registry
    table within zerikai.db. Subsequent calls with the same path (even with different formatting,
    case, or trailing slashes) return the same UUID.

    Display name is derived from the folder name and used for human-readable output.

    Args:
        workspace_path: Filesystem path to the workspace

    Returns:
        tuple: (workspace_uuid, display_name)

    Example:
        >>> _derive_workspace_id("/home/user/projects/my-app")
        ('a3f8c2d1-5e9f-4b7a-9c8d-1e2f3a4b5c6d', 'my_app')
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
    """
    Resolves a workspace identifier (UUID, short UUID, or display name) to its details.

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


UNINITIALIZED_MARKER = "<!-- ZERIKAI_PENDING_SYNTHESIS -->"


async def _synthesize_deep_brief(workspace_id: str, display_name: str, use_cloud: bool = False) -> str:
    """
    Generates a comprehensive project brief using iterative section-by-section synthesis.

    Instead of overwhelming the model with 50 summaries at once, this approach:
    1. Queries the vector DB with section-specific semantic searches
    2. Feeds only relevant context (10-15 results) to the model per section
    3. Uses simple, direct prompts like the original approach
    4. Builds the brief incrementally, one section at a time

    This dramatically improves output quality for small local models by:
    - Reducing context window pressure
    - Providing focused, relevant information per section
    - Using clear, straightforward instructions

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
            "query": "project purpose main features domain application what does system do readme",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, write the Overview section. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                f"Use this format (use the actual project name, not a placeholder):\n"
                f"`{display_name}` is a [type] system designed to [purpose]. "
                "It integrates with [external services] to [key function]. "
                "[One sentence on who it is for or its domain.]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Write the Overview section:"
            ),
        },
        {
            "heading": "## Technical Stack",
            "query": "dependencies requirements packages frameworks libraries database docker deployment python javascript",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, list the Technical Stack. "
                "Be concise and direct. Start directly with 'Listing only primary libraries, max 5:' — no other introductory text.\n\n"
                "IMPORTANT: Only list the 5-10 most important PRIMARY dependencies. "
                "Omit transitive dependencies (e.g., certifi, charset-normalizer, idna, etc.), "
                "low-level utilities, and standard library modules. "
                "Focus on frameworks, databases, and major integrations that define the project's architecture.\n\n"
                "Use this format:\n\n"
                "* **Backend:** [Language/Framework]\n"
                "* **Database:** [Database Technology]\n"
                "* **API Integration:** [External services and what they do]\n"
                "* **Frontend:** [UI Framework/Technology]\n"
                "* **Libraries:**\n"
                "  * [Category]: [Library names]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List the Technical Stack:"
            ),
        },
        {
            "heading": "## Core Architecture",
            "query": "architecture components structure models views api routes data flow patterns authentication services",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, describe the Core Architecture. "
                "Be concise and direct. Start directly with 'The application consists of the following layers:' — no other introductory text.\n\n"
                "Use this format:\n"
                "The application consists of the following layers:\n\n"
                "1. **Frontend:** [Technology and what it handles]\n"
                "2. **Backend:** [Framework and what it handles]\n"
                "3. **[Other Layer]:** [Technology and what it handles]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe the Core Architecture:"
            ),
        },
        {
            "heading": "## Primary Conventions",
            "query": "code organization conventions standards tests testing naming structure folders config style error handling",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, list the Primary Conventions. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format:\n"
                "* **Code Organization:** [How code is structured into directories/modules]\n"
                "* **API Documentation:** [Standard used if any]\n"
                "* **Error Handling:** [Method and logging mechanism]\n"
                "* **Database Schema:** [Where defined and how updated]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List the Primary Conventions:"
            ),
        },
        {
            "heading": "## Purpose",
            "query": "purpose goals objectives why business problem solution users target audience",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, explain the Purpose. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                f"Use this format (replace [Project Name] with the actual name inferred from the codebase):\n"
                f"`{display_name}` aims to [goal] using [tech summary] to reduce [user burden] "
                "and evaluate performance against [key objectives].\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Explain the Purpose:"
            ),
        },
        {
            "heading": "## Key Files & Directories",
            "query": "important files main entry point settings configuration models views routes directory structure",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, identify Key Files & Directories. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format:\n"
                "* **`path/to/file.ext`** - [Brief purpose]\n"
                "* **`directory/`** - [What this directory contains]\n\n"
                "Focus on entry points, configuration files, core models, main routers, and key directories. "
                "Omit test files and generic items.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "List Key Files & Directories:"
            ),
        },
        {
            "heading": "## Development & Testing",
            "query": "setup install dependencies run server test pytest commands docker development environment local",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{display_name}` project. "
                "Based on the following file summaries from the codebase, describe Development & Testing setup. "
                "Be concise and direct. Start directly with the first bullet point — no introductory text.\n\n"
                "Use this format:\n"
                "* **Setup:** [How to install dependencies and prepare environment]\n"
                "* **Running Locally:** [Command to start the development server]\n"
                "* **Testing:** [Test framework and command to run tests]\n"
                "* **Build/Deploy:** [Build process or containerization if present]\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe Development & Testing:"
            ),
        },
        {
            "heading": "## Data Flow & Request Lifecycle",
            "query": "request response flow authentication lifecycle pipeline process middleware routing data flow",
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
                "Include authentication flow if present.\n\n"
                "=== CODEBASE SUMMARIES ===\n"
                "{context}\n\n"
                "Describe Data Flow & Request Lifecycle:"
            ),
        },
        {
            "heading": "## Future Roadmap",
            "query": "todo future roadmap planned features upcoming improvements milestones backlog scaling roadmap",
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

    brief_parts = [f"# Project Brief: {display_name}\n"]

    for section in sections:
        log.info("_synthesize_deep_brief | Generating: %s", section["heading"])

        # Semantic search for section-specific context
        with _db_lock:
            total_docs = collection.count()
            fetch_count = min(75, total_docs) if total_docs > 0 else 1
            results = collection.query(
                query_texts=[section["query"]],
                n_results=fetch_count,
                where={"category": "codebase"},
            )

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        if not docs:
            # Fallback: grab any available context
            with _db_lock:
                fallback = collection.get(
                    where={"category": "codebase"}, limit=fetch_count)
            docs = fallback.get("documents", [])
            metas = fallback.get("metadatas", [])

        # Re-attach filenames stripped by Ollama summarisation.
        # source_file is stored in metadata by save_to_memory; without it the
        # "Key Files & Directories" prompt has no paths to extract and returns empty.
        #
        # Skip source_type="manual" entries (chat snippets, pasted code) — their
        # synthetic filenames would hallucinate non-existent files into the brief.
        context_parts = []
        for doc, meta in zip(docs, metas or [{}] * len(docs)):
            if (meta or {}).get("source_type") == "manual":
                continue
            src = (meta or {}).get("source_file", "")
            header = f"### {src}\n" if src else ""
            context_parts.append(f"{header}{doc}")
        context = "\n\n".join(context_parts)
        prompt = section["prompt_template"].format(context=context)

        try:
            if use_cloud:
                # Use DeepSeek for high-quality synthesis
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
                section_content = response.choices[0].message.content.strip()

                # Track token usage for brief synthesis
                usage = getattr(response, "usage", None)
                if usage:
                    _track_token_usage(
                        workspace_id, "brief_synthesis", DEEPSEEK_MODEL_FAST, usage)
            else:
                # Use Ollama for free local synthesis
                result = await asyncio.to_thread(
                    ol_client.generate,
                    model=OLLAMA_MODEL,
                    prompt=prompt,
                    options={"temperature": 0},
                )
                section_content = result["response"].strip()

            brief_parts.append(
                f"\n{section['heading']}\n\n{section_content}\n")
            log.info("_synthesize_deep_brief | ✓ %s complete",
                     section["heading"])
        except Exception as exc:
            log.error("_synthesize_deep_brief | Failed on %s: %s",
                      section["heading"], exc)
            brief_parts.append(
                f"\n{section['heading']}\n\n(Section generation failed: {exc})\n")

    final_brief = "".join(brief_parts)
    log.info("_synthesize_deep_brief | Complete for %s", workspace_id)
    return final_brief


def _get_collection(workspace_id: str):
    """Returns (or creates) the ChromaDB collection for this workspace."""
    return db_client.get_or_create_collection(f"memory_{workspace_id}")


def _load_project_context(workspace_id: str) -> str:
    """
    Loads the per-workspace project brief from .brain/contexts/<id>.md.

    This text is prepended to every DeepSeek system message as the stable
    cache prefix. The more substantive and stable this content is, the higher
    the DeepSeek KV cache hit rate — and the lower the per-query cost.

    If no brief exists yet, returns a minimal placeholder. Run
    `init_workspace` to scaffold the file for editing.
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


def _build_system_message(workspace_id: str) -> str:
    """
    Assembles the full system message for DeepSeek.

    Structure (order matters for cache hits):
      1. Fixed role instruction     — identical across ALL workspaces
      2. Per-workspace project brief — stable for the life of the project

    DeepSeek caches on PREFIX match from token 0. Because section 1 is
    always identical, at minimum the role instruction hits the cache on
    every second+ call. Once section 2 is also stable (i.e. the brief
    doesn't change between calls), the entire system message prefix is
    cached — covering your largest token block at the cheaper cache hit rate.

    Per https://api-docs.deepseek.com/guides/kv_cache, the cache system:
    - Works on a "best-effort" basis (no 100% hit guarantee)
    - Detects common prefixes across requests automatically
    - Persists cache units at request boundaries and fixed token intervals
    - Builds caches in seconds; unused caches expire in hours to days
    """
    role_instruction = (
        "You are a project memory assistant. "
        "Your role is to synthesize retrieved project context and answer "
        "the developer's query accurately and concisely. "
        "Prioritise specifics over generalities. "
        "Do not repeat the retrieved context verbatim.\n\n"
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
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".md", ".txt", ".rst",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".sql", ".sh", ".env",
    ".java", ".go", ".rs", ".c", ".cpp", ".h",
}

# Never read files larger than this (bytes).
_MAX_FILE_BYTES = 100_000  # Increased from 64KB to 100KB to include main.py

# Files larger than this (in lines) are split into chunks before indexing.
# Prevents DeepSeek from truncating structured extraction on large files.
_CHUNK_LINE_THRESHOLD = 300   # lines — files above this get chunked
_CHUNK_SIZE_LINES = 250   # lines per chunk (with overlap)
_CHUNK_OVERLAP_LINES = 20    # overlap between chunks for context continuity


def _load_memignore(workspace_path: str) -> list[str]:
    """
    Reads .memignore from the workspace root and returns a list of patterns.
    Lines starting with # and blank lines are ignored.
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
    """
    Returns True if file_path should be SKIPPED based on .memignore patterns.
    Matches the mental model of .gitignore.
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
    """
    Splits file content into overlapping line-based chunks.
    Each chunk retains `overlap` lines from the previous chunk
    for context continuity (e.g. a function signature that spans a boundary).
    Returns a list of chunk strings. Single-chunk files return a list of one.
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
    """
    Determines whether this query should be synthesised by DeepSeek or Ollama.

    Priority order:
      1. Explicit override: use_cloud=True/False from the caller always wins.
      2. Keyword escalation: architectural / strategic keywords → cloud.
      3. Length escalation: long, multi-part queries → cloud.
      4. Default fallback: DEFAULT_MEMORY_MODE from .env.
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
    """
    Within cloud mode, selects fast vs pro model.
    Pro is reserved for queries that explicitly signal deep reasoning need.
    """
    pro_triggers = {"architect", "architecture",
                    "design", "tradeoff", "trade-off", "audit"}
    if any(w in pro_triggers for w in user_query.lower().split()):
        log.info("Model → deepseek-v4-pro (reasoning query)")
        return DEEPSEEK_MODEL_PRO
    return DEEPSEEK_MODEL_FAST


# ---------------------------------------------------------------------------
# Tool: init_workspace
# ---------------------------------------------------------------------------

@mcp.tool()
async def init_workspace(workspace_path: str) -> str:
    """
    Scaffolds the project brief file for a new workspace.

    It creates a marker file indicating that the project is waiting
    for its first `scan_workspace` to automatically synthesize the brief.

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

    template = f"{UNINITIALIZED_MARKER}\n# Project Brief — {display_name}\n\n(Waiting for initial scan... run `scan_workspace` to auto-generate the architecture brief)"
    context_file.write_text(template, encoding="utf-8")

    return (
        f"Workspace registered: `{display_name}`\n"
        f"Workspace ID: `{workspace_id[:8]}`\n\n"
        f"Next step:\n"
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
    """
    Summarises and saves an architectural decision, project fact, or
    technical note to this workspace's persistent vector memory.

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
                f"Extract only the heading lines from the Markdown file below. "
                f"Headings are lines starting with #, ##, ###, or ####.\n\n"
                f"Respond ONLY in this exact format — no prose, no explanation:\n\n"
                f"description: <one sentence in plain English describing what this document covers>\n"
                f"file: <filename>\n"
                f"headings:\n"
                f"  - <heading line>\n\n"
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
    """
    Retrieves relevant context from this workspace's memory and synthesises
    an answer via Ollama (local) or DeepSeek (cloud).

    Routing is automatic:
      - Short, specific queries  → Ollama (free, instant)
      - Long or architectural queries → DeepSeek auto-escalation
      - Pass use_cloud=True/False to override the auto-router explicitly.

    Args:
        user_query: The question or topic to look up.
        workspace:  Workspace identifier (UUID, short UUID, or display name).
        category:   Optional filter to scope results by tag.
        use_cloud:  True = force DeepSeek. False = force Ollama.
                    None = auto-route (recommended).
    """
    try:
        workspace_id, display_name, workspace_path = _resolve_workspace(
            workspace)
        collection = _get_collection(workspace_id)

        # 1. Semantic retrieval — scoped to this workspace's collection
        where = {"category": category} if category else None
        results = collection.query(
            query_texts=[user_query],
            n_results=5,
            where=where,
            include=["documents", "distances"],
        )
        docs = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]

        # Hard stop — nothing retrieved at all
        if not docs:
            log.info("query_memory | no documents retrieved for workspace=%s query=%r",
                     workspace_id, user_query)
            return (
                f"No memories found in workspace `{workspace_id}` for this query.\n"
                "Run `scan_workspace` to index the codebase, or `save_to_memory` to store context manually."
            )

        # Distance threshold — ChromaDB returns L2 distances; filter out results
        # that are too dissimilar. Tune via QUERY_DISTANCE_THRESHOLD in .env.
        # (0 = identical, higher = less similar; >1.5 is typically noise)
        relevant = [(doc, dist) for doc, dist in zip(
            docs, distances) if dist <= QUERY_DISTANCE_THRESHOLD]

        if not relevant:
            best = min(distances)
            log.info(
                "query_memory | no results below threshold (best dist=%.3f) workspace=%s query=%r",
                best, workspace_id, user_query,
            )
            return (
                f"I don't know — nothing relevant found in `{workspace_id}` memory for this query "
                f"(closest match distance: {best:.2f}, threshold: {QUERY_DISTANCE_THRESHOLD}).\n"
                "Try rephrasing, or run `scan_workspace` to ensure the codebase is indexed."
            )

        log.info(
            "query_memory | %d/%d results passed threshold for workspace=%s",
            len(relevant), len(docs), workspace_id,
        )
        context = "\n".join(doc for doc, _ in relevant)

        # 2. Route and synthesise
        if _should_use_cloud(user_query, use_cloud):
            return await _query_deepseek(context, user_query, workspace_id)
        else:
            return await _query_ollama(context, user_query)

    except Exception as exc:
        log.error("query_memory failed: %s", exc)
        return f"ERROR: Memory query failed — {exc}"


async def _query_deepseek(context: str, user_query: str, workspace_id: str) -> str:
    """
    Calls DeepSeek with a cache-optimised message structure.

    Message layout (prefix stability is everything):
      - system: fixed role instruction + stable project brief  ← CACHED
      - user:   retrieved context + query                      ← varies per call

    Keeping retrieved context in the USER turn (not the system turn) means
    the system prefix never changes between calls for the same workspace,
    maximising cache hits on the largest token block.
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

    return response.choices[0].message.content


async def _query_ollama(context: str, user_query: str) -> str:
    """Local synthesis via Ollama — zero cost, zero latency on warm model."""
    prompt = (
        f"Project context:\n{context}\n\n"
        f"Query: {user_query}\n\n"
        "Answer concisely and technically:"
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
    """
    Lists stored memories for this workspace, optionally filtered by category.

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
    """
    Lists all known workspaces that have a project brief or stored memories.
    Useful for verifying isolation and seeing what the server knows about.
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
    """
    Resolves a workspace identifier (UUID, short UUID, or display name) to its filesystem path.

    This is a helper tool for agents that don't have filesystem context. Use `list_workspaces`
    to see available workspaces, then use this tool to get the path needed for other operations.

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
    """
    Updates the project brief for a workspace.
    Use this to keep the project context current as the architecture evolves.

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
    """
    Retrieves the current project brief for a workspace.
    Use this to review the synthesized project context and architecture overview.

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
# Tool: scan_workspace
# ---------------------------------------------------------------------------

@mcp.tool()
async def scan_workspace(
    workspace: str,
    category: str = "codebase",
    force_refresh_brief: bool = False,
) -> str:
    """
    Walks the entire workspace directory, respects .memignore, and saves
    every readable text file to this workspace's persistent memory.

    Idempotent and Self-Cleaning:
    - Overwrites existing files with deterministic IDs.
    - Automatically purges memories from this category that are no longer
      present or are now ignored.

    Args:
        workspace:  Workspace identifier (UUID, short UUID, or display name).
        category:   Tag applied to every saved memory (default 'codebase').
        force_refresh_brief: If True, forces the synthesis of a new project brief.
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

    # Track existing IDs in this category to perform a sync/purge at the end
    collection = _get_collection(workspace_id)
    with _db_lock:
        existing = collection.get(where={"category": category})
        old_ids = set(existing.get("ids", []))

    scanned_ids = set()
    saved = 0
    skipped = 0
    errors = 0

    for file_path in sorted(workspace_root.rglob("*")):
        # Skip directories themselves
        if not file_path.is_file():
            continue

        # Skip files that match .memignore
        if _is_ignored(file_path, workspace_root, patterns):
            skipped += 1
            log.debug("scan_workspace | ignored: %s", file_path)
            continue

        # Skip non-text extensions
        if file_path.suffix.lower() not in _TEXT_EXTENSIONS:
            skipped += 1
            continue

        # Skip files that are too large
        if file_path.stat().st_size > _MAX_FILE_BYTES:
            skipped += 1
            log.info("scan_workspace | too large, skipping: %s", file_path)
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            if not content.strip():
                skipped += 1
                continue

            rel_path = file_path.relative_to(workspace_root).as_posix()

            # Capture filesystem modification time for enriched metadata
            last_modified_ts = datetime.fromtimestamp(
                file_path.stat().st_mtime, timezone.utc
            ).isoformat()

            ext = file_path.suffix.lower()

            # -------------------------------------------------------------------
            # tree-sitter extraction for supported code files
            # Each function/class becomes a separate ChromaDB document.
            # No API calls, no token costs, no empty responses.
            # -------------------------------------------------------------------
            if ext in get_supported_extensions():
                try:
                    entities = extract_entities(content, rel_path)
                except Exception as exc:
                    log.warning(
                        "scan_workspace | tree-sitter parse failed for %s: %s",
                        rel_path, exc,
                    )
                    # Fall through to chunk-based save below
                    entities = []

                if entities:
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

                        with _db_lock:
                            collection.upsert(
                                documents=[entity.document_text],
                                metadatas=[meta],
                                ids=[doc_id],
                            )
                        scanned_ids.add(doc_id)
                    saved += 1
                    log.info(
                        "scan_workspace | indexed %d entities from %s [%s]",
                        len(
                            entities), rel_path, entities[0].language if entities else "unknown",
                    )
                    continue  # skip the chunk-based path below

            # -------------------------------------------------------------------
            # Non-code files: keep existing chunking + LLM summarization
            # -------------------------------------------------------------------
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
                    "scan_workspace | saved: %s [chunk %d/%d]",
                    rel_path, chunk_idx + 1, total_chunks,
                )

            saved += 1  # increment once per file, not per chunk

        except Exception as exc:
            errors += 1
            log.error("scan_workspace | error reading %s: %s", file_path, exc)

    # Purge stale memories: anything that was in the DB but NOT found in this scan
    stale_ids = list(old_ids - scanned_ids)
    if stale_ids:
        with _db_lock:
            collection.delete(ids=stale_ids)
        log.info("scan_workspace | purged %d stale memories for %s",
                 len(stale_ids), workspace_id)

    # Brief Synthesis Logic
    context_dir = Path(DB_PATH) / "contexts"
    context_file = context_dir / f"{workspace_id}.md"

    brief_synthesized = False
    if context_file.exists():
        current_text = context_file.read_text(
            encoding="utf-8", errors="ignore")
        needs_synthesis = force_refresh_brief or (
            UNINITIALIZED_MARKER in current_text)

        if needs_synthesis:
            log.info(
                "scan_workspace | triggering deep brief synthesis for %s", display_name)
            new_brief = await _synthesize_deep_brief(workspace_id, display_name, use_cloud=SYNTHESIZE_WITH_CLOUD)
            context_file.write_text(new_brief, encoding="utf-8")
            brief_synthesized = True
    else:
        # First scan - auto-generate the brief if we have any data
        if saved > 0:
            log.info(
                "scan_workspace | first scan detected, auto-generating brief for %s", display_name)
            new_brief = await _synthesize_deep_brief(workspace_id, display_name, use_cloud=SYNTHESIZE_WITH_CLOUD)
            context_dir.mkdir(parents=True, exist_ok=True)
            context_file.write_text(new_brief, encoding="utf-8")
            brief_synthesized = True

    stats = (
        f"Scan complete for `{display_name}`\n"
        f"Workspace ID: `{workspace_id[:8]}`\n"
        f"- Saved/Updated: {saved}\n"
        f"- Skipped: {skipped}\n"
        f"- Purged: {len(stale_ids)}\n"
        f"- Errors: {errors}\n"
        f"- Brief Synthesized: {'Yes' if brief_synthesized else 'No (Cache Stable)'}\n\n"
        f"Query this workspace:\n"
        f"  query_memory(workspace=\"{workspace_id[:8]}\", user_query=\"...\")\n"
        f"  get_brief(workspace=\"{workspace_id[:8]}\")"
    )
    return stats


# ---------------------------------------------------------------------------
# Tool: get_token_usage
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_token_usage(
    workspace: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """
    Returns DeepSeek API token usage and cost statistics.

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
    """
    Shows cache hit/miss rates by operation type.

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
    """
    Generates cost breakdown by operation.

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
    """
    Deletes token tracking records before the specified date.
    Use for cleaning up historical data. Cannot be undone.

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
    """
    Diagnostic tool: Shows what workspace ID would be generated from a given path.
    Useful for debugging path normalization issues.

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
    """
    Merge all data from source workspace into target workspace.
    This consolidates duplicate workspace IDs that were created due to path variations.

    WARNING: This moves briefs, memory, and embeddings from source to target and then
    deletes the source workspace. Cannot be undone.

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
if __name__ == "__main__":
    import sys
    # If run with --sse (like in our Docker container), serve over the network
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="0.0.0.0", port=8200)
    else:
        # Standard local IDE execution
        mcp.run()
