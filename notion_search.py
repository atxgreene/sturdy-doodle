#!/usr/bin/env python3
# ==============================================================================
#  notion-search.py
#  Standalone, interface-agnostic helper for reading a Notion workspace.
#
#  Three subcommands (mirroring obsidian-search.py):
#    search <query>       full-text search via POST /v1/search
#    read <page-id-or-url> fetch page block children and render to markdown
#    list-recent          list pages edited in the last N days
#
#  Reads NOTION_API_KEY from the environment (the wizard writes it to
#  ~/projects/mnemosyne/.env). Can also be passed via --token on the CLI,
#  though putting a token in argv is discouraged — env var is preferred.
#
#  Output formats:
#    default             human-readable, one result per line
#    --json              machine-readable JSON (for agent/tool wrappers)
#
#  Zero dependencies beyond the Python stdlib. Uses Notion API v1 with
#  Notion-Version 2022-06-28 header.
#
#  This helper is deliberately NOT coupled to eternal-context's skill interface.
#  It is meant to be called by a thin wrapper skill (shell-out via subprocess,
#  or `import notion_search` if you drop it into a package).
#
#  Security:
#    - Read-only. No subcommand writes, comments, updates, or creates pages.
#    - Page IDs are validated against a 32-hex-char regex before insertion
#      into any URL. URLs are parsed defensively.
#    - Token is read from env (NOTION_API_KEY) — passing --token on the CLI
#      puts the token in argv / /proc/<pid>/cmdline and is for convenience
#      testing only; the wizard never uses it.
#    - All HTTP goes through urllib.request with a JSON body, never a
#      URL-embedded secret. The only URL interpolation is the validated
#      page ID for GET /v1/blocks/{id}/children.
#
#  Usage:
#    notion-search.py search "project alpha"
#    notion-search.py search "daily" --limit 5 --json
#    notion-search.py read abcdef1234567890abcdef1234567890
#    notion-search.py read 'https://www.notion.so/My-Page-abcdef1234567890abcdef1234567890'
#    notion-search.py list-recent --days 7
#    NOTION_API_KEY=secret_xyz notion-search.py search foo
# ==============================================================================

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# ---- error helper -------------------------------------------------------------

def die(msg: str, code: int = 2) -> None:
    print(f"notion-search: {msg}", file=sys.stderr)
    sys.exit(code)


# ---- token resolution ---------------------------------------------------------

def resolve_token(explicit: str | None) -> str:
    """Return the Notion integration token from --token, $NOTION_API_KEY, or fail."""
    token = explicit or os.environ.get("NOTION_API_KEY", "").strip()
    if not token:
        die(
            "No Notion API key. Set NOTION_API_KEY in ~/projects/mnemosyne/.env "
            "(the wizard does this) or pass --token."
        )
    return token


# ---- page ID validation / extraction ------------------------------------------

# 32 hex chars, optionally dash-formatted as 8-4-4-4-12
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_DASHED = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def extract_page_id(raw: str) -> str:
    """Accept a bare page ID, a dashed UUID, or a notion.so URL; return normalized hex."""
    raw = raw.strip()
    if not raw:
        die("empty page id", code=3)

    # URL form: take the path's last segment, strip query/fragment
    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw)
        if parsed.netloc not in ("notion.so", "www.notion.so"):
            die(f"only notion.so URLs are accepted: {raw}", code=3)
        raw = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    m = _DASHED.search(raw) or _HEX32.search(raw)
    if not m:
        die(f"could not extract a valid Notion page id from: {raw}", code=3)
    # Normalize to dash-less 32-char form
    return m.group(0).replace("-", "").lower()


# ---- HTTP helpers (urllib.request, JSON body, Bearer auth) --------------------

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "notion-search.py/1.0",
    }


def _request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Issue a Notion API request. path must start with /. Returns the JSON body."""
    url = f"{NOTION_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err_body = json.load(e)
            msg = err_body.get("message", str(e))
        except Exception:
            msg = f"HTTP {e.code}"
        die(f"{method} {path} -> {msg}", code=4)
    except urllib.error.URLError as e:
        die(f"{method} {path} -> network error: {e.reason}", code=5)


# ---- page/result shaping ------------------------------------------------------

def _extract_title(obj: dict[str, Any]) -> str:
    """Pull a displayable title from a Notion page/database result."""
    # Database: top-level "title" array
    if obj.get("object") == "database":
        parts = obj.get("title", []) or []
        return "".join(p.get("plain_text", "") for p in parts) or "(untitled database)"
    # Page: iterate properties for the one with type="title"
    props = obj.get("properties", {}) or {}
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", []) or []
            return "".join(p.get("plain_text", "") for p in parts) or "(untitled)"
    return "(untitled)"


def _result_summary(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": obj.get("id", "").replace("-", ""),
        "object": obj.get("object", ""),
        "title": _extract_title(obj),
        "url": obj.get("url", ""),
        "last_edited_time": obj.get("last_edited_time", ""),
    }


# ---- block -> markdown renderer -----------------------------------------------

def _rich_text_plain(rt: list[dict[str, Any]] | None) -> str:
    if not rt:
        return ""
    return "".join(r.get("plain_text", "") for r in rt)


def _render_block(block: dict[str, Any], indent: int = 0) -> str:
    """Render a single block into a markdown-ish line. Recurses into children."""
    btype = block.get("type", "")
    content = block.get(btype, {}) or {}
    pad = "  " * indent
    text = _rich_text_plain(content.get("rich_text"))

    if btype == "paragraph":
        out = f"{pad}{text}"
    elif btype == "heading_1":
        out = f"{pad}# {text}"
    elif btype == "heading_2":
        out = f"{pad}## {text}"
    elif btype == "heading_3":
        out = f"{pad}### {text}"
    elif btype == "bulleted_list_item":
        out = f"{pad}- {text}"
    elif btype == "numbered_list_item":
        out = f"{pad}1. {text}"
    elif btype == "to_do":
        mark = "[x]" if content.get("checked") else "[ ]"
        out = f"{pad}- {mark} {text}"
    elif btype == "quote":
        out = f"{pad}> {text}"
    elif btype == "code":
        lang = content.get("language", "")
        out = f"{pad}```{lang}\n{pad}{text}\n{pad}```"
    elif btype == "divider":
        out = f"{pad}---"
    elif btype == "callout":
        icon = (content.get("icon") or {}).get("emoji", "")
        out = f"{pad}{icon} {text}".strip()
    elif btype == "toggle":
        out = f"{pad}▸ {text}"
    elif btype == "child_page":
        out = f"{pad}[child page: {content.get('title', '')}]"
    elif btype == "child_database":
        out = f"{pad}[child database: {content.get('title', '')}]"
    elif btype == "bookmark":
        out = f"{pad}[bookmark: {content.get('url', '')}]"
    elif btype == "image":
        out = f"{pad}[image]"
    elif btype == "unsupported":
        out = f"{pad}[unsupported block]"
    else:
        # Fallback: best-effort text extraction
        out = f"{pad}{text}" if text else f"{pad}[{btype}]"

    return out


def _fetch_children(token: str, block_id: str) -> list[dict[str, Any]]:
    """Fetch all child blocks of a page/block, handling pagination."""
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        qs = f"?start_cursor={urllib.parse.quote(cursor)}" if cursor else ""
        data = _request("GET", f"/blocks/{block_id}/children{qs}", token)
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results


def _render_page(token: str, block_id: str, indent: int = 0, depth: int = 0) -> list[str]:
    """Recursively render a page's blocks to markdown-ish lines."""
    if depth > 4:  # safety limit on recursion
        return [f"{'  ' * indent}[max depth reached]"]
    lines: list[str] = []
    for block in _fetch_children(token, block_id):
        lines.append(_render_block(block, indent))
        if block.get("has_children"):
            lines.extend(_render_page(token, block["id"], indent + 1, depth + 1))
    return lines


# ---- commands -----------------------------------------------------------------

def cmd_search(token: str, args: argparse.Namespace) -> int:
    if not args.query:
        die("search: empty query")
    body: dict[str, Any] = {
        "query": args.query,
        "page_size": min(max(args.limit, 1), 100),
    }
    if args.kind == "page":
        body["filter"] = {"value": "page", "property": "object"}
    elif args.kind == "database":
        body["filter"] = {"value": "database", "property": "object"}
    data = _request("POST", "/search", token, body)
    results = [_result_summary(obj) for obj in data.get("results", [])[: args.limit]]

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False)
        print()
    else:
        if not results:
            print(f"no matches for: {args.query}")
            return 0
        for r in results:
            print(f"{r['id']}  {r['title']}  ({r['url']})")
    return 0


def cmd_read(token: str, args: argparse.Namespace) -> int:
    page_id = extract_page_id(args.page)
    # Get page metadata first for the title
    meta = _request("GET", f"/pages/{page_id}", token)
    title = _extract_title(meta)
    lines = _render_page(token, page_id)

    if args.json:
        json.dump({
            "id": page_id,
            "title": title,
            "url": meta.get("url", ""),
            "last_edited_time": meta.get("last_edited_time", ""),
            "markdown": "\n".join(lines),
        }, sys.stdout, ensure_ascii=False)
        print()
    else:
        print(f"# {title}")
        print()
        for line in lines:
            print(line)
    return 0


def cmd_list_recent(token: str, args: argparse.Namespace) -> int:
    # Notion search with sort by last_edited_time descending, then filter client-side
    body: dict[str, Any] = {
        "query": "",
        "sort": {"timestamp": "last_edited_time", "direction": "descending"},
        "page_size": min(max(args.limit * 3, 10), 100),  # over-fetch then prune
        "filter": {"value": "page", "property": "object"},
    }
    data = _request("POST", "/search", token, body)
    cutoff = time.time() - args.days * 86400
    entries: list[dict[str, Any]] = []
    for obj in data.get("results", []):
        ts = obj.get("last_edited_time", "")
        try:
            # Notion times are ISO 8601 with Z
            epoch = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S.000Z"))
        except ValueError:
            continue
        if epoch < cutoff:
            continue
        entries.append({
            "id": obj.get("id", "").replace("-", ""),
            "title": _extract_title(obj),
            "url": obj.get("url", ""),
            "last_edited_time": ts,
        })
        if len(entries) >= args.limit:
            break

    if args.json:
        json.dump(entries, sys.stdout, ensure_ascii=False)
        print()
    else:
        if not entries:
            print(f"no pages edited in the last {args.days} days")
            return 0
        for e in entries:
            print(f"{e['last_edited_time']}  {e['title']}")
    return 0


# ---- main ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notion-search",
        description="Read-only helper for a Notion workspace. Mirrors "
                    "obsidian-search.py semantics so a skill wrapper can treat "
                    "both surfaces interchangeably.",
    )
    p.add_argument("--token", help="NOTION_API_KEY (discouraged — prefer env var)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="full-text search across the workspace")
    sp.add_argument("query", help="search query")
    sp.add_argument("--limit", type=int, default=10, help="max results (default 10)")
    sp.add_argument(
        "--kind",
        choices=["any", "page", "database"],
        default="any",
        help="filter result type",
    )

    rp = sub.add_parser("read", help="print a page rendered as markdown")
    rp.add_argument("page", help="page ID (hex or dashed UUID) or notion.so URL")

    lp = sub.add_parser("list-recent", help="list pages edited in the last N days")
    lp.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    lp.add_argument("--limit", type=int, default=20, help="max results (default 20)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    token = resolve_token(args.token)
    if args.cmd == "search":
        return cmd_search(token, args)
    if args.cmd == "read":
        return cmd_read(token, args)
    if args.cmd == "list-recent":
        return cmd_list_recent(token, args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
