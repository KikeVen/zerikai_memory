---
name: embedding-docstring
description: Audit, write, or improve docstrings, comments, and inline documentation
  across an entire workspace or specific files in Python, JavaScript, TypeScript,
  or HTML for embedding quality and semantic searchability. Use this skill whenever
  the user mentions any of these — audit workspace, review docstrings, fix comments,
  document functions, improve docs, write docstrings, audit file documentation,
  embedding quality, clean up comments, document a class, check docstring coverage,
  audit the whole project — even if they don't use the phrase "vector search" or
  "embedding". Trigger on any request to audit, write, fix, or improve inline
  documentation in any supported language, at any scope.
---

# Embedding Docstring Optimizer

Audits and fixes docstrings across an entire workspace — or a specific file or
entity — in Python, JavaScript, TypeScript, and HTML for vector embedding quality.
A well-embedded docstring makes every function, class, and constant findable and
understandable when an LLM retrieves it via semantic search. For HTML, `<!-- -->`
comment blocks are the docstring equivalent for every significant section or
component boundary — if they do not exist, they must be written from scratch.

---

## ENVIRONMENT MODE — DECLARE FIRST

Before doing anything else, determine which execution mode applies and state it
explicitly at the top of the first response.

**CHAT MODE** (claude.ai, pi.dev, or any non-agentic interface):
The skill shows diffs and waits for typed approval before applying. "Apply" means
displaying the corrected block for the user to paste. The approval loop is driven
by the user typing "yes", "skip", or "stop".

**AGENTIC MODE** (Claude Code in VS Code, terminal, or any tool-executing interface):
The skill applies edits using `str_replace` targeting the exact existing text —
never whole-file rewrites. One entity is processed end-to-end (show diff → confirm
applied → verify placement) before the next entity begins. The approval loop works
the same way as CHAT MODE: show the diff, pause, wait for explicit "yes" / "skip" /
"stop", then and only then execute the `str_replace`. Do not batch edits across
entities. Do not proceed past an entity without a confirmed approval response.

**The approval loop is required in both modes.** It is not optional friction.
It is the primary safeguard against corrupting the file.

---

## EXECUTION PROTOCOL

Every run — audit or fix — follows this exact sequence. Do not skip or reorder steps.

### STEP 0 — Workspace discovery and .memignore filtering

This skill operates at workspace scope by default. When the user says "audit the
workspace" or "fix docstrings across the project", treat the workspace root as the
starting point.

**The workspace walk must be driven by `.memignore` — not by any IDE default
exclusion list.** Do not substitute hardcoded directory or file exclusions. Do not
skip `test/`, `tests/`, `node_modules`, `venv`, or any other path unless it appears
in `.memignore`. The only source of truth for what to exclude is `.memignore`.

Execute this exact Python script to discover eligible files:

```python
import os
import fnmatch

def load_memignore(root):
    """Read .memignore from workspace root. Return list of raw pattern strings.
    Returns empty list if file does not exist."""
    path = os.path.join(root, ".memignore")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.readlines()
    return [
        l.strip() for l in lines
        if l.strip() and not l.strip().startswith("#")
    ]

def is_ignored(path, root, patterns):
    """Return True if path matches any .memignore pattern.
    Matches against the full relative path and the basename."""
    rel = os.path.relpath(path, root).replace("\\", "/")
    name = os.path.basename(path)
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if fnmatch.fnmatch(name, pattern):
            return True
        if pattern.endswith("/") and rel.startswith(pattern):
            return True
        if ("/" + pattern.rstrip("/") + "/") in ("/" + rel + "/"):
            return True
    return False

root = os.getcwd()
patterns = load_memignore(root)
extensions = {".py", ".js", ".ts", ".html"}
eligible = []
ignored_count = 0

for dirpath, dirs, files in os.walk(root):
    dirs[:] = [
        d for d in dirs
        if not is_ignored(os.path.join(dirpath, d), root, patterns)
    ]
    for f in files:
        full = os.path.join(dirpath, f)
        if os.path.splitext(f)[1] in extensions:
            if is_ignored(full, root, patterns):
                ignored_count += 1
            else:
                eligible.append(os.path.relpath(full, root).replace("\\", "/"))

eligible.sort()
print(f"Ignored via .memignore: {ignored_count} files")
for i, p in enumerate(eligible, 1):
    print(f"  {i:>3}. {p}")
print(f"{len(eligible)} files eligible.")
```

Emit the output as the file inventory before touching any file:

```
FILE INVENTORY — <workspace root>
  Ignored via .memignore: N files
  Eligible for audit:
     1. src/auth.py
     2. src/models/user.py
     3. templates/dashboard.html
  ...
  N files total.
  Say "start" to begin, or name specific files to scope the run.
```

Wait for user confirmation before proceeding.

If `.memignore` does not exist, report it and proceed with zero exclusions — do not
fall back to any hardcoded exclusion list.

**Three valid scopes — the skill handles all three:**

- **Workspace:** default. Run full STEP 0 discovery, then STEP 1–3 per file.
- **Single file:** user says "audit `auth.py`" or "fix docstrings in `auth.py`".
  Skip workspace walk. Run `is_ignored()` on the file — if it matches, stop and
  report `SKIPPED: <file> matches .memignore`. Otherwise go directly to STEP 1.
- **Single entity:** user says "review the docstring for `convert` in `pdf_app.py`"
  or "fix the comment on `<div id='results'>` in `index.html`". Skip workspace walk
  and skip full file inventory. Read the named file, locate the named entity, run
  STEP 2 audit on that entity only, then STEP 3 if a fix is requested. Still run
  `is_ignored()` on the file first.

`.memignore` exclusions override explicit user requests unless the user says
"override .memignore for this file".

---

### STEP 1 — Per-file entity inventory

For each file in the confirmed list, run the appropriate parser below and emit a
numbered entity inventory before auditing or fixing anything.

#### Python files — run this exact script

Do not improvise an AST walk. Execute this script against the target file. It
recurses into nested functions and async functions via `generic_visit`, which is
why nested closures are not missed.

```python
import ast
import sys

def collect_entities(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=filepath)

    entities = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self._class_stack = []

        def visit_ClassDef(self, node):
            decorators = [ast.unparse(d) for d in node.decorator_list]
            entities.append({
                "kind": "class",
                "name": node.name,
                "line": node.lineno,
                "decorators": decorators,
            })
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        def visit_FunctionDef(self, node):
            self._record_func(node)

        def visit_AsyncFunctionDef(self, node):
            self._record_func(node)

        def _record_func(self, node):
            decorators = [ast.unparse(d) for d in node.decorator_list]
            prefix = ".".join(self._class_stack) + "." if self._class_stack else ""
            kind = "method" if self._class_stack else "function"
            # Detect Flask/FastAPI routes
            for d in decorators:
                if "route" in d or "get(" in d or "post(" in d or "put(" in d or "delete(" in d:
                    kind = "route"
                    break
            entities.append({
                "kind": kind,
                "name": f"{prefix}{node.name}",
                "line": node.lineno,
                "decorators": decorators,
            })
            # Recurse — this captures nested closures
            self._class_stack.append(node.name) if False else None
            self.generic_visit(node)

        def visit_Assign(self, node):
            # Module-level UPPER_CASE constants only
            if not self._class_stack:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == target.id.upper() and "_" in target.id or target.id.isupper():
                        entities.append({
                            "kind": "constant",
                            "name": target.id,
                            "line": node.lineno,
                            "decorators": [],
                        })

    Visitor().visit(tree)
    return entities

if __name__ == "__main__":
    filepath = sys.argv[1]
    for i, e in enumerate(collect_entities(filepath), 1):
        dec = "  " + "  ".join(e["decorators"]) if e["decorators"] else ""
        print(f"  {i:>3}. [{e['kind']}]  {e['name']:<40} line {e['line']}{dec}")
```

Run as: `python collect_entities.py <filepath>`

#### JS/TS files

Use `acorn` (JS) or the `typescript` parser (TS) with a recursive walk. Enumerate:
`FunctionDeclaration`, `FunctionExpression`, `ArrowFunctionExpression`, method
definitions inside `ClassDeclaration` or `ClassExpression`, and `const`/`let`/`var`
UPPER_CASE assignments at module scope. Recurse into every node's body — do not
stop at the top level. Track decorators where the language supports them (TS).

#### HTML files

HTML scanning has two passes — boundary elements and inline scripts. Both must
run. Do not skip either pass.

**Pass 1 — HTML boundary elements**

Use a tag scanner to find every significant boundary element: `<section>`,
`<main>`, `<nav>`, `<header>`, `<footer>`, `<form>`, `<div id="...">`,
`<div class="...">`, and `<template>`. For each element found:

- If a `<!-- -->` comment block immediately precedes it: inventory it as
  `[html:documented]`
- If no `<!-- -->` comment block precedes it: inventory it as `[html:missing]`

**Both cases appear in the inventory.** `[html:missing]` entities are not skipped
— they are the primary fix target for HTML files.

**Pass 2 — Inline JavaScript inside `<script>` blocks**

Locate every `<script>` block in the file that does not have a `src` attribute
(i.e. inline scripts only — not external script imports). For each inline
`<script>` block, run the JS AST walker on its contents to enumerate every
`FunctionDeclaration`, `FunctionExpression`, `ArrowFunctionExpression`, and
`const`/`let`/`var` UPPER_CASE assignments. Track line numbers relative to
the HTML file, not relative to the script block.

For each JS entity found inside a `<script>` block:

- If a `/** */` JSDoc block immediately precedes it: inventory it as `[js:documented]`
- If no `/** */` JSDoc block precedes it: inventory it as `[js:missing]`

**Both cases appear in the inventory.** `[js:missing]` entities are fix targets —
write a JSDoc `/** */` block from scratch. Do not use `<!-- -->` HTML comments
for inline JS entities — use JSDoc format only.

#### Inventory output format

```
ENTITY INVENTORY — src/auth.py
 1. [constant]  MAX_RETRY_COUNT               line 12
 2. [function]  save_user                     line 42
 3. [function]  validate_token                line 78
 4. [class]     UserManager                   line 110
 5. [method]    UserManager.login             line 115  @login_required
 6. [route]     convert  POST /convert        line 36   @app.route('/convert', methods=['POST'])
N entities found.
```

```
ENTITY INVENTORY — templates/page.html
 1. [html:documented]  <form id="upload-form">      line 18
 2. [html:missing]     <div id="results">           line 60
 3. [js:missing]       handleDragOver               line 142
 4. [js:missing]       handleDragLeave              line 149
 5. [js:missing]       handleDrop                   line 156
 6. [js:missing]       handleFileUpload             line 168
6 entities found. 1 missing HTML comment block. 4 missing JSDoc blocks.
```

After completing a file inventory, move automatically to STEP 2 for that file.

---

### STEP 2 — Audit each entity

Use these status labels — exactly these, no others:

- `[PASS]` — docstring or comment block exists and passes the density checklist
- `[FAIL]` — docstring or comment block exists but fails one or more checklist items
- `[MISSING]` — no docstring or comment block exists at all; must be written from scratch
- `[SKIP]` — entity is excluded by `.memignore` or explicitly skipped by the user

Emit one status line per entity:

```
[PASS]    save_user line 42
[FAIL]    validate_token line 78 — Missing: technology name (jwt imported, not named); Missing: guarantees
[FAIL]    MAX_RETRY_COUNT line 12 — Missing: valid range; Missing: what it configures
[MISSING] convert line 36 — no docstring present; decorator @app.route('/convert') must appear in prose
[MISSING] <main id="content"> line 28 — no comment block; must be written from scratch
```

After all entities in a file, emit the file summary:
```
[FILE COMPLETE] src/auth.py — N passed / N failed / N missing / N skipped
```

After all files, emit the workspace summary:
```
[AUDIT COMPLETE] <workspace> — N files / N passed / N failed / N missing / N skipped
```

---

### STEP 3 — Fix (only when user requests fixes)

**`[MISSING]` and `[FAIL]` entities are both fix targets.** `[MISSING]` means write
from scratch. `[FAIL]` means rewrite the existing docstring. The diff format is the
same for both — show what will be inserted or replaced, and where.

**Read the entire file once before writing any docstring or comment block. Do not
re-read per entity.** A single file read captures all imports, module-level constants,
and cross-entity call chains needed to write accurate technology names, routing logic,
and side effects for every entity in the file.

For `[MISSING]` HTML entities, write a new `<!-- -->` block immediately above the
element. Read the element's contents, its `id`, `class`, any `hx-*` attributes, and
any JS event bindings to inform the comment. Do not write a placeholder — write a
real description based on what the element actually does.

For decorated Python/JS functions (`[MISSING]` or `[FAIL]`), the docstring prose
must name the decorator explicitly. For Flask routes: state the HTTP method, the
path, and what the route renders or returns.

For UPPER_CASE constants (`[MISSING]` or `[FAIL]`), write a `#` comment block
immediately above the constant. State what the constant controls, its valid range
or allowed values, and any technology it configures. Each constant gets its own
comment block — never share one block across multiple constants.

---

#### THE APPROVAL LOOP — required for every entity, both modes

This loop runs once per entity. Never batch entities across a single approval.

**For each `[FAIL]` or `[MISSING]` entity:**

1. Show the entity name, kind, and line number.
2. Show a before/after diff (BEFORE: existing text or "none"; AFTER: proposed text).
3. State exactly where the insertion or replacement will land.
4. Stop. Wait for the user to respond with one of:
   - `"yes"` — apply this fix and advance to the next entity
   - `"skip"` — leave this entity unchanged and advance to the next entity
   - `"stop"` — halt the entire run; emit a resume checkpoint (see Resume section)
5. Apply only after receiving an explicit `"yes"`.

**In AGENTIC MODE**, "apply" means executing a `str_replace` using the exact
existing text as the search target. Rules:

- Never rewrite the whole file. `str_replace` only.
- The `old_str` must be the precise existing docstring text (for `[FAIL]`) or the
  exact line of the `def`, `class`, constant assignment, or HTML tag (for `[MISSING]`
  insertions). Match indentation exactly.
- After executing `str_replace`, immediately read back the modified lines to confirm
  the insertion landed at the correct location and did not alter any surrounding code.
- If the read-back shows a mismatch — wrong indentation, wrong location, code lines
  altered — undo the edit, report the failure, and stop. Do not attempt a retry
  without user instruction.
- Do not proceed to the next entity until the read-back confirms the edit is clean.

**In CHAT MODE**, "apply" means displaying the corrected block for the user to paste.
After displaying it, confirm with: `"[READY] Paste the above into <file> at line N.
Type 'yes' when done to continue, or 'skip' to leave it and move on."`

**The approval loop cannot be shortened, collapsed, or batched regardless of how
many entities are in the file.** If the user says "just fix all of them", reply:
`"To protect the file I'll show each fix one at a time and need a 'yes' per entity.
Type 'yes' after each one and I'll move through them as fast as you confirm."`

---

#### Resume from interruption

If the run is interrupted, emit a resume checkpoint before stopping:

```
[CHECKPOINT] Interrupted after <entity_name> in <file>.
To resume: "resume from <file> <entity_name>"
Completed: N entities. Remaining: N entities.
```

To resume, the user says: `"resume from <file> <entity_name>"`.

**Resume is always by entity name, never by line number.** Line numbers shift after
every insertion. Reloading by name is stable. On resume: reload the workspace file
inventory, re-run STEP 1 on the target file to get current line numbers, skip all
entities that were already completed, and continue from the named entity.

---

## DENSITY CHECKLIST

Apply to the prose body (everything above `Args:`/`Returns:`/`Raises:`) of every
entity. Each item is required unless the code genuinely has nothing to say for it.

- [ ] **What it does** — one sentence summary. Always present.
- [ ] **Decorator / route context** — for decorated functions, name the decorator
      and for Flask routes state the HTTP method and path explicitly.
- [ ] **Routing / branches** — "uses X for supported files, falls back to Y",
      or equivalent decision logic if branches exist.
- [ ] **Technology names** — name libraries and tools explicitly. Say "Redis",
      not "key-value store". Say "pdfminer.six", not "PDF parser". Say "HTMX",
      not "frontend library". Embeddings match words, not concepts.
- [ ] **Guarantees** — idempotency, ordering, atomicity, or "no guarantees"
      if relevant.
- [ ] **Side effects** — what it writes, calls, or mutates beyond its return
      value, if any.

---

## SIZE RULES

- **Prose body** (above Args/Returns/Raises): 6 lines or 400 characters, whichever
  is shorter.
- **Args/Returns/Raises**: no size limit. Structural; scale with the signature.
- **Trim rule**: if the prose body line count exceeds the function body line count,
  trim to match. Count only non-blank, non-decorator lines in the function body.
  A 3-line utility function with a 4-line docstring is acceptable — do not trim
  aggressively on short functions. Only trim when the docstring is genuinely longer
  than the code.

---

## NO BLANK LINES — ANYWHERE IN THE DOCSTRING

No blank lines in the prose body, between the prose body and Args/Returns/Raises,
or between Args/Returns/Raises sections. A blank line causes tree-sitter-based
extractors to fragment the docstring or truncate silently — dropping routing,
guarantees, or entire Args sections from the embedding. Write the entire docstring
as one continuous block.

---

## WHAT THIS APPLIES TO

**Functions, methods, classes, decorators, and module-level UPPER_CASE constants.**
The density checklist and size rules apply equally to all.

**Decorators (Python/JS/TS):** capture every decorator on a function or method.
Include the decorator name and, for Flask routes, the HTTP method and path in the
docstring prose. Label decorated entities with their decorator in the inventory.

**Constants (Python):** treat the preceding `#` comment block as the docstring.
The extractor walks backwards from the constant's line, collecting contiguous `#`
lines, stopping at the first non-comment non-empty line. Every constant needs its
own comment block — a shared block above a group only reaches the first constant.

**Constants (JS/TS):** same logic using `//` comment blocks.

**HTML boundary elements:** every significant boundary element (`<section>`,
`<main>`, `<nav>`, `<header>`, `<footer>`, `<form>`, `<div id="...">`,
`<div class="...">`, `<template>`) must have a `<!-- -->` comment block
immediately preceding it. If the block does not exist, it must be written from
scratch — this is not optional and is not skipped. No Args/Returns in HTML
comments. Apply the density checklist to the comment prose only. A comment
under 4 lines for a complex section is too short.

**HTML inline scripts:** every function inside a `<script>` block without a
`src` attribute must have a JSDoc `/** */` block immediately preceding it.
If the block does not exist, it must be written from scratch. Use JSDoc format
— not `<!-- -->` HTML comments. Apply the full density checklist. The docstring
must name any DOM APIs, HTMX attributes, or browser events the function interacts
with explicitly.

---

## FORMAT REFERENCE

**Python**
```python
def function_name(param: str) -> int:
    """Summary sentence. Routing, guarantees, and side effects.
    Names any external technology explicitly (e.g., PostgreSQL, Redis).
    Args:
        param: What it represents.
    Returns:
        What is returned and what the caller should do with it.
    Raises:
        SpecificError: When and why this is raised.
    """
```

**Python decorated route**
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

**Python constant**
```python
# Summary. Purpose, valid range/values, and any technology it configures
# (e.g., "controls PostgreSQL connection pool size; valid range 1–100").
MAX_POOL_SIZE = 20

# Each sequential constant needs its own comment block immediately above it.
# The extractor stops at MAX_POOL_SIZE = 20, so this block is not shared.
QUERY_TIMEOUT_SEC = 30
```

**JavaScript / TypeScript**
```javascript
/**
 * Summary sentence. Routing, guarantees, and side effects.
 * Names any external technology explicitly (e.g., HTMX, Stripe).
 * @param {string} param - What it represents.
 * @returns {number} What is returned and what the caller should do with it.
 * @throws {SpecificError} When and why this is raised.
 */
function functionName(param) {}
```

**JS/TS constant**
```javascript
// Summary. Purpose and valid range. Names any technology it configures.
const MAX_RETRY_COUNT = 3;

// Each constant needs its own comment block. Same backward-walk rule.
const RETRY_DELAY_MS = 500;
```

**HTML — existing comment improved**
```html
<!-- Upload section. Renders the PDF file input and model selector dropdown.
     Submits to POST /convert via HTMX hx-post; response swaps #results div.
     Depends on Flask /models endpoint to populate the model list on page load. -->
<section id="upload">
```

**HTML — missing comment written from scratch**
```html
<!-- Results container. Displays converted output returned by POST /convert.
     Updated by HTMX hx-swap targeting this div. Empty on initial page load.
     Shows error partial if conversion fails; shows formatted output otherwise. -->
<div id="results">
```

**HTML — inline JavaScript function written from scratch**
```html
<script>
/**
 * Handles dragover event on the drop zone element. Prevents default browser
 * behavior and adds the 'dragover' CSS class to #dropZone via classList.
 * Listens for the dragover DOM event. No return value. No side effects beyond
 * CSS class mutation on #dropZone.
 * @param {DragEvent} event - The browser dragover event object.
 */
function handleDragOver(event) {
    event.preventDefault();
    event.stopPropagation();
    document.getElementById('dropZone').classList.add('dragover');
}
</script>
```

---

## WHAT THIS SKILL IS NOT

- Not a style linter. Google, NumPy, Sphinx — any format works.
- Not project-specific. No hardcoded file paths or function names.
  Operates on whatever workspace or file the user provides.
