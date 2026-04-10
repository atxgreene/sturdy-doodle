"""
mnemosyne_config.py — shared configuration for the Mnemosyne harness stack.

Single source of truth for:
  - PROJECTS_DIR resolution ($MNEMOSYNE_PROJECTS_DIR or ~/projects/mnemosyne)
  - .env key parsing (names only, never values — for snapshot/diagnostics)
  - Ollama host resolution

Every other module imports from here instead of reimplementing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def default_projects_dir() -> Path:
    """Resolve $MNEMOSYNE_PROJECTS_DIR or ~/projects/mnemosyne."""
    raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / "projects" / "mnemosyne").resolve()


def default_ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").strip()


def default_ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "qwen3:8b").strip()


def env_file_path(projects_dir: Path | None = None) -> Path:
    return (projects_dir or default_projects_dir()) / ".env"


def parse_env_keys(projects_dir: Path | None = None) -> list[str]:
    """Return sorted unique KEY NAMES from .env. Never reads values."""
    ef = env_file_path(projects_dir)
    if not ef.exists():
        return []
    keys: list[str] = []
    try:
        for line in ef.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k:
                keys.append(k)
    except OSError:
        return []
    return sorted(set(keys))


def parse_env_value(key: str, projects_dir: Path | None = None) -> str | None:
    """Read a single value from .env by key name. For non-secret config only."""
    ef = env_file_path(projects_dir)
    if not ef.exists():
        return None
    try:
        for line in ef.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == key:
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or \
                   (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return None
