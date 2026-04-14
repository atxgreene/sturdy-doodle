#!/usr/bin/env python3
# ==============================================================================
#  obsidian-search.py
#  Standalone, interface-agnostic helper for reading an Obsidian vault.
#
#  Three subcommands:
#    search <query>         full-text search across the vault
#    read <path>            return the content of a specific note
#    list-recent            list notes modified in the last N days
#
#  Reads OBSIDIAN_VAULT_PATH from the environment (the wizard writes it to
#  ~/projects/mnemosyne/.env). Can also be passed via --vault.
#
#  Output formats:
#    default             human-readable, one result per line
#    --json              machine-readable JSON (for agent/tool wrappers)
#
#  Zero dependencies beyond the Python stdlib. Uses `rg` (ripgrep) for search
#  if available, falls back to a pure-Python search otherwise.
#
#  This helper is deliberately NOT coupled to eternal-context's skill interface.
#  It is meant to be called by a thin wrapper skill (shell-out via subprocess,
#  or `import obsidian_search` if you drop it into a package). That way the
#  actual search logic is useful even before the skill wiring is decided.
#
#  Security:
#    - Refuses to read files outside the vault (resolved path + is_relative_to).
#    - Read-only. Never writes to the vault.
#    - Skips hidden directories (.obsidian/, .git/, .trash/).
#
#  Usage:
#    obsidian-search.py search "daily note 2026"
#    obsidian-search.py search "project alpha" --limit 5 --json
#    obsidian-search.py read "Projects/alpha.md"
#    obsidian-search.py list-recent --days 7
#    OBSIDIAN_VAULT_PATH=/mnt/c/Users/me/Obsidian obsidian-search.py search foo
# ==============================================================================

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator


# ---- vault resolution ---------------------------------------------------------

def resolve_vault(explicit: str | None) -> Path:
    """Resolve the vault path from --vault, $OBSIDIAN_VAULT_PATH, or fail."""
    raw = explicit or os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        die(
            "No vault path. Set OBSIDIAN_VAULT_PATH in ~/projects/mnemosyne/.env "
            "(the wizard does this) or pass --vault."
        )
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        die(f"Vault path is not a directory: {vault}")
    return vault


def die(msg: str, code: int = 2) -> None:
    print(f"obsidian-search: {msg}", file=sys.stderr)
    sys.exit(code)


# ---- path safety --------------------------------------------------------------

SKIP_DIR_NAMES = {".obsidian", ".git", ".trash", ".DS_Store"}


def safe_path(vault: Path, requested: str) -> Path:
    """Resolve `requested` relative to the vault, refusing traversal escapes."""
    # Accept absolute paths only if they're already inside the vault.
    p = Path(requested)
    if not p.is_absolute():
        p = vault / p
    p = p.resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        die(f"path escapes vault: {requested}", code=3)
    return p


def iter_markdown(vault: Path) -> Iterator[Path]:
    """Yield every .md file under vault, skipping hidden/config dirs."""
    for root, dirs, files in os.walk(vault):
        # Prune in-place so os.walk doesn't descend
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIR_NAMES]
        for f in files:
            if f.lower().endswith(".md"):
                yield Path(root) / f


# ---- search -------------------------------------------------------------------

def search_ripgrep(vault: Path, query: str, limit: int) -> list[dict]:
    """Fast path: shell out to ripgrep. Returns structured JSON matches."""
    cmd = [
        "rg",
        "--json",
        "--ignore-case",
        "--glob", "*.md",
        "--glob", "!.obsidian/**",
        "--glob", "!.git/**",
        "--glob", "!.trash/**",
        "--max-count", str(limit),
        "--",
        query,
        str(vault),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None  # ripgrep disappeared between check and run
    # rg exits 1 when there are no matches — that's not an error for us
    if proc.returncode not in (0, 1):
        die(f"ripgrep failed: {proc.stderr.strip()}", code=4)

    results: list[dict] = []
    for line in proc.stdout.splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") != "match":
            continue
        data = evt.get("data", {})
        path_raw = data.get("path", {}).get("text", "")
        if not path_raw:
            continue
        try:
            rel = str(Path(path_raw).resolve().relative_to(vault))
        except ValueError:
            rel = path_raw
        text = (data.get("lines", {}) or {}).get("text", "").rstrip("\n")
        results.append({
            "path": rel,
            "line": data.get("line_number", 0),
            "text": text,
        })
        if len(results) >= limit:
            break
    return results


def search_python(vault: Path, query: str, limit: int) -> list[dict]:
    """Fallback: pure-python case-insensitive substring scan."""
    q = query.lower()
    results: list[dict] = []
    for md in iter_markdown(vault):
        try:
            with md.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, start=1):
                    if q in line.lower():
                        results.append({
                            "path": str(md.relative_to(vault)),
                            "line": i,
                            "text": line.rstrip("\n"),
                        })
                        if len(results) >= limit:
                            return results
        except OSError:
            continue
    return results


def cmd_search(vault: Path, args: argparse.Namespace) -> int:
    query = args.query
    limit = args.limit
    if not query:
        die("search: empty query")

    results = None
    if shutil.which("rg"):
        results = search_ripgrep(vault, query, limit)
    if results is None:
        results = search_python(vault, query, limit)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False)
        print()
    else:
        if not results:
            print(f"no matches for: {query}")
            return 0
        for r in results:
            print(f"{r['path']}:{r['line']}: {r['text']}")
    return 0


# ---- read ---------------------------------------------------------------------

def cmd_read(vault: Path, args: argparse.Namespace) -> int:
    target = safe_path(vault, args.path)
    if not target.is_file():
        die(f"not a file: {args.path}", code=3)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        die(f"read failed: {e}", code=4)

    if args.json:
        st = target.stat()
        json.dump({
            "path": str(target.relative_to(vault)),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "content": content,
        }, sys.stdout, ensure_ascii=False)
        print()
    else:
        sys.stdout.write(content)
    return 0


# ---- list-recent --------------------------------------------------------------

def cmd_list_recent(vault: Path, args: argparse.Namespace) -> int:
    cutoff = time.time() - args.days * 86400
    entries: list[dict] = []
    for md in iter_markdown(vault):
        try:
            mtime = md.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            entries.append({
                "path": str(md.relative_to(vault)),
                "mtime": int(mtime),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    if args.limit:
        entries = entries[: args.limit]

    if args.json:
        json.dump(entries, sys.stdout, ensure_ascii=False)
        print()
    else:
        if not entries:
            print(f"no notes modified in the last {args.days} days")
            return 0
        for e in entries:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
            print(f"{when}  {e['path']}")
    return 0


# ---- main ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obsidian-search",
        description="Read-only helper for an Obsidian vault. Interface-agnostic; "
                    "meant to be called from a thin skill wrapper or directly from a shell.",
    )
    p.add_argument(
        "--vault",
        help="Vault path (default: $OBSIDIAN_VAULT_PATH)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human output",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="full-text search across the vault")
    sp.add_argument("query", help="search term (case-insensitive)")
    sp.add_argument("--limit", type=int, default=10, help="max results (default 10)")

    rp = sub.add_parser("read", help="print the content of a note")
    rp.add_argument("path", help="note path relative to the vault")

    lp = sub.add_parser("list-recent", help="list notes modified in the last N days")
    lp.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    lp.add_argument("--limit", type=int, default=50, help="max results (default 50)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    vault = resolve_vault(args.vault)

    if args.cmd == "search":
        return cmd_search(vault, args)
    if args.cmd == "read":
        return cmd_read(vault, args)
    if args.cmd == "list-recent":
        return cmd_list_recent(vault, args)
    parser.error(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
