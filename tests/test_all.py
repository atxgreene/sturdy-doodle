#!/usr/bin/env python3
"""
tests/test_all.py — unit tests for the Mnemosyne observability stack.

Stdlib only, no pytest. Run with:

    python3 tests/test_all.py              # all tests
    python3 tests/test_all.py --verbose    # verbose
    python3 tests/test_all.py --filter redact   # only tests whose name contains "redact"

Exits 0 on all-pass, 1 on any failure. Complements `test-harness.sh`
(which is an end-to-end integration test against a real filesystem);
this file focuses on pure-Python unit tests of the library internals.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# Make sibling modules importable
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import harness_sweep as sweep  # noqa: E402
import harness_telemetry as ht  # noqa: E402
import scenario_runner as sr  # noqa: E402


# Import the hyphenated CLI module via importlib so we can unit-test its
# internal helpers (_dominates, _percentile, _ascii_scatter).
def _load_mnemo_experiments():
    spec = importlib.util.spec_from_file_location(
        "mnemosyne_experiments",
        _REPO / "mnemosyne-experiments.py",
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mex = _load_mnemo_experiments()


# ---- test harness ------------------------------------------------------------

TESTS: list[tuple[str, Callable[[], None]]] = []


def test(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


# ---- fixtures ----------------------------------------------------------------

def _tmp_projects_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="mnemo-unit-"))
    return d


# =============================================================================
#  harness_telemetry: _redact
# =============================================================================

@test("redact: flat dict with token key")
def _():
    out = ht._redact({"token": "secret_abc", "name": "alice"}, ht.DEFAULT_REDACT_PATTERNS)
    assert out == {"token": ht.REDACTED, "name": "alice"}, out


@test("redact: nested dict redacts at any depth")
def _():
    obj = {"user": {"id": 1, "api_key": "sk-123"}, "data": {"nested": {"password": "p"}}}
    out = ht._redact(obj, ht.DEFAULT_REDACT_PATTERNS)
    assert out["user"]["api_key"] == ht.REDACTED
    assert out["data"]["nested"]["password"] == ht.REDACTED
    assert out["user"]["id"] == 1


@test("redact: list of dicts preserves order and redacts per element")
def _():
    obj = [{"name": "a", "token": "x"}, {"name": "b", "token": "y"}]
    out = ht._redact(obj, ht.DEFAULT_REDACT_PATTERNS)
    assert out == [{"name": "a", "token": ht.REDACTED}, {"name": "b", "token": ht.REDACTED}]


@test("redact: non-redactable scalars passthrough")
def _():
    assert ht._redact(42, ht.DEFAULT_REDACT_PATTERNS) == 42
    assert ht._redact("hello", ht.DEFAULT_REDACT_PATTERNS) == "hello"
    assert ht._redact(None, ht.DEFAULT_REDACT_PATTERNS) is None
    assert ht._redact(3.14, ht.DEFAULT_REDACT_PATTERNS) == 3.14


@test("redact: default patterns cover expected secret-like keys")
def _():
    patterns = ht.DEFAULT_REDACT_PATTERNS
    must_redact = [
        "token", "TOKEN", "bot_token", "api_key", "apikey", "API_KEY",
        "password", "PASSWORD", "secret", "SECRET_KEY",
        "bearer", "Bearer-Token", "credential", "Credentials",
        "signing_key", "signing-secret",
    ]
    for key in must_redact:
        assert ht._should_redact(key, patterns), f"should redact: {key}"


@test("redact: default patterns do NOT over-match innocuous keys")
def _():
    patterns = ht.DEFAULT_REDACT_PATTERNS
    must_not_redact = [
        "name", "user_id", "age", "status", "result", "duration_ms",
        "timestamp", "latency", "accuracy", "model", "version",
    ]
    for key in must_not_redact:
        assert not ht._should_redact(key, patterns), f"should NOT redact: {key}"


# =============================================================================
#  harness_telemetry: run lifecycle
# =============================================================================

@test("create_run: creates expected directory structure")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="test:0.1", tags=["unit"], projects_dir=pd)
        rd = pd / "experiments" / run_id
        assert rd.is_dir(), "run dir not created"
        assert (rd / "metadata.json").is_file(), "metadata missing"
        assert (rd / "events.jsonl").is_file(), "events missing"
        meta = json.loads((rd / "metadata.json").read_text())
        assert meta["run_id"] == run_id
        assert meta["model"] == "test:0.1"
        assert meta["status"] == "running"
        assert "unit" in meta["tags"]
    finally:
        shutil.rmtree(pd)


@test("create_run: slug sanitization strips special chars")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", slug="hello/world??", projects_dir=pd)
        assert "/" not in run_id
        assert "?" not in run_id
        assert "helloworld" in run_id
    finally:
        shutil.rmtree(pd)


@test("create_run: freeze_files copies into harness/ dir")
def _():
    pd = _tmp_projects_dir()
    src = pd / "fake_script.sh"
    src.write_text("#!/bin/sh\necho hi\n")
    try:
        run_id = ht.create_run(
            model="m",
            projects_dir=pd,
            freeze_files=[src],
        )
        frozen = pd / "experiments" / run_id / "harness" / "fake_script.sh"
        assert frozen.is_file(), "frozen file missing"
        assert "echo hi" in frozen.read_text()
    finally:
        shutil.rmtree(pd)


@test("finalize_run: writes results.json and updates metadata")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        ht.finalize_run(run_id, metrics={"accuracy": 0.9, "latency_ms_avg": 100.0},
                        projects_dir=pd)
        rd = pd / "experiments" / run_id
        results = json.loads((rd / "results.json").read_text())
        assert results["metrics"]["accuracy"] == 0.9
        meta = json.loads((rd / "metadata.json").read_text())
        assert meta["status"] == "completed"
        assert meta["ended_utc"] is not None
    finally:
        shutil.rmtree(pd)


@test("mark_run_failed: transitions status to failed")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        ht.mark_run_failed(run_id, error="oops", projects_dir=pd)
        meta = json.loads((pd / "experiments" / run_id / "metadata.json").read_text())
        assert meta["status"] == "failed"
        assert meta["error"] == "oops"
    finally:
        shutil.rmtree(pd)


@test("list_runs: returns runs sorted by run_id descending")
def _():
    pd = _tmp_projects_dir()
    try:
        r1 = ht.create_run(model="m", slug="alpha", projects_dir=pd)
        time.sleep(1.01)  # ensure different timestamp in the run_id (YYYYMMDD-HHMMSS)
        r2 = ht.create_run(model="m", slug="beta", projects_dir=pd)
        runs = list(ht.list_runs(projects_dir=pd))
        ids = [rid for rid, _ in runs]
        assert r2 in ids and r1 in ids
        # r2 is more recent, should appear first
        assert ids.index(r2) < ids.index(r1), f"{ids}"
    finally:
        shutil.rmtree(pd)


@test("get_run: returns structured dict with metadata + results + event_count")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)
        sess.log("tool_call", tool="x")
        sess.log("tool_call", tool="y")
        ht.finalize_run(run_id, metrics={"accuracy": 0.5}, projects_dir=pd)
        info = ht.get_run(run_id, projects_dir=pd)
        assert info["metadata"]["model"] == "m"
        assert info["results"]["metrics"]["accuracy"] == 0.5
        assert info["event_count"] == 2
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  harness_telemetry: TelemetrySession + @trace
# =============================================================================

@test("telemetry: trace decorator logs ok event on success")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        with ht.TelemetrySession(run_id, projects_dir=pd) as sess:
            @sess.trace
            def add(a, b):
                return a + b
            assert add(2, 3) == 5
        events_file = pd / "experiments" / run_id / "events.jsonl"
        lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
        tool_calls = [e for e in lines if e["event_type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool"] == "add"
        assert tool_calls[0]["status"] == "ok"
        assert tool_calls[0]["result"] == {"value": 5}
        assert tool_calls[0]["duration_ms"] is not None
    finally:
        shutil.rmtree(pd)


@test("telemetry: trace decorator logs error event and re-raises")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)

        @sess.trace
        def broken():
            raise ValueError("nope")

        try:
            broken()
            assert False, "should have raised"
        except ValueError as e:
            assert str(e) == "nope"

        events_file = pd / "experiments" / run_id / "events.jsonl"
        lines = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
        errors = [e for e in lines if e["status"] == "error"]
        assert len(errors) == 1
        assert errors[0]["error"]["type"] == "ValueError"
        assert errors[0]["error"]["message"] == "nope"
        assert "Traceback" in errors[0]["error"]["traceback"]
    finally:
        shutil.rmtree(pd)


@test("telemetry: secrets redacted in traced args")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)

        @sess.trace
        def authenticated_call(api_key, query):
            return "ok"

        authenticated_call(api_key="TOTALLY_SECRET", query="test")

        raw = (pd / "experiments" / run_id / "events.jsonl").read_text()
        assert "TOTALLY_SECRET" not in raw, "token leaked into events.jsonl"
        assert "<redacted>" in raw
        assert '"query"' in raw
        assert '"test"' in raw
    finally:
        shutil.rmtree(pd)


@test("telemetry: session logs start/end events in context manager")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        with ht.TelemetrySession(run_id, projects_dir=pd) as sess:
            pass
        events = [json.loads(l) for l in
                  (pd / "experiments" / run_id / "events.jsonl").read_text().splitlines()
                  if l.strip()]
        types = [e["event_type"] for e in events]
        assert "session_start" in types
        assert "session_end" in types
    finally:
        shutil.rmtree(pd)


@test("telemetry: missing run directory raises on session construction")
def _():
    pd = _tmp_projects_dir()
    try:
        try:
            ht.TelemetrySession("run_does-not-exist", projects_dir=pd)
            assert False, "should have raised"
        except FileNotFoundError:
            pass
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  harness_sweep
# =============================================================================

@test("sweep.plan: cartesian product of parameter space")
def _():
    p = sweep.plan({"a": [1, 2], "b": ["x", "y"]})
    assert len(p) == 4
    expected = [
        {"a": 1, "b": "x"}, {"a": 1, "b": "y"},
        {"a": 2, "b": "x"}, {"a": 2, "b": "y"},
    ]
    assert p == expected


@test("sweep.plan: empty parameter space -> [{}]")
def _():
    assert sweep.plan({}) == [{}]


@test("sweep.plan: single parameter")
def _():
    assert sweep.plan({"x": [1, 2, 3]}) == [{"x": 1}, {"x": 2}, {"x": 3}]


@test("sweep._build_slug: caps at 40 characters")
def _():
    slug = sweep._build_slug({
        "very_long_param_name": "very_long_value_here",
        "another_param": "another_value",
        "third_key": "third_value",
    })
    assert len(slug) <= 40, f"slug too long: {slug}"


@test("sweep._slugify_value: handles odd characters")
def _():
    assert "/" not in sweep._slugify_value("a/b/c")
    assert "?" not in sweep._slugify_value("what?")
    assert len(sweep._slugify_value("x" * 1000)) == 16


@test("sweep.run: creates one run per combination and finalizes")
def _():
    pd = _tmp_projects_dir()
    try:
        def evaluator(params, session):
            return {"accuracy": params["x"] * 0.1, "latency_ms_avg": 100.0}

        run_ids = sweep.run(
            parameter_space={"x": [1, 2, 3]},
            evaluator=evaluator,
            projects_dir=pd,
            progress=False,
        )
        assert len(run_ids) == 3
        for rid in run_ids:
            info = ht.get_run(rid, projects_dir=pd)
            assert info["metadata"]["status"] == "completed"
            assert info["results"] is not None
    finally:
        shutil.rmtree(pd)


@test("sweep.run: marks failing runs as failed, continues sweep")
def _():
    pd = _tmp_projects_dir()
    try:
        def flaky(params, session):
            if params["x"] == 2:
                raise RuntimeError("boom")
            return {"accuracy": 0.5}

        run_ids = sweep.run(
            parameter_space={"x": [1, 2, 3]},
            evaluator=flaky,
            projects_dir=pd,
            progress=False,
        )
        assert len(run_ids) == 3
        statuses = [ht.get_run(r, projects_dir=pd)["metadata"]["status"] for r in run_ids]
        assert statuses.count("completed") == 2
        assert statuses.count("failed") == 1
    finally:
        shutil.rmtree(pd)


@test("sweep.run: stop_on_error re-raises")
def _():
    pd = _tmp_projects_dir()
    try:
        def boom(params, session):
            raise RuntimeError("stop")

        try:
            sweep.run(
                parameter_space={"x": [1, 2]},
                evaluator=boom,
                projects_dir=pd,
                progress=False,
                stop_on_error=True,
            )
            assert False, "should have raised"
        except RuntimeError:
            pass
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  scenario_runner
# =============================================================================

@test("judges: expected_contains passes when all substrings present")
def _():
    ok, reason = sr._judge_contains({"text": "Paris is the capital"}, ["Paris", "capital"])
    assert ok, reason


@test("judges: expected_contains fails when missing")
def _():
    ok, reason = sr._judge_contains({"text": "Paris"}, ["Paris", "London"])
    assert not ok
    assert "London" in reason


@test("judges: expected_contains is case-insensitive")
def _():
    ok, _ = sr._judge_contains({"text": "PARIS is big"}, ["paris"])
    assert ok


@test("judges: expected_tool_calls passes when all tools present")
def _():
    ok, reason = sr._judge_tool_calls(
        {"tool_calls": ["obsidian_search", "notion_search"]},
        ["obsidian_search"],
    )
    assert ok, reason


@test("judges: expected_tool_calls fails on missing tool")
def _():
    ok, reason = sr._judge_tool_calls({"tool_calls": ["obsidian_search"]}, ["notion_search"])
    assert not ok
    assert "notion_search" in reason


@test("judges: expected_regex with multiple patterns")
def _():
    ok, _ = sr._judge_regex({"text": "email: a@b.co"}, [r"email:", r"@"])
    assert ok


@test("judges: expected_regex with bad regex returns clear error")
def _():
    ok, reason = sr._judge_regex({"text": "x"}, [r"[unclosed"])
    assert not ok
    assert "bad regex" in reason


@test("load_scenarios: parses valid JSONL, skips comments and blanks")
def _():
    pd = _tmp_projects_dir()
    try:
        f = pd / "s.jsonl"
        f.write_text('# comment\n\n{"id": "a", "prompt": "foo"}\n{"id": "b", "prompt": "bar"}\n')
        scenarios = sr.load_scenarios(f)
        assert len(scenarios) == 2
        assert scenarios[0]["id"] == "a"
    finally:
        shutil.rmtree(pd)


@test("load_scenarios: raises on malformed JSON with line number")
def _():
    pd = _tmp_projects_dir()
    try:
        f = pd / "s.jsonl"
        f.write_text('{"id": "a", "prompt": "ok"}\n{not json\n')
        try:
            sr.load_scenarios(f)
            assert False, "should have raised"
        except ValueError as e:
            assert ":2" in str(e)
    finally:
        shutil.rmtree(pd)


@test("load_scenarios: rejects missing id or prompt")
def _():
    pd = _tmp_projects_dir()
    try:
        f = pd / "s.jsonl"
        f.write_text('{"id": "a"}\n')  # no prompt
        try:
            sr.load_scenarios(f)
            assert False, "should have raised"
        except ValueError:
            pass
    finally:
        shutil.rmtree(pd)


@test("run_scenarios: reports accuracy across mixed pass/fail")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)

        def harness(prompt, session):
            return {"text": prompt.upper(), "tool_calls": []}

        scenarios = [
            {"id": "pass", "prompt": "hello", "expected_contains": ["HELLO"]},
            {"id": "fail", "prompt": "hi", "expected_contains": ["GOODBYE"]},
        ]
        result = sr.run_scenarios(scenarios, harness, sess)
        assert result["metrics"]["accuracy"] == 0.5
        assert result["metrics"]["passed"] == 1
        assert result["metrics"]["failed"] == 1
    finally:
        shutil.rmtree(pd)


@test("run_scenarios: catches harness exceptions per-scenario")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)

        def broken(prompt, session):
            raise RuntimeError("kaboom")

        result = sr.run_scenarios(
            [{"id": "x", "prompt": "go", "expected_contains": ["ok"]}],
            broken,
            sess,
        )
        assert result["metrics"]["failed"] == 1
        assert "kaboom" in result["per_scenario"][0]["reason"]
    finally:
        shutil.rmtree(pd)


@test("run_scenarios: tags_filter restricts scenarios")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)

        def harness(prompt, session):
            return {"text": prompt, "tool_calls": []}

        scenarios = [
            {"id": "a", "prompt": "hello", "expected_contains": ["hello"], "tags": ["basic"]},
            {"id": "b", "prompt": "hi", "expected_contains": ["hi"], "tags": ["tool_use"]},
            {"id": "c", "prompt": "bye", "expected_contains": ["bye"], "tags": ["basic"]},
        ]
        result = sr.run_scenarios(scenarios, harness, sess, tags_filter={"basic"})
        assert result["metrics"]["scenarios_total"] == 2
        ids = [r["id"] for r in result["per_scenario"]]
        assert set(ids) == {"a", "c"}
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne-experiments internals
# =============================================================================

@test("_dominates: max axis — higher is better")
def _():
    assert mex._dominates([0.9, 0.9], [0.8, 0.8], ["max", "max"])
    assert not mex._dominates([0.8, 0.8], [0.9, 0.9], ["max", "max"])


@test("_dominates: equal points do not dominate")
def _():
    assert not mex._dominates([0.8, 0.8], [0.8, 0.8], ["max", "max"])


@test("_dominates: mixed direction")
def _():
    # a = (0.9 accuracy, 100ms), b = (0.8 accuracy, 200ms).
    # max accuracy, min latency. a dominates b.
    assert mex._dominates([0.9, 100.0], [0.8, 200.0], ["max", "min"])
    assert not mex._dominates([0.8, 200.0], [0.9, 100.0], ["max", "min"])


@test("_dominates: tradeoff — neither dominates")
def _():
    # a = (0.9, 200ms), b = (0.8, 100ms). max acc, min lat.
    # a better on acc, b better on lat. Neither dominates.
    assert not mex._dominates([0.9, 200.0], [0.8, 100.0], ["max", "min"])
    assert not mex._dominates([0.8, 100.0], [0.9, 200.0], ["max", "min"])


@test("_percentile: empty list returns 0.0")
def _():
    assert mex._percentile([], 50) == 0.0


@test("_percentile: single element")
def _():
    assert mex._percentile([42.0], 50) == 42.0
    assert mex._percentile([42.0], 99) == 42.0


@test("_percentile: p50 of sorted list")
def _():
    assert mex._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


@test("_percentile: p99 grabs near-highest")
def _():
    vals = [float(i) for i in range(100)]
    assert mex._percentile(vals, 99) >= 98.0


@test("_ascii_scatter: renders non-empty output with markers")
def _():
    points = [("a", 0.9, 100.0), ("b", 0.8, 200.0), ("c", 0.7, 150.0)]
    frontier = {"a", "c"}
    out = mex._ascii_scatter(points, frontier, "accuracy", "latency_ms_avg")
    assert "*" in out
    assert "." in out
    assert "legend" in out


@test("_ascii_scatter: empty point list")
def _():
    out = mex._ascii_scatter([], set(), "x", "y")
    assert "no points" in out


# =============================================================================
#  runner
# =============================================================================

def main() -> int:
    argv = sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv
    filter_str = None
    if "--filter" in argv:
        filter_str = argv[argv.index("--filter") + 1]

    to_run = [(n, f) for n, f in TESTS if not filter_str or filter_str in n]
    if not to_run:
        print(f"no tests matched filter: {filter_str!r}")
        return 1

    green = "\033[1;32m"
    red = "\033[1;31m"
    off = "\033[0m"

    passed = 0
    failed = 0
    failures: list[tuple[str, str]] = []
    start = time.monotonic()

    for name, fn in to_run:
        try:
            fn()
            passed += 1
            if verbose:
                print(f"  {green}✓{off} {name}")
        except AssertionError as e:
            failed += 1
            failures.append((name, str(e) or traceback.format_exc()))
            print(f"  {red}✗{off} {name}: {e}")
        except Exception:
            failed += 1
            failures.append((name, traceback.format_exc()))
            print(f"  {red}✗{off} {name}: {traceback.format_exc().splitlines()[-1]}")

    elapsed = time.monotonic() - start
    total = passed + failed
    print()
    if failed == 0:
        print(f"{green}{passed}/{total} tests passed{off} in {elapsed:.2f}s")
        return 0
    else:
        print(f"{red}{passed}/{total} passed, {failed} failed{off} in {elapsed:.2f}s")
        if verbose:
            print()
            print("failures:")
            for name, tb in failures:
                print(f"\n  {red}✗ {name}{off}")
                for line in tb.splitlines():
                    print(f"    {line}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
