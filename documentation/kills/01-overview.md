# Skills Overview

## What a Skill Is

A skill is a structured instruction file (`SKILL.md`) placed inside a named directory
under `skills/` (e.g. `embedding-docstring/SKILL.md`). It is registered in your IDE
as a callable capability — your AI assistant reads it and executes its protocol when
you invoke it by name.

Skills are not scripts you run directly. They are agent-facing instruction sets that
govern how the assistant audits, writes, or modifies specific kinds of content in
your workspace. Each skill declares its execution mode, its scope, its entity
parser, its checklist, and its approval loop.

---

## Why Docstring Quality Is the Foundation of the Entire System

ChromaDB stores entities as vector embeddings generated from their docstring text.
When you run `query_memory`, the system performs a semantic search over those
embeddings and synthesizes an answer from the top matches.

**The retrieval is only as good as the text that was embedded.**

A docstring that says `"Handles user login"` embeds as a generic phrase. It will
not surface when someone queries `"how does JWT token validation work"` — even if
that function is the exact one being asked about. A docstring that says `"Validates
a JWT bearer token using PyJWT. Raises AuthError if the token is expired or the
signature is invalid. Called by every protected Flask route."` embeds with
specificity — it surfaces on JWT queries, on auth queries, on Flask route queries,
and on error-handling queries.

Embeddings match **words**, not concepts. The system cannot infer that
`"key-value store"` means Redis, that `"PDF parser"` means pdfminer.six, or that
`"frontend library"` means HTMX. If those words are not in the docstring, the
entity is invisible during retrieval for those terms.

**A vague or missing docstring is a silent failure.** No error is raised. The entity
simply does not surface during relevant queries. The project brief comes out thin.
Query answers come back weaker. There is no indication to the user that source
material was insufficient — the system just returns the best of what it has.

This is why the `embedding-docstring` skill exists, and why it must run **before**
`scan_workspace`. Fixing docstrings after indexing requires a full rescan.

---

## What Each Docstring Section Does for Retrieval

Every entity's docstring is divided into a prose body and structured sections
(`Args`, `Returns`, `Raises`). Each part serves a distinct retrieval purpose.

### Prose Body (the embedding core)

This is what gets embedded. It is the only part the vector search sees. Everything
the semantic retrieval pipeline needs to find this entity must be in the prose body.

The prose body must contain:

**What it does** — one sentence summary. This anchors the embedding to the entity's
primary function. Without it, the entity has no retrievable identity.

**Technology names** — name every library, protocol, or external system explicitly.
`"Redis"` not `"cache"`. `"pdfminer.six"` not `"PDF extraction"`. `"HTMX hx-post"`
not `"frontend form submission"`. Embeddings are trained on real words. Concepts
without their proper names are invisible.

**Routing and branching** — if the function makes a decision (`uses X for .py
files, falls back to Y for others`), that decision logic must be documented. Without
it, queries about that routing path return nothing.

**Decorator and route context** — for Flask routes, the HTTP method and path must
appear in the prose. A function decorated with `@app.route('/convert',
methods=['POST'])` must say `"Handles POST /convert"` in the docstring. The
decorator line is not parsed as docstring text — only the string body is embedded.

**Guarantees** — idempotency, atomicity, ordering, or `"no guarantees"` where
relevant. These are high-signal terms for architecture queries.

**Side effects** — what the function writes, calls, or mutates beyond its return
value. Filesystem writes, database mutations, external API calls. Without these,
queries about a function's impact return incomplete answers.

### Args / Returns / Raises

These sections are not embedded into the vector store. They are for human readers
and for the LLM synthesis step. They have no size limit — scale them with the
signature. They do not affect retrieval but they do affect the quality of answers
when the entity is retrieved.

---

## Size and Format Constraints

These constraints exist because of how tree-sitter extracts docstrings into ChromaDB.
Violations do not raise errors — they silently truncate or fragment the embedding.

**Prose body limit: 6 lines or 400 characters**, whichever is shorter. Longer prose
does not improve retrieval — it dilutes the embedding signal and wastes the fixed
token budget that ChromaDB allocates per entity.

**No blank lines anywhere in the docstring.** A blank line causes tree-sitter to
fragment the docstring or truncate it silently, dropping routing logic, guarantees,
or entire `Args` sections from the embedding. Write the entire docstring as one
continuous block.

**Trim rule:** if the prose body line count exceeds the function body line count,
trim to match. A 3-line utility function with a 4-line docstring is acceptable —
do not trim aggressively on short functions.

---

## How to Apply This Manually (Without the Skill)

If you prefer to write or audit docstrings without invoking the skill, follow this
checklist for every function, method, class, and UPPER_CASE constant in your
codebase:

**Python functions and methods:**
```python
def function_name(param: str) -> int:
    """Summary sentence. Names technology explicitly (e.g., PostgreSQL, Redis).
    States routing logic if branches exist. States HTTP method and path for routes.
    States guarantees (idempotent, atomic) or side effects (writes to filesystem).
    Args:
        param: What it represents.
    Returns:
        What is returned and what the caller should do with it.
    Raises:
        SpecificError: When and why this is raised.
    """
```

**Python constants:**
```python
# Summary. What the constant controls, its valid range or allowed values,
# and any technology it configures (e.g., "controls PostgreSQL pool size; range 1–100").
MAX_POOL_SIZE = 20
```
Each constant needs its own comment block immediately above it. A shared block
above a group of constants only reaches the first one — tree-sitter stops at the
first assignment.

**HTML boundary elements** (`<section>`, `<main>`, `<nav>`, `<header>`, `<footer>`,
`<form>`, `<div id="...">`, `<div class="...">`):
```html
<!-- Results container. Displays converted output returned by POST /convert.
     Updated by HTMX hx-swap targeting this div. Empty on initial page load.
     Shows error partial if conversion fails; shows formatted output otherwise. -->
<div id="results">
```
A comment under 4 lines for a complex section is too short. If the comment block
does not exist, write one from scratch — this is not optional.

**HTML inline JavaScript** (inside `<script>` blocks without `src`):
```html
<script>
/**
 * Summary sentence. Names any DOM APIs, HTMX attributes, or browser events
 * the function interacts with. States side effects beyond return value.
 * @param {Event} event - The browser event object.
 */
function handleDragOver(event) { ... }
</script>
```
Use JSDoc `/** */` format for inline JS — not `<!-- -->` HTML comments.

---

## Available Skills

| Skill | Directory | Purpose |
|---|---|---|
| Embedding-Docstring Optimizer | `embedding-docstring/SKILL.md` | Audits and rewrites docstrings across Python, JS, TS, and HTML for vector embedding quality. |

See [embedding-docstring.md](embedding-docstring.md) for the full reference.
