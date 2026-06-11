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
    """Configuration for a supported tree-sitter language. Maps a file
    extension to its tree-sitter grammar (Language object), entity query
    (tree-sitter query string for finding functions/classes), and display
    name. Used by extract_entities to select the correct parser per file
    type. Pure data container — no methods, no side effects.
    """

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
    """A single indexable code entity extracted by tree-sitter from a source file."""

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
    """Parse a source file with tree-sitter and extract all functions, classes, and methods.

    Automatically selects the correct tree-sitter grammar based on file extension.
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
    """Walk the tree-sitter CST for Python function and class definitions.

    Recurses into class bodies to extract nested methods. Handles both
    bare and decorated definitions via tree-sitter node types. Mutates
    the entities list in-place — no return value. No guarantees on
    ordering.
    """
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
    """Build a CodeEntity from a tree-sitter Python function node.

    Extracts the name, docstring, decorators, parameters (via
    _extract_python_params), and return type annotation. Returns
    None if the node lacks a name field. Pure — no side effects.
    """
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
    """Build a CodeEntity from a tree-sitter Python class node.

    Extracts the name, base classes, docstring, and decorators.
    Returns None if the node lacks a name field. Pure.
    """
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
    """Extract the docstring from a tree-sitter Python function or class body.

    Walks the body block for the first expression_statement containing a
    tree-sitter string node. Handles both single-line and multi-line
    docstrings. Returns None if no docstring is found.
    """
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
    """Extract decorator names from a tree-sitter Python function/class node.

    Handles both decorated_definition nodes (named children) and bare
    function/class nodes (prev_named_sibling walk). Uses tree-sitter CST
    navigation. Returns empty list if no decorators found. Pure.
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
    """Extract parameter metadata from a tree-sitter Python function node.

    Handles identifier, typed_parameter, default_parameter,
    typed_default_parameter, and splat patterns. Each dict has keys:
    name, type_annotation, default. Uses tree-sitter CST. Pure.
    """
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
    """Extract the return type annotation from a tree-sitter Python function node.

    Uses the child_by_field_name('return_type') API. Returns None
    if no return type annotation is present. Pure.
    """
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
    """Walk the tree-sitter CST for JS/TS/TSX function, method, and class
    definitions. Handles function_declaration, class_declaration,
    method_definition, and arrow functions via lexical_declaration.
    Recurses into class bodies and nested blocks. Mutates entities list
    in-place — no ordering guarantee. Pure beyond the side-effect append.
    """
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
    """Build a CodeEntity from a tree-sitter JS/TS function or method node.

    Extracts the name, JSDoc (via _extract_jsdoc), parameters (via
    _extract_js_params), and return type. Returns None if the node
    lacks a name field. Pure.
    """
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
    """Build a CodeEntity from a tree-sitter const arrow function node.

    Extracts from patterns like `const myFunc = (params) => { ... }`.
    Looks up the arrow_function value child for params and return type.
    Returns None if the node lacks a name field. Pure.
    """
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
    """Build a CodeEntity from a tree-sitter JS/TS class node.

    Extracts the class name, JSDoc comment, and builds a signature.
    Returns None if the node lacks a name field. Pure.
    """
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
    """Extract JSDoc comment preceding a tree-sitter JS/TS function or class.
    Walks backwards from the node's start line through /** ... */ block
    comments, stripping JSDoc markers and leading asterisks. Returns None
    if no JSDoc block is found above the node. Pure — no side effects.
    """
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
    """Extract parameter metadata from a tree-sitter JS/TS function or arrow node.

    Handles identifier, required_parameter (TS typed), and optional_parameter
    (TS optional/default). Each dict has keys: name, type_annotation, default.
    Uses tree-sitter CST. Pure.
    """
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
    """Extract the return type annotation from a tree-sitter JS/TS function node.

    Uses the child_by_field_name('return_type') API. Returns None
    if no return type annotation is present. Pure.
    """
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
    """Walk the tree-sitter CST for CSS rule sets. Extracts each rule_set
    node with its selectors as a CodeEntity containing the full rule text.
    Mutates entities list in-place. No guarantees on selector ordering —
    extraction order follows tree-sitter's depth-first CST traversal.
    """
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
    """Walk the tree-sitter CST for semantic HTML elements using the
    tree-sitter-html grammar. Extracts elements with semantic tag names
    (main, header, section, article, nav, aside, footer), id-bearing
    elements, and script/style elements. Captures preceding HTML comments
    as docstrings. Mutates entities list in-place.
    """
    root = tree.root_node
    semantic_tags = {"main", "header", "footer", "section", "article", "nav", "aside"}

    def _walk(node: Node):
        pending_comment = None
        for child in node.children:
            # Collect HTML comments as docstrings for the next element
            if child.type == "comment":
                pending_comment = child.text.decode("utf-8").strip()
                continue

            if child.type not in ("element", "script_element", "style_element"):
                _walk(child)
                continue

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
                docstring = None
                if pending_comment:
                    docstring = pending_comment.strip("<!--").strip("-->").strip()
                    pending_comment = None

                entities.append(CodeEntity(
                    entity_type=child.type,
                    name=name,
                    signature=f"HTML Element: {name}",
                    docstring=docstring,
                    document_text=f"{name}\n{docstring}" if docstring else text,
                    file_path=file_path,
                    language=config.display_name,
                    lineno=child.start_point[0] + 1,
                    end_lineno=child.end_point[0] + 1,
                    parent_class=None,
                    decorators=[],
                    params=[],
                    return_type=None,
                ))

            _walk(child)

    _walk(root)


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------

def _extract_markdown(
    tree, source_code: str, file_path: str, config: LanguageConfig, entities: list[CodeEntity]
) -> None:
    """Walk the tree-sitter-markdown CST and emit each section as a single
    rich entity containing all its prose, tables, lists, blockquotes, and
    fenced code blocks. Child sections are recursed into as separate
    entities to preserve breadcrumb hierarchy in vector search. Loose
    content before the first heading is captured as a preamble entity.
    Mutates entities list in-place. Uses tree-sitter-markdown grammar.
    """
    root = tree.root_node

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _heading_info(node: Node) -> tuple[int, str] | None:
        """Return (level, text) for an atx_heading child, or None if the
        node isn't a recognised heading."""
        level = 1
        text = ""
        found_marker = False
        for sub in node.children:
            if sub.type == "atx_h1_marker":
                level, found_marker = 1, True
            elif sub.type == "atx_h2_marker":
                level, found_marker = 2, True
            elif sub.type == "atx_h3_marker":
                level, found_marker = 3, True
            elif sub.type == "atx_h4_marker":
                level, found_marker = 4, True
            elif sub.type == "atx_h5_marker":
                level, found_marker = 5, True
            elif sub.type == "atx_h6_marker":
                level, found_marker = 6, True
            elif sub.type == "inline":
                text = sub.text.decode("utf-8").strip()
        return (level, text) if found_marker and text else None

    def _emit(
        entity_type: str,
        name: str,
        signature: str,
        document_text: str,
        start_lineno: int,
        end_lineno: int,
    ) -> None:
        """Append a CodeEntity to the shared list, skipping empty bodies."""
        if not document_text.strip():
            return
        entities.append(CodeEntity(
            entity_type=entity_type,
            name=name,
            signature=signature,
            docstring=None,
            document_text=document_text,
            file_path=file_path,
            language=config.display_name,
            lineno=start_lineno,
            end_lineno=end_lineno,
            parent_class=None,
            decorators=[],
            params=[],
            return_type=None,
        ))

    # -------------------------------------------------------------------
    # Section walker — emits each section as a whole entity
    # -------------------------------------------------------------------

    def _walk(node: Node, heading_stack: list[str]) -> None:
        """Walk children: emit sections as whole entities, then recurse
        into nested child sections with updated breadcrumbs."""
        for child in node.named_children:
            if child.type != "section":
                continue

            # --- Find the section's heading ---
            heading_text: str | None = None
            level = 0
            for sub in child.named_children:
                if "heading" in sub.type:
                    info = _heading_info(sub)
                    if info:
                        level, heading_text = info
                    break

            # tree-sitter-markdown wraps loose preamble content in a
            # heading-less section.  Give it a meaningful name.
            if heading_text is None:
                heading_text = f"{file_path} (preamble)" if not heading_stack else "Untitled"
                level = 1

            new_stack = heading_stack[: max(0, level - 1)] + [heading_text]
            breadcrumb = " > ".join(new_stack)
            sig = f"{'#' * max(level, 1)} {heading_text}" if heading_stack or "preamble" not in heading_text else "Preamble"

            # Emit the entire section body (raw markdown) as one entity.
            # Child sections are included in this text — that's intentional:
            # the parent provides broad context; child entities provide
            # focused precision.  Both are useful for vector search.
            _emit(
                entity_type="markdown_section",
                name=breadcrumb,
                signature=sig,
                document_text=child.text.decode("utf-8"),
                start_lineno=child.start_point[0] + 1,
                end_lineno=child.end_point[0] + 1,
            )

            # Recurse into nested child sections
            _walk(child, new_stack)

    _walk(root, [])

    # -------------------------------------------------------------------
    # Loose non-section nodes at document root (code blocks, HTML blocks,
    # etc. that tree-sitter-markdown places outside any section).
    # -------------------------------------------------------------------

    loose_parts: list[str] = []
    loose_start: int | None = None
    loose_end: int | None = None

    for child in root.named_children:
        if child.type == "section":
            continue  # already handled by _walk
        text = child.text.decode("utf-8").strip()
        if text:
            loose_parts.append(text)
            if loose_start is None:
                loose_start = child.start_point[0] + 1
            loose_end = child.end_point[0] + 1

    if loose_parts:
        _emit(
            entity_type="markdown_section",
            name=f"{file_path} (loose)",
            signature="Loose content",
            document_text="\n\n".join(loose_parts),
            start_lineno=loose_start or 1,
            end_lineno=loose_end or 1,
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean_docstring(docstring: str) -> str:
    """Clean a docstring/JSDoc into embedding-ready text.

    Strips triple quotes and JSDoc markers from the raw text, joins
    consecutive meaningful lines (stops at first blank line). Returns
    the full extracted text with no truncation. Pure.
    """
    if not docstring:
        return ""

    # Remove the triple quotes or JSDoc markers
    text = docstring.strip()
    text = text.strip('"').strip("'").strip()

    # Take the first meaningful line(s) — up to the first blank line
    # Take meaningful lines up to the first blank line
    lines = text.split("\n")
    meaningful = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            break
        meaningful.append(stripped)

    result = " ".join(meaningful)
    return result
