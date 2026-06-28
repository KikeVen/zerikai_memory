"""Multi-language code entity extractor using tree-sitter.
Replaces LLM-based code indexing with deterministic parsing.
Extracts functions, methods, classes, and their docstrings/JSDoc
as individual indexable entities. No API calls, no token costs,
no empty responses. Supports: Python, JavaScript, TypeScript, TSX
(extensible via tree-sitter grammar packages). Requires: pip install
tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript.
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


# Python tree-sitter Language from tree-sitter-python grammar package.
# Used by _extract_python for parsing .py files.
PY_LANG = Language(tspython.language())
# JavaScript tree-sitter Language from tree-sitter-javascript grammar.
# Used by _extract_js_like for .js, .mjs, .cjs, .jsx files.
JS_LANG = Language(tsjavascript.language())
# TypeScript tree-sitter Language from tree-sitter-typescript grammar.
# Used by _extract_js_like for .ts files.
TS_LANG = Language(tstypescript.language_typescript())
# TSX tree-sitter Language from tree-sitter-typescript TSX grammar.
# Used by _extract_js_like for .tsx files.
TSX_LANG = Language(tstypescript.language_tsx())
# CSS tree-sitter Language from tree-sitter-css grammar package.
# Used by _extract_css for .css files.
CSS_LANG = Language(tscss.language())
# HTML tree-sitter Language from tree-sitter-html grammar package.
# Used by _extract_html for .html and .htm files.
HTML_LANG = Language(tshtml.language())
# Markdown tree-sitter Language from tree-sitter-markdown grammar.
# Used by _extract_markdown for .md and .mdx files.
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
    """Single code entity extracted by tree-sitter for ChromaDB vector indexing.
    Holds the signature, docstring/JSDoc, and metadata for one function,
    method, or class. The document_text field is what tree-sitter embeds
    into ChromaDB. Pure dataclass — no methods, no side effects.
    """

    entity_type: str  # "function", "method", "class", "constant", "markdown_section", "rule_set"
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
    """Return all file extensions parsable by tree-sitter grammar packages.
    Derived from LANGUAGE_CONFIGS registry — includes .py, .js, .ts, .css,
    .html, .md and variants. Pure, deterministic, read-only.
    """
    return set(_EXTENSION_MAP.keys())


def extract_entities(source_code: str, file_path: str) -> list[CodeEntity]:
    """Parse a source file with tree-sitter and extract all functions, classes, and methods.
    Routes by file extension: .py → _extract_python, .js/.ts/.tsx →
    _extract_js_like, .css → _extract_css, .html → _extract_html,
    .md → _extract_markdown. Uses tree-sitter grammars from
    tree-sitter-python, tree-sitter-javascript, tree-sitter-typescript,
    tree-sitter-css, tree-sitter-html, and tree-sitter-markdown packages.
    Returns empty list for unsupported extensions. No API calls, no side
    effects beyond CPU parsing.
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
        """Walk a tree-sitter block body node for function/class definitions.
        Dispatches function_definition and class_definition children to
        _extract_python_function / _extract_python_class. Unwraps
        decorated_definition nodes to extract inner definitions with
        decorator lists. Recurses into class bodies for nested methods.
        Mutates the entities list in-place — no return value.
        """
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
            elif parent_class is None and child.type == "expression_statement":
                # Module-level UPPER_CASE constant assignments
                for sub in child.named_children:
                    if sub.type == "assignment":
                        entity = _extract_python_constant(
                            sub, source_bytes, lines, file_path, config.display_name
                        )
                        if entity:
                            entities.append(entity)
                        break

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
    """Build a CodeEntity from a tree-sitter Python function_definition node.
    Extracts name, docstring (via _extract_python_docstring), decorators
    (via _extract_python_decorators), params (via _extract_python_params),
    and return type via tree-sitter's child_by_field_name API. Returns
    None if the node lacks a name field. Sets entity_type to "method"
    when parent_class is provided. Pure — no side effects.
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
    """Build a CodeEntity from a tree-sitter Python class_definition node.
    Extracts name, base classes from argument_list children, docstring
    (via _extract_python_docstring), and decorators (via
    _extract_python_decorators) using tree-sitter CST. Returns None
    if the node lacks a name field. Pure — no side effects.
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


def _extract_python_constant(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
) -> CodeEntity | None:
    """Build a CodeEntity from a tree-sitter Python assignment node for
    UPPER_CASE module-level constants. Routes by child_by_field_name:
    left must be an uppercase identifier; right becomes the value text;
    preceding # comments become the pseudo-docstring. Returns None if
    the left side is not UPPER_CASE. Pure — deterministic, no side effects.
    """
    left = node.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return None

    name = left.text.decode("utf-8")
    if not name.isupper():
        return None

    # Extract the value from the right-hand side
    right = node.child_by_field_name("right")
    value_text = right.text.decode("utf-8") if right else ""

    # Use preceding comments as the docstring
    docstring = _extract_preceding_comment(node, lines)

    # Build signature and document text
    signature = f"{name} = {value_text}"
    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="constant",
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


def _extract_python_docstring(node: Node, lines: list[str]) -> str | None:
    """Extract the docstring from a tree-sitter Python function or class body.
    Walks the body block for the first expression_statement containing a
    tree-sitter string node (type="string"). Handles both single-line and
    multi-line docstrings via start_point/end_point slicing. Returns None
    if no docstring is found or the node has no body. Pure — no side effects.
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
    Two-strategy routing: decorated_definition nodes search named children
    for decorator-typed nodes; bare function/class nodes walk
    prev_named_sibling via tree-sitter CST. Returns empty list if no
    decorators found. Pure — deterministic, no side effects.
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
    Routes by tree-sitter child type: identifier, typed_parameter,
    default_parameter, typed_default_parameter, list_splat_pattern,
    and dictionary_splat_pattern. Each dict has keys: name,
    type_annotation, default. Uses tree-sitter CST child iteration.
    Pure — deterministic, no side effects.
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
    Uses tree-sitter's child_by_field_name('return_type') API to find the
    type annotation child. Returns None if no return type annotation is
    present. Pure — deterministic, no side effects.
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
    definitions. Routes by node type: function_declaration →
    _extract_js_function, class_declaration → _extract_js_class (with
    body recursion for methods), method_definition → _extract_js_function,
    arrow functions via lexical_declaration → _extract_arrow_function.
    Uses tree-sitter-javascript and tree-sitter-typescript grammars.
    Mutates entities list in-place — no ordering guarantee. No side
    effects beyond the list append.
    """
    lines = source_code.split("\n")
    source_bytes = source_code.encode("utf-8")
    root = tree.root_node

    def _walk(node: Node, parent_class: str | None) -> None:
        """Walk tree-sitter CST children dispatching to extractors by node type.
        Routes function_declaration to _extract_js_function, class_declaration
        to _extract_js_class (with method recursion), method_definition to
        _extract_js_function, and lexical_declaration to _extract_arrow_function
        or _extract_js_constant. Recurse-s into non-terminal blocks.
        Mutates entities list in-place, no return value."""
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
                # const myFunc = () => { ... }  OR  const UPPER_CASE = ...
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
                        elif name_node:
                            entity = _extract_js_constant(
                                decl_child, source_bytes, lines, file_path,
                                config.display_name
                            )
                            if entity:
                                entities.append(entity)
            elif child.type == "variable_declaration":
                # var / let UPPER_CASE constants (non-const declarations)
                for decl_child in child.named_children:
                    if decl_child.type == "variable_declarator":
                        entity = _extract_js_constant(
                            decl_child, source_bytes, lines, file_path,
                            config.display_name
                        )
                        if entity:
                            entities.append(entity)
            # Recurse into nested blocks (but stop at function/class/declarations — already handled above)
            if child.type not in ("function_declaration", "class_declaration", "method_definition",
                                  "lexical_declaration", "variable_declaration"):
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
    """Build a CodeEntity from a tree-sitter JS/TS function_declaration or
    method_definition node. Extracts name via child_by_field_name('name'),
    JSDoc (via _extract_jsdoc), params (via _extract_js_params), and
    return type (via _extract_js_return_type). Returns None if the node
    lacks a name field. Sets entity_type to "method" when parent_class
    is provided. Pure — no side effects.
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
    Extracts from lexical_declaration patterns like `const myFunc =
    (params) => { ... }`. Looks up the arrow_function value child via
    child_by_field_name('value'), then extracts params and return type.
    Returns None if the node lacks a name field or the value child is
    not an arrow_function. Pure — no side effects.
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
    """Build a CodeEntity from a tree-sitter JS/TS class_declaration node.
    Extracts the class name via child_by_field_name('name'), JSDoc
    comment (via _extract_jsdoc), and builds a `class Name` signature.
    Returns None if the node lacks a name field. Pure — deterministic,
    no side effects.
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


def _extract_js_constant(
    node: Node,
    source_bytes: bytes,
    lines: list[str],
    file_path: str,
    language: str,
) -> CodeEntity | None:
    """Build a CodeEntity from a tree-sitter JS/TS variable_declarator node
    for UPPER_CASE constants. Routes by value length: scalars pass through;
    multi-line objects/arrays over 5 lines are truncated with "[truncated]"
    to avoid ChromaDB bloat. Preceding // comments become the pseudo-docstring
    via _extract_preceding_comment. Pure — no side effects.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    name = name_node.text.decode("utf-8")
    if not name.isupper():
        return None

    # Extract the value from the right-hand side
    value_node = node.child_by_field_name("value")
    if value_node is None:
        return None

    value_text = value_node.text.decode("utf-8")

    # Truncate multi-line values (objects, arrays, config blocks) to 5 lines
    value_lines = value_text.split("\n")
    if len(value_lines) > 5:
        value_text = "\n".join(value_lines[:5]) + "\n... [truncated]"

    # Use preceding // comments as the docstring
    docstring = _extract_preceding_comment(node, lines)

    # Build signature and document text
    signature = f"{name} = {value_text}"
    text = signature
    if docstring:
        text += "\n" + _clean_docstring(docstring)

    return CodeEntity(
        entity_type="constant",
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
    comments, stripping JSDoc markers and leading asterisks. Falls through
    single-line // comments. Returns None if no JSDoc block is found above
    the node or the node is at line 0. Pure — no side effects.
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
    Routes by tree-sitter child type: identifier, required_parameter (TS
    typed), and optional_parameter (TS optional/default with '?' or '=').
    Each dict has keys: name, type_annotation, default. Uses tree-sitter
    CST child iteration. Pure — deterministic, no side effects.
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
    Uses tree-sitter's child_by_field_name('return_type') API to find the
    type_annotation child. Returns None if no return type annotation is
    present. Pure — deterministic, no side effects.
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
    """Walk the tree-sitter CST for CSS rule sets using tree-sitter-css grammar.
    Extracts each rule_set node with its selectors as a CodeEntity
    containing the full rule text. Semantically empty rule sets (no
    selectors) are skipped. Mutates entities list in-place. No guarantees
    on selector ordering — follows tree-sitter's depth-first CST traversal.
    """
    root = tree.root_node

    def _walk(node: Node):
        """Walk tree-sitter-css CST for rule_set nodes, extracting each as a
        CodeEntity with its selector text. Skips non-rule_set children by
        recursing deeper. Mutates entities list in-place. Pure, deterministic."""
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
    """Walk the tree-sitter CST for semantic HTML elements using tree-sitter-html.
    Extracts elements with semantic tag names (main, header, section,
    article, nav, aside, footer), id-bearing elements (via attribute
    parsing), and script/style elements. Captures preceding HTML comments
    (type="comment") as docstrings. Mutates entities list in-place.
    Skips non-semantic, non-id elements. Pure beyond the list mutation.
    """
    root = tree.root_node
    semantic_tags = {"main", "header", "footer", "section", "article", "nav", "aside"}

    def _walk(node: Node):
        """Walk tree-sitter-html CST for semantic elements and script/style blocks.
        Extracts elements with semantic tags (main, header, section, article, nav,
        aside, footer), id-bearing elements, and script/style containers. Captures
        preceding HTML comment nodes as docstrings. Recurse-s into children.
        Mutates entities list in-place. Uses tree-sitter-html grammar."""
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
    """Walk the tree-sitter-markdown CST and emit sections as ChromaDB entities.
    Each section entity contains all its prose, tables, lists, blockquotes,
    and fenced code blocks. Child sections are recursed as separate
    entities for breadcrumb hierarchy in vector search. Loose content
    before the first heading becomes a preamble entity. Mutates entities
    list in-place. Uses tree-sitter-markdown grammar.
    """
    root = tree.root_node

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _heading_info(node: Node) -> tuple[int, str] | None:
        """Parse a tree-sitter atx_heading node into (level, text).
        Matches atx_h1_marker through atx_h6_marker children for the
        heading level, and inline children for the text. Returns None
        if no marker+text combination is found. Pure, deterministic.
        """
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
        """Append a CodeEntity to the entities list with markdown metadata.
        Creates a CodeEntity with entity_type='markdown_section', the
        given name/signature/document_text, and the current file_path
        and language from the enclosing _extract_markdown scope. Skips
        entities with empty document_text. Pure beyond the list append.
        """
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

    def _extract_checklists(
        section_text: str,
        breadcrumb: str,
        heading: str,
        base_lineno: int,
    ) -> None:
        """Scan a section's markdown text with re (regex) for checkbox items,
        emitting each as a markdown_checklist entity. Routes by marker:
        [ ] → pending, [x]/[X] → completed. Groups items under their
        nearest heading via breadcrumb context. Mutates entities list
        in-place — deterministic, no other side effects.
        """
        pattern = re.compile(r'^[\s]*[-*+]\s+\[([ xX])\]\s+(.+)$', re.MULTILINE)
        for match in pattern.finditer(section_text):
            status_char = match.group(1)
            item_text = match.group(2).strip()
            completed = status_char.lower() == "x"
            status_label = "[x]" if completed else "[ ]"

            # Calculate approximate line number within the file
            preceding_newlines = section_text[:match.start()].count("\n")
            item_lineno = base_lineno + preceding_newlines

            signature = f"{status_label} {item_text}"
            document_text = (
                f"Checklist item under '{heading}'\n"
                f"Status: {'completed' if completed else 'pending'}\n"
                f"{status_label} {item_text}"
            )
            name = f"{breadcrumb} > {item_text[:80]}"

            _emit(
                entity_type="markdown_checklist",
                name=name,
                signature=signature,
                document_text=document_text,
                start_lineno=item_lineno,
                end_lineno=item_lineno,
            )

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
            section_text = child.text.decode("utf-8")
            section_start = child.start_point[0] + 1
            section_end = child.end_point[0] + 1
            _emit(
                entity_type="markdown_section",
                name=breadcrumb,
                signature=sig,
                document_text=section_text,
                start_lineno=section_start,
                end_lineno=section_end,
            )

            # Extract checkbox items as individual searchable entities
            _extract_checklists(
                section_text, breadcrumb, heading_text, section_start
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
    """Clean a docstring/JSDoc into embedding-ready text for ChromaDB.
    Strips triple quotes and JSDoc markers, joins consecutive meaningful
    lines, truncates at the first blank line. Pure — no side effects,
    deterministic output.
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


def _extract_preceding_comment(node: Node, lines: list[str]) -> str | None:
    """Walk backwards from a tree-sitter node collecting contiguous # (Python)
    or // (JS/TS) comment lines, skipping blanks between them. Stops at the
    first non-comment line. Returns joined text with markers stripped, or
    None if no comments found. Pure — deterministic, no side effects.
    """
    start_line = node.start_point[0]
    if start_line == 0:
        return None

    comment_lines: list[str] = []
    prev_line = start_line - 1

    while prev_line >= 0:
        stripped = lines[prev_line].strip()
        if not stripped:
            # Blank line — keep going (common between comment blocks and code)
            prev_line -= 1
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            # Strip the comment marker(s) and leading whitespace from the text
            if stripped.startswith("#"):
                text = stripped.lstrip("#").strip()
            else:
                text = stripped.lstrip("/").strip()
            comment_lines.insert(0, text)
            prev_line -= 1
        else:
            # Non-comment line — stop
            break

    return "\n".join(comment_lines) if comment_lines else None
