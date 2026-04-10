#!/usr/bin/env python3
# ==============================================================================
#  mnemosyne-experiments.py
#
#  Small CLI over the experiments/ tree written by harness_telemetry.py.
#  Implements the operations the Meta-Harness paper recommends for humans
#  (and future agentic proposers) to navigate a run history:
#
#    list       — all runs, most recent first
#    show       — metadata + results + event count for one run
#    top-k      — top K runs by any numeric metric
#    pareto     — Pareto frontier on N metrics at once (with optional ASCII plot)
#    diff       — side-by-side of two runs (metadata, metrics, harness code)
#    events     — event stream for a run, filterable by type/tool
#    aggregate  — per-tool statistics over a run's events.jsonl
#                 (call count, success rate, latency p50/p95/p99)
#
#  Stdlib only. Consumes harness_telemetry as a library. Can be run as
#  ./mnemosyne-experiments.py ... or via `python3 mnemosyne-experiments.py`.
#
#  Environment:
#    MNEMOSYNE_PROJECTS_DIR    default: ~/projects/mnemosyne
#
#  Examples:
#    ./mnemosyne-experiments.py list --limit 20
#    ./mnemosyne-experiments.py show run_20260409-053012-abc123
#    ./mnemosyne-experiments.py top-k 5 --metric accuracy
#    ./mnemosyne-experiments.py top-k 5 --metric latency_ms_avg --direction min
#    ./mnemosyne-experiments.py pareto --axes accuracy,latency_ms_avg --directions max,min
#    ./mnemosyne-experiments.py pareto --axes accuracy,latency_ms_avg --directions max,min --plot
#    ./mnemosyne-experiments.py diff run_A run_B
#    ./mnemosyne-experiments.py events run_A --event-type tool_call --tool obsidian_search
#    ./mnemosyne-experiments.py aggregate run_A
# ==============================================================================

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

# Import the sibling library. Works regardless of cwd.
_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))
import harness_telemetry as ht  # noqa: E402


# ---- output helpers ----------------------------------------------------------

def _emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _fmt_short_ts(iso: str | None) -> str:
    if not iso:
        return "-"
    return iso[:19].replace("T", " ")


# ---- subcommand handlers -----------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    runs = list(ht.list_runs(projects_dir=args.projects_dir))
    if args.tag:
        runs = [(rid, m) for rid, m in runs if args.tag in (m.get("tags") or [])]
    if args.status:
        runs = [(rid, m) for rid, m in runs if m.get("status") == args.status]
    if args.limit:
        runs = runs[: args.limit]

    if args.json:
        _emit_json([{"run_id": rid, **m} for rid, m in runs])
        return 0

    if not runs:
        print("no runs found")
        return 0

    # Wide columns: run_id, status, model, started, events, tags
    for rid, m in runs:
        status = m.get("status", "?")
        model = m.get("model", "?")
        started = _fmt_short_ts(m.get("started_utc"))
        events = m.get("events_recorded", 0) or 0
        tags = ",".join(m.get("tags") or []) or "-"
        print(f"{rid}  [{status:>9}]  {model:<18}  {started}  events={events:<5}  tags={tags}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        info = ht.get_run(args.run_id, projects_dir=args.projects_dir)
    except FileNotFoundError as e:
        print(f"show: {e}", file=sys.stderr)
        return 3

    if args.json:
        _emit_json(info)
        return 0

    meta = info["metadata"]
    print(f"# {info['run_id']}")
    print()
    print(f"path: {info['path']}")
    print()
    print("## metadata")
    for k in sorted(meta):
        print(f"  {k}: {meta[k]}")
    print()
    print(f"## events: {info['event_count']}")
    if info["results"]:
        print()
        print("## metrics")
        for k, v in (info["results"].get("metrics") or {}).items():
            print(f"  {k}: {v}")
    return 0


def _load_metric_scored_runs(
    projects_dir: str | None,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Return runs that have a results.json, each as (run_id, metadata, metrics)."""
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for rid, meta in ht.list_runs(projects_dir=projects_dir):
        run_dir = ht.run_path(rid, Path(projects_dir) if projects_dir else None)
        results_file = run_dir / "results.json"
        if not results_file.exists():
            continue
        try:
            results = json.loads(results_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        metrics = results.get("metrics") or {}
        out.append((rid, meta, metrics))
    return out


def cmd_top_k(args: argparse.Namespace) -> int:
    scored = _load_metric_scored_runs(args.projects_dir)
    picked: list[tuple[str, dict[str, Any], dict[str, Any], float]] = []
    for rid, meta, metrics in scored:
        v = metrics.get(args.metric)
        if isinstance(v, (int, float)):
            picked.append((rid, meta, metrics, float(v)))

    if not picked:
        msg = f"no runs have a numeric '{args.metric}' metric"
        if args.json:
            _emit_json({"error": msg, "results": []})
        else:
            print(msg)
        return 0

    reverse = args.direction == "max"
    picked.sort(key=lambda row: row[3], reverse=reverse)
    picked = picked[: args.k]

    if args.json:
        _emit_json([
            {
                "run_id": rid,
                "model": meta.get("model"),
                "metric": args.metric,
                "value": v,
                "tags": meta.get("tags") or [],
            }
            for rid, meta, _, v in picked
        ])
        return 0

    print(f"Top {args.k} runs by {args.metric} ({args.direction}):")
    for rid, meta, _, v in picked:
        model = meta.get("model", "?")
        print(f"  {rid}  {args.metric}={v}  model={model}")
    return 0


def _dominates(
    a: list[float],
    b: list[float],
    directions: list[str],
) -> bool:
    """Return True iff a dominates b on every axis and is strictly better on at least one."""
    at_least_as_good = True
    strictly_better = False
    for i, direction in enumerate(directions):
        if direction == "max":
            if a[i] < b[i]:
                at_least_as_good = False
                break
            if a[i] > b[i]:
                strictly_better = True
        else:  # "min"
            if a[i] > b[i]:
                at_least_as_good = False
                break
            if a[i] < b[i]:
                strictly_better = True
    return at_least_as_good and strictly_better


def cmd_pareto(args: argparse.Namespace) -> int:
    axes = [a.strip() for a in args.axes.split(",") if a.strip()]
    directions = [d.strip() for d in args.directions.split(",") if d.strip()]
    if len(axes) != len(directions):
        print("pareto: --axes and --directions must have the same length", file=sys.stderr)
        return 2
    for d in directions:
        if d not in ("min", "max"):
            print(f"pareto: direction must be 'min' or 'max', got {d!r}", file=sys.stderr)
            return 2

    scored = _load_metric_scored_runs(args.projects_dir)
    runs_with_values: list[tuple[str, dict[str, Any], list[float]]] = []
    for rid, meta, metrics in scored:
        vals: list[float] = []
        ok = True
        for axis in axes:
            v = metrics.get(axis)
            if not isinstance(v, (int, float)):
                ok = False
                break
            vals.append(float(v))
        if ok:
            runs_with_values.append((rid, meta, vals))

    if not runs_with_values:
        msg = f"no runs have numeric values for all of: {axes}"
        if args.json:
            _emit_json({"error": msg, "frontier": []})
        else:
            print(msg)
        return 0

    # O(n^2) Pareto filter. For a local agent harness this will be tiny.
    frontier: list[tuple[str, dict[str, Any], list[float]]] = []
    for i, (rid_i, meta_i, vals_i) in enumerate(runs_with_values):
        dominated = False
        for j, (_rid_j, _meta_j, vals_j) in enumerate(runs_with_values):
            if i == j:
                continue
            if _dominates(vals_j, vals_i, directions):
                dominated = True
                break
        if not dominated:
            frontier.append((rid_i, meta_i, vals_i))

    if args.json:
        _emit_json([
            {
                "run_id": rid,
                "model": meta.get("model"),
                **{axis: val for axis, val in zip(axes, vals)},
            }
            for rid, meta, vals in frontier
        ])
        return 0

    print(f"Pareto frontier on ({', '.join(axes)}) with directions ({', '.join(directions)}):")
    for rid, meta, vals in frontier:
        axis_str = "  ".join(f"{a}={v}" for a, v in zip(axes, vals))
        print(f"  {rid}  {axis_str}  model={meta.get('model','?')}")

    # Optional ASCII plot. Only supported for exactly two axes —
    # anything else requires a projection strategy that's out of scope.
    if getattr(args, "plot", False):
        if len(axes) != 2:
            print()
            print("(--plot requires exactly 2 axes; skipping)", file=sys.stderr)
            return 0
        frontier_ids = {rid for rid, _, _ in frontier}
        points = [
            (rid, float(vals[0]), float(vals[1]))
            for rid, _, vals in runs_with_values
        ]
        print()
        print(_ascii_scatter(points, frontier_ids, x_label=axes[0], y_label=axes[1]))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    a_dir = ht.run_path(args.run_a, Path(args.projects_dir) if args.projects_dir else None)
    b_dir = ht.run_path(args.run_b, Path(args.projects_dir) if args.projects_dir else None)
    if not a_dir.is_dir() or not b_dir.is_dir():
        print(f"diff: one or both runs not found", file=sys.stderr)
        return 3

    a_meta = json.loads((a_dir / "metadata.json").read_text(encoding="utf-8"))
    b_meta = json.loads((b_dir / "metadata.json").read_text(encoding="utf-8"))

    meta_diffs: dict[str, dict[str, Any]] = {}
    for k in sorted(set(a_meta) | set(b_meta)):
        if a_meta.get(k) != b_meta.get(k):
            meta_diffs[k] = {"a": a_meta.get(k), "b": b_meta.get(k)}

    metric_diffs: dict[str, dict[str, Any]] = {}
    a_results_file = a_dir / "results.json"
    b_results_file = b_dir / "results.json"
    if a_results_file.exists() and b_results_file.exists():
        a_metrics = (json.loads(a_results_file.read_text(encoding="utf-8"))
                     .get("metrics") or {})
        b_metrics = (json.loads(b_results_file.read_text(encoding="utf-8"))
                     .get("metrics") or {})
        for k in sorted(set(a_metrics) | set(b_metrics)):
            metric_diffs[k] = {
                "a": a_metrics.get(k),
                "b": b_metrics.get(k),
                "changed": a_metrics.get(k) != b_metrics.get(k),
            }

    # Harness code diff
    harness_diffs: dict[str, str] = {}
    a_harness = a_dir / "harness"
    b_harness = b_dir / "harness"
    if a_harness.is_dir() and b_harness.is_dir():
        a_files = {f.name for f in a_harness.iterdir() if f.is_file()}
        b_files = {f.name for f in b_harness.iterdir() if f.is_file()}
        for name in sorted(a_files | b_files):
            a_file = a_harness / name
            b_file = b_harness / name
            if name not in a_files:
                harness_diffs[name] = "(added in b)"
            elif name not in b_files:
                harness_diffs[name] = "(removed in b)"
            else:
                a_lines = a_file.read_text(encoding="utf-8", errors="replace").splitlines()
                b_lines = b_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if a_lines != b_lines:
                    diff_lines = list(difflib.unified_diff(
                        a_lines, b_lines,
                        fromfile=f"a/{name}", tofile=f"b/{name}",
                        lineterm="",
                    ))
                    # Cap diff size for sanity
                    if len(diff_lines) > 200:
                        diff_lines = diff_lines[:200] + ["... (truncated)"]
                    harness_diffs[name] = "\n".join(diff_lines)

    if args.json:
        _emit_json({
            "run_a": args.run_a,
            "run_b": args.run_b,
            "metadata": meta_diffs,
            "metrics": metric_diffs,
            "harness": harness_diffs,
        })
        return 0

    print(f"# diff {args.run_a} vs {args.run_b}")
    print()
    print("## metadata changes")
    if not meta_diffs:
        print("  (identical)")
    else:
        for k, v in meta_diffs.items():
            print(f"  {k}:")
            print(f"    a: {v['a']}")
            print(f"    b: {v['b']}")
    print()
    print("## metrics")
    if not metric_diffs:
        print("  (no metrics on either side)")
    else:
        for k, v in metric_diffs.items():
            marker = "*" if v["changed"] else " "
            print(f"  {marker} {k}:  a={v['a']}  b={v['b']}")
    if harness_diffs:
        print()
        print("## harness code")
        for name, d in harness_diffs.items():
            print(f"--- {name} ---")
            print(d)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    rd = ht.run_path(args.run_id, Path(args.projects_dir) if args.projects_dir else None)
    events_file = rd / "events.jsonl"
    if not events_file.exists():
        print(f"events: no events.jsonl for {args.run_id}", file=sys.stderr)
        return 3

    events: list[dict[str, Any]] = []
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if args.event_type:
        events = [e for e in events if e.get("event_type") == args.event_type]
    if args.tool:
        events = [e for e in events if e.get("tool") == args.tool]
    if args.status:
        events = [e for e in events if e.get("status") == args.status]
    if args.limit:
        events = events[: args.limit]

    if args.json:
        _emit_json(events)
        return 0

    if not events:
        print("no matching events")
        return 0

    for e in events:
        eid = e.get("event_id", "?")
        et = e.get("event_type", "?")
        tool = e.get("tool") or "-"
        dur = e.get("duration_ms")
        dur_str = f"{dur:7.1f}ms" if isinstance(dur, (int, float)) else "         -"
        status = e.get("status", "?")
        print(f"  {eid}  {et:<14}  {tool:<24}  {dur_str}  {status}")
    return 0


# ---- aggregate (per-tool statistics from events.jsonl) ----------------------

def _percentile(sorted_values: list[float], p: float) -> float:
    """Inclusive nearest-rank percentile on a pre-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(0, min(len(sorted_values) - 1,
                      int(round((p / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[rank]


def cmd_aggregate(args: argparse.Namespace) -> int:
    """Compute per-tool statistics from a run's events.jsonl.

    Reports, per tool:
      call_count, ok_count, error_count, success_rate,
      duration_ms (min/p50/p95/p99/max/avg), total_duration_ms
    And an overall summary across all tools.
    """
    rd = ht.run_path(args.run_id, Path(args.projects_dir) if args.projects_dir else None)
    events_file = rd / "events.jsonl"
    if not events_file.exists():
        print(f"aggregate: no events.jsonl for {args.run_id}", file=sys.stderr)
        return 3

    by_tool: dict[str, list[dict[str, Any]]] = {}
    event_type_counts: dict[str, int] = {}
    total_events = 0

    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_events += 1
            et = e.get("event_type", "unknown")
            event_type_counts[et] = event_type_counts.get(et, 0) + 1
            if et != "tool_call":
                continue
            tool = e.get("tool") or "(unnamed)"
            by_tool.setdefault(tool, []).append(e)

    if not by_tool:
        if args.json:
            _emit_json({
                "run_id": args.run_id,
                "total_events": total_events,
                "event_type_counts": event_type_counts,
                "tools": {},
            })
        else:
            print(f"aggregate: run {args.run_id} has {total_events} events, 0 tool_calls")
            for et, n in sorted(event_type_counts.items()):
                print(f"  {et}: {n}")
        return 0

    # Build per-tool stats
    stats: dict[str, dict[str, Any]] = {}
    for tool, tool_events in sorted(by_tool.items()):
        durations = sorted(
            e["duration_ms"]
            for e in tool_events
            if isinstance(e.get("duration_ms"), (int, float))
        )
        ok = sum(1 for e in tool_events if e.get("status") == "ok")
        errors = sum(1 for e in tool_events if e.get("status") == "error")
        total = len(tool_events)
        err_types: dict[str, int] = {}
        for e in tool_events:
            err = e.get("error")
            if isinstance(err, dict):
                t = err.get("type", "Unknown")
                err_types[t] = err_types.get(t, 0) + 1

        stats[tool] = {
            "call_count": total,
            "ok_count": ok,
            "error_count": errors,
            "success_rate": (ok / total) if total else 0.0,
            "duration_ms": {
                "min": min(durations) if durations else 0.0,
                "p50": _percentile(durations, 50),
                "p95": _percentile(durations, 95),
                "p99": _percentile(durations, 99),
                "max": max(durations) if durations else 0.0,
                "avg": (sum(durations) / len(durations)) if durations else 0.0,
                "total": sum(durations) if durations else 0.0,
            },
            "error_types": err_types,
        }

    # Overall
    all_tool_calls = sum(v["call_count"] for v in stats.values())
    all_ok = sum(v["ok_count"] for v in stats.values())
    all_errors = sum(v["error_count"] for v in stats.values())
    all_durations = sorted(
        d
        for tool_events in by_tool.values()
        for e in tool_events
        if isinstance(e.get("duration_ms"), (int, float))
        for d in [e["duration_ms"]]
    )

    overall = {
        "tool_calls_total": all_tool_calls,
        "tool_calls_ok": all_ok,
        "tool_calls_error": all_errors,
        "success_rate": (all_ok / all_tool_calls) if all_tool_calls else 0.0,
        "duration_ms": {
            "p50": _percentile(all_durations, 50),
            "p95": _percentile(all_durations, 95),
            "p99": _percentile(all_durations, 99),
            "avg": (sum(all_durations) / len(all_durations)) if all_durations else 0.0,
            "total": sum(all_durations),
        },
    }

    out = {
        "run_id": args.run_id,
        "total_events": total_events,
        "event_type_counts": event_type_counts,
        "overall": overall,
        "tools": stats,
    }

    if args.json:
        _emit_json(out)
        return 0

    print(f"# aggregate for {args.run_id}")
    print()
    print(f"total events: {total_events}")
    for et in sorted(event_type_counts):
        print(f"  {et:<14} {event_type_counts[et]}")
    print()
    print(f"## overall tool_call stats")
    print(f"  calls:        {all_tool_calls}")
    print(f"  ok:           {all_ok}")
    print(f"  errors:       {all_errors}")
    print(f"  success_rate: {overall['success_rate']:.2%}")
    if all_durations:
        d = overall["duration_ms"]
        print(f"  duration_ms:  avg={d['avg']:.1f}  p50={d['p50']:.1f}  "
              f"p95={d['p95']:.1f}  p99={d['p99']:.1f}  total={d['total']:.1f}")
    print()
    print(f"## per-tool")
    print(f"  {'tool':<28}  {'calls':>6}  {'ok':>6}  {'err':>6}  "
          f"{'rate':>7}  {'avg_ms':>8}  {'p95_ms':>8}")
    for tool, v in stats.items():
        d = v["duration_ms"]
        print(f"  {tool:<28}  {v['call_count']:>6}  {v['ok_count']:>6}  "
              f"{v['error_count']:>6}  {v['success_rate']:>6.1%}  "
              f"{d['avg']:>8.1f}  {d['p95']:>8.1f}")
        if v["error_types"]:
            for t, n in sorted(v["error_types"].items(), key=lambda x: -x[1]):
                print(f"      error[{t}]={n}")
    return 0


# ---- ASCII scatter plot for pareto ------------------------------------------

def _ascii_scatter(
    points: list[tuple[str, float, float]],
    frontier_ids: set[str],
    x_label: str,
    y_label: str,
    width: int = 64,
    height: int = 16,
) -> str:
    """Render a small ASCII scatter plot.

    Frontier points use '*', dominated points use '.', overlaps use '#'.
    """
    if not points:
        return "(no points to plot)"

    xs = [p[1] for p in points]
    ys = [p[2] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_span = (x_max - x_min) or 1.0
    y_span = (y_max - y_min) or 1.0

    # Reserve columns for y-axis label + border
    plot_w = max(20, width - 12)
    plot_h = max(8, height)

    grid = [[" "] * plot_w for _ in range(plot_h)]
    # Map each point to a cell; merge into '#' on collision.
    for rid, x, y in points:
        col = int(round((x - x_min) / x_span * (plot_w - 1)))
        row = int(round((y_max - y) / y_span * (plot_h - 1)))
        col = max(0, min(plot_w - 1, col))
        row = max(0, min(plot_h - 1, row))
        ch = "*" if rid in frontier_ids else "."
        existing = grid[row][col]
        if existing == " ":
            grid[row][col] = ch
        elif existing != ch:
            grid[row][col] = "#"

    # Render
    lines: list[str] = []
    lines.append(f"  {y_label}")
    for i, row_cells in enumerate(grid):
        y_val = y_max - (i / (plot_h - 1)) * y_span if plot_h > 1 else y_max
        label = f"{y_val:9.2f} |"
        lines.append(label + "".join(row_cells))
    lines.append(" " * 10 + "+" + "-" * plot_w)
    # X-axis ticks: just min and max under the first and last columns
    x_axis_line = " " * 10 + f"{x_min:<{plot_w - len(f'{x_max:.2f}')}.2f}{x_max:.2f}"
    lines.append(x_axis_line)
    lines.append(" " * (10 + plot_w // 2 - len(x_label) // 2) + x_label)
    lines.append("")
    lines.append("  legend:  * = on Pareto frontier   . = dominated   # = overlap")
    return "\n".join(lines)


# ---- arg parsing + dispatch --------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Shared flags: work at the top level AND on every subcommand so users
    # can write either `mex --json list` or `mex list --json`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--projects-dir",
                        help="override $MNEMOSYNE_PROJECTS_DIR or ~/projects/mnemosyne")
    common.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")

    p = argparse.ArgumentParser(
        prog="mnemosyne-experiments",
        parents=[common],
        description="Navigate the Mnemosyne experiments/ directory. "
                    "List runs, show details, find top-K, compute the Pareto frontier, "
                    "diff two runs, and read their event streams.",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", parents=[common], help="list runs")
    lp.add_argument("--limit", type=int, default=None)
    lp.add_argument("--tag", help="filter by tag")
    lp.add_argument("--status", help="filter by status (running/completed/failed)")

    sp = sub.add_parser("show", parents=[common], help="show one run")
    sp.add_argument("run_id")

    tp = sub.add_parser("top-k", parents=[common], help="top K runs by a single metric")
    tp.add_argument("k", type=int)
    tp.add_argument("--metric", required=True)
    tp.add_argument("--direction", choices=["min", "max"], default="max")

    pp = sub.add_parser("pareto", parents=[common], help="Pareto frontier on multiple metrics")
    pp.add_argument("--axes", required=True,
                    help="comma-separated metric names (e.g. 'accuracy,latency_ms_avg')")
    pp.add_argument("--directions", required=True,
                    help="comma-separated direction per axis ('max' or 'min')")
    pp.add_argument("--plot", action="store_true",
                    help="render an ASCII scatter plot of all runs with the frontier highlighted "
                         "(requires exactly 2 axes)")

    dp = sub.add_parser("diff", parents=[common], help="diff two runs")
    dp.add_argument("run_a")
    dp.add_argument("run_b")

    ep = sub.add_parser("events", parents=[common], help="read a run's event stream")
    ep.add_argument("run_id")
    ep.add_argument("--event-type", help="filter by event_type")
    ep.add_argument("--tool", help="filter by tool name")
    ep.add_argument("--status", help="filter by status")
    ep.add_argument("--limit", type=int, default=None)

    ap = sub.add_parser("aggregate", parents=[common],
                        help="per-tool statistics from a run's events.jsonl "
                             "(call count, success rate, latency p50/p95/p99)")
    ap.add_argument("run_id")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "list": cmd_list,
        "show": cmd_show,
        "top-k": cmd_top_k,
        "pareto": cmd_pareto,
        "diff": cmd_diff,
        "events": cmd_events,
        "aggregate": cmd_aggregate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
