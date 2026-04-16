"""
mnemosyne_adapter_claude_code.py — Claude Code ↔ Mnemosyne bridge.

Inspired by agentic-stack's harness-adapter pattern. Instead of asking
Claude Code users to switch tools, we wrap their existing workflow
with Mnemosyne's memory + identity lock + skills + permissions.

What this ships
---------------
A single CLI: `mnemosyne-adapter-claude-code`. Three subcommands:

    install <target-dir>      install the adapter into a project
    status                    show what's installed + what's active
    uninstall <target-dir>    remove the adapter

Install does three things:
  1. Writes `CLAUDE.md` (or appends to an existing one) with a block
     referencing Mnemosyne's identity + memory + permissions.
  2. Symlinks `$PROJECTS_DIR/memory.db` → `<target>/.claude/memory.db`
     so Claude Code sessions read the same ICMS memory.
  3. Writes `<target>/.claude/mnemosyne/hooks/` with user-stop and
     session-start hook scripts that invoke `mnemosyne-memory search`
     to prefill context on each turn + capture turns post-hoc.

Design principles
-----------------
- **Non-destructive.** If `CLAUDE.md` already exists, we append a
  delimited block; never overwrite user text.
- **Reversible.** `uninstall` removes exactly what `install` wrote,
  leaving the user's own `CLAUDE.md` content intact.
- **Symlink, don't copy.** Memory lives at `$PROJECTS_DIR/memory.db`
  (source of truth). Claude Code reads via symlink — any update from
  one side is visible to the other immediately.
- **No runtime dependency on Mnemosyne for Claude Code itself.**
  The hooks invoke `mnemosyne-memory` on `$PATH`. If Mnemosyne is
  uninstalled, the hooks become no-ops; Claude Code continues to
  work without error.

The hooks pattern follows Claude Code's documented hook system:
`.claude/settings.json` declares which hook runs on which event.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


# Begin/end markers for the block we manage inside the user's CLAUDE.md.
# Everything between these is owned by us; everything outside is the
# user's. `uninstall` removes only this block.
_BEGIN_MARK = "<!-- mnemosyne-adapter:begin — managed, do not edit -->"
_END_MARK   = "<!-- mnemosyne-adapter:end -->"


def _default_projects_dir() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:
        import os
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return (Path(raw).expanduser().resolve() if raw
                else (Path.home() / "projects" / "mnemosyne").resolve())


def _mnemosyne_block(projects_dir: Path) -> str:
    """The text we inject into CLAUDE.md."""
    try:
        from mnemosyne_identity import MNEMOSYNE_IDENTITY
        ident = MNEMOSYNE_IDENTITY.strip()
    except ImportError:
        ident = "You are Mnemosyne, a local-first agent framework."
    return f"""\
{_BEGIN_MARK}

## Mnemosyne identity + memory bridge

This project is configured to share memory + identity with the
Mnemosyne framework installed at `{projects_dir}`.

### Identity

{ident}

### Memory access

Your working memory is available via the `mnemosyne-memory` CLI on
`$PATH`:

    mnemosyne-memory search "<query>" --limit 8 --tier-max 2

Use this at the start of a task to recall what you already know
before exploring the codebase. When you finish a task, capture what
you learned:

    mnemosyne-memory write "<summary>" --kind fact --tier 2

### Permissions

Before invoking any tool that writes files, runs shell commands, or
makes outbound HTTP calls, check the permissions file at
`{projects_dir}/permissions.md`. If it doesn't exist, conservative
defaults apply.

### Goals

Active goals across sessions live in `{projects_dir}/goals.jsonl`:

    mnemosyne-goals list

Refer to these when planning; reference them explicitly when you
make progress.

{_END_MARK}
"""


def _claude_settings_hooks() -> dict[str, Any]:
    """JSON fragment for .claude/settings.json that wires our hooks."""
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": ".claude/mnemosyne/hooks/session_start.sh",
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": ".claude/mnemosyne/hooks/on_stop.sh",
                        }
                    ]
                }
            ],
        }
    }


_SESSION_START_HOOK = """\
#!/usr/bin/env bash
# Mnemosyne session-start hook.
# Fires once when a Claude Code session begins. Prints a short context
# block to stdout which Claude Code prepends to the assistant's first
# message context.
set -euo pipefail

command -v mnemosyne-memory >/dev/null 2>&1 || exit 0

echo "## Mnemosyne context"
echo
echo "Recent L1+L2 memories:"
mnemosyne-memory search "." --limit 8 --tier-max 2 2>/dev/null | head -20 \\
    || echo "(no memories yet)"
echo
if command -v mnemosyne-goals >/dev/null 2>&1; then
    echo "Open goals:"
    mnemosyne-goals list 2>/dev/null | head -10 || echo "(no goals)"
fi
"""


_ON_STOP_HOOK = """\
#!/usr/bin/env bash
# Mnemosyne on-stop hook.
# Fires when Claude Code finishes a turn. We don't capture full turn
# content here (that would leak user prompts to disk without consent);
# this is a placeholder for future integration with Claude Code's
# turn-capture API once that exists.
set -euo pipefail
# Currently a no-op; the live-memory write path is via the user
# explicitly running `mnemosyne-memory write ...` when they want to
# capture a learning.
exit 0
"""


# ---- installation ---------------------------------------------------------

def install(target_dir: Path,
            *,
            projects_dir: Path | None = None,
            force: bool = False) -> dict[str, Any]:
    """Install the adapter into `target_dir`.

    Creates (or augments) CLAUDE.md and .claude/ with the Mnemosyne
    integration. Returns a report describing what changed.
    """
    pd = projects_dir or _default_projects_dir()
    target_dir = Path(target_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "target_dir": str(target_dir),
        "projects_dir": str(pd),
        "actions": [],
        "warnings": [],
    }

    # 1. CLAUDE.md
    claude_md = target_dir / "CLAUDE.md"
    block = _mnemosyne_block(pd)
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if _BEGIN_MARK in existing and not force:
            report["warnings"].append(
                f"{claude_md} already has a mnemosyne-adapter block; "
                "pass --force to replace"
            )
        else:
            if _BEGIN_MARK in existing:
                # Replace existing managed block
                start = existing.find(_BEGIN_MARK)
                end = existing.find(_END_MARK, start)
                if end >= 0:
                    end += len(_END_MARK)
                    new = existing[:start] + block.rstrip() + existing[end:]
                else:
                    new = existing + "\n\n" + block
            else:
                new = existing.rstrip() + "\n\n" + block
            claude_md.write_text(new, encoding="utf-8")
            report["actions"].append(f"updated {claude_md}")
    else:
        claude_md.write_text(block, encoding="utf-8")
        report["actions"].append(f"created {claude_md}")

    # 2. .claude/ directory + hooks
    claude_dir = target_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    hooks_dir = claude_dir / "mnemosyne" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    for fname, body in (("session_start.sh", _SESSION_START_HOOK),
                         ("on_stop.sh", _ON_STOP_HOOK)):
        hook = hooks_dir / fname
        hook.write_text(body, encoding="utf-8")
        hook.chmod(0o755)
        report["actions"].append(f"created {hook}")

    # 3. .claude/settings.json — merge, don't overwrite
    settings = claude_dir / "settings.json"
    fragment = _claude_settings_hooks()
    if settings.exists():
        try:
            existing_settings = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_settings = {}
            report["warnings"].append(
                f"{settings} had invalid JSON; replacing with adapter "
                "defaults only"
            )
        # Merge hooks — append ours if not already present
        existing_hooks = existing_settings.setdefault("hooks", {})
        for event, our_entries in fragment["hooks"].items():
            existing_list = existing_hooks.setdefault(event, [])
            # Dedupe by the command path we inject
            our_cmd = our_entries[0]["hooks"][0]["command"]
            already = any(
                any(h.get("command") == our_cmd
                    for h in entry.get("hooks") or [])
                for entry in existing_list
            )
            if not already:
                existing_list.extend(our_entries)
        settings.write_text(
            json.dumps(existing_settings, indent=2),
            encoding="utf-8",
        )
        report["actions"].append(f"merged hooks into {settings}")
    else:
        settings.write_text(json.dumps(fragment, indent=2), encoding="utf-8")
        report["actions"].append(f"created {settings}")

    # 4. Symlink memory.db if it exists in the source
    mem_db = pd / "memory.db"
    if mem_db.exists():
        link = claude_dir / "memory.db"
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(mem_db)
            report["actions"].append(f"symlinked {link} → {mem_db}")
        except OSError as e:
            # Windows without admin can't create symlinks; fall back to copy
            shutil.copy2(mem_db, link)
            report["actions"].append(f"copied {link} ← {mem_db} "
                                      f"(symlink failed: {e})")
            report["warnings"].append(
                "memory.db was copied, not symlinked; updates from the "
                "Claude Code side won't propagate back. Install the "
                "adapter from a shell with symlink privileges to enable "
                "bidirectional sync."
            )
    else:
        report["warnings"].append(
            f"no memory.db at {mem_db}; create one by running "
            "`mnemosyne-memory write \"your first memory\"` first"
        )

    return report


def uninstall(target_dir: Path) -> dict[str, Any]:
    """Remove the adapter from `target_dir`. Non-destructive to user
    content — only removes what `install` wrote."""
    target_dir = Path(target_dir).expanduser().resolve()
    report: dict[str, Any] = {
        "target_dir": str(target_dir),
        "actions": [],
        "warnings": [],
    }

    # 1. Strip our block from CLAUDE.md
    claude_md = target_dir / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text(encoding="utf-8")
        if _BEGIN_MARK in text:
            start = text.find(_BEGIN_MARK)
            end = text.find(_END_MARK, start)
            if end >= 0:
                end += len(_END_MARK)
                new = (text[:start].rstrip() + "\n" + text[end:].lstrip())
                if new.strip():
                    claude_md.write_text(new, encoding="utf-8")
                    report["actions"].append(
                        f"removed mnemosyne block from {claude_md}")
                else:
                    claude_md.unlink()
                    report["actions"].append(
                        f"removed {claude_md} (was mnemosyne-only)")

    # 2. Remove hooks dir
    hooks_dir = target_dir / ".claude" / "mnemosyne"
    if hooks_dir.exists():
        shutil.rmtree(hooks_dir)
        report["actions"].append(f"removed {hooks_dir}")

    # 3. Strip our hooks from settings.json
    settings = target_dir / ".claude" / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "hooks" in data:
            for event, entries in list(data["hooks"].items()):
                data["hooks"][event] = [
                    e for e in entries
                    if not any(".claude/mnemosyne/" in (h.get("command") or "")
                               for h in e.get("hooks") or [])
                ]
                if not data["hooks"][event]:
                    del data["hooks"][event]
            if not data["hooks"]:
                del data["hooks"]
            if data:
                settings.write_text(
                    json.dumps(data, indent=2), encoding="utf-8")
                report["actions"].append(
                    f"removed mnemosyne hooks from {settings}")
            else:
                settings.unlink()
                report["actions"].append(
                    f"removed {settings} (was mnemosyne-only)")

    # 4. Memory symlink
    link = target_dir / ".claude" / "memory.db"
    if link.is_symlink() or link.exists():
        link.unlink()
        report["actions"].append(f"removed memory link {link}")

    return report


def status(target_dir: Path) -> dict[str, Any]:
    """Report what's installed in the target directory."""
    target_dir = Path(target_dir).expanduser().resolve()
    claude_md = target_dir / "CLAUDE.md"
    hooks_dir = target_dir / ".claude" / "mnemosyne" / "hooks"
    settings = target_dir / ".claude" / "settings.json"
    mem_link = target_dir / ".claude" / "memory.db"

    md_has_block = False
    if claude_md.exists():
        md_has_block = _BEGIN_MARK in claude_md.read_text(
            encoding="utf-8", errors="replace"
        )

    return {
        "target_dir": str(target_dir),
        "claude_md_present":  claude_md.exists(),
        "claude_md_has_mnemosyne_block": md_has_block,
        "hooks_installed": hooks_dir.is_dir()
            and (hooks_dir / "session_start.sh").is_file(),
        "settings_present": settings.is_file(),
        "memory_linked":   mem_link.is_symlink()
            or (mem_link.exists() and not mem_link.is_symlink()),
        "memory_is_symlink": mem_link.is_symlink(),
    }


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-adapter-claude-code",
        description="Install/uninstall the Mnemosyne adapter into a Claude "
                    "Code project. Brings Mnemosyne's memory, identity, "
                    "permissions, and goals to any Claude Code session "
                    "running in the target project directory. "
                    "Inspired by agentic-stack.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("install", help="install the adapter")
    ip.add_argument("target_dir", help="project directory to install into")
    ip.add_argument("--projects-dir",
                     help="override $MNEMOSYNE_PROJECTS_DIR")
    ip.add_argument("--force", action="store_true",
                     help="replace an existing mnemosyne-adapter block")
    ip.add_argument("--json", action="store_true")

    up = sub.add_parser("uninstall", help="remove the adapter")
    up.add_argument("target_dir")
    up.add_argument("--json", action="store_true")

    sp = sub.add_parser("status", help="report install status")
    sp.add_argument("target_dir", nargs="?", default=".")
    sp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "install":
        pd = Path(args.projects_dir).expanduser() if args.projects_dir else None
        report = install(Path(args.target_dir), projects_dir=pd,
                          force=args.force)
    elif args.cmd == "uninstall":
        report = uninstall(Path(args.target_dir))
    elif args.cmd == "status":
        report = status(Path(args.target_dir))
    else:
        return 2

    if getattr(args, "json", False):
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
        return 0

    for action in report.get("actions", []):
        print(f"  {action}")
    for warning in report.get("warnings", []):
        print(f"  warning: {warning}", file=sys.stderr)
    if args.cmd == "status":
        for k, v in report.items():
            if k == "target_dir":
                continue
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
