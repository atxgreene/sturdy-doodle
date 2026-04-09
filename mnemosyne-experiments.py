#!/usr/bin/env python3
# ==============================================================================
#  mnemosyne-experiments.py
#
#  Small CLI over the experiments/ tree written by harness_telemetry.py.
#  Implements the six operations the Meta-Harness paper recommends for
#  humans (and future agentic proposers) to navigate a run history:
#
#    list      — all runs, most recent first
#    show      — metadata + results + event count for one run
#    top-k     — top K runs by any numeric metric
#    pareto    — Pareto frontier on N metrics at once
#    diff      — side-by-side of two runs (metadata, metrics, harness code)
#    events    — event stream for a run, filterable by type/tool
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
#    ./mnemosyne-experiments.py pareto --axes accuracy,latency_ms_avg \
#                                      --directions max,min
#    ./mnemosyne-experiments.py diff run_A run_B
#    ./mnemosyne-experiments.py events run_A --event-type tool_call --tool obsidian_search
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

    dp = sub.add_parser("diff", parents=[common], help="diff two runs")
    dp.add_argument("run_a")
    dp.add_argument("run_b")

    ep = sub.add_parser("events", parents=[common], help="read a run's event stream")
    ep.add_argument("run_id")
    ep.add_argument("--event-type", help="filter by event_type")
    ep.add_argument("--tool", help="filter by tool name")
    ep.add_argument("--status", help="filter by status")
    ep.add_argument("--limit", type=int, default=None)

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
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
