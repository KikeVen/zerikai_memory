# embedding-docstring skill

Audits and fixes docstrings, comment blocks, and inline documentation across an
entire workspace — or a specific file or single entity — in Python, JavaScript,
TypeScript, and HTML for vector embedding quality.

A well-embedded docstring makes every function, class, constant, and HTML section
findable and understandable when an LLM retrieves it via semantic search. The skill
writes missing documentation from scratch — it does not skip entities that have no
docstring, it fixes them.

---

## What it covers

- **Python** — functions, async functions, methods, classes, and module-level
  UPPER_CASE constants. Decorated functions (e.g. Flask routes) have their
  decorator and HTTP method/path captured in the docstring.
- **JavaScript / TypeScript** — same entity types using JSDoc format.
- **HTML** — every significant boundary element (`<section>`, `<main>`, `<nav>`,
  `<header>`, `<footer>`, `<form>`, `<div id="...">`, `<div class="...">`,
  `<template>`). If a `<!-- -->` comment block does not exist above the element,
  the skill writes one from scratch. This is not optional.

---

## Prerequisite — `.memignore`

> **You must have a `.memignore` file at the root of your workspace.**

The skill uses `.memignore` as the only source of truth for what to exclude. It
does not fall back to any IDE default exclusion list. Without `.memignore`, the
skill will walk and audit every eligible file in the workspace with zero exclusions.

`.memignore` follows the same pattern syntax as `.gitignore`:

```
venv/
node_modules/
__pycache__/
.brain/
dist/
build/
*.pyc
*.log
```

If `.memignore` is missing, the skill reports it and proceeds with zero exclusions.
See the main repository README for the recommended `.memignore` baseline.

---

## How to invoke it

The skill supports three scopes:

### 1. Audit or fix the entire workspace

```
audit the workspace
```

The skill runs the `.memignore`-filtered discovery walk, emits a file inventory,
and waits for your confirmation before starting. Say `start` to begin. One file
at a time — diffs shown, you approve, applied, moves to next file.

To resume after an interruption:
```
resume from src/module.py my_function line 42
```

### 2. Audit or fix a single file

```
audit src/module.py
fix docstrings in templates/page.html
```

Skips the workspace walk. Reads the entire file once — not once per entity — then
audits or fixes all entities in that file.

### 3. Audit or fix a single entity

```
fix def my_function in src/module.py
review def my_method in src/models/mymodel.py
fix <div id="my-section"> in templates/page.html
```

Skips the workspace walk and full file inventory. Reads the file, locates the named
entity, audits or fixes that one entity only. Cheapest invocation.

---

## What the audit produces

Each entity gets one status line:

| Label | Meaning |
|---|---|
| `[PASS]` | Docstring or comment block exists and passes all checklist items |
| `[FAIL]` | Exists but missing one or more checklist items — will be rewritten |
| `[MISSING]` | No docstring or comment block at all — will be written from scratch |
| `[SKIP]` | Excluded by `.memignore` or explicitly skipped by the user |

Example:
```
ENTITY INVENTORY — src/module.py
 1. [constant]  MAX_RETRY_COUNT        line 12
 2. [function]  my_function            line 28
 3. [function]  my_other_function      line 54
 4. [route]     handle_request  POST /submit  line 80  @app.route('/submit', methods=['POST'])
4 entities found.

[PASS]    my_function line 28
[FAIL]    my_other_function line 54 — Missing: technology names; Missing: guarantees
[MISSING] handle_request line 80 — no docstring; decorator @app.route('/submit') must appear in prose
[MISSING] MAX_RETRY_COUNT line 12 — no comment block

[FILE COMPLETE] src/module.py — 1 passed / 1 failed / 2 missing / 0 skipped
```

---

## Approving fixes

**Single entity:** diff shown → you approve → applied.

**Single file or workspace:** all diffs for the current file shown in one block →
say `approve all` or list numbers to skip → applied in a single pass → moves to
the next file.

---

## Cost and token efficiency

- **Workspace** — most expensive. Use for initial setup or after major refactoring.
- **Single file** — mid-cost. File read once, all fixes batched.
- **Single entity** — cheapest. Use for targeted fixes during development.
