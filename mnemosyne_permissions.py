"""
mnemosyne_permissions.py — user-editable permission model.

Inspired by agentic-stack's `permissions.md` convention. A single
markdown file at `$PROJECTS_DIR/permissions.md` declares what skills
the agent may invoke, what file paths are forbidden, and per-skill
rate limits. The brain consults this file before dispatching any
tool call.

Why a markdown file instead of JSON/YAML? Three reasons:

  1. It lives alongside `AGENTS.md`, `TOOLS.md`, and `IDENTITY.md` —
     one editing workflow, one file pattern.
  2. Users can leave comments explaining *why* they denied something,
     which is as important as the rule itself.
  3. It diffs cleanly in git, survives human edits, and doesn't need
     a schema validator to fail loudly.

Default-deny for explicitly-listed forbidden paths, default-allow
for skills (so the framework works out of the box for new users).

File format (all sections optional; missing = permissive default):

    # Mnemosyne permissions

    ## allowed_skills
    - fs_read
    - fs_list
    - grep_code
    - git_status

    ## denied_skills
    - fs_write_safe        # no writes unless I explicitly add here
    - shell_exec_safe      # I don't trust shell tools yet

    ## forbidden_paths
    - ~/.ssh
    - ~/.aws
    - ~/.config/gh
    - /etc

    ## rate_limits
    - http_get: 30/min     # at most 30 outbound HTTP calls per minute
    - web_fetch_text: 10/min

    ## notes
    Any free-text the user wants. Not parsed; just preserved in diffs.

If both `allowed_skills` AND `denied_skills` are present, allow-list
wins — only listed skills are callable. If only `denied_skills` is
present, everything except listed is allowed.

Stdlib only.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SECTION_RE = re.compile(r"^#+\s*(.+?)\s*$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+?)(?:\s+#.*)?$", re.MULTILINE)
_RATE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(\d+)\s*/\s*(sec|min|hour|s|m|h)$")


@dataclass
class Permissions:
    """Parsed permissions.md contents.

    `allowed_skills` — if non-empty, acts as an allow-list (denies
    everything not listed). If empty, every skill is allowed unless
    in `denied_skills`.

    `denied_skills` — always denies listed skill names regardless of
    allow-list.

    `forbidden_paths` — resolved absolute paths that skills must not
    read from or write to. Rejection is per-skill (each skill that
    touches filesystem checks).

    `rate_limits` — {skill_name: (count, window_seconds)}
    """
    allowed_skills: set[str] = field(default_factory=set)
    denied_skills: set[str] = field(default_factory=set)
    forbidden_paths: list[Path] = field(default_factory=list)
    rate_limits: dict[str, tuple[int, int]] = field(default_factory=dict)
    source_path: Path | None = None

    def is_skill_allowed(self, name: str) -> tuple[bool, str]:
        """Return (allowed, reason). `reason` is empty when allowed."""
        if name in self.denied_skills:
            return False, f"skill {name!r} is in denied_skills"
        if self.allowed_skills and name not in self.allowed_skills:
            return False, (f"skill {name!r} is not in allowed_skills "
                           f"(allow-list mode)")
        return True, ""

    def is_path_allowed(self, path: str | Path) -> tuple[bool, str]:
        """Return (allowed, reason). Matches by prefix against resolved path."""
        try:
            p = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return True, ""   # can't resolve = we can't enforce; let skill handle
        for forb in self.forbidden_paths:
            try:
                p.relative_to(forb)
                return False, f"path {p} is under forbidden root {forb}"
            except ValueError:
                continue
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_skills": sorted(self.allowed_skills),
            "denied_skills": sorted(self.denied_skills),
            "forbidden_paths": [str(p) for p in self.forbidden_paths],
            "rate_limits": {k: list(v) for k, v in self.rate_limits.items()},
            "source_path": str(self.source_path) if self.source_path else None,
        }


# ---- parsing ---------------------------------------------------------------

_WINDOWS: dict[str, int] = {"sec": 1, "s": 1, "min": 60, "m": 60,
                            "hour": 3600, "h": 3600}


def _parse_section(text: str, header: str) -> list[str]:
    """Extract items from a `## header` list block."""
    # Split on section headers, keep the chunk under the matching one
    chunks = re.split(r"^##+\s+", text, flags=re.MULTILINE)
    for chunk in chunks:
        first_line, _, body = chunk.partition("\n")
        if first_line.strip().lower() == header.lower():
            items: list[str] = []
            for m in _LIST_ITEM_RE.finditer(body):
                items.append(m.group(1).strip())
                # Stop at the next section header
            # Trim body at next `## ...`
            stop = re.search(r"^##+\s+", body, re.MULTILINE)
            if stop:
                body = body[: stop.start()]
                items = [m.group(1).strip()
                         for m in _LIST_ITEM_RE.finditer(body)]
            return items
    return []


def parse(text: str, *, source_path: Path | None = None) -> Permissions:
    """Parse a permissions.md file into a Permissions object."""
    p = Permissions(source_path=source_path)

    p.allowed_skills = set(_parse_section(text, "allowed_skills"))
    p.denied_skills = set(_parse_section(text, "denied_skills"))

    paths_raw = _parse_section(text, "forbidden_paths")
    for raw in paths_raw:
        try:
            p.forbidden_paths.append(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            continue

    rates_raw = _parse_section(text, "rate_limits")
    for raw in rates_raw:
        # lines like "http_get: 30/min"
        m = _RATE_RE.match(raw.strip())
        if m:
            name, count, unit = m.group(1), int(m.group(2)), m.group(3)
            window_s = _WINDOWS.get(unit, 60)
            p.rate_limits[name] = (count, window_s)

    return p


def load(projects_dir: Path | None = None) -> Permissions:
    """Load permissions.md from $PROJECTS_DIR. Empty default if absent."""
    if projects_dir is None:
        try:
            from mnemosyne_config import default_projects_dir
            projects_dir = default_projects_dir()
        except ImportError:
            import os
            raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
            projects_dir = (Path(raw).expanduser().resolve()
                            if raw else Path.home() / "projects" / "mnemosyne")
    target = projects_dir / "permissions.md"
    if not target.is_file():
        return Permissions(source_path=target)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Permissions(source_path=target)
    return parse(text, source_path=target)


# ---- rate limiter ----------------------------------------------------------

class _RollingRateLimiter:
    """In-memory sliding-window rate limiter keyed on skill name.

    One instance per Brain/session. Tracks timestamps per skill and
    rejects when (count, window_s) exceeds the per-skill budget.
    """
    def __init__(self) -> None:
        self._stamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, name: str, count: int, window_s: int) -> tuple[bool, str]:
        """Return (allowed, reason). Records the call if allowed."""
        now = time.monotonic()
        cutoff = now - window_s
        with self._lock:
            stamps = self._stamps.setdefault(name, [])
            # Prune old
            while stamps and stamps[0] < cutoff:
                stamps.pop(0)
            if len(stamps) >= count:
                remaining = int(stamps[0] + window_s - now)
                return False, (f"rate limit for {name!r}: {count}/{window_s}s "
                               f"exceeded; retry in ~{max(1, remaining)}s")
            stamps.append(now)
            return True, ""


# ---- example template ------------------------------------------------------

EXAMPLE = """\
# Mnemosyne permissions

This file controls what Mnemosyne can and cannot do on your machine.
Edit freely; the agent re-reads it every first turn of a new session.

## allowed_skills
# If any skills are listed here, ONLY these can be invoked (allow-list mode).
# Comment out this section to allow every registered skill by default.
# - fs_read
# - fs_list
# - grep_code
# - git_status
# - git_log
# - datetime_now
# - http_get
# - web_fetch_text
# - sqlite_query

## denied_skills
# Skills listed here are ALWAYS blocked, even if they'd otherwise be allowed.
# - shell_exec_safe     # uncomment to block shell exec entirely
# - fs_write_safe       # uncomment to run fully read-only

## forbidden_paths
# Absolute paths the agent may never read from or write to.
# Prefix match: a listed path protects itself and everything under it.
- ~/.ssh
- ~/.aws
- ~/.config/gh
- ~/.gnupg
- /etc/shadow

## rate_limits
# Hard caps per skill, per time window. Units: sec / min / hour.
- http_get: 60/min
- web_fetch_text: 30/min
- shell_exec_safe: 20/min

## notes
Keep this file in version control alongside AGENTS.md and IDENTITY.md.
Every change is a deliberate choice you want auditable later.
"""


def write_example(projects_dir: Path | None = None,
                    *, overwrite: bool = False) -> Path:
    """Write the example permissions.md to the projects dir. Never
    overwrites by default — this is user data."""
    pd = projects_dir
    if pd is None:
        try:
            from mnemosyne_config import default_projects_dir
            pd = default_projects_dir()
        except ImportError:
            import os
            raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
            pd = (Path(raw).expanduser().resolve()
                  if raw else Path.home() / "projects" / "mnemosyne")
    pd.mkdir(parents=True, exist_ok=True)
    target = pd / "permissions.md"
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists; pass overwrite=True")
    target.write_text(EXAMPLE, encoding="utf-8")
    return target
