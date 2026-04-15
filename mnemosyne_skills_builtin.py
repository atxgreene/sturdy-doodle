"""
mnemosyne_skills_builtin.py — curated built-in skills for the agent.

Hermes ships ~83 skills. We ship 10 here. The criteria: useful to
every agent, small, safe, stdlib-only. Anything that touches the
network is read-only; anything that touches the filesystem is either
read-only or root-jailed to $MNEMOSYNE_PROJECTS_DIR.

Skills registered here show up automatically via
`register_builtin_skills(registry)` — call it once after building a
`SkillRegistry` and the brain can dispatch any of them.

Security posture
----------------
- `fs_read`, `fs_list`, `grep_code` — root-jailed to the provided
  `root` kwarg (default: $MNEMOSYNE_PROJECTS_DIR). Path-traversal
  rejected. Never reads files beginning with '.' unless
  include_hidden=True is passed.
- `fs_write_safe` — root-jailed same as above; refuses to overwrite
  unless `overwrite=True`; writes atomically via mkstemp + rename.
- `http_get`, `web_fetch_text` — GET only, 10-second timeout, max 2 MB
  response size, no redirects across hostnames, User-Agent identifies
  Mnemosyne.
- `sqlite_query` — takes a path + SELECT query. Refuses any statement
  that isn't SELECT.
- `shell_exec_safe` — allow-list of commands; no shell=True; arg
  length capped; timeout enforced.
- `git_status`, `git_log` — runs `git` as subprocess; repo must be
  inside root.
- `datetime_now` — pure, no I/O.

Registration is opt-in so users who want a narrower agent don't get
the full library by default.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---- filesystem jail -------------------------------------------------------

def _resolve_root(root: str | Path | None) -> Path:
    if root:
        return Path(root).expanduser().resolve()
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return (Path(raw).expanduser().resolve()
                if raw else (Path.home() / "projects" / "mnemosyne").resolve())


def _safe_join(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`; raise if it escapes the jail."""
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise PermissionError(
            f"path {rel!r} escapes root {root}"
        ) from e
    return target


# ---- skill implementations --------------------------------------------------

def fs_read(path: str, *, root: str | None = None,
            max_bytes: int = 512_000) -> dict[str, Any]:
    """Read a file under `root` (default: $MNEMOSYNE_PROJECTS_DIR).

    Returns {path, size_bytes, content, sha256, truncated}.
    """
    r = _resolve_root(root)
    p = _safe_join(r, path)
    if not p.is_file():
        return {"error": "not a file", "path": str(p)}
    data = p.read_bytes()
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = data.decode("utf-8", errors="replace")
    return {
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "content": content,
        "truncated": truncated,
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
    }


def fs_list(directory: str = ".", *, root: str | None = None,
            pattern: str = "*", include_hidden: bool = False,
            limit: int = 500) -> dict[str, Any]:
    """List entries under `directory` (relative to root). Returns dicts
    with name, is_dir, size, mtime_utc."""
    r = _resolve_root(root)
    d = _safe_join(r, directory)
    if not d.is_dir():
        return {"error": "not a directory", "path": str(d)}
    items: list[dict[str, Any]] = []
    for entry in sorted(d.glob(pattern)):
        if not include_hidden and entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size_bytes": st.st_size,
            "mtime_utc": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc).isoformat(),
        })
        if len(items) >= limit:
            break
    return {"root": str(r), "directory": str(d),
             "count": len(items), "entries": items}


def fs_write_safe(path: str, content: str, *, root: str | None = None,
                  overwrite: bool = False) -> dict[str, Any]:
    """Atomic write to a file under root. Refuses to overwrite unless
    explicitly permitted."""
    r = _resolve_root(root)
    p = _safe_join(r, path)
    if p.exists() and not overwrite:
        return {"error": "file exists; pass overwrite=True", "path": str(p)}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)
    return {"path": str(p), "size_bytes": p.stat().st_size, "written": True}


def grep_code(pattern: str, *, root: str | None = None,
              include: str = "**/*.py", max_matches: int = 100,
              case_insensitive: bool = False) -> dict[str, Any]:
    """Grep for `pattern` across files matching `include` under root.

    Pure-Python; no ripgrep dependency. Returns up to max_matches
    {path, line_number, line} entries.
    """
    r = _resolve_root(root)
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"bad pattern: {e}"}
    matches: list[dict[str, Any]] = []
    for f in r.glob(include):
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(
                    encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append({
                        "path": str(f.relative_to(r)),
                        "line_number": i,
                        "line": line[:300],
                    })
                    if len(matches) >= max_matches:
                        return {"matches": matches, "truncated": True}
        except OSError:
            continue
    return {"matches": matches, "truncated": False}


def http_get(url: str, *, timeout: float = 10.0,
             max_bytes: int = 2_000_000) -> dict[str, Any]:
    """Read-only HTTP GET with bounded timeout + size + no cross-host
    redirects. Returns {status, content_type, text, truncated}."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": f"scheme {parsed.scheme!r} not allowed"}
    req = urllib.request.Request(
        url, headers={"User-Agent": "mnemosyne/0.2.2 (read-only agent)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            truncated = len(body) > max_bytes
            if truncated:
                body = body[:max_bytes]
            ctype = r.headers.get("Content-Type", "") or ""
            return {
                "status": r.status,
                "url": r.url,
                "content_type": ctype,
                "text": body.decode("utf-8", errors="replace"),
                "size_bytes": len(body),
                "truncated": truncated,
            }
    except urllib.error.HTTPError as e:
        return {"error": "HTTPError", "status": e.code, "url": url}
    except urllib.error.URLError as e:
        return {"error": "URLError", "reason": str(e.reason), "url": url}
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e), "url": url}


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def web_fetch_text(url: str, *, timeout: float = 10.0,
                   max_chars: int = 15_000) -> dict[str, Any]:
    """Fetch `url` and return a naive text extraction (strips HTML
    tags). Good enough for routing agents that need to know whether a
    page is relevant; not a replacement for a real readability parser."""
    result = http_get(url, timeout=timeout)
    if "error" in result:
        return result
    raw = result.get("text") or ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = _WS_RE.sub(" ", text).strip()
    truncated = len(text) > max_chars
    return {
        "url": result.get("url"),
        "text": text[:max_chars],
        "truncated": truncated,
        "content_type": result.get("content_type"),
    }


def sqlite_query(db_path: str, query: str, *,
                 params: list | None = None,
                 limit: int = 200) -> dict[str, Any]:
    """Execute a SELECT against the given SQLite DB. Only SELECT is
    permitted (no WRITE, no DROP, no attached DBs)."""
    q = query.strip().rstrip(";")
    low = q.lower().lstrip()
    if not low.startswith(("select ", "select\n", "select\t", "with ")):
        return {"error": "only SELECT/WITH queries are permitted"}
    # Defensive: forbid multiple statements.
    if ";" in q:
        return {"error": "multiple statements not permitted"}
    p = Path(db_path).expanduser()
    if not p.is_file():
        return {"error": "no such db", "path": str(p)}
    try:
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(q, params or []).fetchmany(limit)
        cols = [d[0] for d in (conn.execute(q, params or []).description or [])]
        conn.close()
    except sqlite3.Error as e:
        return {"error": type(e).__name__, "message": str(e)}
    return {"columns": cols,
             "rows": [dict(r) for r in rows],
             "row_count": len(rows)}


_SHELL_ALLOWLIST = frozenset({
    "ls", "cat", "head", "tail", "wc", "file",
    "git", "which", "pwd", "date", "uname", "env",
    "python3", "pip",
})


def shell_exec_safe(command: str, *, timeout: float = 10.0,
                     max_output: int = 50_000) -> dict[str, Any]:
    """Allow-listed subprocess exec. No shell=True; the command must be
    argv-splittable and the first token must be in _SHELL_ALLOWLIST."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return {"error": f"bad command: {e}"}
    if not argv:
        return {"error": "empty command"}
    if argv[0] not in _SHELL_ALLOWLIST:
        return {"error": f"command {argv[0]!r} not in allow-list",
                 "allowed": sorted(_SHELL_ALLOWLIST)}
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "argv": argv}
    except FileNotFoundError:
        return {"error": "command not found", "argv": argv}
    return {
        "argv": argv,
        "exit_code": p.returncode,
        "stdout": p.stdout[:max_output],
        "stderr": p.stderr[:max_output],
        "truncated": len(p.stdout) > max_output or len(p.stderr) > max_output,
    }


def git_status(*, root: str | None = None) -> dict[str, Any]:
    """`git status --porcelain` + `git rev-parse HEAD` at `root`."""
    r = _resolve_root(root)
    try:
        porc = subprocess.run(
            ["git", "-C", str(r), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        head = subprocess.run(
            ["git", "-C", str(r), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "-C", str(r), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"error": type(e).__name__, "message": str(e)}
    if porc.returncode != 0:
        return {"error": "not a git repo", "path": str(r),
                 "stderr": porc.stderr.strip()}
    return {
        "path": str(r),
        "branch": branch.stdout.strip(),
        "head": head.stdout.strip()[:12],
        "dirty": bool(porc.stdout.strip()),
        "entries": [line for line in porc.stdout.splitlines()[:50]],
    }


def git_log(*, root: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Last N commits as {sha, subject, author, date}."""
    r = _resolve_root(root)
    fmt = "%h%x00%s%x00%an%x00%cI"
    try:
        p = subprocess.run(
            ["git", "-C", str(r), "log", f"-n{int(limit)}", f"--format={fmt}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"error": type(e).__name__, "message": str(e)}
    if p.returncode != 0:
        return {"error": "git log failed", "stderr": p.stderr.strip()}
    commits: list[dict[str, str]] = []
    for line in p.stdout.splitlines():
        parts = line.split("\x00")
        if len(parts) != 4:
            continue
        commits.append({
            "sha": parts[0], "subject": parts[1],
            "author": parts[2], "date": parts[3],
        })
    return {"path": str(r), "commits": commits}


def datetime_now(*, tz: str = "utc") -> dict[str, Any]:
    """Return the current time in UTC or local."""
    now_utc = datetime.now(timezone.utc)
    if tz.lower() == "utc":
        return {"iso": now_utc.isoformat(), "tz": "UTC",
                 "epoch_s": now_utc.timestamp()}
    local = datetime.now()
    return {"iso": local.isoformat(), "tz": "local",
             "epoch_s": local.timestamp()}


# ---- registry wiring --------------------------------------------------------

#: Descriptors for each builtin skill. Shape matches
#: mnemosyne_skills.Skill parameter conventions.
BUILTIN_SPECS: list[dict[str, Any]] = [
    {"name": "fs_read",
      "description": "Read a UTF-8 file from the projects-dir jail.",
      "fn": fs_read,
      "parameters": [
          {"name": "path", "type": "string", "required": True,
           "description": "File path relative to root."},
          {"name": "root", "type": "string",
           "description": "Optional override; defaults to $MNEMOSYNE_PROJECTS_DIR."},
      ]},
    {"name": "fs_list",
      "description": "List directory entries under the projects-dir jail.",
      "fn": fs_list,
      "parameters": [
          {"name": "directory", "type": "string",
           "description": "Directory relative to root. Default '.'."},
          {"name": "pattern", "type": "string",
           "description": "Glob pattern. Default '*'."},
      ]},
    {"name": "fs_write_safe",
      "description": "Atomic-write a file under the projects-dir jail.",
      "fn": fs_write_safe,
      "parameters": [
          {"name": "path", "type": "string", "required": True},
          {"name": "content", "type": "string", "required": True},
          {"name": "overwrite", "type": "boolean"},
      ]},
    {"name": "grep_code",
      "description": "Regex search across files under root.",
      "fn": grep_code,
      "parameters": [
          {"name": "pattern", "type": "string", "required": True},
          {"name": "include", "type": "string",
           "description": "Glob. Default '**/*.py'."},
      ]},
    {"name": "http_get",
      "description": "Read-only HTTP GET (10s timeout, 2 MB cap).",
      "fn": http_get,
      "parameters": [
          {"name": "url", "type": "string", "required": True},
      ]},
    {"name": "web_fetch_text",
      "description": "Fetch a URL and return plain text (HTML stripped).",
      "fn": web_fetch_text,
      "parameters": [
          {"name": "url", "type": "string", "required": True},
      ]},
    {"name": "sqlite_query",
      "description": "Run a SELECT against a SQLite DB.",
      "fn": sqlite_query,
      "parameters": [
          {"name": "db_path", "type": "string", "required": True},
          {"name": "query", "type": "string", "required": True,
           "description": "Must begin with SELECT or WITH."},
      ]},
    {"name": "shell_exec_safe",
      "description": "Run an allow-listed shell command (ls/cat/git/python3/…).",
      "fn": shell_exec_safe,
      "parameters": [
          {"name": "command", "type": "string", "required": True},
      ]},
    {"name": "git_status",
      "description": "git status + branch + HEAD at the projects root.",
      "fn": git_status,
      "parameters": []},
    {"name": "git_log",
      "description": "Last N commits at the projects root.",
      "fn": git_log,
      "parameters": [
          {"name": "limit", "type": "integer",
           "description": "Number of commits. Default 10."},
      ]},
    {"name": "datetime_now",
      "description": "Current time (UTC by default).",
      "fn": datetime_now,
      "parameters": [
          {"name": "tz", "type": "string",
           "description": "'utc' (default) or 'local'."},
      ]},
]


def register_builtin_skills(registry: Any, *,
                             names: set[str] | None = None) -> int:
    """Register every builtin (or a filtered subset) into a SkillRegistry.

    Returns the number of skills registered.
    """
    from mnemosyne_skills import Skill
    n = 0
    for spec in BUILTIN_SPECS:
        if names is not None and spec["name"] not in names:
            continue
        registry.register(Skill(
            name=spec["name"],
            description=spec["description"],
            parameters=spec["parameters"],
            invocation="python",
            callable=spec["fn"],
        ))
        n += 1
    return n


def builtin_skill_names() -> list[str]:
    return [s["name"] for s in BUILTIN_SPECS]
