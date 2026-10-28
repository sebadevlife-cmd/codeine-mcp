"""Codeine - Universal MCP server for LLM context offloading.

Submodules:
    parser     - Tree-sitter AST extraction, skeletons and syntax validation
    indexer    - Incremental SQLite + FTS5 (BM25) symbol index
    runner     - Smart command runner with log pruning
    connectors - Distilled inspectors for external infrastructure (.env aware)
    server     - FastMCP tool surface (stdio transport)
"""

__version__ = "1.0.0"
