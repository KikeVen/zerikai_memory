import os
import asyncio
import threading
import logging
import hashlib
import re
import fnmatch
from uuid import uuid4
from pathlib import Path

import ollama
from ollama import Client
from mcp.server.fastmcp import FastMCP
from chromadb import PersistentClient
from openai import OpenAI

from config import (
    DEFAULT_MEMORY_MODE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL_FAST,
    DEEPSEEK_MODEL_PRO,
    OLLAMA_MODEL,
    OLLAMA_HOST,
    DB_PATH,
    CLOUD_ESCALATION_WORD_COUNT,
    CLOUD_ESCALATION_KEYWORDS,
    SYNTHESIZE_WITH_CLOUD,
)

from logging.handlers import RotatingFileHandler

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
# Workspace helpers
# ---------------------------------------------------------------------------

def _derive_workspace_id(workspace_path: str) -> str:
    """
    Converts a filesystem path into a stable, human-readable collection ID.

    e.g. "/home/user/projects/saas-dashboard" → "saas_dashboard_88bc2e"

    Uses folder name + 6-char MD5 of the full path to handle projects
    that share the same folder name (e.g. two repos both named 'api').
    
    Normalizes paths to lowercase and resolves to absolute form to ensure
    consistency across different path representations (d:\\ vs D:\\, / vs \\).
    """
    if not workspace_path:
        return "default"
    # Normalize the path to prevent duplicate workspace IDs due to:
    # - Case differences (d:\ vs D:\)
    # - Separator differences (/ vs \)
    # - Relative vs absolute paths
    normalized_path = str(Path(workspace_path).resolve()).lower()
    name = os.path.basename(normalized_path)
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    short_hash = hashlib.md5(normalized_path.encode()).hexdigest()[:6]
    return f"{slug}_{short_hash}"

UNINITIALIZED_MARKER = "<!-- ZERIKAI_PENDING_SYNTHESIS -->"

async def _synthesize_deep_brief(workspace_id: str, use_cloud: bool = False) -> str:
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
        workspace_id: The workspace identifier
        use_cloud: If True, uses DeepSeek for higher quality (small cost).
                   If False, uses Ollama (free, may include noise).
    """
    log.info("_synthesize_deep_brief | Starting iterative synthesis for %s", workspace_id)
    collection = _get_collection(workspace_id)
    
    # Check if we have any codebase data at all
    with _db_lock:
        check = collection.get(where={"category": "codebase"}, limit=1)
    if not check.get("documents"):
        return f"# Project Brief: {workspace_id}\n\nNo codebase files found during scan."
    
    # Section definitions with semantic queries and format-guided prompts
    sections = [
        {
            "heading": "## Overview",
            "query": "project purpose main features domain application what does system do readme",
            "prompt_template": (
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
                "Based on the following file summaries from the codebase, write the Overview section. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                f"Use this format (use the actual project name, not a placeholder):\n"
                f"`{workspace_id}` is a [type] system designed to [purpose]. "
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
                "Based on the following file summaries from the codebase, list the Technical Stack. "
                "Be concise and direct. Do not preface your answer with any introductory sentence. "
                "Only list third-party libraries explicitly found in dependency files (requirements.txt, package.json, pyproject.toml, etc.). "
                "Do not include standard library modules or internal framework utilities.\n\n"
                "Use this format:\n"
                "* **Backend:** [Language/Framework]\n"
                "* **Database:** [Database Technology]\n"
                "* **Workflow Management:** [Platform/Tool if any]\n"
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
                "Based on the following file summaries from the codebase, describe the Core Architecture. "
                "Be concise and direct. Start directly with 'The application consists of the following layers:' — no other introductory text.\n\n"
                "Use this format:\n"
                "The application consists of the following layers:\n"
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
                "Based on the following file summaries from the codebase, explain the Purpose. "
                "Be concise and direct. Do not preface your answer with any introductory sentence.\n\n"
                f"Use this format (replace [Project Name] with the actual name inferred from the codebase):\n"
                f"`{workspace_id}` aims to [goal] using [tech summary] to reduce [user burden] "
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
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
                f"You are a senior software architect analyzing the `{workspace_id}` project. "
                "Based on the following file summaries from the codebase, describe the Data Flow & Request Lifecycle. "
                "Be concise and direct. Start directly with 'A typical request flows through:' — no other introductory text.\n\n"
                "Use this format:\n"
                "A typical request flows through:\n"
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
    ]
    
    brief_parts = [f"# Project Brief: {workspace_id}\n"]
    
    for section in sections:
        log.info("_synthesize_deep_brief | Generating: %s", section["heading"])
        
        # Semantic search for section-specific context
        with _db_lock:
            results = collection.query(
                query_texts=[section["query"]],
                n_results=15,
                where={"category": "codebase"},
            )
        
        docs = results.get("documents", [[]])[0]
        if not docs:
            # Fallback: grab any available context
            with _db_lock:
                fallback = collection.get(where={"category": "codebase"}, limit=15)
            docs = fallback.get("documents", [])
        
        context = "\n\n".join(docs)
        prompt = section["prompt_template"].format(context=context)
        
        try:
            if use_cloud:
                # Use DeepSeek for high-quality synthesis
                response = await asyncio.to_thread(
                    ds_client.chat.completions.create,
                    model=DEEPSEEK_MODEL_FAST,
                    messages=[
                        {"role": "system", "content": "You are a senior software architect."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0,
                    max_tokens=2048,
                )
                section_content = response.choices[0].message.content.strip()
            else:
                # Use Ollama for free local synthesis
                result = await asyncio.to_thread(
                    ol_client.generate,
                    model=OLLAMA_MODEL,
                    prompt=prompt,
                    options={"temperature": 0},
                )
                section_content = result["response"].strip()
            
            brief_parts.append(f"\n{section['heading']}\n\n{section_content}\n")
            log.info("_synthesize_deep_brief | ✓ %s complete", section["heading"])
        except Exception as exc:
            log.error("_synthesize_deep_brief | Failed on %s: %s", section["heading"], exc)
            brief_parts.append(f"\n{section['heading']}\n\n(Section generation failed: {exc})\n")
    
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
    cached — covering your largest token block at the 10x cheaper rate.

    Minimum 64 tokens required for a cache unit. A well-populated project
    brief (stack, conventions, architecture decisions) easily reaches
    500–1000 tokens, making cache savings substantial.
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
    return role_instruction + project_context


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
_MAX_FILE_BYTES = 64_000


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
    pro_triggers = {"architect", "architecture", "design", "tradeoff", "trade-off", "audit"}
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
    workspace_id = _derive_workspace_id(workspace_path)
    context_dir = Path(DB_PATH) / "contexts"
    context_dir.mkdir(parents=True, exist_ok=True)
    context_file = context_dir / f"{workspace_id}.md"

    if context_file.exists():
        return (
            f"Brief already exists for workspace `{workspace_id}`.\n"
            f"Edit: {context_file}"
        )

    template = f"{UNINITIALIZED_MARKER}\n# Project Brief — {workspace_id}\n\n(Waiting for initial scan... run `scan_workspace` to auto-generate the architecture brief)"
    context_file.write_text(template, encoding="utf-8")
    
    return (
        f"Workspace initialised: `{workspace_id}`\n"
        f"Run `scan_workspace` to build project memory and generate the brief."
    )


# ---------------------------------------------------------------------------
# Tool: save_to_memory
# ---------------------------------------------------------------------------

@mcp.tool()
async def save_to_memory(
    content: str,
    workspace_path: str,
    category: str = "general",
    source_id: str | None = None,
) -> str:
    """
    Summarises and saves an architectural decision, project fact, or
    technical note to this workspace's persistent vector memory.

    Args:
        content:        The raw content to remember.
        workspace_path: Absolute path to the project root (for isolation).
        category:       Tag for filtering (e.g. 'architecture', 'api', 'decision').
        source_id:      Optional unique identifier (like a file path) to prevent duplicates on re-scans.
    """
    try:
        workspace_id = _derive_workspace_id(workspace_path)

        result = await asyncio.to_thread(
            ol_client.generate,
            model=OLLAMA_MODEL,
            prompt=(
                f"Summarise the following for long-term technical memory "
                f"in 2–3 concise sentences:\n\n{content}"
            ),
        )
        summary = result["response"].strip()

        # Use a deterministic ID if source_id is provided so re-scans overwrite instead of duplicate
        if source_id:
            doc_id = hashlib.md5(f"{workspace_id}:{source_id}".encode()).hexdigest()
        else:
            doc_id = str(uuid4())

        with _db_lock:
            collection = _get_collection(workspace_id)
            collection.upsert(
                documents=[summary],
                metadatas=[{"category": category, "workspace": workspace_id}],
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
    workspace_path: str,
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
        user_query:     The question or topic to look up.
        workspace_path: Absolute path to the project root.
        category:       Optional filter to scope results by tag.
        use_cloud:      True = force DeepSeek. False = force Ollama.
                        None = auto-route (recommended).
    """
    try:
        workspace_id = _derive_workspace_id(workspace_path)
        collection = _get_collection(workspace_id)

        # 1. Semantic retrieval — scoped to this workspace's collection
        where = {"category": category} if category else None
        results = collection.query(
            query_texts=[user_query],
            n_results=5,
            where=where,
        )
        docs = results.get("documents", [[]])[0]
        context = "\n".join(docs) if docs else "No prior context found for this query."

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
        hit  = getattr(usage, "prompt_cache_hit_tokens",  0)
        miss = getattr(usage, "prompt_cache_miss_tokens", 0)
        total = hit + miss
        hit_pct = round(hit / total * 100) if total else 0
        log.info(
            "DeepSeek cache | workspace=%s | model=%s | hit=%d | miss=%d | hit_rate=%d%%",
            workspace_id, model, hit, miss, hit_pct,
        )

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
    workspace_path: str,
    category: str | None = None,
    limit: int = 10,
) -> str:
    """
    Lists stored memories for this workspace, optionally filtered by category.

    Args:
        workspace_path: Absolute path to the project root.
        category:       Optional tag filter.
        limit:          Max entries to return (default 10).
    """
    try:
        workspace_id = _derive_workspace_id(workspace_path)
        collection = _get_collection(workspace_id)

        where = {"category": category} if category else None
        results = collection.get(where=where, limit=limit)

        docs  = results.get("documents", [])
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

        lines = ["Known workspaces:\n"]
        workspace_ids = {f.stem for f in briefs} | {
            c.replace("memory_", "") for c in collections if c.startswith("memory_")
        }
        for wid in sorted(workspace_ids):
            has_brief      = (context_dir / f"{wid}.md").exists()
            has_collection = f"memory_{wid}" in collections
            lines.append(
                f"  {wid}  "
                f"[brief={'Y' if has_brief else 'N'}]  "
                f"[memory={'Y' if has_collection else 'N'}]"
            )

        return "\n".join(lines)

    except Exception as exc:
        log.error("list_workspaces failed: %s", exc)
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Tool: update_brief
# ---------------------------------------------------------------------------

@mcp.tool()
async def update_brief(workspace_path: str, new_content: str) -> str:
    """
    Updates the project brief for a workspace. 
    Use this to keep the project context current as the architecture evolves.
    
    Args:
        workspace_path: Absolute path to the project root.
        new_content:    The full markdown content for the new brief.
    """
    try:
        workspace_id = _derive_workspace_id(workspace_path)
        context_dir = Path(DB_PATH) / "contexts"
        context_file = context_dir / f"{workspace_id}.md"
        
        context_file.write_text(new_content, encoding="utf-8")
        return f"Brief updated for workspace `{workspace_id}`."
    except Exception as exc:
        log.error("update_brief failed: %s", exc)
        return f"ERROR: Could not update brief — {exc}"


# ---------------------------------------------------------------------------
# Tool: get_brief
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_brief(workspace_path: str) -> str:
    """
    Retrieves the current project brief for a workspace.
    Use this to review the synthesized project context and architecture overview.
    
    Args:
        workspace_path: Absolute path to the project root.
    """
    try:
        workspace_id = _derive_workspace_id(workspace_path)
        context_dir = Path(DB_PATH) / "contexts"
        context_file = context_dir / f"{workspace_id}.md"
        
        if not context_file.exists():
            return (
                f"No brief found for workspace `{workspace_id}`.\n"
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
    workspace_path: str,
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
        workspace_path: Absolute path to the project root.
        category:       Tag applied to every saved memory (default 'codebase').
        force_refresh_brief: If True, forces the synthesis of a new project brief.
    """
    workspace_root = Path(workspace_path)
    workspace_id = _derive_workspace_id(workspace_path)
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
    saved   = 0
    skipped = 0
    errors  = 0

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
            labelled = f"### {rel_path}\n{content}"

            await save_to_memory(
                content=labelled,
                workspace_path=workspace_path,
                category=category,
                source_id=rel_path,
            )
            
            # Record that this ID is still valid
            doc_id = hashlib.md5(f"{workspace_id}:{rel_path}".encode()).hexdigest()
            scanned_ids.add(doc_id)
            
            saved += 1
            log.info("scan_workspace | saved: %s", rel_path)

        except Exception as exc:
            errors += 1
            log.error("scan_workspace | error reading %s: %s", file_path, exc)

    # Purge stale memories: anything that was in the DB but NOT found in this scan
    stale_ids = list(old_ids - scanned_ids)
    if stale_ids:
        with _db_lock:
            collection.delete(ids=stale_ids)
        log.info("scan_workspace | purged %d stale memories for %s", len(stale_ids), workspace_id)

    # Brief Synthesis Logic
    context_dir = Path(DB_PATH) / "contexts"
    context_file = context_dir / f"{workspace_id}.md"
    
    brief_synthesized = False
    if context_file.exists():
        current_text = context_file.read_text(encoding="utf-8", errors="ignore")
        needs_synthesis = force_refresh_brief or (UNINITIALIZED_MARKER in current_text)
        
        if needs_synthesis:
            log.info("scan_workspace | triggering deep brief synthesis for %s", workspace_id)
            new_brief = await _synthesize_deep_brief(workspace_id, use_cloud=SYNTHESIZE_WITH_CLOUD)
            context_file.write_text(new_brief, encoding="utf-8")
            brief_synthesized = True
    else:
        # First scan - auto-generate the brief if we have any data
        if saved > 0:
            log.info("scan_workspace | first scan detected, auto-generating brief for %s", workspace_id)
            new_brief = await _synthesize_deep_brief(workspace_id, use_cloud=SYNTHESIZE_WITH_CLOUD)
            context_dir.mkdir(parents=True, exist_ok=True)
            context_file.write_text(new_brief, encoding="utf-8")
            brief_synthesized = True

    stats = (
        f"Scan complete for `{workspace_id}`\n"
        f"- Saved/Updated: {saved}\n"
        f"- Skipped: {skipped}\n"
        f"- Purged: {len(stale_ids)}\n"
        f"- Errors: {errors}\n"
        f"- Brief Synthesized: {'Yes' if brief_synthesized else 'No (Cache Stable)'}"
    )
    return stats


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    # If run with --sse (like in our Docker container), serve over the network
    if "--sse" in sys.argv:
        mcp.run(transport="sse", host="0.0.0.0", port=8200)
    else:
        # Standard local IDE execution
        mcp.run()
