"""
mnemosyne_triage.py — self-healing feedback loop for Mnemosyne.

Purpose
-------
Adapts Peter Pang's CREAO triage-engine pattern to the local-first Mnemosyne
stack. Where CREAO's engine queries CloudWatch + Sentry and writes Linear
tickets, ours reads `events.jsonl` from recent runs and writes markdown
reports to `$PROJECTS_DIR/health/`.

The engine:

  1. Scans events.jsonl files under $PROJECTS_DIR/experiments/run_*/
  2. Collects errors (status="error"), tool failures, identity slips,
     model_call errors, and any event with an `error` block
  3. Clusters by signature: (event_type, tool, error_type) triple
  4. Scores severity across six dimensions:
       - frequency         (how often this cluster fires)
       - recency           (how recently)
       - diversity         (how many distinct runs it appeared in)
       - blast_radius      (identity slips and session_error > tool errors)
       - fix_age           (if previously reported and not resolved)
       - regression        (cluster appeared, went quiet, came back)
  5. Writes a markdown health report per day to:
       $PROJECTS_DIR/health/YYYY-MM-DD.md
  6. Returns a dict suitable for telemetry, CI, or a future agentic proposer

Zero dependencies. Stdlib only. Safe to run on cron, manually, or via CI.
Idempotent: re-running on the same day overwrites the same report.

CLI
---
    mnemosyne-triage scan              # run triage, print summary, write report
    mnemosyne-triage daily             # alias for `scan --window-days=1`
    mnemosyne-triage weekly            # window_days=7
    mnemosyne-triage --json scan       # machine-readable output
    mnemosyne-triage show YYYY-MM-DD   # read back a past report

Design notes
------------
- Clusters are stable across runs: same (event_type, tool, error_type)
  always produces the same cluster_id so auto-close / auto-reopen work.
- Severity is deterministic: same inputs → same score. Enables Pareto
  comparison across sweeps ("did changing the harness reduce severity?").
- Reports are markdown, grep-navigable, git-committable.
- The engine does NOT take corrective action. It surfaces. Action is
  either human (investigate the report) or agentic (a proposer reads
  the report and proposes a harness change). Matches the observation-
  first philosophy of the Meta-Harness paper.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


# ---- shared config ----------------------------------------------------------

def _default_projects_dir() -> Path:
    try:
        from mnemosyne_config import default_projects_dir
        return default_projects_dir()
    except ImportError:
        import os
        raw = os.environ.get("MNEMOSYNE_PROJECTS_DIR", "").strip()
        return (Path(raw).expanduser().resolve()
                if raw else Path.home() / "projects" / "mnemosyne")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- data ------------------------------------------------------------------

@dataclass
class Cluster:
    """A group of events sharing the same (event_type, tool, error_type)."""
    cluster_id: str                                  # stable hash across runs
    event_type: str
    tool: str | None
    error_type: str | None
    events: list[dict[str, Any]] = field(default_factory=list)
    runs: set[str] = field(default_factory=set)      # which run_ids touched
    first_seen_utc: str | None = None
    last_seen_utc: str | None = None

    @property
    def count(self) -> int:
        return len(self.events)

    def sample(self, n: int = 3) -> list[dict[str, Any]]:
        return self.events[-n:]  # most recent n for human readability


@dataclass
class TriageReport:
    generated_utc: str
    window_days: int
    total_events_scanned: int
    error_event_count: int
    clusters: list[dict[str, Any]]
    top_clusters: list[dict[str, Any]]
    identity_slip_rate: float                        # per 1000 events
    tool_failure_rate: float                         # per 1000 tool_calls
    affected_models: dict[str, int]
    runs_scanned: int
    health_grade: str                                # A / B / C / D / F

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_utc": self.generated_utc,
            "window_days": self.window_days,
            "total_events_scanned": self.total_events_scanned,
            "error_event_count": self.error_event_count,
            "identity_slip_rate_per_1000": self.identity_slip_rate,
            "tool_failure_rate_per_1000": self.tool_failure_rate,
            "affected_models": self.affected_models,
            "runs_scanned": self.runs_scanned,
            "health_grade": self.health_grade,
            "clusters": self.clusters,
            "top_clusters": self.top_clusters,
        }


# ---- event ingestion --------------------------------------------------------

def _iter_events(projects_dir: Path, window_days: int) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (run_id, event_dict) from every events.jsonl within `window_days`."""
    exp = projects_dir / "experiments"
    if not exp.is_dir():
        return
    cutoff = _utcnow() - timedelta(days=window_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    for run_dir in sorted(exp.iterdir(), reverse=True):
        if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
            continue
        meta_file = run_dir / "metadata.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                started = meta.get("started_utc", "")
                if started and started < cutoff_iso:
                    continue  # run is older than window
            except (json.JSONDecodeError, OSError):
                pass
        events_file = run_dir / "events.jsonl"
        if not events_file.exists():
            continue
        try:
            with events_file.open(encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        yield run_dir.name, json.loads(s)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _cluster_id_for(event_type: str, tool: str | None, error_type: str | None) -> str:
    """Deterministic cluster id from the three classifier dimensions."""
    key = f"{event_type}\0{tool or ''}\0{error_type or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def cluster_events(
    events_iter: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[list[Cluster], dict[str, Any]]:
    """Bucket error events into clusters. Returns (clusters, scan_stats)."""
    clusters: dict[str, Cluster] = {}
    stats = {
        "total_events": 0,
        "error_events": 0,
        "identity_slips": 0,
        "tool_calls": 0,
        "tool_call_errors": 0,
        "model_calls": 0,
        "model_call_errors": 0,
        "runs": set(),
        "models": {},
    }

    for run_id, ev in events_iter:
        stats["total_events"] += 1
        stats["runs"].add(run_id)
        et = ev.get("event_type", "")
        status = ev.get("status", "ok")

        if et == "tool_call":
            stats["tool_calls"] += 1
            if status == "error":
                stats["tool_call_errors"] += 1
        elif et == "model_call":
            stats["model_calls"] += 1
            if status == "error":
                stats["model_call_errors"] += 1
        elif et == "identity_slip_detected":
            stats["identity_slips"] += 1

        # Count models seen (from model_call events' args or metadata)
        args = ev.get("args") or {}
        model = args.get("model") if isinstance(args, dict) else None
        if model:
            stats["models"][model] = stats["models"].get(model, 0) + 1

        # Only cluster things worth triaging
        is_error = (status == "error") or (ev.get("error") is not None)
        is_slip = (et == "identity_slip_detected")
        if not (is_error or is_slip):
            continue
        stats["error_events"] += 1

        tool = ev.get("tool")
        err = ev.get("error") or {}
        err_type = err.get("type") if isinstance(err, dict) else None
        cid = _cluster_id_for(et, tool, err_type)

        c = clusters.get(cid)
        if c is None:
            c = Cluster(
                cluster_id=cid,
                event_type=et,
                tool=tool,
                error_type=err_type,
                first_seen_utc=ev.get("timestamp_utc"),
            )
            clusters[cid] = c
        c.events.append(ev)
        c.runs.add(run_id)
        ts = ev.get("timestamp_utc")
        if ts:
            if c.first_seen_utc is None or ts < c.first_seen_utc:
                c.first_seen_utc = ts
            if c.last_seen_utc is None or ts > c.last_seen_utc:
                c.last_seen_utc = ts

    stats["runs"] = len(stats["runs"])  # convert to count
    return list(clusters.values()), stats


# ---- severity scoring ------------------------------------------------------

def severity_score(cluster: Cluster, stats: dict[str, Any]) -> dict[str, Any]:
    """Score a cluster on six dimensions, return a dict with per-axis and total.

    All six sub-scores are 0..1; total is 0..100 (weighted sum).
    """
    now = _utcnow()
    # frequency: log-scaled, saturates at 100 events
    freq = min(1.0, cluster.count / 100.0)

    # recency: 1.0 if last seen today, decays linearly to 0 at 30 days ago
    recency = 0.0
    if cluster.last_seen_utc:
        try:
            last = datetime.strptime(cluster.last_seen_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            age_days = (now - last).total_seconds() / 86400.0
            recency = max(0.0, 1.0 - age_days / 30.0)
        except ValueError:
            pass

    # diversity: how many distinct runs touched this cluster, saturates at 10
    diversity = min(1.0, len(cluster.runs) / 10.0)

    # blast_radius: identity slips, session errors, and "error" event_type rank higher
    blast = 0.1
    if cluster.event_type == "identity_slip_detected":
        blast = 0.9
    elif cluster.event_type in ("session_error", "turn_end"):
        blast = 0.7
    elif cluster.event_type == "tool_call":
        blast = 0.4
    elif cluster.event_type == "model_call":
        blast = 0.6

    # fix_age: stale clusters that keep firing are worse than new ones (placeholder;
    # real implementation needs a report DB to know "previously reported and not
    # resolved." For v1 we approximate by age-of-first-seen.)
    fix_age = 0.0
    if cluster.first_seen_utc:
        try:
            first = datetime.strptime(cluster.first_seen_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            age_days = (now - first).total_seconds() / 86400.0
            if age_days > 7:
                fix_age = min(1.0, (age_days - 7) / 30.0)
        except ValueError:
            pass

    # regression: placeholder — requires cross-report state. For v1: 0.
    regression = 0.0

    # Weighted combine. Recency + frequency dominate because an active, frequent
    # cluster is always the top thing to fix.
    weights = {
        "frequency":     0.25,
        "recency":       0.30,
        "diversity":     0.15,
        "blast_radius":  0.20,
        "fix_age":       0.07,
        "regression":    0.03,
    }
    sub = {
        "frequency":     freq,
        "recency":       recency,
        "diversity":     diversity,
        "blast_radius":  blast,
        "fix_age":       fix_age,
        "regression":    regression,
    }
    total = sum(weights[k] * sub[k] for k in weights) * 100.0
    return {
        "cluster_id": cluster.cluster_id,
        "severity": round(total, 1),
        "sub_scores": {k: round(v, 3) for k, v in sub.items()},
    }


def _health_grade(clusters_scored: list[dict[str, Any]],
                  error_rate_per_1000: float) -> str:
    """Collapse a bunch of scores + rates into a single A-F grade."""
    if not clusters_scored and error_rate_per_1000 < 1.0:
        return "A"
    top_sev = max((c["severity"] for c in clusters_scored), default=0.0)
    if top_sev < 30 and error_rate_per_1000 < 5:
        return "A"
    if top_sev < 50 and error_rate_per_1000 < 20:
        return "B"
    if top_sev < 70 and error_rate_per_1000 < 50:
        return "C"
    if top_sev < 85:
        return "D"
    return "F"


# ---- report generation -----------------------------------------------------

def run_triage(
    projects_dir: Path | None = None,
    *,
    window_days: int = 1,
    top_n: int = 10,
) -> TriageReport:
    """Main entry: scan, cluster, score, return a report."""
    pd = projects_dir or _default_projects_dir()
    clusters, stats = cluster_events(_iter_events(pd, window_days))

    scored: list[dict[str, Any]] = []
    for c in clusters:
        s = severity_score(c, stats)
        scored.append({
            **s,
            "event_type": c.event_type,
            "tool": c.tool,
            "error_type": c.error_type,
            "count": c.count,
            "run_count": len(c.runs),
            "first_seen_utc": c.first_seen_utc,
            "last_seen_utc": c.last_seen_utc,
            "sample_events": [
                {k: v for k, v in e.items() if k not in ("raw",)}
                for e in c.sample(3)
            ],
        })
    scored.sort(key=lambda x: x["severity"], reverse=True)
    top = scored[:top_n]

    total_events = stats["total_events"]
    tool_calls = stats["tool_calls"] or 1
    slip_rate = (stats["identity_slips"] / max(total_events, 1)) * 1000
    tool_fail_rate = (stats["tool_call_errors"] / tool_calls) * 1000
    error_rate = (stats["error_events"] / max(total_events, 1)) * 1000

    report = TriageReport(
        generated_utc=_utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        window_days=window_days,
        total_events_scanned=total_events,
        error_event_count=stats["error_events"],
        clusters=scored,
        top_clusters=top,
        identity_slip_rate=round(slip_rate, 2),
        tool_failure_rate=round(tool_fail_rate, 2),
        affected_models=stats["models"],
        runs_scanned=stats["runs"],
        health_grade=_health_grade(scored, error_rate),
    )
    return report


def write_markdown_report(
    report: TriageReport,
    projects_dir: Path | None = None,
    date_override: str | None = None,
) -> Path:
    """Write the report to $PROJECTS_DIR/health/YYYY-MM-DD.md."""
    pd = projects_dir or _default_projects_dir()
    health = pd / "health"
    health.mkdir(parents=True, exist_ok=True)
    day = date_override or _utcnow().strftime("%Y-%m-%d")
    path = health / f"{day}.md"

    lines: list[str] = []
    lines.append(f"# Mnemosyne health report — {day}")
    lines.append("")
    lines.append(f"**Grade: {report.health_grade}**  ·  "
                 f"window: {report.window_days}d  ·  "
                 f"runs: {report.runs_scanned}  ·  "
                 f"events: {report.total_events_scanned}  ·  "
                 f"generated: {report.generated_utc}")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append(f"- Error events:           {report.error_event_count}")
    lines.append(f"- Identity-slip rate:     {report.identity_slip_rate} per 1000 events")
    lines.append(f"- Tool-failure rate:      {report.tool_failure_rate} per 1000 tool_calls")
    lines.append(f"- Distinct clusters:      {len(report.clusters)}")
    lines.append("")
    if report.affected_models:
        lines.append("## Models observed")
        lines.append("")
        for m, n in sorted(report.affected_models.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{m}`: {n} events")
        lines.append("")

    lines.append(f"## Top {min(len(report.top_clusters), 10)} clusters (by severity)")
    lines.append("")
    if not report.top_clusters:
        lines.append("_No error clusters in window. Nothing to triage._")
    else:
        for c in report.top_clusters:
            lines.append(f"### cluster `{c['cluster_id']}` — severity {c['severity']}")
            lines.append("")
            lines.append(f"- event_type: `{c['event_type']}`")
            lines.append(f"- tool: `{c['tool'] or '-'}`")
            lines.append(f"- error_type: `{c['error_type'] or '-'}`")
            lines.append(f"- count: {c['count']}  ·  runs: {c['run_count']}")
            lines.append(f"- first seen: `{c['first_seen_utc']}`")
            lines.append(f"- last seen:  `{c['last_seen_utc']}`")
            lines.append("- sub-scores: " +
                         ", ".join(f"{k}={v}" for k, v in c["sub_scores"].items()))
            if c["sample_events"]:
                lines.append("")
                lines.append("<details><summary>Sample events</summary>")
                lines.append("")
                lines.append("```json")
                for s in c["sample_events"]:
                    lines.append(json.dumps(s, default=str, ensure_ascii=False))
                lines.append("```")
                lines.append("")
                lines.append("</details>")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---- CLI --------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="mnemosyne-triage",
        description="Self-healing feedback loop for Mnemosyne. Clusters errors, "
                    "scores severity, writes a daily health report to "
                    "$PROJECTS_DIR/health/YYYY-MM-DD.md.",
    )
    p.add_argument("--projects-dir")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("scan", help="run triage + emit report (default)")
    sp.add_argument("--window-days", type=int, default=1)
    sp.add_argument("--top-n", type=int, default=10)
    sp.add_argument("--no-write", action="store_true",
                    help="compute report but don't write markdown to disk")

    dp = sub.add_parser("daily", help="window-days=1 (alias)")
    dp.add_argument("--no-write", action="store_true")

    wp = sub.add_parser("weekly", help="window-days=7 (alias)")
    wp.add_argument("--no-write", action="store_true")

    shp = sub.add_parser("show", help="read back a past report")
    shp.add_argument("date", help="YYYY-MM-DD")

    args = p.parse_args(argv)
    cmd = args.cmd or "scan"
    pd = Path(args.projects_dir).expanduser() if args.projects_dir else None

    if cmd == "show":
        path = (pd or _default_projects_dir()) / "health" / f"{args.date}.md"
        if not path.is_file():
            print(f"no report for {args.date} at {path}", file=sys.stderr)
            return 3
        print(path.read_text(encoding="utf-8"))
        return 0

    window = {"scan": getattr(args, "window_days", 1),
              "daily": 1, "weekly": 7}[cmd]
    top_n = getattr(args, "top_n", 10)
    no_write = getattr(args, "no_write", False)

    report = run_triage(projects_dir=pd, window_days=window, top_n=top_n)

    if not no_write:
        path = write_markdown_report(report, projects_dir=pd)
    else:
        path = None

    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2, default=str)
        print()
        return 0

    # Human summary
    print(f"Mnemosyne health — grade {report.health_grade}")
    print(f"  window:      {report.window_days}d")
    print(f"  runs:        {report.runs_scanned}")
    print(f"  events:      {report.total_events_scanned}")
    print(f"  errors:      {report.error_event_count}")
    print(f"  identity slip rate:  {report.identity_slip_rate} per 1000 events")
    print(f"  tool failure rate:   {report.tool_failure_rate} per 1000 tool_calls")
    print(f"  clusters:    {len(report.clusters)}")
    if report.top_clusters:
        print()
        print(f"  top {min(5, len(report.top_clusters))} clusters:")
        for c in report.top_clusters[:5]:
            print(f"    [{c['severity']:>5.1f}]  {c['event_type']:<22}  "
                  f"{c['tool'] or '-':<18}  {c['error_type'] or '-'}  "
                  f"(n={c['count']}, runs={c['run_count']})")
    if path:
        print()
        print(f"  report written: {path}")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main())
