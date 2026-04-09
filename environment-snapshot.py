#!/usr/bin/env python3
# ==============================================================================
#  environment-snapshot.py
#
#  Pre-compute the environment context that a Mnemosyne agent would otherwise
#  spend 2–4 exploratory turns discovering via tool calls. Inspired by the
#  Meta-Harness Terminal-Bench 2 optimization (Stanford, 2026), where the
#  proposer agent found that replacing a ~4-turn "discover the environment"
#  sequence with a single one-shot snapshot in the *first* LLM call was the
#  breakthrough that beat the baseline.
#
#  This helper produces the same kind of snapshot for Mnemosyne:
#
#    - Does $PROJECTS_DIR exist? What's at the top level?
#    - What keys are configured in $PROJECTS_DIR/.env?  (NAMES ONLY — never values)
#    - Is Ollama reachable? Which models are pulled?
#    - Is the Python venv healthy? What version?
#    - Which skill helpers are installed alongside this script?
#    - Is an Obsidian vault configured? Does it exist? How many notes?
#    - Disk free on the $PROJECTS_DIR volume.
#
#  Output formats:
#    default : human-readable markdown, suitable as a first-turn preamble
#    --json  : machine-readable dict, suitable for injection into prompt scaffolding
#
#  Security:
#    - Never emits .env VALUES. Only key names.
#    - Never emits any token, bot key, or secret.
#    - Never contacts any external service except $OLLAMA_HOST (localhost by
#      default).
#
#  Usage:
#    ./environment-snapshot.py
#    ./environment-snapshot.py --json
#    MNEMOSYNE_PROJECTS_DIR=/tmp/fake ./environment-snapshot.py
# ==============================================================================

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---- defaults ----------------------------------------------------------------

def default_projects_dir() -> Path:
    raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "projects" / "mnemosyne").resolve()


# ---- snapshot fragments ------------------------------------------------------

def snapshot_projects_dir(pd: Path) -> dict[str, Any]:
    if not pd.exists():
        return {"exists": False, "path": str(pd)}
    try:
        entries = sorted(p.name for p in pd.iterdir())
    except PermissionError:
        return {"exists": True, "path": str(pd), "readable": False}
    return {
        "exists": True,
        "path": str(pd),
        "readable": True,
        "entry_count": len(entries),
        "top_level_entries": entries[:30],
    }


def snapshot_env_file(pd: Path) -> dict[str, Any]:
    """Return only the KEY NAMES from .env. Values never cross this boundary."""
    env_file = pd / ".env"
    if not env_file.exists():
        return {"exists": False, "path": str(env_file)}
    keys: list[str] = []
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k:
                keys.append(k)
    except OSError:
        return {"exists": True, "path": str(env_file), "readable": False}
    return {
        "exists": True,
        "path": str(env_file),
        "key_count": len(set(keys)),
        "keys": sorted(set(keys)),
    }


def snapshot_ollama(host: str | None = None) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            data = json.load(r)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return {"reachable": False, "host": host, "error": type(e).__name__}
    except json.JSONDecodeError:
        return {"reachable": True, "host": host, "error": "invalid_json"}
    models = [m.get("name", "") for m in data.get("models", [])]
    return {"reachable": True, "host": host, "models": models, "model_count": len(models)}


def snapshot_venv(pd: Path) -> dict[str, Any]:
    venv = pd / ".venv"
    python = venv / "bin" / "python"
    if not python.exists():
        return {"exists": False, "path": str(venv)}
    try:
        out = subprocess.run(
            [str(python), "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version = (out.stdout + out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    return {"exists": True, "path": str(venv), "python_version": version}


def snapshot_skills() -> dict[str, Any]:
    """Discover skill helper scripts installed alongside this script."""
    script_dir = Path(__file__).parent.resolve()
    skills: list[str] = []

    # Known-pattern helpers
    for name in ("obsidian-search.py", "notion-search.py"):
        if (script_dir / name).is_file():
            skills.append(name.removesuffix(".py"))

    # Generic pattern: anything ending in -search.py or -snapshot.py
    for p in sorted(script_dir.glob("*-search.py")):
        stem = p.stem
        if stem not in skills:
            skills.append(stem)
    for p in sorted(script_dir.glob("*-snapshot.py")):
        stem = p.stem
        if stem not in skills and stem != Path(__file__).stem:
            skills.append(stem)

    return {"available": sorted(skills), "script_dir": str(script_dir)}


def snapshot_vault(pd: Path) -> dict[str, Any]:
    """Peek at .env for OBSIDIAN_VAULT_PATH without reading any other values."""
    env_file = pd / ".env"
    if not env_file.exists():
        return {"configured": False, "reason": ".env missing"}

    vault_path: str | None = None
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            if s.startswith("OBSIDIAN_VAULT_PATH="):
                vault_path = s.split("=", 1)[1].strip()
                # Strip quotes
                if (vault_path.startswith('"') and vault_path.endswith('"')) or \
                   (vault_path.startswith("'") and vault_path.endswith("'")):
                    vault_path = vault_path[1:-1]
                break
    except OSError:
        return {"configured": False, "reason": ".env unreadable"}

    if not vault_path:
        return {"configured": False}

    p = Path(vault_path)
    if not p.exists() or not p.is_dir():
        return {"configured": True, "path": vault_path, "exists": False}

    try:
        note_count = sum(1 for _ in p.rglob("*.md"))
    except (PermissionError, OSError):
        note_count = None
    return {
        "configured": True,
        "path": vault_path,
        "exists": True,
        "note_count": note_count,
    }


def snapshot_disk(pd: Path) -> dict[str, Any]:
    target = pd if pd.exists() else pd.parent
    try:
        stat = shutil.disk_usage(str(target))
    except (OSError, FileNotFoundError):
        return {}
    return {
        "total_gb": round(stat.total / 1e9, 1),
        "free_gb": round(stat.free / 1e9, 1),
        "used_gb": round(stat.used / 1e9, 1),
        "percent_free": round(stat.free / stat.total * 100, 1) if stat.total else 0.0,
    }


def snapshot_platform() -> dict[str, Any]:
    import platform
    return {
        "system": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
    }


# ---- aggregate ---------------------------------------------------------------

def build_snapshot(projects_dir: Path | None = None) -> dict[str, Any]:
    pd = projects_dir or default_projects_dir()
    return {
        "schema_version": 1,
        "projects_dir": snapshot_projects_dir(pd),
        "env_file": snapshot_env_file(pd),
        "ollama": snapshot_ollama(),
        "venv": snapshot_venv(pd),
        "skills": snapshot_skills(),
        "vault": snapshot_vault(pd),
        "disk": snapshot_disk(pd),
        "platform": snapshot_platform(),
    }


# ---- markdown formatter ------------------------------------------------------

def _fmt_section(title: str, body: str) -> str:
    return f"**{title}:** {body}"


def format_markdown(snap: dict[str, Any]) -> str:
    lines = ["# Mnemosyne environment snapshot", ""]

    pd = snap["projects_dir"]
    if pd["exists"]:
        entries_preview = ", ".join(pd.get("top_level_entries", [])[:12])
        lines.append(_fmt_section("Projects dir",
                                  f"{pd['path']} ({pd.get('entry_count', 0)} entries)"))
        if entries_preview:
            lines.append(f"  top-level: {entries_preview}")
    else:
        lines.append(_fmt_section("Projects dir",
                                  f"{pd['path']} — NOT FOUND (run install-mnemosyne.sh)"))
    lines.append("")

    env = snap["env_file"]
    if env.get("exists"):
        lines.append(_fmt_section(".env",
                                  f"{env['key_count']} keys configured"))
        lines.append(f"  keys: {', '.join(env.get('keys', []))}")
    else:
        lines.append(_fmt_section(".env",
                                  "not found (run mnemosyne-wizard.sh)"))
    lines.append("")

    ollama = snap["ollama"]
    if ollama.get("reachable"):
        models_str = ", ".join(ollama.get("models") or []) or "(none pulled)"
        lines.append(_fmt_section("Ollama",
                                  f"reachable at {ollama['host']}"))
        lines.append(f"  models: {models_str}")
    else:
        lines.append(_fmt_section("Ollama",
                                  f"NOT reachable at {ollama['host']} ({ollama.get('error', '?')})"))
    lines.append("")

    venv = snap["venv"]
    if venv["exists"]:
        lines.append(_fmt_section("venv",
                                  f"{venv['python_version']} at {venv['path']}"))
    else:
        lines.append(_fmt_section("venv",
                                  f"NOT FOUND at {venv['path']}"))
    lines.append("")

    skills = snap["skills"]
    if skills["available"]:
        lines.append(_fmt_section("Skills available",
                                  ", ".join(skills["available"])))
    else:
        lines.append(_fmt_section("Skills available", "(none found)"))
    lines.append("")

    vault = snap["vault"]
    if vault.get("configured"):
        exists = vault.get("exists", False)
        nc = vault.get("note_count")
        if exists and nc is not None:
            lines.append(_fmt_section("Obsidian vault",
                                      f"{vault['path']} ({nc} notes)"))
        elif exists:
            lines.append(_fmt_section("Obsidian vault",
                                      f"{vault['path']} (note count unavailable)"))
        else:
            lines.append(_fmt_section("Obsidian vault",
                                      f"{vault['path']} — path missing"))
    else:
        lines.append(_fmt_section("Obsidian vault",
                                  f"not configured ({vault.get('reason', '-')})"))
    lines.append("")

    disk = snap["disk"]
    if disk:
        lines.append(
            _fmt_section(
                "Disk",
                f"{disk.get('free_gb', '?')} GB free of {disk.get('total_gb', '?')} GB "
                f"({disk.get('percent_free', '?')}% free)",
            )
        )

    plat = snap["platform"]
    lines.append("")
    lines.append(_fmt_section("Platform",
                              f"{plat['system']} {plat['release']}, Python {plat['python']}"))

    return "\n".join(lines) + "\n"


# ---- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="environment-snapshot",
        description="Pre-compute the Mnemosyne environment context for "
                    "injection into an agent's first turn. Mirrors the "
                    "Meta-Harness Terminal-Bench 2 optimization.",
    )
    p.add_argument("--projects-dir",
                   help="override $MNEMOSYNE_PROJECTS_DIR")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of markdown")
    args = p.parse_args(argv)

    pd = Path(args.projects_dir).expanduser().resolve() if args.projects_dir else None
    snap = build_snapshot(pd)

    if args.json:
        json.dump(snap, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_markdown(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
