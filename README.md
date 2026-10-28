# 🧬 Codeine — Universal MCP Server for LLM Context Offloading

[![CI](https://github.com/soporte2-iRG/codeine-mcp/actions/workflows/ci.yml/badge.svg)](../../actions)
[![Tests](https://img.shields.io/badge/tests-27%2F27-brightgreen)]()
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-green)]()
[![License](https://img.shields.io/badge/license-MIT-blue)]()

**Codeine** (`codeine-mcp`) lets any LLM — local via Ollama or in the cloud
(Claude, GPT) — explore, understand and edit codebases, inspect databases and
analyze APIs while consuming **~90 % fewer tokens**. It replaces "read the
whole file" workflows with AST-aware surgical operations.

## How it saves tokens

| Instead of… | Codeine gives you |
|---|---|
| `Read` a 600-line file | `get_file_skeleton` → signatures + `[body: lines X-Y]` markers |
| Grepping around blindly | `search_symbols` → BM25 over name/signature/docstring |
| Reading whole files to find one function | `read_symbol` → only `L10-L45`, line-numbered |
| Refactoring on faith | `impact_graph` → callees + callers in ≤20 lines |
| `Edit` + pray | `surgical_patch` → Tree-sitter-validated, rejects bad syntax **before** writing |
| Pasting raw build logs | `run_pruned_command` → head + error lines + tail, omission markers |
| Dumping huge API JSONs | `inspect_external_schema` → distilled schemas from `.env`-detected services |

## Cross-platform (Windows / macOS / Linux) — zero local compilation

Codeine installs out-of-the-box on all three OSes with **no compilers
needed** (no gcc, no MSVC):

| Guarantee | How |
|---|---|
| No string-built paths | 100 % `pathlib.Path` in `indexer.py` / `parser.py` / `server.py` / `connectors.py` — backslashes on Windows, slashes on macOS/Linux handled natively |
| No local compilation | `tree-sitter` and `tree-sitter-language-pack` ship **prebuilt binary wheels** for Windows x64, macOS (Apple Silicon + Intel) and Linux (x86_64 + aarch64) on PyPI; the C parsers load directly from the wheel |
| No encoding surprises | Every file read/write forces `encoding="utf-8"` (with `errors="replace"` on reads) — safe on Windows machines whose default codec is cp1252 |
| Index isolation | `<workspace_root>/.codeine/` is created on demand, contains only `index.db` + a self-ignoring `.gitignore` (`*`); paths inside the DB are stored POSIX-style so the index is portable across OSes |
| Workspace containment | Tool paths that try to escape the workspace root (`../`, absolute paths outside) are rejected before any disk access |

A compiler is only ever required on exotic platforms/interpreters where pip
cannot find a prebuilt wheel — the three OSes above always have them.

## Install

```bash
pip install -e ~/codeine-mcp        # or: pip install codeine-mcp
```

Requirements: Python 3.11+, `mcp` (v1.x, FastMCP), `tree-sitter`,
`tree-sitter-language-pack`. Optional: `pip install 'codeine-mcp[postgres]'`
for Supabase/PostgreSQL inspection.

## Connect to Claude Code

```bash
claude mcp add codeine -- codeine-mcp
```

## Connect to Hermes Agent (`~/.hermes/config.yaml`)

```yaml
mcp_servers:
  codeine:
    command: codeine-mcp
    args: []
```

## Connect to any MCP client (stdio)

The server speaks standard MCP over stdio:

```json
{ "command": "codeine-mcp", "args": [] }
```

## Golden rule (put in CLAUDE.md or your agent's system prompt)

> **STRICT CONTEXT RULE:** Never Read/cat whole code files. ALWAYS use
> codeine's `search_symbols` or `get_file_skeleton` first, then
> `read_symbol` / `surgical_patch` for surgical interaction.

Variant in Spanish, for Spanish-prompted agents:

> **REGLA DE CONTEXTO ESTRICTA:** Está prohibido usar Read o cat sobre
> archivos completos de código. Usa SIEMPRE `search_symbols` o
> `get_file_skeleton` de codeine primero, y `read_symbol` o `surgical_patch`
> para interactuar quirúrgicamente con el código.

## The 7 tools

| Tool | What it does |
|---|---|
| `get_file_skeleton(file_path, workspace_root)` | Structural skeleton: imports/globals/signatures kept, bodies collapsed to `# ... [body: lines X-Y]` |
| `read_symbol(symbol_name, file_path?, workspace_root)` | Only the symbol's `start_line..end_line`, `L<n>:` prefixed |
| `search_symbols(query, limit, workspace_root)` | BM25 (FTS5, porter) over symbols → `[file:L10-L45] (kind) signature — doc` |
| `impact_graph(symbol_name, workspace_root)` | Defined-at / callees / callers, ≤20 lines |
| `surgical_patch(file_path, symbol_name, new_code, workspace_root)` | Replaces one symbol block; **validates syntax first, refuses to write on error**; re-indexes on success. Accepts `func` or `Class.method` |
| `run_pruned_command(command, workspace_root, timeout)` | Shell with smart pruning: success → last 25 lines; failure → head 10 + error patterns + tail 25 |
| `inspect_external_schema(service, target, workspace_root)` | `.env`-aware distilled schemas (below) |

### Supported languages

Python · TypeScript · TSX · JavaScript/JSX · PHP · Rust · Go · HTML · CSS

### External inspectors (auto-detected from workspace `.env`)

| Service | `.env` keys | Output |
|---|---|---|
| Supabase / Postgres | `DATABASE_URL` or `SUPABASE_URL` | `table(col:type, col2 [PK], col3 [FK->other.col])` |
| Odoo | `ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS` | `model -> field (type), …` via xmlrpc |
| WordPress / ACF | `WP_URL, WP_USER, WP_APP_PASSWORD` | CPTs + ACF field groups, no HTML junk |
| Typeform | `TYPEFORM_TOKEN` | hidden vars + field ids/choices |
| n8n | pass exported JSON path in `target` | logical node sequence + connections (no X/Y coordinates) |

Adapters stay disabled (and out of the prompt) until their keys exist in
`.env`. Call with `service=""` to list what's active.

## Architecture

```
codeine-mcp (stdio)
 ├─ parser.py     tree-sitter AST: symbols, signatures, calls, skeletons,
 │                syntax validation (ERROR/MISSING descent)
 ├─ indexer.py    <root>/.codeine/index.db — SQLite + FTS5 (porter unicode61)
 │                incremental: mtime + sha256, only re-parses changed files
 ├─ runner.py     subprocess + heuristic log pruning
 ├─ connectors.py .env parsing + distilled external inspectors
 └─ server.py     FastMCP tool surface (7 tools, compact text responses)
```

The index self-hides: `.codeine/.gitignore` (containing `*`) is created
automatically. Garbage dirs (`.git node_modules venv .venv __pycache__ dist
build vendor .next …`) are skipped.

## Development

```bash
git clone <repo> && cd codeine-mcp
pip install -e '.[dev,postgres]'
pytest            # 27 tests: skeletons, BM25, impact, patch rejection,
                  # pruning, connectors, cross-OS guarantees — all offline
```

CI runs the full suite on an **ubuntu / macos / windows × Python 3.11/3.12/3.13
matrix**, plus a live stdio handshake against the server — proving the
zero-compilation, cross-platform claim on every push.

## License

MIT
