# Embedding-Docstring Skill

**Location:** `embedding-docstring/SKILL.md`

Audits and fixes docstrings, comment blocks, and inline documentation across an
entire workspace — or a specific file or single entity — in Python, JavaScript,
TypeScript, and HTML for vector embedding quality.

> **Run this before `scan_workspace`.** Docstrings fixed after indexing require
> a full rescan to take effect. See [configuration-reference.md](../configuration-reference.md)
> for the required setup sequence.

---

## Prerequisites

Before invoking the skill, confirm:

1. `venv` is activated and `pip install -r requirements.txt` has completed. The
   skill's workspace discovery runs a Python script — the interpreter must be
   available.

2. `.memignore` exists at the workspace root. The skill's discovery script uses it
   as the sole source of truth for what to exclude. Without it, the skill walks and
   audits every eligible file in the workspace with zero exclusions.

---

## Execution Modes

The skill declares its execution mode at the start of every run.

**CHAT MODE** (claude.ai or any non-agentic interface) — shows diffs and waits for
typed approval before applying. "Apply" means displaying the corrected block for
the user to paste manually.

**AGENTIC MODE** (Claude Code, VS Code, or any tool-executing interface) — applies
edits using `str_replace` targeting the exact existing text. Never whole-file
rewrites. One entity end-to-end before the next begins.

The approval loop (`yes` / `skip` / `stop`) is required in both modes. It is not
optional friction — it is the primary safeguard against corrupting the file.

---

## Invocation Patterns

### Workspace (most expensive — use for initial setup or after major refactor)

```
"Audit and optimise docstrings across this project using the embedding-docstring
skill, respecting .memignore."
```

The skill runs `.memignore`-filtered discovery, emits a file inventory, and waits
for `"start"` before touching any file.

To resume after an interruption:
```
"resume from src/module.py my_function"
```
Resume is always by entity name — never by line number. Line numbers shift after
every insertion.

### Single File (mid-cost — use after editing a specific file)

```
"audit src/module.py"
"fix docstrings in templates/page.html"
```

Skips workspace walk. Reads the entire file once, audits all entities.

### Single Entity (cheapest — use during active development)

```
"fix def my_function in src/module.py"
"fix <div id='results'> in templates/page.html"
```

Skips workspace walk and full file inventory. Reads the named file, locates the
entity, audits or fixes that one entity only.

---

## Workspace Discovery Script

The skill uses this exact Python script for file discovery. It respects `.memignore`
and no other exclusion source:

```python
import os
import fnmatch

def load_memignore(root):
    path = os.path.join(root, ".memignore")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]

def is_ignored(path, root, patterns):
    rel = os.path.relpath(path, root).replace("\\", "/")
    name = os.path.basename(path)
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern): return True
        if fnmatch.fnmatch(name, pattern): return True
        if pattern.endswith("/") and rel.startswith(pattern): return True
        if ("/" + pattern.rstrip("/") + "/") in ("/" + rel + "/"): return True
    return False

root = os.getcwd()
patterns = load_memignore(root)
extensions = {".py", ".js", ".ts", ".html"}
eligible = []

for dirpath, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if not is_ignored(os.path.join(dirpath, d), root, patterns)]
    for f in files:
        full = os.path.join(dirpath, f)
        if os.path.splitext(f)[1] in extensions:
            if not is_ignored(full, root, patterns):
                eligible.append(os.path.relpath(full, root).replace("\\", "/"))

eligible.sort()
```

---

## Density Checklist

Applied to the prose body of every entity. Each item is required unless the code
genuinely has nothing to say for it.

| Item | Requirement |
|---|---|
| **What it does** | One sentence summary. Always present. |
| **Decorator / route context** | For decorated functions, name the decorator. For Flask routes, state the HTTP method and path explicitly. |
| **Routing / branches** | `"Uses X for supported files, falls back to Y"` — document any decision logic. |
| **Technology names** | Name libraries and tools explicitly. `"Redis"` not `"key-value store"`. `"pdfminer.six"` not `"PDF parser"`. `"HTMX"` not `"frontend library"`. Embeddings match words, not concepts. |
| **Guarantees** | Idempotency, ordering, atomicity, or `"no guarantees"` where relevant. |
| **Side effects** | What it writes, calls, or mutates beyond its return value. |

---

## Size Rules

- **Prose body:** 6 lines or 400 characters, whichever is shorter.
- **Args / Returns / Raises:** no size limit. Scale with the signature.
- **Trim rule:** if prose body line count exceeds function body line count, trim to
  match. Only trim when the docstring is genuinely longer than the code.

---

## No Blank Lines

No blank lines anywhere in the docstring — not in the prose body, not between the
prose body and `Args`/`Returns`/`Raises`, not between structured sections. A blank
line causes tree-sitter to fragment or truncate the docstring silently, dropping
routing, guarantees, or entire `Args` sections from the embedding.

---

## Audit Status Labels

| Label | Meaning |
|---|---|
| `[PASS]` | Docstring exists and passes all checklist items |
| `[FAIL]` | Docstring exists but fails one or more items — will be rewritten |
| `[MISSING]` | No docstring at all — will be written from scratch |
| `[SKIP]` | Excluded by `.memignore` or skipped by the user |

Example output:
```
ENTITY INVENTORY — src/auth.py
  1. [constant]  MAX_RETRY_COUNT        line 12
  2. [function]  save_user              line 42
  3. [route]     convert  POST /convert line 36   @app.route('/convert', methods=['POST'])

[PASS]    save_user line 42
[FAIL]    MAX_RETRY_COUNT line 12 — Missing: valid range; Missing: what it configures
[MISSING] convert line 36 — no docstring; decorator @app.route('/convert') must appear in prose

[FILE COMPLETE] src/auth.py — 1 passed / 1 failed / 1 missing / 0 skipped
```

---

## Format Reference

**Python function:**
```python
def function_name(param: str) -> int:
    """Summary sentence. Names technology (e.g., PostgreSQL, Redis).
    States routing logic. States guarantees or side effects.
    Args:
        param: What it represents.
    Returns:
        What is returned.
    Raises:
        SpecificError: When and why.
    """
```

**Python Flask route:**
```python
@app.route('/convert', methods=['POST'])
def convert():
    """Handles POST /convert via Flask route. Uses HTMX for partial page updates.
    Branches on file validity before invoking pdfminer.six for text extraction.
    Passes extracted text to Ollama generate API for format conversion.
    Mutates filesystem by saving uploaded and converted files to a temp folder.
    No guarantee on strict output formatting from Ollama LLM response.
    """
```

**Python constant:**
```python
# Summary. What it controls, valid range, and any technology it configures
# (e.g., "controls PostgreSQL connection pool size; valid range 1–100").
MAX_POOL_SIZE = 20

# Each constant needs its own comment block. The extractor stops at MAX_POOL_SIZE.
QUERY_TIMEOUT_SEC = 30
```

**JavaScript / TypeScript:**
```javascript
/**
 * Summary sentence. Names technology (e.g., HTMX, Stripe).
 * States routing, guarantees, and side effects.
 * @param {string} param - What it represents.
 * @returns {number} What is returned.
 * @throws {SpecificError} When and why.
 */
function functionName(param) {}
```

**HTML boundary element — written from scratch:**
```html
<!-- Results container. Displays converted output returned by POST /convert.
     Updated by HTMX hx-swap targeting this div. Empty on initial page load.
     Shows error partial if conversion fails; shows formatted output otherwise. -->
<div id="results">
```

**HTML inline JavaScript:**
```html
<script>
/**
 * Handles dragover event on the drop zone element. Prevents default browser
 * behavior and adds the 'dragover' CSS class to #dropZone via classList.
 * Listens for the dragover DOM event. No side effects beyond CSS class mutation.
 * @param {DragEvent} event - The browser dragover event object.
 */
function handleDragOver(event) { ... }
</script>
```

---

## What the Skill Does Not Do

- Not a style linter. Google, NumPy, Sphinx — any format works.
- Not project-specific. No hardcoded file paths or function names. Operates on
  whatever workspace or file you provide.
- Does not batch approvals. Each entity requires an explicit `yes` regardless of
  how many need fixing.
