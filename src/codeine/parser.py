"""Tree-sitter AST extractor, skeleton generator and syntax validator.

Supports (via tree-sitter-language-pack): Python, TypeScript/TSX,
JavaScript/JSX, PHP, Rust, Go, HTML and CSS.

Everything here works on plain strings and returns compact Python data —
no file I/O, no exceptions for the happy paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

# --------------------------------------------------------------------------
# Language maps
# --------------------------------------------------------------------------

LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".php": "php",
    ".rs": "rust",
    ".go": "go",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
}

# node_type -> (kind, name_field, body_field|None)
_SYMBOL_SPECS: dict[str, dict[str, tuple[str, str, str | None]]] = {
    "python": {
        "function_definition": ("function", "name", "body"),
        "class_definition": ("class", "name", "body"),
    },
    "typescript": {
        "function_declaration": ("function", "name", "body"),
        "method_definition": ("method", "name", "body"),
        "class_declaration": ("class", "name", "body"),
        "abstract_class_declaration": ("class", "name", "body"),
        "interface_declaration": ("interface", "name", "body"),
        "type_alias_declaration": ("type", "name", None),
        "enum_declaration": ("type", "name", "body"),
    },
    "tsx": {},  # filled from typescript at import end
    "javascript": {
        "function_declaration": ("function", "name", "body"),
        "method_definition": ("method", "name", "body"),
        "class_declaration": ("class", "name", "body"),
        "abstract_class_declaration": ("class", "name", "body"),
        "enum_declaration": ("type", "name", "body"),
    },
    "php": {
        "function_definition": ("function", "name", "body"),
        "method_declaration": ("method", "name", "body"),
        "class_declaration": ("class", "name", "body"),
        "interface_declaration": ("interface", "name", "body"),
        "trait_declaration": ("interface", "name", "body"),
    },
    "rust": {
        "function_item": ("function", "name", "body"),
        "struct_item": ("class", "name", None),
        "trait_item": ("interface", "name", "body"),
        "enum_item": ("type", "name", None),
    },
    "go": {
        "function_declaration": ("function", "name", "body"),
        "method_declaration": ("method", "name", "body"),
    },
    "html": {},
    "css": {},
}
_SYMBOL_SPECS["tsx"] = _SYMBOL_SPECS["typescript"]

_CALL_TYPES: dict[str, set[str]] = {
    "python": {"call"},
    "typescript": {"call_expression", "optional_chaining_expression"},
    "tsx": {"call_expression", "optional_chaining_expression"},
    "javascript": {"call_expression", "optional_chaining_expression"},
    "php": {"function_call_expression", "method_call",
            "scoped_call_expression", "member_call_expression"},
    "rust": {"call_expression", "macro_invocation"},
    "go": {"call_expression"},
    "html": set(),
    "css": set(),
}

_CONTAINER_TYPES: dict[str, set[str]] = {
    "python": {"class_definition"},
    "typescript": {"class_declaration", "abstract_class_declaration"},
    "tsx": {"class_declaration", "abstract_class_declaration"},
    "javascript": {"class_declaration", "abstract_class_declaration"},
    "php": {"class_declaration"},
    "rust": {"struct_item", "impl_item"},
    "go": {"type_declaration"},
    "html": set(),
    "css": set(),
}

_COMMENT_PREFIX: dict[str, str] = {
    "python": "#", "typescript": "//", "tsx": "//", "javascript": "//",
    "php": "//", "rust": "//", "go": "//", "html": "<!--", "css": "/*",
}

_MAX_SIGNATURE_CHARS = 160
_MAX_NAME_CHARS = 60


@dataclass
class Symbol:
    """One extracted code symbol (function, class, method, ...)."""

    name: str
    qualified: str
    kind: str
    signature: str
    docstring: str
    start_line: int          # 1-based, inclusive
    end_line: int            # 1-based, inclusive
    calls: list[str] = field(default_factory=list)

    def calls_json(self) -> str:
        import json
        return json.dumps(self.calls)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_parser_cache: dict[str, object] = {}


def get_cached_parser(lang: str):
    parser = _parser_cache.get(lang)
    if parser is None:
        parser = get_parser(lang)
        _parser_cache[lang] = parser
    return parser


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _field(node, name: str):
    try:
        return node.child_by_field_name(name)
    except Exception:  # noqa: BLE001 - very old tree-sitter bindings
        return None


def _collapse(text: str, limit: int = _MAX_SIGNATURE_CHARS) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _docstring_for(node, body_node, source: bytes, lang: str) -> str:
    if body_node is not None and lang == "python":
        if not body_node.named_child_count:
            return ""
        stmt = body_node.named_child(0)
        # Newer tree-sitter-python exposes the docstring directly as `string`;
        # older versions wrap it in `expression_statement`.
        if stmt.type == "expression_statement" and stmt.named_child_count:
            stmt = stmt.named_child(0)
        if stmt is not None and stmt.type == "string":
            return _collapse(_node_text(stmt, source), 160)
    if lang in ("typescript", "tsx", "javascript"):
        # Comments are grammar "extras" and exported declarations are wrapped
        # in export_statement: climb a few ancestor levels looking for a
        # JSDoc comment immediately preceding the symbol.
        cur = node
        for _ in range(3):
            parent = cur.parent
            if parent is None:
                break
            siblings = parent.children
            idx = next((i for i, c in enumerate(siblings) if c.id == cur.id), None)
            if idx is None:
                break
            j = idx - 1
            while j >= 0 and siblings[j].type == "comment":
                text = _node_text(siblings[j], source)
                if text.startswith("/**"):
                    lines = [l.strip(" /**\n") for l in text.splitlines()]
                    return _collapse(" ".join(x for x in lines if x), 160)
                j -= 1
            cur = parent
    return ""


def _calls_for(node, source: bytes, lang: str) -> list[str]:
    wanted = _CALL_TYPES.get(lang, set())
    if not wanted:
        return []
    calls: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in wanted:
            fn = _field(cur, "function")
            if fn is not None:
                raw = _node_text(fn, source).strip()
                raw = raw.split("(")[0].strip()
                if raw:
                    name = raw[-_MAX_NAME_CHARS:]
                    if name not in calls:
                        calls.append(name)
        stack.extend(cur.named_children)
    calls.reverse()  # approximate source order (stack pops in reverse)
    return calls[:32]


# --------------------------------------------------------------------------
# Core API
# --------------------------------------------------------------------------

def extract_symbols(source: str, lang: str) -> list[Symbol]:
    """Extract every symbol declared in *source* for *lang*."""
    specs = _SYMBOL_SPECS.get(lang, {})
    if not specs:
        return _css_symbols(source, lang) if lang == "css" else []

    parser = get_cached_parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    src_lines = source.splitlines()
    symbols: list[Symbol] = []

    def walk(node: object, container: str) -> None:
        spec = specs.get(node.type)
        if spec is not None:
            kind, name_field, body_field = spec
            name_node = _field(node, name_field)
            if name_node is not None:
                name = _node_text(name_node, source.encode("utf-8"))
                body = _field(node, body_field) if body_field else None

                sig = _signature_for(src_lines, node, body)
                doc = _docstring_for(node, body, source.encode("utf-8"), lang)
                calls = _calls_for(body or node, source.encode("utf-8"), lang)

                symbols.append(Symbol(
                    name=name,
                    qualified=f"{container}.{name}" if container else name,
                    kind=kind,
                    signature=sig,
                    docstring=doc,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    calls=calls,
                ))
                new_container = name if kind == "class" else container
            else:
                new_container = container
        else:
            is_container = node.type in _CONTAINER_TYPES.get(lang, set())
            cn = _field(node, "name")
            new_container = (_node_text(cn, source.encode("utf-8"))
                             if is_container and cn is not None else container)
        for child in node.named_children:
            walk(child, new_container)

    walk(tree.root_node, "")
    return symbols


def _signature_for(src_lines: list[str], node, body) -> str:
    start = node.start_point[0]
    if start >= len(src_lines):
        return ""
    if body is None:
        return _collapse(src_lines[start])
    brow, bcol = body.start_point
    if brow == start:
        # body opens on the declaration line: keep the text before it
        return _collapse(src_lines[start][:bcol])
    # declaration spans lines [start, brow); line brow may hold the tail
    # of the parameter list (e.g. ')' or '{') before the body opens.
    parts = src_lines[start:brow]
    prefix = src_lines[brow][:bcol].strip() if brow < len(src_lines) else ""
    if prefix:
        parts.append(prefix)
    return _collapse(" ".join(parts))


def _css_symbols(source: str, lang: str) -> list[Symbol]:
    parser = get_cached_parser("css")
    tree = parser.parse(source.encode("utf-8"))
    out: list[Symbol] = []
    stack = [tree.root_node]
    while stack:
        cur = stack.pop()
        if cur.type == "rule_set":
            sel = cur.named_child(0)
            if sel is not None:
                name = _collapse(_node_text(sel, source.encode("utf-8")), 80)
                out.append(Symbol(name, name, "rule", name, "",
                                  cur.start_point[0] + 1, cur.end_point[0] + 1))
        stack.extend(cur.named_children)
    out.reverse()
    return out


# --------------------------------------------------------------------------
# Skeleton
# --------------------------------------------------------------------------

def generate_skeleton(source: str, lang: str) -> str:
    """Structural skeleton: signatures + imports kept, bodies collapsed."""
    if lang in ("html", "css", ""):
        return source  # declarative formats: skeleton == source
    specs = _SYMBOL_SPECS.get(lang, {})
    body_specs = {t: s[2] for t, s in specs.items() if s[2]}
    if not body_specs:
        return source

    parser = get_cached_parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    lines = source.splitlines()

    # Collect every body range of function-like nodes.
    ranges: list[tuple[int, int, int, int]] = []  # (srow, scol, erow, ecol)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        body_field = body_specs.get(node.type)
        if body_field:
            body = _field(node, body_field)
            if body is not None:
                ranges.append((body.start_point[0], body.start_point[1],
                               body.end_point[0], body.end_point[1]))
        stack.extend(node.named_children)

    # Keep only leaf bodies (those not containing another body).
    leaves = []
    for i, (sr, sc, er, ec) in enumerate(ranges):
        contains = False
        for j, (sr2, sc2, er2, ec2) in enumerate(ranges):
            if i != j and (sr2, sc2) >= (sr, sc) and (er2, ec2) <= (er, ec):
                contains = True
                break
        if not contains:
            leaves.append((sr, sc, er, ec))
    if not leaves:
        return source

    comment = _COMMENT_PREFIX.get(lang, "//")
    out: list[str] = []
    cursor = 0  # current line index (0-based)
    for sr, sc, er, ec in sorted(leaves):
        if sr >= len(lines):
            break
        if sr > cursor:
            out.extend(lines[cursor:sr])
        if sr == er:
            # inline body: keep whatever precedes the body on that line
            out.append(lines[sr][:sc] + f" {comment} ... [body: lines {sr + 1}]")
        else:
            indent = lines[sr][: len(lines[sr]) - len(lines[sr].lstrip())]
            out.append(f"{indent}{comment} ... [body: lines {sr + 1}-{er + 1}]")
            cursor = er + 1
            if cursor >= len(lines):
                break
            continue
        cursor = sr + 1
    if cursor < len(lines):
        out.extend(lines[cursor:])
    return "\n".join(out)


# --------------------------------------------------------------------------
# Syntax validation
# --------------------------------------------------------------------------

def validate_syntax(source: str, lang: str) -> tuple[bool, str]:
    """Return (ok, ''). If invalid, (False, 'line L, column C' + snippet)."""
    if lang == "":
        return True, ""
    try:
        parser = get_cached_parser(lang)
    except Exception as exc:  # noqa: BLE001 - unknown language
        return False, f"language '{lang}' unavailable: {exc}"
    tree = parser.parse(source.encode("utf-8"))
    if not tree.root_node.has_error:
        return True, ""

    # Descend only along branches that carry the error; report the first
    # ERROR node or MISSING token found (deepest root cause near the top).
    from collections import deque

    root = tree.root_node
    queue = deque([root])
    bad = None
    while queue:
        node = queue.popleft()
        if node.type == "ERROR" or getattr(node, "is_error", False) \
                or getattr(node, "is_missing", False):
            bad = node
            break
        if node is root or node.has_error:
            queue.extend(node.children)
    if bad is None:
        return False, "unknown syntax error"
    row, col = bad.start_point
    snippet = source.splitlines()[row][:120] if row < len(source.splitlines()) else ""
    return False, f"syntax error at line {row + 1}, column {col + 1} | near: {snippet!r}"


def lang_for_path(path: "str | os.PathLike") -> str:
    return LANG_BY_EXT.get(Path(path).suffix.lower(), "")
