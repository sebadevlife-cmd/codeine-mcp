"""Codeine MCP server - FastMCP over stdio.

Tools are designed for LLM *context offloading*: every response is dense
plain text/Markdown with exact file:line references, never raw JSON dumps.

Cross-OS guarantees: 100 % ``pathlib`` path handling, forced UTF-8 on every
file operation, and strict workspace containment (paths that escape the
workspace root are rejected).
"""
from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import connectors, indexer, parser, runner

mcp = FastMCP(
    "codeine",
    instructions=(
        "Codeine provides token-efficient code intelligence. ALWAYS prefer "
        "these tools over reading whole files: get_file_skeleton to map a "
        "file, search_symbols to locate code, read_symbol for exact source, "
        "surgical_patch to edit, run_pruned_command for builds/tests."
    ),
)


# --------------------------------------------------------------------------
# Internal helpers (pathlib-only, containment-enforced)
# --------------------------------------------------------------------------

def _abs(root: str | Path) -> Path:
    return Path(root if root else ".").resolve()


def _resolve(root: str | Path, file_path: str | Path) -> tuple[Path, Path]:
    """Return (abs_root, abs_file); reject paths escaping the workspace root."""
    root = _abs(root)
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(
            f"path escapes workspace root ({root}): {file_path}")
    return root, candidate


def _rel(root: Path, abs_file: Path) -> str:
    try:
        return abs_file.relative_to(root).as_posix()
    except ValueError:
        return abs_file.as_posix()


def _read_text(abs_file: Path) -> str:
    with abs_file.open(encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# Tool 1: get_file_skeleton
# --------------------------------------------------------------------------

@mcp.tool()
def get_file_skeleton(file_path: str, workspace_root: str = ".") -> str:
    """Structural skeleton of a source file: keeps imports, globals, class
    and function signatures; collapses implementation bodies into
    `# ... [body: lines X-Y]` markers. ~10x fewer tokens than the full file.
    Supports py/ts/tsx/js/jsx/php/rs/go/html/css."""
    try:
        root, abs_file = _resolve(workspace_root, file_path)
        lang = parser.lang_for_path(abs_file)
        if not lang:
            return f"Unsupported file type: {file_path}"
        skeleton = parser.generate_skeleton(_read_text(abs_file), lang)
        head = f"[skeleton {file_path} ({lang})]"
        return head + "\n" + skeleton
    except ValueError as exc:
        return f"[ERROR] {exc}"
    except OSError as exc:
        return f"[ERROR] cannot read {file_path}: {exc!r}"
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] skeleton failed: {exc!r}"


# --------------------------------------------------------------------------
# Tool 2: read_symbol
# --------------------------------------------------------------------------

@mcp.tool()
def read_symbol(symbol_name: str, file_path: str | None = None,
                workspace_root: str = ".") -> str:
    """Read ONLY the exact source block of a named symbol (function/class/
    method), from its start_line to end_line, with `L<n>:` line prefixes.
    Optionally scope to one file. Use after search_symbols/skeleton."""
    try:
        root = _abs(workspace_root)
        indexer.ensure_index(root)
        defs = indexer.find_definitions(root, symbol_name)
        if not defs and "." in symbol_name:  # Class.method -> bare method name
            defs = indexer.find_definitions(root, symbol_name.split(".")[-1])
        if file_path:
            want = Path(file_path).as_posix()
            suffix = f"/{want}"
            defs = [d for d in defs
                    if d["file_path"] == want or d["file_path"].endswith(suffix)]
        if not defs:
            return f"Symbol '{symbol_name}' not found in index."

        blocks = []
        for d in defs[:3]:
            abs_file = root / d["file_path"]
            lines = _read_text(abs_file).splitlines()
            lo, hi = d["start_line"], min(d["end_line"], len(lines))
            numbered = "\n".join(f"L{i}: {lines[i - 1]}"
                                 for i in range(lo, hi + 1))
            blocks.append(f"[{d['file_path']}:{lo}-{hi}] ({d['kind']}) "
                          f"{d['signature']}\n{numbered}")
        text = "\n\n".join(blocks)
        if len(defs) > 3:
            text += f"\n\n({len(defs) - len(blocks)} more definitions omitted)"
        return text
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] read_symbol failed: {exc!r}"


# --------------------------------------------------------------------------
# Tool 3: search_symbols (BM25)
# --------------------------------------------------------------------------

@mcp.tool()
def search_symbols(query: str, limit: int = 10, workspace_root: str = ".") -> str:
    """BM25 search over indexed symbols (name/signature/docstring) across the
    workspace. Returns compact rows: `[file:L10-L45] (kind) signature — doc`.
    Use this INSTEAD of grepping or reading whole files."""
    try:
        root = _abs(workspace_root)
        stats = indexer.ensure_index(root)
        rows = indexer.search_symbols(root, query, limit)
        if not rows:
            return (f"No symbols match {query!r} "
                    f"(index: {stats['symbols']} symbols in {stats}).")
        out = [f"[BM25 '{query}' - {len(rows)} hit(s), index {stats['symbols']} symbols]"]
        for r in rows:
            doc = f" — {r['docstring'][:60]}" if r["docstring"] else ""
            out.append(f"[{r['file_path']}:L{r['start_line']}-L{r['end_line']}] "
                       f"({r['kind']}) {r['signature']}{doc}")
        return "\n".join(out)
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] search failed: {exc!r}"


# --------------------------------------------------------------------------
# Tool 4: impact_graph
# --------------------------------------------------------------------------

@mcp.tool()
def impact_graph(symbol_name: str, workspace_root: str = ".") -> str:
    """Impact analysis in <=20 lines: where the symbol is DEFINED, what it
    CALLS (callees) and WHO calls it (callers), with file:line refs.
    Run this before refactoring or renaming anything."""
    try:
        root = _abs(workspace_root)
        indexer.ensure_index(root)
        defs = indexer.find_definitions(root, symbol_name)
        if not defs and "." in symbol_name:
            defs = indexer.find_definitions(root, symbol_name.split(".")[-1])
        if not defs:
            return f"Symbol '{symbol_name}' not found in index."

        lines = [f"IMPACT '{symbol_name}'"]
        lines.append("Defined at:")
        for d in defs[:5]:
            lines.append(f"  {d['file_path']}:{d['start_line']} ({d['kind']}) "
                         f"{d['signature'][:90]}")

        callee_names: set[str] = set()
        for d in defs:
            try:
                callee_names.update(json.loads(d["calls_raw"]))
            except json.JSONDecodeError:
                pass
        if callee_names:
            lines.append("Calls (callees):")
            shown = 0
            for cname in sorted(callee_names):
                if shown >= 8:
                    lines.append(f"  +{len(callee_names) - shown} more callees")
                    break
                cdefs = indexer.find_definitions(root, cname)
                if cdefs:
                    loc = ", ".join(f"{c['file_path']}:{c['start_line']}"
                                    for c in cdefs[:2])
                    lines.append(f"  {cname} -> {loc}")
                    shown += 1

        callers = indexer.find_callers(root, defs[0]["name"])
        if callers:
            lines.append("Called by (callers):")
            for c in callers[:8]:
                lines.append(f"  {c['file_path']}:{c['start_line']} "
                             f"({c['kind']}) {c['name']}")
            if len(callers) > 8:
                lines.append(f"  +{len(callers) - 8} more callers")
        else:
            lines.append("Called by: (none found)")

        return "\n".join(lines[:20])
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] impact_graph failed: {exc!r}"


# --------------------------------------------------------------------------
# Tool 5: surgical_patch
# --------------------------------------------------------------------------

@mcp.tool()
def surgical_patch(file_path: str, symbol_name: str, new_code: str,
                   workspace_root: str = ".") -> str:
    """Replace ONE symbol's block in a file. The patched file is validated
    with Tree-sitter BEFORE writing: if the result has syntax errors the
    change is REJECTED and disk stays untouched. On success the file is
    updated and re-indexed. `symbol_name` may be `func` or `Class.method`."""
    try:
        root, abs_file = _resolve(workspace_root, file_path)
        lang = parser.lang_for_path(abs_file)
        if not lang:
            return f"Unsupported file type: {file_path}"

        source = _read_text(abs_file)
        # Locate the symbol node in a fresh parse.
        from tree_sitter_language_pack import get_parser
        tree = get_parser(lang).parse(source.encode("utf-8"))
        src_lines = source.splitlines()
        target = None
        src_bytes = source.encode("utf-8")

        specs = parser._SYMBOL_SPECS.get(lang, {})  # noqa: SLF001

        def walk(node, container):
            nonlocal target
            spec = specs.get(node.type)
            if spec is not None:
                kind, name_field, _ = spec
                name_node = node.child_by_field_name(name_field)
                if name_node is not None:
                    name = src_bytes[name_node.start_byte:name_node.end_byte].decode()
                    qualified = f"{container}.{name}" if container else name
                    if name == symbol_name or qualified == symbol_name:
                        target = node
                        return
                    new_container = name if kind == "class" else container
                    for child in node.named_children:
                        walk(child, new_container)
                    return
            is_container = node.type in parser._CONTAINER_TYPES.get(lang, set())  # noqa: SLF001
            cn = node.child_by_field_name("name")
            new_container = (src_bytes[cn.start_byte:cn.end_byte].decode()
                             if is_container and cn is not None else container)
            for child in node.named_children:
                walk(child, new_container)

        walk(tree.root_node, "")
        if target is None:
            return (f"Symbol '{symbol_name}' not found in {file_path}. "
                    "Use get_file_skeleton to see available names.")

        start_idx = target.start_point[0]
        end_idx = target.end_point[0]
        patched_lines = (src_lines[:start_idx]
                         + new_code.splitlines()
                         + src_lines[end_idx + 1:])
        patched = "\n".join(patched_lines) + ("\n" if source.endswith("\n") else "")

        ok, err = parser.validate_syntax(patched, lang)
        if not ok:
            return (f"[REJECTED - no changes written] Tree-sitter says:\n  {err}")

        # Forced UTF-8, newline="" keeps the original line endings untouched.
        abs_file.write_text(patched, encoding="utf-8", newline="")
        rel = _rel(root, abs_file)
        indexer.reindex_file(indexer.connect(root), root, rel)
        return (f"[PATCHED] {rel}:{start_idx + 1} <- symbol '{symbol_name}' "
                f"({len(new_code.splitlines())} new lines). Syntax OK, index updated.")
    except ValueError as exc:
        return f"[ERROR] {exc}"
    except OSError as exc:
        return f"[ERROR] cannot write {file_path}: {exc!r}"
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] surgical_patch failed: {exc!r}"


# --------------------------------------------------------------------------
# Tool 6: run_pruned_command
# --------------------------------------------------------------------------

@mcp.tool()
def run_pruned_command(command: str, workspace_root: str = ".",
                       timeout: int = 30) -> str:
    """Run a shell command (build/test/lint) in the workspace and get a
    PRUNED transcript: on success only the last 25 lines; on failure the
    first 10 lines + every error-pattern line (Traceback/FAILED/error[E..])
    + last 25 lines. Never floods your context with raw logs."""
    return runner.run_pruned(command, str(_abs(workspace_root)), timeout)


# --------------------------------------------------------------------------
# Tool 7: inspect_external_schema
# --------------------------------------------------------------------------

@mcp.tool()
def inspect_external_schema(service: str, target: str = "",
                            workspace_root: str = ".") -> str:
    """Distilled schemas of external infra, auto-detected from the workspace
    .env: supabase/postgres (DATABASE_URL) -> `table(col:type [PK/FK->...])`;
    odoo (ODOO_*) -> model fields; wordpress (WP_*) -> CPTs + ACF groups;
    typeform (TYPEFORM_TOKEN) -> hidden vars + field ids; n8n -> pass the
    exported workflow JSON path in `target`. No service -> lists which are
    active. Call with empty service to discover."""
    return connectors.inspect_external_schema(service, target,
                                              str(_abs(workspace_root)))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    """Run the Codeine MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
