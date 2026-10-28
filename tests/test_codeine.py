"""Codeine test suite - skeletons, BM25 search, impact, surgical patches,
log pruning and connector adapters (offline-safe)."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from codeine import connectors, indexer, parser, runner  # noqa: E402

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

SAMPLE_PY = '''"""Module docstring."""
import requests


def bar(x: int) -> int:
    """The bar helper."""
    return x * 2


class Greeter:
    """Greets people."""

    def greet(self, name: str) -> str:
        msg = f"hello {name}"
        return msg

    def run_all(self, n: int) -> int:
        total = bar(n)
        self.greet("world")
        requests.get("https://example.com")
        return total


def top_level_calls():
    return bar(3)
'''

SAMPLE_TS = '''import axios from "axios";

/** Fetches a user by id. */
export function fetchUser(id: number) {
  return axios.get("/users/" + id);
}

export interface User {
  id: number;
  name: string;
}

export class UserService {
  async load(id: number) {
    const u = await fetchUser(id);
    return u;
  }
}
'''

N8N_FIXTURE = {
    "name": "my-flow",
    "active": True,
    "nodes": [
        {"name": "Webhook", "type": "n8n-nodes-base.webhook",
         "position": [10, 20], "parameters": {"path": "in"}},
        {"name": "Code", "type": "n8n-nodes-base.code",
         "position": [30, 40], "parameters": {"functionName": "transform"}},
        {"name": "HTTP", "type": "n8n-nodes-base.httpRequest",
         "position": [50, 60], "parameters": {"url": "https://x.dev"}},
    ],
    "connections": {
        "Webhook": {"main": [[{"node": "Code", "type": "main", "index": 0}]]},
        "Code": {"main": [[{"node": "HTTP", "type": "main", "index": 0}]]},
    },
}


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "app.py").write_text(SAMPLE_PY, encoding="utf-8")
    (tmp_path / "svc.ts").write_text(SAMPLE_TS, encoding="utf-8")
    (tmp_path / "workflow.json").write_text(json.dumps(N8N_FIXTURE), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "SUPABASE_URL=https://demo.supabase.co\n"
        "TYPEFORM_TOKEN=abc123\n",
        encoding="utf-8",
    )
    return tmp_path


# --------------------------------------------------------------------------
# parser: symbols, skeleton, validation
# --------------------------------------------------------------------------

class TestParser:
    def test_extract_python_symbols(self):
        syms = parser.extract_symbols(SAMPLE_PY, "python")
        names = {s.name for s in syms}
        assert {"bar", "Greeter", "greet", "run_all", "top_level_calls"} <= names
        bar = next(s for s in syms if s.name == "bar")
        assert bar.kind == "function"
        assert "def bar(x: int) -> int:" in bar.signature
        assert "bar helper" in bar.docstring

    def test_extract_typescript_symbols(self):
        syms = parser.extract_symbols(SAMPLE_TS, "typescript")
        names = {s.name for s in syms}
        assert {"fetchUser", "User", "UserService", "load"} <= names
        fetch = next(s for s in syms if s.name == "fetchUser")
        assert "Fetches a user" in fetch.docstring
        assert any(c.startswith("axios.get") for c in fetch.calls)

    def test_calls_extracted(self):
        syms = parser.extract_symbols(SAMPLE_PY, "python")
        run_all = next(s for s in syms if s.name == "run_all")
        assert {"bar", "self.greet", "requests.get"} <= set(run_all.calls)

    def test_skeleton_collapses_bodies(self):
        skel = parser.generate_skeleton(SAMPLE_PY, "python")
        assert "[body:" in skel
        assert "def bar(x: int) -> int:" in skel          # signature kept
        assert "return x * 2" not in skel                  # body dropped
        assert "class Greeter:" in skel                    # class kept
        assert "def greet(self, name: str) -> str:" in skel  # nested sig kept

    def test_skeleton_preserves_nested_defs(self):
        skel = parser.generate_skeleton(SAMPLE_PY, "python")
        assert "def run_all(self, n: int) -> int:" in skel  # inner def survives

    def test_validate_syntax_ok(self):
        ok, err = parser.validate_syntax(SAMPLE_PY, "python")
        assert ok and err == ""

    def test_validate_syntax_error_reports_line(self):
        bad = "def f(:\n    pass\n"
        ok, err = parser.validate_syntax(bad, "python")
        assert not ok
        assert "line 1" in err

    def test_lang_for_path(self):
        assert parser.lang_for_path("a/b/c.tsx") == "tsx"
        assert parser.lang_for_path("style.css") == "css"
        assert parser.lang_for_path("img.png") == ""


# --------------------------------------------------------------------------
# indexer: incremental + BM25 + impact
# --------------------------------------------------------------------------

class TestIndexer:
    def test_ensure_index_creates_db(self, workspace):
        stats = indexer.ensure_index(str(workspace))
        assert stats["symbols"] >= 8
        assert os.path.isfile(indexer.db_path(str(workspace)))
        # self-ignoring cache
        assert (workspace / ".codeine" / ".gitignore").read_text().strip() == "*"

    def test_incremental_no_reparse_when_unchanged(self, workspace):
        root = str(workspace)
        first = indexer.ensure_index(root)
        assert first["indexed"] >= 2
        second = indexer.ensure_index(root)
        assert second["indexed"] == 0  # nothing changed
        (workspace / "app.py").write_text(SAMPLE_PY + "\n\n# touched\n",
                                          encoding="utf-8")
        third = indexer.ensure_index(root)
        assert third["indexed"] == 1   # only the modified file

    def test_bm25_search(self, workspace):
        root = str(workspace)
        indexer.ensure_index(root)
        rows = indexer.search_symbols(root, "bar helper")
        assert rows and rows[0]["name"] == "bar"
        rows_ts = indexer.search_symbols(root, "fetchUser")
        assert rows_ts and rows_ts[0]["file_path"] == "svc.ts"

    def test_find_callers(self, workspace):
        root = str(workspace)
        indexer.ensure_index(root)
        callers = indexer.find_callers(root, "bar")
        names = {c["name"] for c in callers}
        assert "run_all" in names and "top_level_calls" in names


# --------------------------------------------------------------------------
# runner: pruning
# --------------------------------------------------------------------------

class TestRunner:
    def test_success_returns_tail_and_ok(self, workspace):
        out = runner.run_pruned(
            f'{sys.executable} -c "print(\'A\'*5); print(\'done OK\')"',
            str(workspace))
        assert "[OK]" in out and "done OK" in out

    def test_failure_keeps_traceback_and_prunes(self, workspace):
        code = ("print('start'); print('x'*10); "
                "[print('noise%d' % i) for i in range(80)]; "
                "raise ValueError('boom')")
        out = runner.run_pruned(f'{sys.executable} -c "{code}"', str(workspace))
        assert "[FAILED" in out
        assert "Traceback" in out
        assert "ValueError" in out or "boom" in out
        assert "lines omitted" in out
        assert "noise10" not in out  # middle noise dropped

    def test_timeout(self, workspace):
        out = runner.run_pruned(
            f'{sys.executable} -c "import time; time.sleep(5)"',
            str(workspace), timeout=1)
        assert "[TIMEOUT]" in out


# --------------------------------------------------------------------------
# connectors: .env detection + n8n (offline)
# --------------------------------------------------------------------------

class TestConnectors:
    def test_detect_services_from_env(self, workspace):
        services = connectors.detect_services(str(workspace))
        assert services["supabase"] is True
        assert services["typeform"] is True
        assert services["odoo"] is False
        assert services["wordpress"] is False

    def test_n8n_parser_strips_coordinates(self, workspace):
        out = connectors.inspect_n8n(str(workspace / "workflow.json"))
        assert "Webhook" in out and "HTTP" in out
        assert "Webhook -> Code" in out and "Code -> HTTP" in out
        assert "10" not in out.split("--- connections ---")[0].split("nodes")[0]
        assert "position" not in out.lower()

    def test_unknown_service_message(self, workspace):
        out = connectors.inspect_external_schema("madeup", "", str(workspace))
        assert "Unknown service" in out

    def test_discovery_lists_active(self, workspace):
        out = connectors.inspect_external_schema("", "", str(workspace))
        assert "supabase" in out and "typeform" in out

    def test_missing_env_adapter_disabled(self, tmp_path):
        out = connectors.inspect_external_schema("odoo", "", str(tmp_path))
        assert "not present in .env" in out


# --------------------------------------------------------------------------
# integration: server-level flows (patch semantics)
# --------------------------------------------------------------------------

class TestSurgicalFlow:
    def test_patch_rejects_bad_syntax_and_keeps_disk(self, workspace):
        from codeine import server as srv
        root = str(workspace)
        before = (workspace / "app.py").read_text(encoding="utf-8")
        result = srv.surgical_patch("app.py", "bar", "def bar(:\n  pass",
                                    workspace_root=root)
        assert "[REJECTED" in result
        assert (workspace / "app.py").read_text(encoding="utf-8") == before

    def test_patch_accepts_valid_and_reindexes(self, workspace):
        from codeine import server as srv
        root = str(workspace)
        new_bar = ("def bar(x: int) -> int:\n"
                   '    """Doubled with a bonus."""\n'
                   "    return x * 2 + 1\n")
        result = srv.surgical_patch("app.py", "bar", new_bar, workspace_root=root)
        assert "[PATCHED]" in result
        disk = (workspace / "app.py").read_text(encoding="utf-8")
        assert "x * 2 + 1" in disk
        # index reflects the new docstring via BM25
        rows = indexer.search_symbols(root, "bonus")
        assert rows and rows[0]["name"] == "bar"

    def test_patch_class_method_qualified(self, workspace):
        from codeine import server as srv
        root = str(workspace)
        new_greet = ('    def greet(self, name: str) -> str:\n'
                     '        return "hi " + name\n')
        result = srv.surgical_patch("app.py", "Greeter.greet", new_greet,
                                    workspace_root=root)
        assert "[PATCHED]" in result
        assert 'hi " + name' in (workspace / "app.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# cross-OS robustness: pathlib, UTF-8, containment, DB isolation
# --------------------------------------------------------------------------

class TestCrossOS:
    def test_utf8_content_roundtrip(self, workspace):
        """Skeleton + patch on files with accents/emoji (cp1252 hosts)."""
        from codeine import server as srv
        (workspace / "uni.py").write_text(
            "def función_año():\n    return 'héllo 🚀'\n", encoding="utf-8")
        skel = srv.get_file_skeleton("uni.py", str(workspace))
        assert "función_año" in skel
        result = srv.surgical_patch(
            "uni.py", "función_año",
            "def función_año():\n    return 'patched ✅'\n",
            workspace_root=str(workspace))
        assert "[PATCHED]" in result
        assert "patched ✅" in (workspace / "uni.py").read_text(encoding="utf-8")

    def test_workspace_escape_rejected(self, workspace):
        """Paths trying to leave the workspace root are refused."""
        from codeine import server as srv
        out = srv.surgical_patch("../evil.py", "x", "def x(): pass",
                                 workspace_root=str(workspace))
        assert "[ERROR]" in out and "escapes" in out
        skel = srv.get_file_skeleton("../outside.py", workspace_root=str(workspace))
        assert "[ERROR]" in skel

    def test_db_isolation_and_self_gitignore(self, workspace):
        """index.db lives alone in .codeine/ with a '*' .gitignore."""
        from pathlib import Path
        root = str(workspace)
        indexer.ensure_index(root)
        db = workspace / ".codeine" / "index.db"
        assert db.is_file()
        gi = workspace / ".codeine" / ".gitignore"
        assert gi.is_file() and gi.read_text(encoding="utf-8").strip() == "*"
        siblings = [p.name for p in (workspace / ".codeine").iterdir()]
        assert set(siblings) <= {"index.db", "index.db-wal", "index.db-shm",
                                 ".gitignore"}
        # posix-style stored paths (portable index)
        assert str(indexer.db_path(root)).endswith(
            str(Path(".codeine") / "index.db"))

    def test_pathlib_only_no_os_path_join_left(self):
        """Source audit: no os.path.join / string path concatenation remain."""
        import re as _re
        from pathlib import Path
        src_dir = Path(__file__).resolve().parent.parent / "src" / "codeine"
        offenders = []
        for fname in ("indexer.py", "parser.py", "server.py", "connectors.py"):
            text = (src_dir / fname).read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "os.path.join" in line or _re.search(r'"\s*\+\s*"/"\s*\+', line):
                    offenders.append(f"{fname}:{i}: {line.strip()}")
        assert not offenders, f"path violations: {offenders}"
