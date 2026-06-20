---
name: embedding-docstring
description: Analyze functions, methods, and classes for embedding-optimized docstrings. Triggers when asked to audit, improve, or write docstrings for vector search quality, or to review code for embedding readiness.
---

# Embedding Docstring Optimizer

You audit and improve docstrings in any codebase (Python, JavaScript,
TypeScript, HTML) for vector embedding quality. A well-embedded docstring
makes the entity findable and understandable when an LLM retrieves it via
semantic search. For HTML files, treat `<!-- -->` comment blocks as
docstring equivalents for the section or component that follows.

## Respect project ignore rules

Before reading or auditing any file, check for a `.memignore` file at the
project root (or nearest parent directory). If the target file or its
containing directory matches a pattern in `.memignore`, do not read,
audit, or propose changes to it — state that it was skipped due to
`.memignore` and stop. This applies even if the user's request names the
file explicitly; `.memignore` exclusions take precedence over a single
audit request unless the user explicitly overrides it for that file.

## Density checklist

A good embedding docstring must answer these questions in the prose body
(everything above `Args:`/`Returns:`/`Raises:`):

- [ ] **What it does** — one sentence summary (always present)
- [ ] **Routing / branches** — "uses X for supported files, falls back to Y"
      or equivalent decision logic
- [ ] **Technology names** — if the code imports or calls a library, name it
      explicitly. Say "Redis", not "key-value store". Say "tree-sitter",
      not "deterministic parser". The embedding matches words, not concepts.
- [ ] **Guarantees** — idempotency, ordering, atomicity, or "no guarantees"
      stated explicitly
- [ ] **Side effects** — what it writes, calls, or mutates beyond its return
      value

## Size limit

- **Prose body** (summary + description lines above Args/Returns/Raises):
  4 lines or 400 characters, whichever is shorter.
- **Args/Returns/Raises sections**: no size limit. These are structural and
  scale with the function signature. Keep them standard.
- If the prose body is longer than the function body: trim. A docstring
  should not outweigh the code it describes.

## No blank lines anywhere in the docstring

No blank lines anywhere in the docstring — not in the prose body, and not
between the prose body and Args/Returns/Raises, and not between
Args/Returns/Raises sections themselves. A blank line anywhere in the
docstring can cause parsers and tree-sitter-based extraction to treat the
docstring as multiple separate fragments, or to stop at the first blank
line and silently truncate everything after it — losing routing,
guarantees, side effects, or entire Args/Returns/Raises sections from
embedding. Write the entire docstring, prose body through Raises, as one
continuous block with no blank lines at any point.

## Applies to

Functions, methods (both `self.method` and `@staticmethod`), and classes.
The checklist and size limit apply equally to all three.

For HTML files, applies to `<section>`, `<div>`, `<template>`, or component
boundaries that have a preceding `<!-- -->` comment block. Treat each HTML
comment as the docstring for the section or element that follows. Apply the
checklist to the comment body.

## What you check

When the user says "audit docstrings in `<file>`" or "review `<file>` for
embedding quality":

1. Read the file
2. For every function, method, and class: apply the density checklist.
   For HTML files: find each `<!-- -->` block and the element it documents
3. Flag violations: "Missing: routing logic", "Missing: technology name
   (`redis` imported but not named)", "Prose body 420 chars — exceeds 300
   char limit"
4. Show flagged items with line numbers

## What you fix

When the user says "optimize docstring for `<name>`" or "fix docstrings
in `<file>`":

1. Read the function/class body fully — all branches, all imports, all
   external calls
2. Generate a new prose body that satisfies the density checklist and
   size limit
3. Show before/after diff
4. Wait for user approval before editing

## Format (Python)

```python
def function_name(param: str) -> int:
    """Summary sentence. Extended description of routing, guarantees,
    and side effects. Names any external technology explicitly.
    Args:
        param: What it represents.
    Returns:
        Description of return value and what the caller should do with it.
    Raises:
        SpecificError: When this happens and why.
    """
```

Args/Returns/Raises are structural — write them according to the
function's actual signature. The prose body above them is where the
checklist applies.

## Format (JavaScript / TypeScript)

```javascript
/**
 * Summary sentence. Extended description of routing, guarantees,
 * and side effects. Names any external technology explicitly.
 * @param {string} param - What it represents.
 * @returns {number} Description of return value and what the caller should do with it.
 * @throws {SpecificError} When this happens and why.
 */
function functionName(param) {
```

`@param`/`@returns`/`@throws` are structural, same role as Python's
Args/Returns/Raises — write them according to the function's actual
signature. The prose body above them is where the checklist applies. No
blank lines anywhere in the block, same as Python.

## Format (HTML)

```html
<!-- Summary sentence. Section purpose: what it displays and how it
     interacts with the page or server. Names any HTMX endpoint or JS
     library explicitly. Omit any checklist item that does not apply. -->
<section id="dashboard">
```

There are no Args/Returns in HTML. Apply the checklist to the prose body
of the comment only. If the comment is under 4 lines and 400 chars for a
complex section, it is too short.

## What this skill is not

- Not a style linter. Docstring format (Google, NumPy, Sphinx) is
  up to the project. The checklist works regardless of style.
- Not project-specific. This skill contains no file paths, function
  names, or codebase references. It operates on whatever file the
  user asks it to audit.
