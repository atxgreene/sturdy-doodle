"""
mnemosyne_batch.py — parallel prompt → trajectory runner.

Purpose
-------
`harness_sweep` varies *config parameters* (model, temperature) across
one input. `mnemosyne_batch` is the orthogonal piece: vary *prompts*
across one config. The output is a single experiments run whose
`events.jsonl` contains a `training_turn` event per prompt — directly
consumable by `mnemosyne-train export`.

Use case: "I have 1000 prompts in `prompts.jsonl`. Run them through
the current model, capture every turn for fine-tuning, and tell me
which ones errored." That's a ~50-line bash script today; this
module makes it a one-line CLI.

Inspired by NousResearch/hermes-agent's `batch_runner.py` (MIT). We
re-implement narrowly using our existing Brain + telemetry instead of
porting their agent loop wholesale.

Design
------
- ThreadPoolExecutor — chat I/O is the bottleneck, not CPU.
- One TelemetrySession per run. Each prompt's turn becomes a logical
  child via parent_event_id, so the existing `mnemosyne-train export`
  walks the tree without changes.
- Resumable: pass `--resume` to skip prompts whose `id` already
  appears as a `training_turn` in the current run's events.jsonl.
- Bounded retries on transient errors (HTTP 429/5xx, timeouts).
- Progress: tqdm-style line on stderr, printed once per N prompts.
- Failures don't abort the batch unless `--stop-on-error`.

Input format
------------
JSONL, one prompt per line. Either a string:

    "What is the capital of France?"

or a dict:

    {"id": "geo-001", "prompt": "What is the capital of France?",
     "tags": ["geo", "easy"], "metadata": {"category": "facts"}}

The CLI accepts both shapes per line; missing IDs get auto-assigned.

CLI
---
    mnemosyne-batch run prompts.jsonl \\
        --workers 4 \\
        --backend ollama --model qwen3:8b \\
        --tags batch,training-data \\
        --out-run-id custom-run-name

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---- prompt loading --------------------------------------------------------

@dataclass
class Prompt:
    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def load_prompts(path: Path) -> list[Prompt]:
    """JSONL with one prompt per line. String or dict, comments ok."""
    out: list[Prompt] = []
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, str):
                out.append(Prompt(id=f"p-{i:06d}", text=obj))
            elif isinstance(obj, dict):
                text = obj.get("prompt") or obj.get("text") or ""
                if not text:
                    continue
                out.append(Prompt(
                    id=str(obj.get("id") or f"p-{i:06d}"),
                    text=text,
                    tags=list(obj.get("tags") or []),
                    metadata=dict(obj.get("metadata") or {}),
                ))
    return out


def load_completed_ids(events_file: Path) -> set[str]:
    """Walk a run's events.jsonl, return the set of prompt IDs that
    completed successfully.

    The brain attaches `metadata={"prompt_id": ...}` to its `turn_start`
    event (via the `metadata` arg we pass to `brain.turn()`). We pair
    each turn_start with its turn_end (matched by parent_event_id) and
    only count IDs whose turn ended status='ok'.
    """
    done: set[str] = set()
    if not events_file.exists():
        return done
    starts: dict[str, str] = {}    # event_id → prompt_id
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = e.get("event_type")
            if et == "turn_start":
                pid = (e.get("metadata") or {}).get("prompt_id")
                eid = e.get("event_id")
                if pid and eid:
                    starts[eid] = str(pid)
            elif et == "turn_end":
                if e.get("status") != "ok":
                    continue
                parent = e.get("parent_event_id")
                pid = starts.get(parent or "")
                if pid:
                    done.add(pid)
    return done


# ---- batch runner ----------------------------------------------------------

@dataclass
class BatchSummary:
    run_id: str
    prompts_total: int
    prompts_completed: int
    prompts_failed: int
    prompts_skipped_resume: int
    duration_s: float
    errors: list[dict[str, Any]] = field(default_factory=list)


def _retryable(exc: Exception) -> bool:
    msg = (str(exc) or "").lower()
    return any(s in msg for s in (
        "timeout", "timed out", "503", "502", "429",
        "connection reset", "connection refused",
    ))


def _run_one(
    prompt: Prompt,
    *,
    brain_factory: Callable[[Any], Any],
    session: Any,
    max_retries: int,
    retry_backoff_s: float,
) -> tuple[Prompt, dict[str, Any] | None, dict[str, Any] | None]:
    """Run one prompt through a brain. Returns (prompt, resp_dict, error_dict)."""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            brain = brain_factory(session)
            metadata = {
                "tags": prompt.tags + ["batch"],
                "prompt_id": prompt.id,
                **prompt.metadata,
            }
            resp = brain.turn(prompt.text, metadata=metadata)
            if resp.error:
                # Treat brain-reported error as retryable if it looks transient
                last_err = RuntimeError(
                    f"{resp.error.get('type')}: {resp.error.get('message')}"
                )
                if attempt < max_retries and _retryable(last_err):
                    time.sleep(retry_backoff_s * (2 ** attempt))
                    continue
                return prompt, None, resp.error
            return prompt, {
                "text": resp.text,
                "tool_calls": resp.tool_calls,
                "duration_ms": resp.duration_ms,
                "model": resp.model,
            }, None
        except Exception as e:
            last_err = e
            if attempt < max_retries and _retryable(e):
                time.sleep(retry_backoff_s * (2 ** attempt))
                continue
            return prompt, None, {"type": type(e).__name__, "message": str(e)}
    return prompt, None, {"type": "MaxRetries",
                            "message": str(last_err) if last_err else ""}


def run_batch(
    prompts: list[Prompt],
    *,
    brain_factory: Callable[[Any], Any],
    workers: int = 4,
    projects_dir: Path | None = None,
    tags: list[str] | None = None,
    notes: str = "",
    model_label: str = "batch",
    resume: bool = False,
    max_retries: int = 2,
    retry_backoff_s: float = 1.0,
    progress_every: int = 10,
    stop_on_error: bool = False,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> BatchSummary:
    """Run `prompts` through `brain_factory(session)` in parallel.

    `brain_factory(session)` is called per prompt; it must return a
    Brain (or anything with a `.turn(text, metadata=...)` method).
    Return a fresh Brain each time so per-prompt state doesn't leak.
    """
    import harness_telemetry as ht

    run_id = ht.create_run(
        model=model_label,
        tags=(tags or []) + ["batch"],
        projects_dir=projects_dir,
        notes=notes or f"mnemosyne-batch: {len(prompts)} prompts × {workers} workers",
    )
    rd = ht.run_path(run_id, projects_dir)
    events_file = rd / "events.jsonl"

    skipped_ids: set[str] = load_completed_ids(events_file) if resume else set()
    pending = [p for p in prompts if p.id not in skipped_ids]

    started = time.monotonic()
    summary = BatchSummary(
        run_id=run_id,
        prompts_total=len(prompts),
        prompts_completed=0,
        prompts_failed=0,
        prompts_skipped_resume=len(skipped_ids),
        duration_s=0.0,
    )

    progress_lock = threading.Lock()
    last_progress_print = 0

    def _maybe_progress(done: int, failed: int):
        nonlocal last_progress_print
        with progress_lock:
            should = done - last_progress_print >= progress_every
            if should:
                last_progress_print = done
        if should:
            sys.stderr.write(
                f"[batch] {done}/{len(pending)} done, {failed} failed\n"
            )
        if on_progress:
            on_progress(done, len(pending), failed)

    with ht.TelemetrySession(run_id, projects_dir=projects_dir) as session:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _run_one, p,
                    brain_factory=brain_factory, session=session,
                    max_retries=max_retries, retry_backoff_s=retry_backoff_s,
                )
                for p in pending
            ]
            for fut in as_completed(futures):
                prompt, _resp, err = fut.result()
                if err:
                    summary.prompts_failed += 1
                    summary.errors.append({
                        "prompt_id": prompt.id, "prompt": prompt.text[:120],
                        "error": err,
                    })
                    if stop_on_error:
                        for f in futures:
                            f.cancel()
                        break
                else:
                    summary.prompts_completed += 1
                _maybe_progress(
                    summary.prompts_completed + summary.prompts_failed,
                    summary.prompts_failed,
                )

    summary.duration_s = time.monotonic() - started
    ht.finalize_run(
        run_id,
        metrics={
            "prompts_total": summary.prompts_total,
            "prompts_completed": summary.prompts_completed,
            "prompts_failed": summary.prompts_failed,
            "prompts_skipped_resume": summary.prompts_skipped_resume,
            "duration_s": summary.duration_s,
            "throughput_per_sec": (summary.prompts_completed
                                     / summary.duration_s
                                     if summary.duration_s else 0.0),
        },
        projects_dir=projects_dir,
    )
    return summary


# ---- default brain factory --------------------------------------------------

def make_default_brain_factory(
    *, provider: str = "ollama",
    model: str = "qwen3:8b",
    capture_for_training: bool = True,
    inject_env_snapshot: bool = False,
    adapt_to_context: bool = False,
    skills_load_builtins: bool = False,
) -> Callable[[Any], Any]:
    """Return a `brain_factory(session)` that builds a fresh Brain with
    `capture_for_training=True` so trajectories land in events.jsonl."""
    def factory(session: Any) -> Any:
        import mnemosyne_brain as br
        import mnemosyne_memory as mm
        import mnemosyne_models as models
        import mnemosyne_skills as skills_mod

        backend = models.Backend(provider=provider, default_model=model)
        memory = mm.MemoryStore(telemetry=session)
        registry = (
            skills_mod.default_registry(load_builtins=skills_load_builtins,
                                          discover_commands=False,
                                          load_learned=False)
            if skills_load_builtins
            else skills_mod.SkillRegistry()
        )
        return br.Brain(
            backend=backend,
            memory=memory,
            skills=registry,
            telemetry=session,
            config=br.BrainConfig(
                backend=backend,
                inject_env_snapshot=inject_env_snapshot,
                adapt_to_context=adapt_to_context,
                capture_for_training=capture_for_training,
            ),
        )
    return factory


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-batch",
        description="Parallel prompt → trajectory runner. Reads JSONL "
                    "prompts, runs them through Mnemosyne's Brain, and "
                    "captures `training_turn` events for mnemosyne-train.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="run a batch of prompts")
    rp.add_argument("prompts", help="JSONL prompts file")
    rp.add_argument("--workers", type=int, default=4)
    rp.add_argument("--projects-dir")
    rp.add_argument("--provider", default="ollama")
    rp.add_argument("--model", default="qwen3:8b")
    rp.add_argument("--tags", default="",
                     help="comma-separated tags applied to the run")
    rp.add_argument("--notes", default="")
    rp.add_argument("--resume", action="store_true",
                     help="skip prompts already captured in the target run")
    rp.add_argument("--stop-on-error", action="store_true")
    rp.add_argument("--max-retries", type=int, default=2)
    rp.add_argument("--load-builtins", action="store_true",
                     help="load the 11 builtin skills into each brain")
    rp.add_argument("--json", action="store_true")

    cp = sub.add_parser("count", help="count prompts in a JSONL file")
    cp.add_argument("prompts")

    args = p.parse_args(argv)
    pd = Path(args.projects_dir).expanduser() \
        if getattr(args, "projects_dir", None) else None

    if args.cmd == "count":
        prompts = load_prompts(Path(args.prompts).expanduser())
        print(f"  {len(prompts)} prompts in {args.prompts}")
        return 0

    if args.cmd == "run":
        prompts = load_prompts(Path(args.prompts).expanduser())
        if not prompts:
            print(f"batch: no prompts loaded from {args.prompts}",
                   file=sys.stderr)
            return 1
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        factory = make_default_brain_factory(
            provider=args.provider, model=args.model,
            skills_load_builtins=args.load_builtins,
        )
        summary = run_batch(
            prompts,
            brain_factory=factory,
            workers=args.workers,
            projects_dir=pd,
            tags=tags,
            notes=args.notes,
            model_label=f"{args.provider}/{args.model}",
            resume=args.resume,
            max_retries=args.max_retries,
            stop_on_error=args.stop_on_error,
        )
        if args.json:
            json.dump(summary.__dict__, sys.stdout, indent=2, default=str)
            print()
            return 0
        print(f"batch: run_id={summary.run_id}")
        print(f"  total:     {summary.prompts_total}")
        print(f"  completed: {summary.prompts_completed}")
        print(f"  failed:    {summary.prompts_failed}")
        if summary.prompts_skipped_resume:
            print(f"  skipped (resume): {summary.prompts_skipped_resume}")
        print(f"  duration:  {summary.duration_s:.2f}s")
        if summary.errors:
            print("  first 3 errors:")
            for e in summary.errors[:3]:
                print(f"    [{e['prompt_id']}] {e['error'].get('type')}: "
                      f"{e['error'].get('message')[:80]}")
        return 0 if not summary.prompts_failed or not args.stop_on_error else 1

    return 2


_ = os  # keep import for sys.path / env probing scenarios

if __name__ == "__main__":
    sys.exit(_main())
