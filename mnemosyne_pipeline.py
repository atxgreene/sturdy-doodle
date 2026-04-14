#!/usr/bin/env python3
"""
mnemosyne_pipeline.py — the loop connector.

Runs steps 1-5 of the Meta-Harness optimization loop in sequence:

  1. OBSERVE     create_run + TelemetrySession (→ events.jsonl)
  2. EVALUATE    scenario_runner.run_scenarios (→ metrics dict)
  3. SWEEP       harness_sweep.run over a parameter space (→ experiments/ tree)
  4. COMPARE     compute Pareto frontier from finalized runs
  5. INSPECT     print diff of best vs baseline, aggregate stats

Each step writes to the SAME experiments/ directory. Data flows in ONE
direction (run → events → metrics → frontier). No component reads data
that another already wrote in a different format — every downstream
consumer reads from the canonical experiments/ tree.

Data flow diagram:

  parameter_space ──► harness_sweep.run ──►┐
                        │                    │
                        │ for each combo:    │
                        ├─ create_run        │
                        ├─ TelemetrySession  │  (step 1: OBSERVE)
                        ├─ scenario_runner   │  (step 2: EVALUATE)
                        ├─ finalize_run      │
                        │                    │
                        ▼                    │
                   experiments/              │
                     run_A/                  │
                       metadata.json         │
                       results.json          │
                       events.jsonl          │
                     run_B/                  │
                       ...                   │
                        │                    │
                        ▼                    │
                   pareto_frontier    ◄──────┘  (step 4: COMPARE)
                        │
                        ▼
                   diff + aggregate          (step 5: INSPECT)
                        │
                        ▼
                   pipeline_report.json
                   (for step 6: a future proposer reads this)

Where eternal-context / fantastic-disco plug in:
  - The `harness` callable passed to the pipeline IS the agent.
  - For now (no skill file yet): use a mock harness or sweep_demo's fake.
  - With brain.py: the brain IS the harness callable.
    brain.turn(prompt, session) → routes to eternal-context tools,
    calls the voice model, returns {text, tool_calls}.
  - No data is duplicated: eternal-context's ICMS doesn't separately
    log what harness_telemetry already logs. The brain is the single
    observation point.

Usage:
    from mnemosyne_pipeline import run_pipeline

    report = run_pipeline(
        harness=my_harness_fn,        # (prompt, session) -> {text, tool_calls}
        scenarios="scenarios.example.jsonl",
        parameter_space={"model": ["qwen3.5:9b", "gemma4:e4b"]},
        tags=["nightly"],
    )
    # report has: run_ids, frontier, best_run, comparison

CLI:
    python3 mnemosyne_pipeline.py \\
        --scenarios scenarios.example.jsonl \\
        --parameter-space '{"model":["qwen3.5:9b","gemma4:e4b"]}' \\
        --tags nightly

    (CLI mode uses a mock harness for demonstration. Wire a real one via Python API.)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

# After `pip install -e .` these are plain imports. The fallback keeps the
# module runnable as a script from an un-installed clone.
try:
    import harness_sweep as sweep
    import harness_telemetry as ht
    import scenario_runner as sr
except ImportError:
    _SCRIPT_DIR = Path(__file__).parent.resolve()
    sys.path.insert(0, str(_SCRIPT_DIR))
    import harness_sweep as sweep  # noqa: E402
    import harness_telemetry as ht  # noqa: E402
    import scenario_runner as sr  # noqa: E402


HarnessCallable = Callable[[str, ht.TelemetrySession], dict[str, Any]]


# ---- step 4: Pareto frontier from finalized runs ----------------------------

def _dominates(
    a: list[float], b: list[float], directions: list[str]
) -> bool:
    at_least = True
    strictly = False
    for i, d in enumerate(directions):
        if d == "max":
            if a[i] < b[i]:
                at_least = False
                break
            if a[i] > b[i]:
                strictly = True
        else:
            if a[i] > b[i]:
                at_least = False
                break
            if a[i] < b[i]:
                strictly = True
    return at_least and strictly


def compute_frontier(
    run_ids: list[str],
    axes: list[str],
    directions: list[str],
    projects_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Compute the Pareto frontier over a set of runs."""
    pd = Path(projects_dir) if projects_dir else None
    runs_with_values: list[tuple[str, dict, list[float]]] = []
    for rid in run_ids:
        try:
            info = ht.get_run(rid, projects_dir=pd)
        except FileNotFoundError:
            continue
        if not info["results"]:
            continue
        metrics = info["results"].get("metrics") or {}
        vals: list[float] = []
        ok = True
        for axis in axes:
            v = metrics.get(axis)
            if not isinstance(v, (int, float)):
                ok = False
                break
            vals.append(float(v))
        if ok:
            runs_with_values.append((rid, info["metadata"], vals))

    frontier: list[dict[str, Any]] = []
    for i, (rid_i, meta_i, vals_i) in enumerate(runs_with_values):
        dominated = False
        for j, (_, _, vals_j) in enumerate(runs_with_values):
            if i == j:
                continue
            if _dominates(vals_j, vals_i, directions):
                dominated = True
                break
        if not dominated:
            frontier.append({
                "run_id": rid_i,
                "model": meta_i.get("model"),
                **{a: v for a, v in zip(axes, vals_i)},
            })
    return frontier


# ---- the pipeline ------------------------------------------------------------

def run_pipeline(
    harness: HarnessCallable,
    scenarios: str | Path | list[dict[str, Any]],
    parameter_space: dict[str, list[Any]] | None = None,
    *,
    projects_dir: str | Path | None = None,
    tags: list[str] | None = None,
    pareto_axes: list[str] | None = None,
    pareto_directions: list[str] | None = None,
    baseline_run_id: str | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    """Run the full OBSERVE→EVALUATE→SWEEP→COMPARE→INSPECT loop.

    Parameters
    ----------
    harness
        The agent callable: (prompt, session) -> {text, tool_calls}.
        This is where eternal-context/brain.py plugs in.
    scenarios
        Path to a JSONL scenario file, or a pre-loaded list of dicts.
    parameter_space
        Dict of parameter lists for the sweep. If None or empty,
        runs a single evaluation with no sweep.
    projects_dir
        Override $MNEMOSYNE_PROJECTS_DIR.
    tags
        Tags applied to every run.
    pareto_axes
        Metric names for the Pareto frontier (default: ["accuracy", "latency_ms_avg"]).
    pareto_directions
        Direction per axis (default: ["max", "min"]).
    baseline_run_id
        If provided, diff the best new run against this baseline.
    progress
        Print progress to stderr.

    Returns
    -------
    dict with:
      run_ids      : list of all created run IDs
      frontier     : list of Pareto-optimal runs
      best_run     : the single best run on the primary axis
      comparison   : diff vs baseline (if provided)
      elapsed_s    : total wall time
    """
    start_time = time.monotonic()
    axes = pareto_axes or ["accuracy", "latency_ms_avg"]
    directions = pareto_directions or ["max", "min"]

    # Load scenarios if given as a path
    if isinstance(scenarios, (str, Path)):
        scenario_list = sr.load_scenarios(scenarios)
    else:
        scenario_list = scenarios

    # Build the evaluator: wraps harness + scenarios into a sweep-compatible callable
    def evaluator(params: dict, session: ht.TelemetrySession) -> dict:
        # Inject params into the harness's environment so it can adapt
        # (e.g. brain.py reads params["model"] to choose the voice model)
        session.log("pipeline_params", metadata=params)

        result = sr.run_scenarios(
            scenarios=scenario_list,
            harness=harness,
            session=session,
        )
        return result["metrics"]

    # Step 3: SWEEP (which internally does steps 1+2 per combination)
    if parameter_space:
        run_ids = sweep.run(
            parameter_space=parameter_space,
            evaluator=evaluator,
            projects_dir=projects_dir,
            tags=tags,
            progress=progress,
        )
    else:
        # Single run, no sweep
        run_id = ht.create_run(
            model="default",
            tags=tags or [],
            projects_dir=projects_dir,
        )
        try:
            with ht.TelemetrySession(run_id, projects_dir=projects_dir) as sess:
                metrics = evaluator({}, sess)
            ht.finalize_run(run_id, metrics=metrics, projects_dir=projects_dir)
        except Exception as exc:
            ht.mark_run_failed(run_id, error=str(exc), projects_dir=projects_dir)
            if progress:
                print(f"  ! single run failed: {exc}", file=sys.stderr)
        run_ids = [run_id]

    # Step 4: COMPARE — compute Pareto frontier
    frontier = compute_frontier(run_ids, axes, directions, projects_dir)

    # Identify best run on primary axis
    best_run = None
    if frontier:
        primary = axes[0]
        primary_dir = directions[0]
        best_run = max(frontier, key=lambda r: r.get(primary, 0)
                       if primary_dir == "max"
                       else -r.get(primary, 0))

    # Step 5: INSPECT — diff vs baseline if provided
    comparison = None
    if baseline_run_id and best_run:
        try:
            baseline_info = ht.get_run(baseline_run_id, projects_dir=projects_dir)
            best_info = ht.get_run(best_run["run_id"], projects_dir=projects_dir)
            baseline_metrics = (baseline_info.get("results") or {}).get("metrics") or {}
            best_metrics = (best_info.get("results") or {}).get("metrics") or {}
            comparison = {
                "baseline": {"run_id": baseline_run_id, "metrics": baseline_metrics},
                "best": {"run_id": best_run["run_id"], "metrics": best_metrics},
                "deltas": {
                    k: {"baseline": baseline_metrics.get(k),
                        "best": best_metrics.get(k),
                        "improved": (
                            best_metrics.get(k, 0) > baseline_metrics.get(k, 0)
                            if directions[axes.index(k)] == "max" and k in axes
                            else best_metrics.get(k, 0) < baseline_metrics.get(k, 0)
                            if k in axes
                            else None
                        )}
                    for k in sorted(set(baseline_metrics) | set(best_metrics))
                },
            }
        except (FileNotFoundError, KeyError):
            comparison = {"error": f"baseline {baseline_run_id} not found"}

    elapsed = time.monotonic() - start_time

    report = {
        "run_ids": run_ids,
        "runs_total": len(run_ids),
        "runs_completed": sum(
            1 for rid in run_ids
            if ht.get_run(rid, projects_dir=projects_dir)["metadata"].get("status") == "completed"
        ),
        "frontier": frontier,
        "frontier_size": len(frontier),
        "best_run": best_run,
        "comparison": comparison,
        "elapsed_s": round(elapsed, 2),
        "axes": axes,
        "directions": directions,
    }

    if progress:
        print(file=sys.stderr)
        print(f"Pipeline complete: {report['runs_completed']}/{report['runs_total']} "
              f"completed in {report['elapsed_s']}s", file=sys.stderr)
        print(f"Pareto frontier: {report['frontier_size']} run(s) on "
              f"({', '.join(axes)})", file=sys.stderr)
        if best_run:
            print(f"Best run: {best_run['run_id']} "
                  f"({', '.join(f'{a}={best_run.get(a)}' for a in axes)})",
                  file=sys.stderr)

    return report


# ---- CLI (uses mock harness for demo) ----------------------------------------

def _mock_harness(prompt: str, session: ht.TelemetrySession) -> dict[str, Any]:
    """Trivial mock harness for CLI demo mode."""
    text_parts: list[str] = []
    tool_calls: list[str] = []
    lower = prompt.lower()
    if "capital" in lower and "france" in lower:
        text_parts.append("Paris is the capital of France.")
    if "obsidian" in lower or "vault" in lower:
        tool_calls.append("obsidian_search")
    if "notion" in lower:
        tool_calls.append("notion_search")
    if "delete" in lower:
        text_parts.append("I cannot perform destructive actions.")
    if "fibonacci" in lower:
        text_parts.append("def fib(n): return n if n < 2 else fib(n-1)+fib(n-2)")
    return {
        "text": " ".join(text_parts) or "I'm not sure.",
        "tool_calls": tool_calls,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-pipeline",
        description="Run the full OBSERVE→EVALUATE→SWEEP→COMPARE→INSPECT loop. "
                    "CLI mode uses a mock harness. For real runs, use the Python API.",
    )
    p.add_argument("--scenarios", default="scenarios.example.jsonl",
                   help="path to JSONL scenario file")
    p.add_argument("--parameter-space", type=json.loads, default=None,
                   help='JSON dict, e.g. \'{"model":["a","b"]}\'')
    p.add_argument("--tags", nargs="*", default=["pipeline"])
    p.add_argument("--projects-dir")
    p.add_argument("--baseline")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = run_pipeline(
        harness=_mock_harness,
        scenarios=args.scenarios,
        parameter_space=args.parameter_space,
        projects_dir=args.projects_dir,
        tags=args.tags,
        baseline_run_id=args.baseline,
    )

    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
    else:
        print()
        print(f"=== Pipeline Report ===")
        print(f"Runs: {report['runs_completed']}/{report['runs_total']} completed")
        print(f"Elapsed: {report['elapsed_s']}s")
        print(f"Frontier: {report['frontier_size']} run(s)")
        if report["best_run"]:
            br = report["best_run"]
            print(f"Best: {br['run_id']}")
            for a in report["axes"]:
                print(f"  {a}: {br.get(a)}")
        if report["comparison"]:
            print()
            print("=== Comparison vs baseline ===")
            for k, d in (report["comparison"].get("deltas") or {}).items():
                marker = "+" if d.get("improved") else "-" if d.get("improved") is False else " "
                print(f"  {marker} {k}: {d.get('baseline')} → {d.get('best')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
