"""Incremental SQLite + FTS5 (BM25) symbol index.

Cross-OS by construction: 100 % ``pathlib`` path handling (no string
concatenation), forced UTF-8, and a self-ignoring ``.codeine/`` database
directory created on demand at ``<workspace_root>/.codeine/index.db``.

Database layout:

    files(path PRIMARY KEY, mtime REAL, sha256 TEXT)
    symbols(id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, kind TEXT,
            signature TEXT, docstring TEXT, start_line INT, end_line INT,
            calls_raw TEXT)
    symbols_fts USING fts5(name, signature, docstring, file_path,
                           tokenize='porter unicode61')

Indexing is incremental: only files whose mtime AND sha256 changed are
re-parsed with Tree-sitter. Paths are stored POSIX-style so indexes are
portable across Windows / macOS / Linux.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .parser import extract_symbols, lang_for_path

IGNORED_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist",
    "build", "vendor", ".next", ".codeine", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "target", "bin", "obj",
}
MAX_FILE_BYTES = 2_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    path   TEXT PRIMARY KEY,
    mtime  REAL NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols(
    id         INTEGER PRIMARY KEY,
    file_path  TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    signature  TEXT NOT NULL DEFAULT '',
    docstring  TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    calls_raw  TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, signature, docstring, file_path,
    tokenize='porter unicode61'
);
"""


# --------------------------------------------------------------------------
# Connection / bootstrap
# --------------------------------------------------------------------------

def db_path(root: str | Path) -> Path:
    """Absolute-agnostic Path to the isolated index database."""
    return Path(root) / ".codeine" / "index.db"


def connect(root: str | Path) -> sqlite3.Connection:
    root = Path(root)
    codeine_dir = root / ".codeine"
    codeine_dir.mkdir(parents=True, exist_ok=True)
    _ensure_self_gitignore(codeine_dir)
    conn = sqlite3.connect(codeine_dir / "index.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _ensure_self_gitignore(codeine_dir: Path) -> None:
    """Make the .codeine cache self-ignoring for git workspaces."""
    gitignore = codeine_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Walking / incremental indexing (pure pathlib traversal)
# --------------------------------------------------------------------------

def _iter_source_files(root: Path) -> list[Path]:
    found: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue  # permission errors etc. - never crash the indexer
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in IGNORED_DIRS:
                        stack.append(entry)
                elif entry.is_file() and lang_for_path(entry):
                    if entry.stat().st_size <= MAX_FILE_BYTES:
                        found.append(entry)
            except OSError:
                continue
    return found


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reindex_file(conn: sqlite3.Connection, root: str | Path,
                 rel_path: str | Path) -> int:
    """Parse one file and replace its rows in files/symbols/symbols_fts."""
    root = Path(root)
    rel = Path(rel_path)
    rel_posix = rel.as_posix()
    abs_path = root / rel
    try:
        with abs_path.open("rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    lang = lang_for_path(rel)
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    old_ids = [row[0] for row in
               conn.execute("SELECT id FROM symbols WHERE file_path=?",
                            (rel_posix,))]
    if old_ids:
        conn.executemany("DELETE FROM symbols_fts WHERE rowid=?",
                         [(i,) for i in old_ids])
        conn.execute("DELETE FROM symbols WHERE file_path=?", (rel_posix,))

    count = 0
    for sym in extract_symbols(data.decode("utf-8", errors="replace"), lang):
        cur = conn.execute(
            "INSERT INTO symbols(file_path, name, kind, signature, docstring,"
            " start_line, end_line, calls_raw) VALUES(?,?,?,?,?,?,?,?)",
            (rel_posix, sym.name, sym.kind, sym.signature,
             sym.docstring, sym.start_line, sym.end_line, sym.calls_json()),
        )
        conn.execute(
            "INSERT INTO symbols_fts(rowid, name, signature, docstring, file_path)"
            " VALUES(?,?,?,?,?)",
            (cur.lastrowid, sym.name, sym.signature, sym.docstring, rel_posix),
        )
        count += 1

    conn.execute(
        "INSERT INTO files(path, mtime, sha256) VALUES(?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,"
        " sha256=excluded.sha256",
        (rel_posix, mtime, _sha256(data)),
    )
    conn.commit()
    return count


def ensure_index(root: str | Path) -> dict:
    """Bring the index up to date. Returns {'indexed': n, 'removed': m, 'symbols': s}."""
    root = Path(root).resolve()
    if not root.is_dir():
        return {"indexed": 0, "removed": 0, "symbols": 0,
                "error": f"workspace root not found: {root}"}
    conn = connect(root)
    try:
        on_disk = {p.relative_to(root).as_posix()
                   for p in _iter_source_files(root)}
        known: dict[str, tuple[float, str]] = {
            row[0]: (row[1], row[2])
            for row in conn.execute("SELECT path, mtime, sha256 FROM files")
        }

        indexed = removed = 0
        for rel_posix in sorted(on_disk):
            abs_path = root / rel_posix
            try:
                mtime = abs_path.stat().st_mtime
            except OSError:
                continue
            prev = known.get(rel_posix)
            if prev is not None and prev[0] == mtime:
                continue  # unchanged since last index
            if prev is not None:
                with abs_path.open("rb") as fh:
                    if _sha256(fh.read()) == prev[1]:
                        conn.execute("UPDATE files SET mtime=? WHERE path=?",
                                     (mtime, rel_posix))
                        conn.commit()
                        continue  # touched but identical content
            reindex_file(conn, root, rel_posix)
            indexed += 1

        for rel_posix in set(known) - on_disk:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM symbols WHERE file_path=?", (rel_posix,))]
            conn.executemany("DELETE FROM symbols_fts WHERE rowid=?",
                             [(i,) for i in ids])
            conn.execute("DELETE FROM symbols WHERE file_path=?", (rel_posix,))
            conn.execute("DELETE FROM files WHERE path=?", (rel_posix,))
            removed += 1
        if removed:
            conn.commit()

        (total,) = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()
        return {"indexed": indexed, "removed": removed, "symbols": total}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Queries used by the MCP tools
# --------------------------------------------------------------------------

def search_symbols(root: str | Path, query: str, limit: int = 10) -> list[dict]:
    """BM25 search over symbols_fts -> compact result rows."""
    conn = connect(root)
    try:
        safe = " ".join(f'"{tok}"' for tok in query.split() if tok.strip())
        if not safe:
            return []
        sql = (
            "SELECT s.file_path, s.name, s.start_line, s.end_line, s.kind,"
            "       s.signature, s.docstring, bm25(symbols_fts) AS rank"
            "  FROM symbols_fts f JOIN symbols s ON s.id = f.rowid"
            " WHERE symbols_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        rows = conn.execute(sql, (safe, max(1, min(limit, 50)))).fetchall()
        return [dict(zip(("file_path", "name", "start_line", "end_line", "kind",
                          "signature", "docstring", "rank"), r)) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def find_definitions(root: str | Path, symbol_name: str) -> list[dict]:
    """Exact-name matches, optionally qualified (Class.method handled by caller)."""
    conn = connect(root)
    try:
        rows = conn.execute(
            "SELECT file_path, name, kind, signature, docstring,"
            " start_line, end_line, calls_raw FROM symbols WHERE name = ?"
            " ORDER BY file_path, start_line",
            (symbol_name,),
        ).fetchall()
        return [dict(zip(("file_path", "name", "kind", "signature", "docstring",
                          "start_line", "end_line", "calls_raw"), r)) for r in rows]
    finally:
        conn.close()


def find_callers(root: str | Path, symbol_name: str) -> list[dict]:
    """Symbols whose parsed call list contains *symbol_name* exactly."""
    conn = connect(root)
    try:
        like = f'%"{symbol_name}"%'
        rows = conn.execute(
            "SELECT file_path, name, kind, start_line, end_line, calls_raw"
            " FROM symbols WHERE calls_raw LIKE ?"
            " ORDER BY file_path, start_line",
            (like,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                calls = json.loads(r[5])
            except json.JSONDecodeError:
                continue
            if symbol_name in calls:
                out.append(dict(zip(("file_path", "name", "kind", "start_line",
                                     "end_line"), r[:5])))
        return out
    finally:
        conn.close()
