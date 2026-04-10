"""
harness_sweep.py — deterministic parameter-space sweep over Mnemosyne harnesses.

Purpose
-------
Provides the simplest useful layer on top of `harness_telemetry`: run the
same evaluator callable once per point in a Cartesian parameter space, with
a fresh TelemetrySession per run, and finalize each with metrics returned
by the evaluator. After the sweep, the user inspects results via the
`mnemosyne-experiments` CLI:

    mnemosyne-experiments pareto --axes accuracy,latency_ms_avg --directions max,min

This is NOT the Stanford Meta-Harness agentic proposer — no LLM rewrites code
here. It is the deterministic grid-search baseline: useful on its own, and
useful as the scaffolding a future agentic proposer would plug in to.

Usage
-----
    import harness_sweep as sweep
    from harness_telemetry import TelemetrySession

    def evaluate(params: dict, session: TelemetrySession) -> dict:
        # User-supplied: run the harness under these params against some
        # scenario set, return a metrics dict. The session is already set
        # up — any tool calls traced via @session.trace land in the run's
        # events.jsonl automatically.
        return {
            "accuracy": 0.82,
            "latency_ms_avg": 1250.0,
        }

    run_ids = sweep.run(
        parameter_space={
            "model": ["qwen3:8b", "gemma4:e4b"],
            "retrieval_limit": [5, 10, 20],
            "temperature": [0.0, 0.3],
        },
        evaluator=evaluate,
        tags=["sweep", "2026-04-09"],
    )
    print(f"created {len(run_ids)} runs")

Safety
------
- The sweep never mutates anything outside `$PROJECTS_DIR/experiments/`.
- Evaluator exceptions are caught and marked on the failing run via
  `mark_run_failed`; the sweep continues with the next combination.
- Progress is printed to stderr (not stdout) so a caller can pipe the
  returned run IDs without interleaving.
- Resumability: you can pass a `skip_if` predicate to skip combinations
  that already have a matching run (e.g. from an earlier interrupted
  sweep).

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import harness_telemetry as ht


Evaluator = Callable[[dict[str, Any], ht.TelemetrySession], dict[str, Any]]
SkipPredicate = Callable[[dict[str, Any]], bool]


def _slugify_value(v: Any) -> str:
    """Best-effort short slug for a parameter value."""
    s = str(v)
    # Keep alphanumerics, dash, dot; replace everything else with dash.
    out = []
    for ch in s:
        if ch.isalnum() or ch in "-.":
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)[:16] or "val"


def _build_slug(params: dict[str, Any], max_len: int = 40) -> str:
    parts = []
    for k, v in params.items():
        # Use first 4 chars of key + first 8 of value
        k_short = "".join(c for c in k if c.isalnum())[:4]
        parts.append(f"{k_short}-{_slugify_value(v)[:8]}")
    slug = "-".join(parts)
    if len(slug) > max_len:
        slug = slug[: max_len - 3] + "..."
    return slug or "sweep"


def plan(parameter_space: dict[str, Iterable[Any]]) -> list[dict[str, Any]]:
    """Return the list of parameter dicts that would be executed.

    Useful for sanity-checking a sweep definition without running it.
    """
    param_names = list(parameter_space.keys())
    if not param_names:
        return [{}]
    param_values = [list(parameter_space[k]) for k in param_names]
    return [dict(zip(param_names, combo)) for combo in itertools.product(*param_values)]


def run(
    parameter_space: dict[str, Iterable[Any]],
    evaluator: Evaluator,
    *,
    projects_dir: str | Path | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    freeze_files: list[str | Path] | None = None,
    progress: bool = True,
    skip_if: SkipPredicate | None = None,
    stop_on_error: bool = False,
) -> list[str]:
    """Run the evaluator against every point in the parameter space.

    Parameters
    ----------
    parameter_space : dict[str, iterable]
        Mapping of parameter name to the list of values to sweep over.
        The sweep runs the Cartesian product.
    evaluator : callable
        Function (params, session) -> metrics. The session is a live
        TelemetrySession; any `@session.trace` decorators the evaluator
        applies will log events into the current run automatically.
        Must return a dict of metrics that can be serialized as JSON.
    projects_dir : path, optional
        Override $MNEMOSYNE_PROJECTS_DIR.
    tags : list of str, optional
        Tags applied to every run in the sweep. A "sweep" tag is always
        added in addition.
    notes : str, optional
        Written to each run's metadata.json notes field.
    freeze_files : list of paths, optional
        Snapshotted into each run's harness/ dir (see create_run).
    progress : bool
        Print per-run progress to stderr (default True).
    skip_if : callable, optional
        (params) -> bool. If it returns True for a params dict, that
        combination is skipped. Useful for resuming interrupted sweeps.
    stop_on_error : bool
        If True, re-raise the first evaluator exception instead of
        continuing with the next combination. Default False.

    Returns
    -------
    list of run_id strings, in execution order.
    """
    combos = plan(parameter_space)
    total = len(combos)
    run_ids: list[str] = []
    final_tags = ["sweep"] + list(tags or [])

    start_time = time.monotonic()

    for idx, params in enumerate(combos, start=1):
        if skip_if and skip_if(params):
            if progress:
                print(f"[{idx}/{total}] SKIP params={params}", file=sys.stderr)
            continue

        slug = _build_slug(params)
        run_id = ht.create_run(
            model=str(params.get("model", "unknown")),
            notes=notes,
            tags=final_tags,
            slug=slug,
            projects_dir=projects_dir,
            freeze_files=freeze_files,
            extra_metadata={
                "sweep_params": params,
                "sweep_index": idx,
                "sweep_total": total,
            },
        )
        run_ids.append(run_id)

        if progress:
            elapsed = time.monotonic() - start_time
            print(
                f"[{idx}/{total}] {run_id}  params={params}  elapsed={elapsed:.1f}s",
                file=sys.stderr,
            )

        try:
            with ht.TelemetrySession(run_id, projects_dir=projects_dir) as sess:
                metrics = evaluator(params, sess)
            if not isinstance(metrics, dict):
                raise TypeError(
                    f"evaluator must return a dict, got {type(metrics).__name__}"
                )
            ht.finalize_run(run_id, metrics=metrics, projects_dir=projects_dir)
        except KeyboardInterrupt:
            ht.mark_run_failed(run_id, error="KeyboardInterrupt", projects_dir=projects_dir)
            if progress:
                print(f"  ! interrupted — stopping sweep", file=sys.stderr)
            raise
        except Exception as exc:  # noqa: BLE001 — we intentionally catch all
            ht.mark_run_failed(
                run_id,
                error=f"{type(exc).__name__}: {exc}",
                projects_dir=projects_dir,
            )
            if progress:
                print(f"  ! failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            if stop_on_error:
                raise

    if progress:
        elapsed = time.monotonic() - start_time
        print(
            f"sweep complete: {len(run_ids)} runs in {elapsed:.1f}s",
            file=sys.stderr,
        )

    return run_ids
