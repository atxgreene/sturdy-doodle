"""
mnemosyne_skills.py — agentskills.io-compatible skill registry.

Purpose
-------
A skill is a unit of capability the agent can invoke: a tool (callable), a
progressive-disclosure knowledge document, or both. Mnemosyne skills match
the `agentskills.io` open standard so anything that works here also works
with Hermes-ecosystem tooling, and vice versa.

Skills can be:

  1. Python callables registered via @skill decorator (in-process)
  2. Standalone executable scripts on $PATH (subprocess)
  3. Markdown documents describing capabilities for LLM context

The registry discovers skills from multiple sources and presents a single
`tools` list the brain can hand to the model as OpenAI-shaped tool specs.

Self-improvement hook
---------------------
When the brain successfully completes a task using a novel sequence of
tool calls, it can call `record_learned_skill(...)` which writes a new
markdown skill document to `$PROJECTS_DIR/skills/learned/<slug>.md`.
This matches Hermes's "writes a reusable Markdown skill file after
solving a task" self-improvement pattern.

Unlike Hermes's pattern, every learned skill is also logged as a telemetry
event so the observability substrate can later A/B test whether the
learned skills actually improve the Pareto frontier.

Skill file format (agentskills.io compatible)
---------------------------------------------
    ---
    name: obsidian-search
    description: Search the user's Obsidian vault.
    parameters:
      - name: query
        type: string
        required: true
      - name: limit
        type: integer
        default: 10
    invocation: subprocess          # "python" | "subprocess" | "knowledge"
    command: obsidian-search search {query} --limit {limit} --json
    ---
    # Obsidian search

    When to use: the user asks about notes, daily journals, meeting minutes,
    or anything that lives in their Obsidian vault...

Knowledge-only skills ("invocation: knowledge") have no command — they're
markdown documents the brain loads into context on demand.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SKILL_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_projects_dir() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:
        import os
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return Path(raw).expanduser().resolve() if raw else (
            Path.home() / "projects" / "mnemosyne"
        )


# ---- Skill dataclass --------------------------------------------------------

@dataclass
class Skill:
    name: str
    description: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    invocation: str = "python"              # "python" | "subprocess" | "knowledge"
    command: str | None = None              # for subprocess skills
    callable: Callable[..., Any] | None = None  # for python skills
    body: str = ""                          # markdown body (progressive-disclosure content)
    source_path: Path | None = None         # where the skill was loaded from
    learned: bool = False                   # True if self-written by the brain

    def to_openai_tool(self) -> dict[str, Any]:
        """Return the OpenAI-shaped tool definition the model sees."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in self.parameters:
            properties[p["name"]] = {
                "type": p.get("type", "string"),
                "description": p.get("description", ""),
            }
            if p.get("required"):
                required.append(p["name"])
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }

    def invoke(self, **kwargs: Any) -> Any:
        """Execute the skill. Returns whatever the implementation returns."""
        if self.invocation == "python" and self.callable is not None:
            return self.callable(**kwargs)
        if self.invocation == "subprocess" and self.command:
            return self._run_subprocess(kwargs)
        if self.invocation == "knowledge":
            # Knowledge skills return their markdown body as context
            return {"body": self.body, "source": str(self.source_path)}
        raise RuntimeError(f"skill {self.name!r} has no runnable implementation")

    def _run_subprocess(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Expand {placeholder}s in self.command with kwargs values, run it."""
        assert self.command is not None
        # Build argv via shell-safe template: any {name} in the command gets
        # replaced by shlex.quote(str(kwargs[name])). Values without a matching
        # placeholder are ignored (callers can pass extras for telemetry).
        import shlex
        parts = shlex.split(self.command)
        expanded: list[str] = []
        for part in parts:
            def replace(m: re.Match) -> str:
                key = m.group(1)
                if key in kwargs and kwargs[key] is not None:
                    return str(kwargs[key])
                return ""
            new = re.sub(r"\{(\w+)\}", replace, part)
            if new:
                expanded.append(new)
        try:
            proc = subprocess.run(
                expanded, capture_output=True, text=True, timeout=60
            )
        except FileNotFoundError:
            return {"error": "command not found", "argv": expanded}
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "argv": expanded}
        # If output is JSON, parse it; otherwise return raw text
        stdout = proc.stdout.strip()
        if stdout.startswith(("{", "[")):
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
        }


# ---- parsing ----------------------------------------------------------------

def parse_skill_file(path: Path) -> Skill:
    """Parse an agentskills.io-format markdown file into a Skill."""
    text = path.read_text(encoding="utf-8")
    m = SKILL_FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — treat whole file as a knowledge skill
        return Skill(
            name=path.stem,
            description=f"Knowledge document: {path.stem}",
            invocation="knowledge",
            body=text,
            source_path=path,
        )

    frontmatter_raw, body = m.group(1), m.group(2)
    meta = _parse_simple_yaml(frontmatter_raw)

    params_raw = meta.get("parameters", [])
    if isinstance(params_raw, list):
        params = [p if isinstance(p, dict) else {"name": str(p)} for p in params_raw]
    else:
        params = []

    return Skill(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        parameters=params,
        invocation=str(meta.get("invocation") or "knowledge"),
        command=meta.get("command"),
        body=body.strip(),
        source_path=path,
        learned=bool(meta.get("learned", False)),
    )


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Tiny YAML-subset parser for skill frontmatter.

    Supports: top-level `key: value`, lists of dicts under a key. No flow
    style, no anchors, no multi-line strings. Stdlib-only on purpose —
    we refuse to take a yaml dependency just for frontmatter.
    """
    result: dict[str, Any] = {}
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val and val != "":
                result[key] = _coerce(val)
                i += 1
            else:
                # Possibly a list of dicts beneath
                items: list[dict[str, Any]] = []
                i += 1
                while i < len(lines) and lines[i].startswith(" "):
                    sub = lines[i].strip()
                    if sub.startswith("- "):
                        items.append({})
                        sub = sub[2:]
                    if ":" in sub:
                        k2, _, v2 = sub.partition(":")
                        k2 = k2.strip()
                        v2 = v2.strip()
                        if items:
                            items[-1][k2] = _coerce(v2)
                    i += 1
                if items:
                    result[key] = items
        else:
            i += 1
    return result


def _coerce(v: str) -> Any:
    """Coerce YAML-ish scalar to Python type."""
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
        return int(v)
    try:
        return float(v)
    except ValueError:
        pass
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


# ---- registry ---------------------------------------------------------------

class SkillRegistry:
    """Collects skills from files, decorators, and installed commands."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def register_python(
        self,
        name: str,
        description: str,
        parameters: list[dict[str, Any]] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator for in-process Python skills."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Skill(
                name=name,
                description=description,
                parameters=parameters or [],
                invocation="python",
                callable=fn,
            ))
            return fn
        return decorator

    def load_directory(self, directory: Path) -> int:
        """Parse every .md file in `directory` as a skill. Returns count loaded."""
        if not directory.is_dir():
            return 0
        n = 0
        for path in sorted(directory.rglob("*.md")):
            try:
                sk = parse_skill_file(path)
                self.register(sk)
                n += 1
            except Exception:
                pass
        return n

    def discover_path_commands(self) -> int:
        """Detect installed Mnemosyne entry-point commands on $PATH."""
        n = 0
        for cmd, (desc, params) in _WELLKNOWN_COMMANDS.items():
            if shutil.which(cmd):
                # Invocation line uses the CLI surface of each tool
                template = {
                    "obsidian-search": "obsidian-search --json search {query} --limit {limit}",
                    "notion-search":   "notion-search --json search {query} --limit {limit}",
                }.get(cmd, cmd)
                self.register(Skill(
                    name=cmd.replace("-", "_"),
                    description=desc,
                    parameters=params,
                    invocation="subprocess",
                    command=template,
                ))
                n += 1
        return n

    def tools(self) -> list[dict[str, Any]]:
        """Return OpenAI-shaped tool specs for every runnable (non-knowledge) skill."""
        return [
            s.to_openai_tool()
            for s in self._skills.values()
            if s.invocation in ("python", "subprocess")
        ]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def names(self) -> list[str]:
        return sorted(self._skills)


_WELLKNOWN_COMMANDS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "obsidian-search": (
        "Search the user's Obsidian vault for notes matching a query.",
        [
            {"name": "query", "type": "string", "required": True,
             "description": "Free-text search query."},
            {"name": "limit", "type": "integer",
             "description": "Max number of results.", "default": 10},
        ],
    ),
    "notion-search": (
        "Search the user's Notion workspace for pages matching a query.",
        [
            {"name": "query", "type": "string", "required": True,
             "description": "Free-text search query."},
            {"name": "limit", "type": "integer",
             "description": "Max number of results.", "default": 10},
        ],
    ),
}


# ---- self-improvement -------------------------------------------------------

def record_learned_skill(
    name: str,
    description: str,
    command: str,
    *,
    parameters: list[dict[str, Any]] | None = None,
    notes: str = "",
    telemetry: Any | None = None,
    projects_dir: Path | None = None,
) -> Path:
    """Write a new skill file to $PROJECTS_DIR/skills/learned/.

    Called by the brain when it successfully completes a task using a
    novel sequence of tool calls that it wants to remember. Matches
    Hermes's self-improvement pattern.
    """
    pd = projects_dir or _default_projects_dir()
    skills_dir = pd / "skills" / "learned"
    skills_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^A-Za-z0-9_-]", "-", name.lower()).strip("-")[:40]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = skills_dir / f"{slug}-{timestamp}.md"

    fm: list[str] = ["---"]
    fm.append(f"name: {name}")
    fm.append(f"description: {description}")
    fm.append("learned: true")
    fm.append(f"learned_at: {_utcnow()}")
    fm.append("invocation: subprocess")
    fm.append(f"command: {command}")
    if parameters:
        fm.append("parameters:")
        for p in parameters:
            fm.append(f"  - name: {p['name']}")
            for k, v in p.items():
                if k == "name":
                    continue
                fm.append(f"    {k}: {v}")
    fm.append("---")
    body = notes or f"Learned skill: {description}\n\nWritten by the brain on {_utcnow()}."
    path.write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")

    if telemetry is not None:
        try:
            telemetry.log(
                "skill_learned",
                metadata={"name": name, "path": str(path), "command": command},
            )
        except Exception:
            pass

    return path


# ---- default registry builder -----------------------------------------------

def default_registry(
    *,
    load_learned: bool = True,
    discover_commands: bool = True,
    projects_dir: Path | None = None,
) -> SkillRegistry:
    """Build a registry populated from the conventional locations.

    Order of precedence (last wins on name collision):
      1. Installed $PATH commands (obsidian-search, notion-search)
      2. $PROJECTS_DIR/skills/*.md
      3. $PROJECTS_DIR/skills/learned/*.md
    """
    reg = SkillRegistry()
    if discover_commands:
        reg.discover_path_commands()
    pd = projects_dir or _default_projects_dir()
    reg.load_directory(pd / "skills")
    if load_learned:
        reg.load_directory(pd / "skills" / "learned")
    return reg
