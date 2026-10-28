"""Distilled inspectors for external infrastructure (.env aware).

Each adapter is enabled lazily: the workspace ``.env`` is parsed on demand and
only services whose credentials are present are callable. Every method returns
ultra-compact plain text — never raw JSON dumps.

Supported:
    supabase / postgres  (DATABASE_URL or SUPABASE_URL) -> distilled catalog
    odoo                 (ODOO_URL/DB/USER/PASS, xmlrpc) -> models / fields
    wordpress            (WP_URL + app-password REST)    -> CPTs + ACF fields
    typeform             (TYPEFORM_TOKEN)                -> hidden vars + fields
    n8n                  (path to exported workflow JSON on disk)
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
import xmlrpc.client
from pathlib import Path

_HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------
# .env parsing / service detection
# --------------------------------------------------------------------------

def parse_env(workspace_root: str | Path) -> dict[str, str]:
    env_path = Path(workspace_root) / ".env"
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    try:
        with env_path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip("'\"")
                if key.strip():
                    values[key.strip()] = val
    except OSError:
        pass
    return values


def detect_services(workspace_root: str) -> dict[str, bool]:
    env = parse_env(workspace_root)
    return {
        "supabase": bool(env.get("DATABASE_URL") or env.get("SUPABASE_URL")),
        "odoo": bool(env.get("ODOO_URL")),
        "wordpress": bool(env.get("WP_URL")),
        "typeform": bool(env.get("TYPEFORM_TOKEN")),
        "n8n": True,  # file-based, no .env needed
    }


# --------------------------------------------------------------------------
# HTTP helper (stdlib only)
# --------------------------------------------------------------------------

def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "codeine-mcp/1.0",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
            return True, resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, repr(exc)


def _http_json(url: str, headers: dict[str, str] | None = None):
    ok, body = _http_get(url, headers)
    if not ok:
        return None, body
    try:
        return json.loads(body), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON from {url}: {exc}"


# --------------------------------------------------------------------------
# Supabase / PostgreSQL
# --------------------------------------------------------------------------

def _normalize_postgres_url(url: str) -> str:
    # Support postgres:// scheme and Supabase pooler hosts.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def inspect_postgres(workspace_root: str, table_filter: str = "") -> str:
    env = parse_env(workspace_root)
    url = env.get("DATABASE_URL") or env.get("SUPABASE_URL") or ""
    if not url:
        return "DATABASE_URL / SUPABASE_URL not present in .env - adapter disabled."

    try:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(_normalize_postgres_url(url))
        host, port = parsed.hostname, parsed.port or 5432
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        dbname = parsed.path.lstrip("/") or "postgres"
    except ValueError as exc:
        return f"Could not parse connection URL: {exc!r}"

    conn = None
    driver = ""
    try:
        try:
            import psycopg  # type: ignore[import-not-found]
            conn = psycopg.connect(host=host, port=port, user=user,
                                   password=password, dbname=dbname, connect_timeout=10)
            driver = "psycopg"
        except ImportError:
            import pg8000  # type: ignore[import-not-found]
            conn = pg8000.native.Connection(user, password=password, host=host,
                                            port=port, database=dbname,
                                            timeout=10)
            driver = "pg8000"
    except ImportError:
        return ("No PostgreSQL driver installed. Run:"
                "\n  pip install 'codeine-mcp[postgres]'  (or pip install pg8000)"
                "\nthen retry.")
    except Exception as exc:  # noqa: BLE001 - connection errors
        return f"[ERROR] could not connect ({driver}): {exc!r}"

    try:
        cur = conn.cursor()
        like = f"%{table_filter}%" if table_filter else "%"
        cur.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name LIKE %s"
            " ORDER BY table_name", (like,))
        tables = [r[0] for r in cur.fetchall()]
        if not tables:
            return f"No public tables matching {table_filter!r}."

        cur.execute(
            "SELECT tc.table_name, kcu.column_name, tc.constraint_type,"
            "       ccu.table_name AS fk_table, ccu.column_name AS fk_col"
            "  FROM information_schema.table_constraints tc"
            "  JOIN information_schema.key_column_usage kcu"
            "    ON tc.constraint_name = kcu.constraint_name"
            "  JOIN information_schema.constraint_column_usage ccu"
            "    ON ccu.constraint_name = tc.constraint_name"
            " WHERE tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY')"
            "   AND tc.table_schema = 'public'")
        marks: dict[tuple[str, str], list[str]] = {}
        for tname, col, ctype, fk_t, fk_c in cur.fetchall():
            if ctype == "PRIMARY KEY":
                marks.setdefault((tname, col), []).append("PK")
            else:
                marks.setdefault((tname, col), []).append(f"FK->{fk_t}.{fk_c}")

        out = [f"# Postgres schema ({dbname} @ {host}) - {len(tables)} tables"]
        for tname in tables[:40]:
            cur.execute(
                "SELECT column_name, data_type, character_maximum_length"
                "  FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name=%s"
                " ORDER BY ordinal_position", (tname,))
            cols = []
            for cname, dtype, maxlen in cur.fetchall():
                tags = marks.get((tname, cname))
                tag = f" [{' '.join(tags)}]" if tags else ""
                length = f"({maxlen})" if maxlen else ""
                cols.append(f"{cname}:{dtype}{length}{tag}")
            out.append(f"{tname}(" + ", ".join(cols) + ")")
        if len(tables) > 40:
            out.append(f"[... {len(tables) - 40} more tables omitted ...]")
        return "\n".join(out)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Odoo (xmlrpc, stdlib)
# --------------------------------------------------------------------------

def _odoo_common(url: str):
    return xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")


def _odoo_models(url: str):
    return xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/object")


def inspect_odoo(workspace_root: str, model_filter: str = "") -> str:
    env = parse_env(workspace_root)
    url = env.get("ODOO_URL", "")
    db = env.get("ODOO_DB", "")
    user = env.get("ODOO_USER", "")
    password = env.get("ODOO_PASS", env.get("ODOO_API_KEY", ""))
    if not url:
        return "ODOO_URL not present in .env - adapter disabled."
    if not (db and user and password):
        return "Odoo needs ODOO_DB, ODOO_USER and ODOO_PASS in .env."

    try:
        uid = _odoo_common(url).authenticate(db, user, password, {})
        if not uid:
            return "[ERROR] Odoo authentication failed (check credentials)."
        models = _odoo_models(url)
        domain = [("transient", "=", False)]
        if model_filter:
            domain = ["|", ("model", "like", model_filter),
                      ("name", "like", model_filter)] + domain
        rows = models.execute_kw(db, uid, password, "ir.model", "search_read",
                                 [domain], {"fields": ["model"], "limit": 60})
        out = [f"# Odoo models @ {url} - {len(rows)} shown"]
        for row in rows:
            if model_filter:
                fields = models.execute_kw(
                    db, uid, password, "ir.model.fields", "search_read",
                    [[("model", "=", row["model"])]],
                    {"fields": ["name", "ttype"], "limit": 25})
                flds = ", ".join(f"{f['name']} ({f['ttype']})" for f in fields)
                out.append(f"{row['model']} -> {flds}")
            else:
                out.append(row["model"])
        if not rows:
            out.append(f"No models matching {model_filter!r}.")
        return "\n".join(out)
    except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError, OSError) as exc:
        return f"[ERROR] Odoo xmlrpc failure: {exc!r}"


# --------------------------------------------------------------------------
# WordPress / ACF
# --------------------------------------------------------------------------

def _wp_auth(env: dict[str, str]) -> dict[str, str]:
    user = env.get("WP_USER", "")
    token = env.get("WP_APP_PASSWORD", env.get("WP_PASSWORD", ""))
    basic = base64.b64encode(f"{user}:{token}".encode()).decode()
    return {"Authorization": f"Basic {basic}"}


def inspect_wordpress(workspace_root: str, target: str = "") -> str:
    env = parse_env(workspace_root)
    base = env.get("WP_URL", "").rstrip("/")
    if not base:
        return "WP_URL not present in .env - adapter disabled."
    if not (env.get("WP_USER") and
            (env.get("WP_APP_PASSWORD") or env.get("WP_PASSWORD"))):
        return "WordPress needs WP_USER + WP_APP_PASSWORD (application password)."

    types, err = _http_json(f"{base}/wp-json/wp/v2/types", _wp_auth(env))
    if types is None:
        return f"[ERROR] WP REST unreachable: {err}"
    cpts = [name for name in types.get("rest_base", {}).keys()
            if name not in ("posts", "pages", "attachments", "types",
                            "categories", "tags", "comments", "search")]

    out = [f"# WordPress @ {base}", f"CPTs (rest_base): {', '.join(cpts) or '(none)'}"]

    # ACF field groups via the acf/v3 route (ACF Pro).
    groups, err = _http_json(f"{base}/wp-json/acf/v3/field-groups", _wp_auth(env))
    if isinstance(groups, list):
        for group in groups[:20]:
            title = group.get("post", {}).get("title") or group.get("title") or "?"
            fields = (group.get("acf", {}).get("fields")
                      or group.get("fields") or [])
            names = ", ".join(
                f"{f.get('name') or f.get('_name')}({f.get('type')})"
                for f in fields if isinstance(f, dict)) or "(no fields)"
            out.append(f"acf_group[{title}]: {names}")
    else:
        out.append("acf/v3 not available (is ACF Pro + REST enabled?)")

    if target:
        posts, err = _http_json(
            f"{base}/wp-json/wp/v2/{target}?per_page=1", _wp_auth(env))
        if isinstance(posts, list) and posts:
            acf = posts[0].get("acf", {})
            keys = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in list(acf.items())[:15])
            out.append(f"sample[{target}].acf: {keys or '(empty)'}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Typeform
# --------------------------------------------------------------------------

def inspect_typeform(workspace_root: str, form_id: str = "") -> str:
    env = parse_env(workspace_root)
    token = env.get("TYPEFORM_TOKEN", "")
    if not token:
        return "TYPEFORM_TOKEN not present in .env - adapter disabled."
    if not form_id:
        return "Provide the form ID as 'target' (from the form URL)."

    form, err = _http_json(
        f"https://api.typeform.com/forms/{form_id}",
        {"Authorization": f"Bearer {token}"})
    if form is None:
        return f"[ERROR] Typeform API failure: {err}"

    out = [f"# Typeform {form_id}: {form.get('title', '')}"]
    hidden = form.get("hidden") or []
    out.append(f"hidden: {', '.join(hidden) if hidden else '(none)'}")
    for field in form.get("fields", [])[:40]:
        fid = field.get("id") or field.get("ref")
        ftype = field.get("type", "?")
        title = " ".join(str(field.get("title", "")).split())[:50]
        props = field.get("properties", {})
        choices = ""
        if props.get("choices"):
            labels = [c.get("label") for c in props["choices"]][:6]
            choices = " choices=[" + ", ".join(str(x) for x in labels) + "]"
        out.append(f"  {fid} ({ftype}): {title}{choices}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# n8n workflow parser (local export file)
# --------------------------------------------------------------------------

def inspect_n8n(workflow_path: str | Path) -> str:
    path = Path(workflow_path)
    if not path.is_file():
        return f"File not found: {workflow_path}"
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            wf = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return f"[ERROR] cannot read workflow: {exc!r}"

    nodes = {n["name"]: n for n in wf.get("nodes", []) if isinstance(n, dict)}
    out = [f"# n8n workflow: {wf.get('name', '(unnamed)')} "
           f"({len(nodes)} nodes, {wf.get('active', False) and 'ACTIVE' or 'inactive'})"]
    for name, node in nodes.items():
        ntype = (node.get("type") or "?").replace("n8n-nodes-base.", "")
        params = node.get("parameters", {})
        hint = ""
        for key in ("functionName", "path", "httpRequestMethod", "url",
                    "rule", "mode"):
            if key in params:
                hint = f" {key}={str(params[key])[:40]!r}"
                break
        disabled = " [DISABLED]" if node.get("disabled") else ""
        out.append(f"- {name} ({ntype}){disabled}{hint}")

    out.append("--- connections ---")
    edges: list[str] = []
    for src, conns in (wf.get("connections") or {}).items():
        for main in conns.get("main", []) if isinstance(conns.get("main"), list) else []:
            for link in main or []:
                if isinstance(link, dict) and link.get("node"):
                    edges.append(f"{src} -> {link['node']}")
    if edges:
        out.extend(edges[:60])
        if len(edges) > 60:
            out.append(f"[... {len(edges) - 60} more connections omitted ...]")
    else:
        out.append("(no connections)")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

def inspect_external_schema(service: str, target: str = "",
                            workspace_root: str = ".") -> str:
    service = (service or "").lower().strip()
    if not service:
        active = [k for k, v in detect_services(workspace_root).items()
                  if k != "n8n" and v]
        return ("External inspectors (from workspace .env): "
                + (", ".join(active) if active else "(none configured)")
                + "\nUsage: inspect_external_schema(service='supabase|odoo|"
                  "wordpress|typeform|n8n', target='<table|model|cpt|form_id|"
                  "path-to-json>')")
    if service in ("supabase", "postgres", "postgresql", "pg"):
        return inspect_postgres(workspace_root, target)
    if service == "odoo":
        return inspect_odoo(workspace_root, target)
    if service in ("wordpress", "wp"):
        return inspect_wordpress(workspace_root, target)
    if service == "typeform":
        return inspect_typeform(workspace_root, target)
    if service == "n8n":
        return inspect_n8n(Path(target) if target
                           else Path(workspace_root) / "workflow.json")
    return f"Unknown service '{service}'. Valid: supabase, odoo, wordpress, typeform, n8n."
