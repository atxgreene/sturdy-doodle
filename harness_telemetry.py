"""
harness_telemetry.py — observability + experiment bookkeeping for Mnemosyne.

Purpose
-------
Provides the substrate a Meta-Harness-style optimizer would need to observe
and compare agent runs. Inspired by the Stanford Meta-Harness paper
(Khattab et al., 2026) and its core insight that *compressing feedback loses
the information an optimizer needs*. This module never summarizes.

It provides:

  1. An experiments directory convention under $PROJECTS_DIR/experiments/,
     structured so the whole history of runs is navigable with plain
     `grep`, `cat`, `ls`, and `find`:

        experiments/
          latest -> run_<id>/      (symlink to most recent, best effort)
          run_<YYYYMMDD-HHMMSS>-<slug>/
            metadata.json          # run-level info (model, tags, status)
            results.json           # final metrics (written at finalize)
            events.jsonl           # append-only event log (one JSON/line)
            harness/               # optional: frozen code snapshot
            notes.md               # optional: free-form

  2. A TelemetrySession class that writes an event per tool call,
     prompt, response, error, or arbitrary state change — no summarization,
     no batching, raw args + raw results. Secrets are redacted by key name
     at write time.

  3. A `trace` decorator that turns any callable into an instrumented one
     for the lifetime of a session.

  4. Module functions `create_run`, `finalize_run`, `list_runs`, `get_run`,
     `run_path` for driving the experiments directory from other scripts
     (notably the `mnemosyne-experiments` CLI).

Stdlib only. Python 3.9+. Safe to `import harness_telemetry` from any script
in this repo; `mnemosyne-experiments.py` is one such caller.

Design notes vs Meta-Harness
----------------------------
- The paper uses one JSON-ish file per candidate plus traces per run. This
  module uses one directory per run with append-only JSONL for events —
  functionally equivalent, marginally friendlier to `cat` and `tail -f`.

- The paper's optimizer runs *inside* the filesystem via grep/cat. This
  module does not ship an optimizer — it ships the substrate. The
  `mnemosyne-experiments` CLI gives you (and any future proposer agent)
  human-scale access: list, show, top-k, pareto, diff, events.

- For a local-first agent like Mnemosyne, token cost is effectively zero
  (local Ollama). We substitute latency_ms_avg as the second Pareto axis.
  You can register arbitrary metrics; accuracy × latency is just the
  recommended default.

Security
--------
- Secrets are redacted by key name before being written to disk. Default
  patterns cover token / secret / api_key / password / bearer / credential
  / signing_key (case-insensitive, substring). Override via `redact_patterns`.
- Redaction is key-based, not value-based: a token embedded in a free-text
  field under an innocent key name (e.g. `response="Your token is xoxb-…"`)
  will NOT be caught. Audit your call sites if this matters.
- Events are written with the file's default umask. If you want strict
  600 across the whole tree, create the run dir yourself with umask 077
  before calling into this module.

Usage
-----
    import harness_telemetry as ht

    run_id = ht.create_run(model="qwen3:8b", tags=["baseline"],
                           notes="first ICMS run after wizard")
    with ht.TelemetrySession(run_id) as sess:

        @sess.trace
        def obsidian_search(query, limit=10):
            return run_the_actual_search(query, limit)

        for q in scenario_queries:
            obsidian_search(q)

    ht.finalize_run(run_id, metrics={
        "accuracy": 0.82,
        "latency_ms_avg": 1250.5,
        "turns_successful": 34,
    })
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


# ---- constants ---------------------------------------------------------------

DEFAULT_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"token", re.I),
    re.compile(r"secret", re.I),
    re.compile(r"api_?key", re.I),
    re.compile(r"password", re.I),
    re.compile(r"passwd", re.I),
    re.compile(r"bearer", re.I),
    re.compile(r"credential", re.I),
    re.compile(r"signing[_-]?(key|secret)", re.I),
]

REDACTED = "<redacted>"


# ---- small utilities ---------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def default_projects_dir() -> Path:
    """Resolve $MNEMOSYNE_PROJECTS_DIR or ~/projects/mnemosyne.

    Re-exported from mnemosyne_config for backward compat — code that
    already does `from harness_telemetry import default_projects_dir` keeps
    working. New code should import from mnemosyne_config directly.
    """
    try:
        from mnemosyne_config import default_projects_dir as _dpd
        return _dpd()
    except ImportError:
        # Fallback if mnemosyne_config not on path (e.g. standalone use)
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return (Path.home() / "projects" / "mnemosyne").resolve()


def _experiments_root(projects_dir: Path | None = None) -> Path:
    return (projects_dir or default_projects_dir()) / "experiments"


def _get_git_sha(start: Path) -> str | None:
    """Best-effort current git SHA for the repo containing `start`."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ---- redaction ---------------------------------------------------------------

def _should_redact(key: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(key) for p in patterns)


def _redact(obj: Any, patterns: list[re.Pattern[str]]) -> Any:
    """Recursively replace values at redactable keys with REDACTED.

    - dict values: redact if key matches any pattern.
    - list / tuple: recurse element-wise, no key context.
    - scalars: return unchanged.

    Does NOT scan string values for embedded secrets. See module docstring.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _should_redact(k, patterns):
                out[k] = REDACTED
            else:
                out[k] = _redact(v, patterns)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact(item, patterns) for item in obj]
    return obj


# ---- experiments directory helpers ------------------------------------------

def run_path(run_id: str, projects_dir: Path | None = None) -> Path:
    """Return the absolute path to a run directory (may not exist)."""
    return _experiments_root(projects_dir) / run_id


def create_run(
    model: str,
    notes: str | None = None,
    tags: Iterable[str] | None = None,
    freeze_files: Iterable[str | Path] | None = None,
    projects_dir: str | Path | None = None,
    slug: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """Create a new experiments/run_<id>/ directory and return the run_id.

    Parameters
    ----------
    model : str
        The LLM the harness runs against (e.g. "qwen3:8b", "gemma4:e4b").
    notes : str, optional
        Free-form notes written to metadata.json. Also copied to notes.md.
    tags : iterable of str, optional
        Tags for grouping / filtering in the CLI.
    freeze_files : iterable of paths, optional
        Files to snapshot into harness/ under the run dir. Typical use is
        the scripts that define the harness's deployment shape at the
        moment of the run (install-mnemosyne.sh, mnemosyne-wizard.sh, etc.)
    projects_dir : path, optional
        Override the default projects directory.
    slug : str, optional
        Short human-friendly suffix for the run_id. A 6-char hex suffix is
        used if omitted.
    extra_metadata : dict, optional
        Extra fields merged into metadata.json.
    """
    pd = Path(projects_dir).expanduser().resolve() if projects_dir else default_projects_dir()
    experiments = _experiments_root(pd)
    experiments.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = slug or uuid.uuid4().hex[:6]
    # Sanitize slug: alphanumerics and dashes only
    slug = re.sub(r"[^A-Za-z0-9_-]", "", slug) or "run"
    run_id = f"run_{ts}-{slug}"
    run_dir = experiments / run_id

    # Retry with a fresh suffix if the directory already exists (unlikely)
    attempt = 0
    while run_dir.exists():
        attempt += 1
        if attempt > 5:
            raise RuntimeError(f"could not find a free run_id under {experiments}")
        run_id = f"run_{ts}-{slug}-{attempt}"
        run_dir = experiments / run_id

    run_dir.mkdir()
    (run_dir / "events.jsonl").touch()

    metadata: dict[str, Any] = {
        "run_id": run_id,
        "started_utc": _utcnow_iso(),
        "ended_utc": None,
        "status": "running",
        "model": model,
        "harness_version": _get_git_sha(pd) or "unknown",
        "projects_dir": str(pd),
        "notes": notes,
        "tags": list(tags or []),
        "events_recorded": 0,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    _write_json(run_dir / "metadata.json", metadata)

    if notes:
        (run_dir / "notes.md").write_text(notes + "\n", encoding="utf-8")

    if freeze_files:
        harness_dir = run_dir / "harness"
        harness_dir.mkdir()
        for fspec in freeze_files:
            src = Path(fspec)
            if src.is_file():
                (harness_dir / src.name).write_bytes(src.read_bytes())

    # Update "latest" symlink (best effort — may fail on filesystems
    # without symlink support, which is fine).
    latest = experiments / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_id)
    except OSError:
        pass

    return run_id


def update_run_metadata(
    run_id: str,
    patch: dict[str, Any],
    projects_dir: str | Path | None = None,
) -> None:
    """Merge `patch` into a run's metadata.json."""
    rd = run_path(run_id, Path(projects_dir) if projects_dir else None)
    meta_file = rd / "metadata.json"
    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    metadata.update(patch)
    _write_json(meta_file, metadata)


def finalize_run(
    run_id: str,
    metrics: dict[str, Any],
    projects_dir: str | Path | None = None,
) -> None:
    """Mark a run as completed and write its final metrics.

    `metrics` is a free-form dict. The convention for the Pareto frontier
    analysis is to include at least:
        accuracy        (higher = better)
        latency_ms_avg  (lower = better)
    But any numeric key can be ranked by the `mnemosyne-experiments` CLI.
    """
    rd = run_path(run_id, Path(projects_dir) if projects_dir else None)
    meta_file = rd / "metadata.json"
    if not meta_file.exists():
        raise FileNotFoundError(f"run not found: {rd}")

    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    metadata["ended_utc"] = _utcnow_iso()
    metadata["status"] = "completed"

    events_file = rd / "events.jsonl"
    if events_file.exists():
        with events_file.open(encoding="utf-8") as f:
            metadata["events_recorded"] = sum(1 for line in f if line.strip())

    _write_json(meta_file, metadata)
    _write_json(rd / "results.json", {"run_id": run_id, "metrics": metrics})


def mark_run_failed(
    run_id: str,
    error: str,
    projects_dir: str | Path | None = None,
) -> None:
    """Mark a run as failed without writing final metrics."""
    update_run_metadata(
        run_id,
        {"status": "failed", "ended_utc": _utcnow_iso(), "error": error},
        projects_dir=projects_dir,
    )


def list_runs(projects_dir: str | Path | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (run_id, metadata) pairs sorted by run_id descending."""
    pd = Path(projects_dir) if projects_dir else None
    experiments = _experiments_root(pd)
    if not experiments.exists():
        return
    for rd in sorted(experiments.iterdir(), reverse=True):
        if not rd.is_dir() or not rd.name.startswith("run_"):
            continue
        meta_file = rd / "metadata.json"
        if not meta_file.exists():
            continue
        try:
            metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        yield rd.name, metadata


def get_run(
    run_id: str,
    projects_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a dict with metadata, results, and event_count for one run."""
    rd = run_path(run_id, Path(projects_dir) if projects_dir else None)
    if not rd.is_dir():
        raise FileNotFoundError(f"run not found: {rd}")

    metadata = json.loads((rd / "metadata.json").read_text(encoding="utf-8"))
    results_file = rd / "results.json"
    results = (
        json.loads(results_file.read_text(encoding="utf-8"))
        if results_file.exists()
        else None
    )
    events_file = rd / "events.jsonl"
    event_count = 0
    if events_file.exists():
        with events_file.open(encoding="utf-8") as f:
            event_count = sum(1 for line in f if line.strip())

    return {
        "run_id": run_id,
        "path": str(rd),
        "metadata": metadata,
        "results": results,
        "event_count": event_count,
    }


def _write_json(path: Path, data: Any) -> None:
    """Write JSON with a trailing newline, pretty-printed."""
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


# ---- telemetry session -------------------------------------------------------

class TelemetrySession:
    """A run-scoped sink for events. Thread-safe."""

    def __init__(
        self,
        run_id: str,
        projects_dir: str | Path | None = None,
        redact_patterns: list[re.Pattern[str]] | None = None,
    ) -> None:
        self.run_id = run_id
        self.projects_dir = (
            Path(projects_dir).expanduser().resolve()
            if projects_dir
            else default_projects_dir()
        )
        self.run_dir = run_path(run_id, self.projects_dir)
        self.events_file = self.run_dir / "events.jsonl"
        self.redact_patterns = redact_patterns or DEFAULT_REDACT_PATTERNS
        self._event_counter = 0
        self._lock = threading.Lock()

        if not self.run_dir.is_dir():
            raise FileNotFoundError(
                f"run not found: {self.run_dir}. "
                f"Create it with harness_telemetry.create_run() first."
            )

    # ---- event writing -------------------------------------------------------

    def _next_event_id(self) -> str:
        with self._lock:
            self._event_counter += 1
            return f"evt_{self._event_counter:06d}"

    def log(
        self,
        event_type: str,
        tool: str | None = None,
        args: Any = None,
        result: Any = None,
        duration_ms: float | None = None,
        status: str = "ok",
        error: dict[str, Any] | None = None,
        parent_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Append a single event to events.jsonl. Returns the event_id.

        event_type is free-form but common values are:
          "tool_call"   — tool invocation (use `tool`, `args`, `result`)
          "prompt"      — a prompt sent to the LLM
          "response"    — a raw LLM response
          "state_change"— harness-level state mutation
          "session_start" / "session_end" / "session_error"
        """
        event_id = self._next_event_id()
        record = {
            "event_id": event_id,
            "run_id": self.run_id,
            "timestamp_utc": _utcnow_iso(),
            "event_type": event_type,
            "tool": tool,
            "args": _redact(args, self.redact_patterns) if args is not None else None,
            "result": _redact(result, self.redact_patterns) if result is not None else None,
            "duration_ms": duration_ms,
            "status": status,
            "error": error,
            "parent_event_id": parent_event_id,
            "metadata": metadata or {},
        }
        line = json.dumps(record, default=str)
        with self._lock:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        return event_id

    # ---- instrumentation helpers --------------------------------------------

    def trace(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator: log a tool_call event around each call to `fn`.

        Args are logged verbatim (subject to redaction). If the wrapped
        callable raises, the exception is logged and re-raised.
        """
        session = self

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            arg_record = {"positional": list(args), "kwargs": kwargs}
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                duration_ms = (time.monotonic() - start) * 1000.0
                session.log(
                    "tool_call",
                    tool=fn.__name__,
                    args=arg_record,
                    result=None,
                    duration_ms=duration_ms,
                    status="error",
                    error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
                raise
            duration_ms = (time.monotonic() - start) * 1000.0
            session.log(
                "tool_call",
                tool=fn.__name__,
                args=arg_record,
                result={"value": result},
                duration_ms=duration_ms,
                status="ok",
            )
            return result

        return wrapper

    # ---- context-manager lifecycle ------------------------------------------

    def __enter__(self) -> "TelemetrySession":
        self.log("session_start", metadata={"pid": os.getpid()})
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.log("session_end")
        else:
            self.log(
                "session_error",
                status="error",
                error={"type": exc_type.__name__, "message": str(exc_val)},
            )
        return False  # never suppress


# ---- tiny CLI for smoke tests ------------------------------------------------

def _main_smoke(argv: list[str] | None = None) -> int:
    """Minimal CLI: create, log, finalize, inspect.

    Intended for developer smoke testing only. The user-facing CLI is
    `mnemosyne-experiments.py` which lives in the same directory.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="harness_telemetry",
        description="Smoke-test CLI for harness_telemetry (internal use).",
    )
    p.add_argument("--projects-dir", help="override MNEMOSYNE_PROJECTS_DIR")
    sub = p.add_subparsers(dest="cmd", required=True)

    np_ = sub.add_parser("new-run", help="create a new run")
    np_.add_argument("--model", required=True)
    np_.add_argument("--notes")
    np_.add_argument("--tag", action="append", dest="tags")
    np_.add_argument("--slug")

    lp = sub.add_parser("log", help="append an event to an existing run")
    lp.add_argument("run_id")
    lp.add_argument("--tool", required=True)
    lp.add_argument("--event-type", default="tool_call")
    lp.add_argument("--args", help="JSON-encoded args dict")
    lp.add_argument("--result", help="JSON-encoded result")
    lp.add_argument("--status", default="ok")
    lp.add_argument("--duration-ms", type=float)

    fp = sub.add_parser("finalize", help="finalize a run with metrics")
    fp.add_argument("run_id")
    fp.add_argument("--metrics", required=True,
                    help="JSON-encoded metrics dict (e.g. '{\"accuracy\":0.8}')")

    args = p.parse_args(argv)
    pd = args.projects_dir

    if args.cmd == "new-run":
        run_id = create_run(
            model=args.model,
            notes=args.notes,
            tags=args.tags,
            slug=args.slug,
            projects_dir=pd,
        )
        print(run_id)
        return 0

    if args.cmd == "log":
        sess = TelemetrySession(args.run_id, projects_dir=pd)
        sess.log(
            args.event_type,
            tool=args.tool,
            args=json.loads(args.args) if args.args else None,
            result=json.loads(args.result) if args.result else None,
            status=args.status,
            duration_ms=args.duration_ms,
        )
        return 0

    if args.cmd == "finalize":
        finalize_run(
            args.run_id,
            metrics=json.loads(args.metrics),
            projects_dir=pd,
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main_smoke())
