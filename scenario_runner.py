"""
scenario_runner.py — evaluate a harness against a fixed scenario set.

Purpose
-------
A scenario is one unit of evaluation: a prompt, an expectation about what the
harness should produce, and some metadata. The runner evaluates a harness
callable against a list of scenarios, scores each via a user-supplied judge,
and returns a metrics dict suitable for `harness_telemetry.finalize_run`.

Scenarios live in a JSONL file — one scenario per line — so you can grep
them, diff them with git, and generate new ones from LLM output without
needing a schema migration. A minimal scenario looks like:

    {"id": "basic_recall", "prompt": "Capital of France?",
     "expected_contains": ["Paris"], "tags": ["basic"]}

You can also use `expected_tool_calls` to assert that a specific tool was
invoked, or `custom` for a judge hook you implement yourself.

Usage
-----
    import scenario_runner as sr
    from harness_telemetry import create_run, finalize_run, TelemetrySession

    def my_harness(prompt: str, session: TelemetrySession) -> dict:
        # Call your agent. Return {"text": response_string,
        # "tool_calls": [list of tool names used]}
        ...
        return {"text": "Paris is the capital of France.",
                "tool_calls": ["obsidian_search"]}

    run_id = create_run(model="gemma4:e4b", tags=["eval"])
    with TelemetrySession(run_id) as sess:
        results = sr.run_scenarios(
            scenarios=sr.load_scenarios("scenarios.example.jsonl"),
            harness=my_harness,
            session=sess,
        )
    finalize_run(run_id, metrics=results["metrics"])

The returned dict is:
    {
      "metrics": {accuracy, passed, failed, latency_ms_avg, ...},
      "per_scenario": [ {id, passed, reason, duration_ms}, ... ],
    }

Judges
------
Built-in judges:
  - `expected_contains`: all strings in the list must appear in `text`
    (case-insensitive substring).
  - `expected_tool_calls`: every tool name listed must appear in
    `tool_calls` at least once.
  - `expected_regex`: all regexes must match `text` (re.search).

Custom judges: pass a `judges` dict mapping field name to callable.
See `DEFAULT_JUDGES` for the signature.

Safety
------
- The runner catches harness exceptions per-scenario and marks that
  scenario as failed; it does not abort the whole run.
- Timeouts are NOT enforced. Bring your own timeout if you need one.
- Scenario files are parsed with `json.loads` per line — no eval, no
  shell interpretation.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from harness_telemetry import TelemetrySession


# A judge takes the harness output dict and the scenario's expected value
# and returns (passed: bool, reason: str).
Judge = Callable[[dict[str, Any], Any], tuple[bool, str]]


# ---- built-in judges ---------------------------------------------------------

def _judge_contains(output: dict[str, Any], expected: Any) -> tuple[bool, str]:
    text = str(output.get("text", ""))
    if not isinstance(expected, list):
        return False, f"expected_contains must be a list, got {type(expected).__name__}"
    missing = [s for s in expected if str(s).lower() not in text.lower()]
    if missing:
        return False, f"missing substrings: {missing}"
    return True, "all substrings present"


def _judge_tool_calls(output: dict[str, Any], expected: Any) -> tuple[bool, str]:
    actual = output.get("tool_calls") or []
    if not isinstance(expected, list):
        return False, f"expected_tool_calls must be a list, got {type(expected).__name__}"
    if not isinstance(actual, list):
        return False, f"output.tool_calls must be a list, got {type(actual).__name__}"
    missing = [t for t in expected if t not in actual]
    if missing:
        return False, f"missing tool calls: {missing} (got {actual})"
    return True, f"all required tools called (got {actual})"


def _judge_regex(output: dict[str, Any], expected: Any) -> tuple[bool, str]:
    text = str(output.get("text", ""))
    patterns = expected if isinstance(expected, list) else [expected]
    missing = []
    for pat in patterns:
        try:
            if not re.search(str(pat), text):
                missing.append(pat)
        except re.error as e:
            return False, f"bad regex {pat!r}: {e}"
    if missing:
        return False, f"patterns did not match: {missing}"
    return True, "all patterns matched"


DEFAULT_JUDGES: dict[str, Judge] = {
    "expected_contains": _judge_contains,
    "expected_tool_calls": _judge_tool_calls,
    "expected_regex": _judge_regex,
}


# ---- scenario file I/O -------------------------------------------------------

def load_scenarios(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL scenario file. Skips blank lines and lines starting with #."""
    scenarios: list[dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"scenarios file not found: {p}")
    with p.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{lineno}: invalid JSON: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{p}:{lineno}: scenario must be a JSON object")
            if "id" not in obj or "prompt" not in obj:
                raise ValueError(f"{p}:{lineno}: scenario must have 'id' and 'prompt'")
            scenarios.append(obj)
    return scenarios


# ---- runner ------------------------------------------------------------------

def run_scenarios(
    scenarios: list[dict[str, Any]],
    harness: Callable[[str, TelemetrySession], dict[str, Any]],
    session: TelemetrySession,
    *,
    judges: dict[str, Judge] | None = None,
    tags_filter: set[str] | None = None,
) -> dict[str, Any]:
    """Run every scenario through `harness` and return aggregated metrics.

    Parameters
    ----------
    scenarios : list of dict
        Loaded via `load_scenarios` or constructed directly.
    harness : callable
        (prompt, session) -> dict. Must return a dict with at least "text".
        May include "tool_calls" (list of tool names) for tool-use judges.
    session : TelemetrySession
        Live session to attach events to. Each scenario is logged as a
        `scenario_start` / `scenario_end` event pair.
    judges : dict, optional
        Override or extend the default judge table.
    tags_filter : set of str, optional
        If provided, only run scenarios whose `tags` list contains at
        least one of the given tags.

    Returns
    -------
    dict with keys:
      metrics        — {accuracy, passed, failed, scenarios_total,
                        latency_ms_avg, latency_ms_total}
      per_scenario   — list of {id, passed, reason, duration_ms, tags}
    """
    judge_table = dict(DEFAULT_JUDGES)
    if judges:
        judge_table.update(judges)

    results: list[dict[str, Any]] = []
    total_duration_ms = 0.0

    for scenario in scenarios:
        sid = scenario.get("id", "?")
        if tags_filter:
            scen_tags = set(scenario.get("tags") or [])
            if not scen_tags & tags_filter:
                continue

        start_evt = session.log(
            "scenario_start",
            metadata={"scenario_id": sid, "tags": scenario.get("tags") or []},
        )
        prompt = scenario["prompt"]
        start = time.monotonic()

        try:
            output = harness(prompt, session)
            if not isinstance(output, dict):
                raise TypeError(
                    f"harness must return a dict, got {type(output).__name__}"
                )
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000.0
            total_duration_ms += duration_ms
            session.log(
                "scenario_end",
                status="error",
                duration_ms=duration_ms,
                parent_event_id=start_evt,
                error={"type": type(exc).__name__, "message": str(exc)},
                metadata={"scenario_id": sid},
            )
            results.append({
                "id": sid,
                "passed": False,
                "reason": f"harness raised {type(exc).__name__}: {exc}",
                "duration_ms": duration_ms,
                "tags": scenario.get("tags") or [],
            })
            continue

        duration_ms = (time.monotonic() - start) * 1000.0
        total_duration_ms += duration_ms

        # Apply every judge key that appears in the scenario
        reasons: list[str] = []
        passed = True
        for key, expected in scenario.items():
            if key not in judge_table:
                continue
            judge = judge_table[key]
            ok, reason = judge(output, expected)
            reasons.append(f"{key}: {reason}")
            if not ok:
                passed = False

        if not any(k in scenario for k in judge_table):
            passed = False
            reasons.append("no judge fields present (expected_contains / expected_tool_calls / expected_regex)")

        session.log(
            "scenario_end",
            status="ok" if passed else "error",
            duration_ms=duration_ms,
            parent_event_id=start_evt,
            metadata={
                "scenario_id": sid,
                "passed": passed,
                "reasons": reasons,
            },
        )
        results.append({
            "id": sid,
            "passed": passed,
            "reason": "; ".join(reasons),
            "duration_ms": duration_ms,
            "tags": scenario.get("tags") or [],
        })

    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count
    n = len(results) or 1
    metrics = {
        "accuracy": passed_count / n,
        "passed": passed_count,
        "failed": failed_count,
        "scenarios_total": len(results),
        "latency_ms_avg": total_duration_ms / n,
        "latency_ms_total": total_duration_ms,
    }

    return {"metrics": metrics, "per_scenario": results}
