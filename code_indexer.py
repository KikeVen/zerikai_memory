"""Multi-language code entity extractor using tree-sitter.

Replaces LLM-based code indexing with deterministic parsing.
Extracts functions, methods, classes, and their docstrings/JSDoc
as individual indexable entities. No API calls, no token costs,
no empty responses.

Supports: Python, JavaScript, TypeScript, TSX
          (extensible via tree-sitter grammar packages)

Requires: pip install tree-sitter tree-sitter-python
          tree-sitter-javascript tree-sitter-typescript
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_css as tscss
import tree_sitter_html as tshtml
import tree_sitter_javascript as tsjavascript
import tree_sitter_markdown as tsmarkdown
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser, Query


# ---------------------------------------------------------------------------
# Language registry — maps file extensions to grammars and extractors
# ---------------------------------------------------------------------------

@dataclass
class LanguageConfig:
    """Configuration for a supported language."""

    language: Language  # tree-sitter Language object
    extensions: set[str]  # e.g. {".py"}
    display_name: str  # "python", "javascript", etc.
    entity_query: str  # tree-sitter query to find functions/classes


# Build Language objects from grammar packages
PY_LANG = Language(tspython.language())
JS_LANG = Language(tsjavascript.language())
TS_LANG = Language(tstypescript.language_typescript())
TSX_LANG = Language(tstypescript.language_tsx())
CSS_LANG = Language(tscss.language())
HTML_LANG = Language(tshtml.language())
MD_LANG = Language(tsmarkdown.language())

# Shared entity query for JavaScript-like languages (JS, TS, TSX)
JS_LIKE_QUERY = """
    (function_declaration
      name: (identifier) @name
      parameters: (formal_parameters) @params
      return_type: (type_annotation)? @return_type
    ) @function

    (arrow_function) @arrow

    (class_declaration
      name: (identifier) @name
      body: (class_body) @body
    ) @class

    (method_definition
      name: (property_identifier) @name
      parameters: (formal_parameters) @params
      return_type: (type_annotation)? @return_type
    ) @method

    (lexical_declaration
      (variable_declarator
        name: (identifier) @name
        value: (arrow_function) @arrow_value
      )
    ) @arrow_variable
"""

LANGUAGE_CONFIGS: dict[str, LanguageConfig] = {
    ".py": LanguageConfig(
        language=PY_LANG,
        extensions={".py"},
        display_name="python",
        entity_query="""
            (function_definition
              name: (identifier) @name
              parameters: (parameters) @params
              return_type: (type)? @return_type
            ) @function

            (class_definition
              name: (identifier) @name
              body: (block) @body
            ) @class
        """,
    ),
    ".js": LanguageConfig(
        language=JS_LANG,
        extensions={".js", ".mjs", ".cjs"},
        display_name="javascript",
        entity_query=JS_LIKE_QUERY,
    ),
    ".jsx": LanguageConfig(
        language=JS_LANG,
        extensions={".jsx"},
        display_name="javascript",
        entity_query=JS_LIKE_QUERY,
    ),
    ".ts": LanguageConfig(
        language=TS_LANG,
        extensions={".ts"},
        display_name="typescript",
        entity_query=JS_LIKE_QUERY,
    ),
    ".tsx": LanguageConfig(
        language=TSX_LANG,
        extensions={".tsx"},
        display_name="typescript",
        entity_query=JS_LIKE_QUERY,
    ),
    ".css": LanguageConfig(
        language=CSS_LANG,
        extensions={".css"},
        display_name="css",
        entity_query="",
    ),
    ".html": LanguageConfig(
        language=HTML_LANG,
        extensions={".html", ".htm"},
        display_name="html",
        entity_query="",
    ),
    ".md": LanguageConfig(
        language=MD_LANG,
        extensions={".md", ".mdx"},
        display_name="markdown",
        entity_query="",
    ),
}

# Build reverse lookup: extension → config
_EXTENSION_MAP: dict[str, LanguageConfig] = {}
for config in LANGUAGE_CONFIGS.values():
    for ext in config.extensions:
        _EXTENSION_MAP[ext] = config


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass
class CodeEntity:
    """A single indexable code entity extracted from a source file."""

    entity_type: str  # "function", "method", "class"
    name: str  # e.g. "scan_workspace"
    signature: str  # e.g. "scan_workspace(workspace: str, category: str = 'codebase') -> str"
    docstring: str | None  # The docstring or JSDoc comment
    document_text: str  # signature + docstring — what gets EMBEDDED
    file_path: str  # Relative path from workspace root
    language: str  # "python", "javascript", "typescript"
    lineno: int  # Starting line number
    end_lineno: int  # Ending line number
    parent_class: str | None  # Class name if this is a method
    decorators: list[str]  # @decorator, @annotation, etc.
    params: list[dict]  # [{name, type_annotation, default}, ...]
    return_type: str | None  # Return type annotation


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_supported_extensions() -> set[str]:
    """Return all file extensions that can be parsed by tree-sitter."""
    return set(_EXTENSION_MAP.keys())


def extract_entities(source_code: str, file_path: str) -> list[CodeEntity]:
    """Parse a source file and extract all functions, classes, and methods.

    Automatically selects the correct grammar based on file extension.
    Returns empty list for unsupported extensions.

    Args:
        source_code: Raw source code.
        file_path: Relative path from workspace root (used for extension detection).

    Returns:
        List of CodeEntity objects, one per function/method/class.
    """
    ext = Path(file_path).suffix.lower()
    config = _EXTENSION_MAP.get(ext)
    if config is None:
        return []

    parser = Parser(config.language)
    tree = parser.parse(source_code.encode("utf-8"))

    entities: list[CodeEntity] = []

    if config.display_name == "python":
        _extract_python(tree, source_code, file_path, config, entities)
    elif config.display_name == "css":
        _extract_css(tree, source_code, file_path, config, entities)
    elif config.display_name == "html":
        _extract_html(tree, source_code, file_path, config, entities)
    elif config.display_name == "markdown":
        _extract_markdown(tree, source_code, file_path, config, entities)
    else:
        _extract_js_like(tree, source_code, file_path, config, entities)

    return entities


# ---------------------------------------------------------------------------
# Python extraction (dedicated walker — more reliable than queries for Python)
# ---------------------------------------------------------------------------


def _extract_python(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    """Walk the tree-sitter CST for Python function and class definitions."""
    root = tree.root_node
    source_bytes = source_code.encode("utf-8")
    lines = source_code.split("\n")

    def _extract_from_body(
        body_node: Node, parent_class: str | None
    ) -> None:
        """Recursively extract functions and classes from a block body."""
        for child in body_node.named_children:
            if child.type == "function_definition":
                entity = _extract_python_function(
                    child, source_bytes, lines, file_path, config.display_name, parent_class
                )
                if entity:
                    entities.append(entity)
            elif child.type == "decorated_definition":
                # @decorator\n def func(...): — unwrap the inner definition
                for sub in child.named_children:
                    if sub.type == "function_definition":
                        entity = _extract_python_function(
                            sub, source_bytes, lines, file_path, config.display_name, parent_class
                        )
                        if entity:
                            # Add decorators from the outer node too
                            extra_decorators = _extract_python_decorators(
                                child, source_bytes
                            )
                            entity.decorators = extra_decorators + entity.decorators
                            entities.append(entity)
                    elif sub.type == "class_definition":
                        class_entity = _extract_python_class(
                            sub, source_bytes, lines, file_path, config.display_name
                        )
                        if class_entity:
                            class_entity.decorators = (
                                _extract_python_decorators(child, source_bytes)
                                + class_entity.decorators
                            )
                            entities.append(class_entity)
                        body = sub.child_by_field_name("body")
                        if body:
                            _extract_from_body(body, class_entity.name)
            elif child.type == "class_definition":
                class_entity = _extract_python_class(
                    child, source_bytes, lines, file_path, config.display_name
                )
                if class_entity:
                    entities.append(class_entity)
                # Walk class body for methods
                body = child.child_by_field_name("body")
                if body:
                    _extract_from_body(body, class_entity.name)

    # Top-level extraction
    _extract_from_body(root, None)


def _extract_python_function(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
    parent_class: str | None,
) -> CodeEntity | None:
    """Extract a Python function definition from a tree-sitter node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")

    # Extract docstring
    docstring = _extract_python_docstring(node, lines)

    # Extract decorators
    decorators = _extract_python_decorators(node, source_bytes)

    # Build signature
    params = _extract_python_params(node, source_bytes)
    return_type = _extract_python_return_type(node, source_bytes)

    # Build signature string
    param_strs = []
    for p in params:
        s = p["name"]
        if p["type_annotation"]:
            s += f": {p['type_annotation']}"
        if p["default"] is not None:
            s += f" = {p['default']}"
        param_strs.append(s)

    signature = f"{name}({', '.join(param_strs)})"
    if return_type:
        signature += f" -> {return_type}"

    # Build document text
    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="method" if parent_class else "function",
        name=name,
        signature=signature,
        docstring=docstring,
        document_text=text,
        file_path=file_path,
        language=language,
        lineno=node.start_point[0] + 1,
        end_lineno=node.end_point[0] + 1,
        parent_class=parent_class,
        decorators=decorators,
        params=params,
        return_type=return_type,
    )


def _extract_python_class(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
) -> CodeEntity | None:
    """Extract a Python class definition from a tree-sitter node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")

    # Extract base classes
    bases = []
    for child in node.named_children:
        if child.type == "argument_list":
            for arg in child.named_children:
                bases.append(arg.text.decode("utf-8"))

    # Extract docstring
    docstring = _extract_python_docstring(node, lines)

    # Extract decorators
    decorators = _extract_python_decorators(node, source_bytes)

    # Build signature
    if bases:
        signature = f"class {name}({', '.join(bases)})"
    else:
        signature = f"class {name}"

    # Build document text
    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="class",
        name=name,
        signature=signature,
        docstring=docstring,
        document_text=text,
        file_path=file_path,
        language=language,
        lineno=node.start_point[0] + 1,
        end_lineno=node.end_point[0] + 1,
        parent_class=None,
        decorators=decorators,
        params=[],
        return_type=None,
    )


def _extract_python_docstring(node: Node, lines: list[str]) -> str | None:
    """Extract the docstring from a Python function or class body."""
    body = node.child_by_field_name("body")
    if body is None:
        return None

    # The docstring is the first expression statement containing a string
    for child in body.named_children:
        if child.type == "expression_statement":
            for expr_child in child.named_children:
                if expr_child.type == "string":
                    # Get the full string including triple quotes
                    start_line = expr_child.start_point[0]
                    end_line = expr_child.end_point[0]

                    if start_line == end_line:
                        return lines[start_line][
                            expr_child.start_point[1] : expr_child.end_point[1]
                        ]
                    else:
                        # Multi-line docstring
                        parts = []
                        parts.append(
                            lines[start_line][expr_child.start_point[1] :]
                        )
                        for i in range(start_line + 1, end_line):
                            parts.append(lines[i])
                        parts.append(
                            lines[end_line][: expr_child.end_point[1]]
                        )
                        return "\n".join(parts)
        break  # Only check the first statement

    return None


def _extract_python_decorators(node: Node, source_bytes: bytes) -> list[str]:
    """Extract decorator names from a Python function/class definition.

    Handles both:
    - decorated_definition nodes (decorators are named children)
    - function_definition / class_definition nodes (decorators are prev siblings)
    """
    decorators = []

    # Case 1: node is a decorated_definition — decorators are named children
    if node.type == "decorated_definition":
        for child in node.named_children:
            if child.type == "decorator":
                decorators.append(child.text.decode("utf-8"))
        return decorators

    # Case 2: node is a bare function/class — look at prev named siblings
    current = node.prev_named_sibling
    while current and current.type == "decorator":
        decorators.insert(0, current.text.decode("utf-8"))
        current = current.prev_named_sibling
    return decorators


def _extract_python_params(node: Node, source_bytes: bytes) -> list[dict]:
    """Extract parameter names, types, and defaults from a Python function."""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return []

    params = []
    for child in params_node.named_children:
        if child.type == "identifier":
            # self, cls (first param, no type annotation)
            params.append(
                {"name": child.text.decode("utf-8"), "type_annotation": None, "default": None}
            )
        elif child.type == "typed_parameter":
            name = None
            type_ann = None
            for sub in child.children:
                if sub.type == "identifier" and sub.is_named:
                    name = sub.text.decode("utf-8")
                elif sub.type == "type" and sub.is_named:
                    type_ann = sub.text.decode("utf-8")
            if name:
                params.append(
                    {"name": name, "type_annotation": type_ann, "default": None}
                )
        elif child.type == "default_parameter":
            name = None
            type_ann = None
            default = None
            for sub in child.children:
                if sub.type == "identifier" and sub.is_named:
                    name = sub.text.decode("utf-8")
                elif sub.type == "type" and sub.is_named:
                    type_ann = sub.text.decode("utf-8")
            # Default value is everything after the first '='
            eq_idx = child.text.decode("utf-8").find("=")
            if eq_idx >= 0:
                default = child.text.decode("utf-8")[eq_idx + 1 :].strip()
            if name:
                params.append(
                    {"name": name, "type_annotation": type_ann, "default": default}
                )
        elif child.type == "typed_default_parameter":
            name = None
            type_ann = None
            default = None
            for sub in child.children:
                if sub.type == "identifier" and sub.is_named:
                    name = sub.text.decode("utf-8")
                elif sub.type == "type" and sub.is_named:
                    type_ann = sub.text.decode("utf-8")
            eq_idx = child.text.decode("utf-8").find("=")
            if eq_idx >= 0:
                default = child.text.decode("utf-8")[eq_idx + 1 :].strip()
            if name:
                params.append(
                    {"name": name, "type_annotation": type_ann, "default": default}
                )
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            name = child.text.decode("utf-8")
            params.append(
                {"name": name, "type_annotation": None, "default": None}
            )

    return params


def _extract_python_return_type(node: Node, source_bytes: bytes) -> str | None:
    """Extract the return type annotation from a Python function."""
    return_type_node = node.child_by_field_name("return_type")
    if return_type_node is None:
        return None
    return return_type_node.text.decode("utf-8")


# ---------------------------------------------------------------------------
# JavaScript-like extraction
# ---------------------------------------------------------------------------


def _extract_js_like(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    """Extract functions, methods, and classes from JS/TS/TSX source."""
    lines = source_code.split("\n")
    source_bytes = source_code.encode("utf-8")
    root = tree.root_node

    def _walk(node: Node, parent_class: str | None) -> None:
        for child in node.named_children:
            if child.type == "function_declaration":
                entity = _extract_js_function(
                    child, source_bytes, lines, file_path, config.display_name, parent_class
                )
                if entity:
                    entities.append(entity)
            elif child.type == "class_declaration":
                class_entity = _extract_js_class(
                    child, source_bytes, lines, file_path, config.display_name
                )
                if class_entity:
                    entities.append(class_entity)
                # Walk class body for methods
                body = child.child_by_field_name("body")
                if body:
                    _walk(body, class_entity.name)
            elif child.type == "method_definition":
                entity = _extract_js_function(
                    child, source_bytes, lines, file_path, config.display_name, parent_class
                )
                if entity:
                    entities.append(entity)
            elif child.type == "lexical_declaration":
                # const myFunc = () => { ... }
                for decl_child in child.named_children:
                    if decl_child.type == "variable_declarator":
                        name_node = decl_child.child_by_field_name("name")
                        value_node = decl_child.child_by_field_name("value")
                        if name_node and value_node and value_node.type == "arrow_function":
                            entity = _extract_arrow_function(
                                decl_child, source_bytes, lines, file_path,
                                config.display_name, parent_class
                            )
                            if entity:
                                entities.append(entity)
            # Recurse into nested blocks (but stop at function/class — they're handled above)
            if child.type not in ("function_declaration", "class_declaration", "method_definition"):
                _walk(child, parent_class)

    _walk(root, None)


def _extract_js_function(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
    parent_class: str | None,
) -> CodeEntity | None:
    """Extract a JS/TS function or method definition."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")

    # Extract JSDoc
    docstring = _extract_jsdoc(node, lines)

    # Build signature
    params = _extract_js_params(node, source_bytes)
    return_type = _extract_js_return_type(node, source_bytes)

    param_strs = [p["name"] for p in params]  # JS params often lack type annotations
    for p in params:
        s = p["name"]
        if p["type_annotation"]:
            s += f": {p['type_annotation']}"
        if p["default"] is not None:
            s += f" = {p['default']}"
        param_strs.append(s)

    # Rebuild without duplicates (simple approach)
    param_strs_unique = []
    seen = set()
    for p in params:
        s = p["name"]
        if p["type_annotation"]:
            s += f": {p['type_annotation']}"
        if p["default"] is not None:
            s += f" = {p['default']}"
        if s not in seen:
            param_strs_unique.append(s)
            seen.add(s)

    signature = f"{name}({', '.join(param_strs_unique)})"
    if return_type:
        signature += f": {return_type}"

    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="method" if parent_class else "function",
        name=name,
        signature=signature,
        docstring=docstring,
        document_text=text,
        file_path=file_path,
        language=language,
        lineno=node.start_point[0] + 1,
        end_lineno=node.end_point[0] + 1,
        parent_class=parent_class,
        decorators=[],
        params=params,
        return_type=return_type,
    )


def _extract_arrow_function(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
    parent_class: str | None,
) -> CodeEntity | None:
    """Extract a const arrow function: `const myFunc = (params) => { ... }`"""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")

    # Try to get JSDoc from preceding comments
    docstring = _extract_jsdoc(node, lines)

    # Find the arrow function value
    value_node = node.child_by_field_name("value")
    if value_node is None or value_node.type != "arrow_function":
        return None

    # Extract params from the arrow function
    params = _extract_js_params(value_node, source_bytes)
    return_type = _extract_js_return_type(value_node, source_bytes)

    param_strs: list[str] = []
    seen: set[str] = set()
    for p in params:
        s = p["name"]
        if p["type_annotation"]:
            s += f": {p['type_annotation']}"
        if p["default"] is not None:
            s += f" = {p['default']}"
        if s not in seen:
            param_strs.append(s)
            seen.add(s)

    signature = f"{name}({', '.join(param_strs)})"
    if return_type:
        signature += f": {return_type}"

    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="function",
        name=name,
        signature=signature,
        docstring=docstring,
        document_text=text,
        file_path=file_path,
        language=language,
        lineno=node.start_point[0] + 1,
        end_lineno=value_node.end_point[0] + 1,
        parent_class=parent_class,
        decorators=[],
        params=params,
        return_type=return_type,
    )


def _extract_js_class(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
) -> CodeEntity | None:
    """Extract a JS/TS class definition."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")

    docstring = _extract_jsdoc(node, lines)

    signature = f"class {name}"
    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="class",
        name=name,
        signature=signature,
        docstring=docstring,
        document_text=text,
        file_path=file_path,
        language=language,
        lineno=node.start_point[0] + 1,
        end_lineno=node.end_point[0] + 1,
        parent_class=None,
        decorators=[],
        params=[],
        return_type=None,
    )


def _extract_jsdoc(node: Node, lines: list[str]) -> str | None:
    """Extract JSDoc comment preceding a JS/TS function or class."""
    start_line = node.start_point[0]
    # Look at the line just before this node's start
    if start_line == 0:
        return None

    prev_line = start_line - 1
    # Find the start of the JSDoc comment block (walk backwards)
    jsdoc_lines = []
    while prev_line >= 0:
        stripped = lines[prev_line].strip()
        if stripped.endswith("*/") or stripped == "*/":
            # Found end of JSDoc block — collect backwards
            jsdoc_lines.insert(0, stripped.rstrip("*/").strip().lstrip("* "))
            prev_line -= 1
            while prev_line >= 0:
                inner = lines[prev_line].strip()
                if inner.startswith("/**"):
                    # Start of JSDoc
                    jsdoc_lines.insert(0, inner.lstrip("/**").strip().rstrip("*/").strip())
                    return "\n".join(line for line in jsdoc_lines if line)
                elif inner.startswith("//"):
                    prev_line -= 1
                    continue
                elif inner.startswith("*"):
                    jsdoc_lines.insert(0, inner.lstrip("* ").rstrip("*/").strip())
                    prev_line -= 1
                else:
                    return "\n".join(line for line in jsdoc_lines if line) or None
            return "\n".join(line for line in jsdoc_lines if line) or None
        elif stripped.startswith("//"):
            # Single-line comment — could be a brief doc
            prev_line -= 1
            continue
        else:
            break

    return None


def _extract_js_params(node: Node, source_bytes: bytes) -> list[dict]:
    """Extract parameters from a JS/TS function or arrow function."""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return []

    params = []
    for child in params_node.named_children:
        if child.type == "identifier":
            params.append(
                {"name": child.text.decode("utf-8"), "type_annotation": None, "default": None}
            )
        elif child.type == "required_parameter":
            # TS: param: Type
            name = None
            type_ann = None
            for sub in child.children:
                if sub.type == "identifier" and sub.is_named:
                    name = sub.text.decode("utf-8")
                elif sub.type == "type_annotation" and sub.is_named:
                    type_ann = sub.text.decode("utf-8")
            if name:
                params.append(
                    {"name": name, "type_annotation": type_ann, "default": None}
                )
        elif child.type == "optional_parameter":
            # TS: param?: Type or param: Type = default
            name = None
            type_ann = None
            default = None
            for sub in child.children:
                if sub.type == "identifier" and sub.is_named:
                    name = sub.text.decode("utf-8")
                elif sub.type == "type_annotation" and sub.is_named:
                    type_ann = sub.text.decode("utf-8")
            # Check for default value
            eq_idx = child.text.decode("utf-8").find("=")
            if eq_idx >= 0:
                default = child.text.decode("utf-8")[eq_idx + 1 :].strip()
            if name:
                params.append(
                    {"name": name, "type_annotation": type_ann, "default": default}
                )

    return params


def _extract_js_return_type(node: Node, source_bytes: bytes) -> str | None:
    """Extract the return type annotation from a JS/TS function."""
    return_type_node = node.child_by_field_name("return_type")
    if return_type_node is None:
        return None
    return return_type_node.text.decode("utf-8")


# ---------------------------------------------------------------------------
# CSS extraction
# ---------------------------------------------------------------------------

def _extract_css(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    root = tree.root_node

    def _walk(node: Node):
        for child in node.named_children:
            if child.type == "rule_set":
                selectors = ""
                for c in child.named_children:
                    if c.type == "selectors":
                        selectors = c.text.decode("utf-8")
                        break

                if selectors:
                    text = child.text.decode("utf-8")
                    entities.append(CodeEntity(
                        entity_type="rule_set",
                        name=selectors.strip(),
                        signature=f"CSS Rule: {selectors.strip()}",
                        docstring=None,
                        document_text=text,
                        file_path=file_path,
                        language=config.display_name,
                        lineno=child.start_point[0] + 1,
                        end_lineno=child.end_point[0] + 1,
                        parent_class=None,
                        decorators=[],
                        params=[],
                        return_type=None,
                    ))
            else:
                _walk(child)

    _walk(root)


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _extract_html(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    root = tree.root_node
    semantic_tags = {"main", "header", "footer", "section", "article", "nav", "aside"}

    def _walk(node: Node):
        for child in node.named_children:
            if child.type in ("element", "script_element", "style_element"):
                tag_name = ""
                element_id = ""

                start_tag = None
                for c in child.named_children:
                    if c.type == "start_tag":
                        start_tag = c
                        break

                if start_tag:
                    for c in start_tag.named_children:
                        if c.type == "tag_name":
                            tag_name = c.text.decode("utf-8")
                        elif c.type == "attribute":
                            attr_name = ""
                            attr_val = ""
                            for ac in c.named_children:
                                if ac.type == "attribute_name":
                                    attr_name = ac.text.decode("utf-8")
                                elif ac.type == "quoted_attribute_value":
                                    attr_val = ac.text.decode("utf-8").strip("\"'")
                            if attr_name == "id":
                                element_id = attr_val

                should_extract = False
                if child.type in ("script_element", "style_element"):
                    should_extract = True
                elif tag_name in semantic_tags:
                    should_extract = True
                elif element_id:
                    should_extract = True

                if should_extract:
                    name = f"<{tag_name}>" if tag_name else f"<{child.type}>"
                    if element_id:
                        name += f" #{element_id}"

                    text = child.text.decode("utf-8")
                    entities.append(CodeEntity(
                        entity_type=child.type,
                        name=name,
                        signature=f"HTML Element: {name}",
                        docstring=None,
                        document_text=text,
                        file_path=file_path,
                        language=config.display_name,
                        lineno=child.start_point[0] + 1,
                        end_lineno=child.end_point[0] + 1,
                        parent_class=None,
                        decorators=[],
                        params=[],
                        return_type=None,
                    ))

            # Recurse to find nested ids or semantic tags
            _walk(child)

    _walk(root)


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------

def _extract_markdown(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    """Walk the tree-sitter CST for Markdown: headings and fenced code blocks."""
    root = tree.root_node
    lines = source_code.split("\n")

    def _walk_sections(node: Node, heading_stack: list[str]) -> None:
        """Recursively walk section nodes, extracting headings and code blocks."""
        for child in node.named_children:
            if child.type == "atx_heading":
                # Determine heading level from marker (atx_h1_marker ... atx_h6_marker)
                level = 1
                heading_text = ""
                for sub in child.children:
                    if sub.type == "atx_h1_marker":
                        level = 1
                    elif sub.type == "atx_h2_marker":
                        level = 2
                    elif sub.type == "atx_h3_marker":
                        level = 3
                    elif sub.type == "atx_h4_marker":
                        level = 4
                    elif sub.type == "atx_h5_marker":
                        level = 5
                    elif sub.type == "atx_h6_marker":
                        level = 6
                    elif sub.type == "inline":
                        heading_text = sub.text.decode("utf-8").strip()

                if not heading_text:
                    continue

                # Build breadcrumb path
                breadcrumb = heading_stack[: level - 1] + [heading_text]
                path_str = " > ".join(breadcrumb)

                # First paragraph after this heading (for richer document_text)
                body_text = ""
                # Walk siblings within the parent section to find the first paragraph
                parent = child.parent
                if parent:
                    found_self = False
                    for sibling in parent.named_children:
                        if sibling == child:
                            found_self = True
                            continue
                        if found_self and sibling.type == "paragraph":
                            # Extract inline text
                            for pchild in sibling.named_children:
                                if pchild.type == "inline":
                                    body_text = pchild.text.decode("utf-8").strip()
                                    break
                            break
                        if found_self and sibling.type in ("atx_heading", "fenced_code_block", "section"):
                            break

                signature = f"{'#' * level} {heading_text}"
                document_text = signature
                if body_text:
                    document_text += "\n" + body_text

                entities.append(CodeEntity(
                    entity_type="heading",
                    name=path_str,
                    signature=signature,
                    docstring=None,
                    document_text=document_text,
                    file_path=file_path,
                    language=config.display_name,
                    lineno=child.start_point[0] + 1,
                    end_lineno=child.end_point[0] + 1,
                    parent_class=None,
                    decorators=[],
                    params=[],
                    return_type=None,
                ))

            elif child.type == "fenced_code_block":
                info_string = ""
                code_content = ""
                for sub in child.named_children:
                    if sub.type == "info_string":
                        info_string = sub.text.decode("utf-8").strip()
                    elif sub.type == "code_fence_content":
                        code_content = sub.text.decode("utf-8").strip()

                if not code_content:
                    continue

                lang = info_string if info_string else "text"
                signature = f"```{lang}"
                document_text = f"```{lang}\n{code_content}\n```"

                entities.append(CodeEntity(
                    entity_type="code_block",
                    name=lang,
                    signature=signature,
                    docstring=None,
                    document_text=document_text,
                    file_path=file_path,
                    language=config.display_name,
                    lineno=child.start_point[0] + 1,
                    end_lineno=child.end_point[0] + 1,
                    parent_class=None,
                    decorators=[],
                    params=[],
                    return_type=None,
                ))

            elif child.type == "section":
                # Recurse into subsections with inherited breadcrumb
                _walk_sections(child, heading_stack)

    _walk_sections(root, [])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean_docstring(docstring: str) -> str:
    """Clean up a docstring/JSDoc for embedding text.

    Extracts the first paragraph/sentence — the summary line.
    """
    if not docstring:
        return ""

    # Remove the triple quotes or JSDoc markers
    text = docstring.strip()
    text = text.strip('"').strip("'").strip()

    # Take the first meaningful line(s) — up to the first blank line
    # or first period, whichever is shorter (but at least 1 sentence)
    lines = text.split("\n")
    meaningful = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            break
        meaningful.append(stripped)

    result = " ".join(meaningful)

    # Try to take up to the first period for a one-liner
    first_period = result.find(". ")
    if first_period > 20:  # Only truncate if we have a meaningful sentence
        result = result[: first_period + 1]

    return result
