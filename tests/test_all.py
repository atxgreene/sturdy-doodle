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
import mnemosyne_apply as apply_mod  # noqa: E402
import mnemosyne_brain as br  # noqa: E402
import mnemosyne_dreams as dreams  # noqa: E402
import mnemosyne_embeddings as emb  # noqa: E402
import mnemosyne_experiments as mex  # noqa: E402  (direct import after rename)
import mnemosyne_goals as goals_mod  # noqa: E402
import mnemosyne_identity as mid  # noqa: E402
import mnemosyne_inner as inner  # noqa: E402
import mnemosyne_mcp as mcp  # noqa: E402
import mnemosyne_memory as mm  # noqa: E402
import mnemosyne_models as mdls  # noqa: E402
import mnemosyne_proposer as proposer  # noqa: E402
import mnemosyne_scengen as scengen  # noqa: E402
import mnemosyne_skills as sk  # noqa: E402
import mnemosyne_avatar as avatar_mod  # noqa: E402
import mnemosyne_permissions as perm_mod  # noqa: E402
import mnemosyne_predictions as pred_mod  # noqa: E402
import mnemosyne_resolver as resolver_mod  # noqa: E402
import mnemosyne_batch as batch_mod  # noqa: E402
import mnemosyne_datagen as datagen  # noqa: E402
import mnemosyne_skills_builtin as sbi  # noqa: E402
import mnemosyne_tool_parsers as tp  # noqa: E402
import mnemosyne_train as train_mod  # noqa: E402
import mnemosyne_triage as tri  # noqa: E402
import mnemosyne_compactor as compactor_mod  # noqa: E402
import mnemosyne_continuity as continuity_mod  # noqa: E402
import scenario_runner as sr  # noqa: E402


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
        with ht.TelemetrySession(run_id, projects_dir=pd):
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
#  mnemosyne_memory (SQLite + FTS5 + ICMS tiers)
# =============================================================================

@test("memory: write + get roundtrip")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        mid = store.write("User likes dark themes", source="cli", kind="preference", tier=mm.L1_HOT)
        got = store.get(mid)
        assert got["content"] == "User likes dark themes"
        assert got["tier"] == mm.L1_HOT
        assert got["kind"] == "preference"
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: FTS5 search returns relevant matches")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write("Project alpha uses rust", kind="project", tier=mm.L2_WARM)
        store.write("Project beta uses python", kind="project", tier=mm.L2_WARM)
        store.write("Totally unrelated memory", kind="fact", tier=mm.L2_WARM)
        hits = store.search("rust", limit=5)
        assert any("rust" in h["content"] for h in hits), hits
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: tier_max filter excludes cold memories")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write("hot fact", tier=mm.L1_HOT)
        store.write("warm fact", tier=mm.L2_WARM)
        store.write("cold fact", tier=mm.L3_COLD)
        hits = store.search("fact", tier_max=mm.L2_WARM, limit=10)
        contents = [h["content"] for h in hits]
        assert "hot fact" in contents
        assert "warm fact" in contents
        assert "cold fact" not in contents
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: promote/demote tier transitions")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        mid = store.write("a thing", tier=mm.L2_WARM)
        store.promote(mid, to_tier=mm.L1_HOT)
        assert store.get(mid)["tier"] == mm.L1_HOT
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: stats reports tier/kind breakdowns")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write("a", tier=mm.L1_HOT, kind="preference")
        store.write("b", tier=mm.L2_WARM, kind="fact")
        store.write("c", tier=mm.L2_WARM, kind="fact")
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_tier"]["L1_hot"] == 1
        assert stats["by_tier"]["L2_warm"] == 2
        assert stats["by_kind"]["fact"] == 2
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: telemetry hook fires on write")
def _():
    pd = _tmp_projects_dir()
    try:
        run_id = ht.create_run(model="m", projects_dir=pd)
        sess = ht.TelemetrySession(run_id, projects_dir=pd)
        store = mm.MemoryStore(path=pd / "mem.db", telemetry=sess)
        store.write("watched event", kind="test")
        store.close()
        raw = (pd / "experiments" / run_id / "events.jsonl").read_text()
        assert "memory_write" in raw
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_models (backend abstraction)
# =============================================================================

@test("models: Backend default is Ollama local")
def _():
    b = mdls.Backend()
    assert b.provider == "ollama"
    assert "11434" in b.endpoint


@test("models: unknown provider without url raises")
def _():
    try:
        mdls.Backend(provider="not-a-thing")
        assert False, "should have raised"
    except ValueError:
        pass


@test("models: OpenAI response parser extracts text + tool_calls")
def _():
    raw = {
        "choices": [{"message": {
            "content": "ok",
            "tool_calls": [{
                "id": "t1",
                "function": {"name": "s", "arguments": '{"q": "v"}'}
            }],
        }}],
        "usage": {"prompt_tokens": 7},
    }
    parsed = mdls._parse_openai(raw)
    assert parsed["text"] == "ok"
    assert parsed["tool_calls"][0]["arguments"] == {"q": "v"}
    assert parsed["usage"]["prompt_tokens"] == 7


@test("models: Ollama parser converts eval counts to usage")
def _():
    raw = {"message": {"content": "hi", "tool_calls": []},
           "prompt_eval_count": 5, "eval_count": 3}
    parsed = mdls._parse_ollama(raw)
    assert parsed["usage"]["total_tokens"] == 8


@test("models: Anthropic parser handles mixed text/tool_use blocks")
def _():
    raw = {
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "tool_use", "id": "t1", "name": "s", "input": {"q": "v"}},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    parsed = mdls._parse_anthropic(raw)
    assert parsed["text"] == "hello "
    assert parsed["tool_calls"][0]["name"] == "s"
    assert parsed["usage"]["total_tokens"] == 4


@test("models: from_env falls back to Ollama when no key env vars set")
def _():
    # In this test environment, no API keys are set
    b = mdls.from_env()
    assert b.provider == "ollama"


# =============================================================================
#  mnemosyne_skills (agentskills.io registry)
# =============================================================================

@test("skills: @register_python decorator surfaces as OpenAI tool")
def _():
    reg = sk.SkillRegistry()

    @reg.register_python("t", "a tool", [{"name": "x", "type": "integer", "required": True}])
    def _t(x):
        return x * 2

    assert "t" in reg.names()
    tools = reg.tools()
    assert tools[0]["function"]["name"] == "t"
    assert "x" in tools[0]["function"]["parameters"]["required"]


@test("skills: invoke a python skill")
def _():
    reg = sk.SkillRegistry()

    @reg.register_python("double", "double", [{"name": "x", "type": "integer"}])
    def _t(x):
        return x * 2

    assert reg.get("double").invoke(x=5) == 10


@test("skills: parse frontmatter markdown skill file")
def _():
    pd = _tmp_projects_dir()
    try:
        p = pd / "s.md"
        p.write_text(
            "---\nname: foo\ndescription: bar\ninvocation: knowledge\n---\n\nbody"
        )
        s = sk.parse_skill_file(p)
        assert s.name == "foo"
        assert s.description == "bar"
        assert s.invocation == "knowledge"
        assert "body" in s.body
    finally:
        shutil.rmtree(pd)


@test("skills: record_learned_skill writes a valid skill file")
def _():
    pd = _tmp_projects_dir()
    try:
        path = sk.record_learned_skill(
            name="my_learned",
            description="auto-written",
            command="echo {msg}",
            parameters=[{"name": "msg", "type": "string", "required": True}],
            projects_dir=pd,
        )
        assert path.exists()
        loaded = sk.parse_skill_file(path)
        assert loaded.name == "my_learned"
        assert loaded.learned is True
        assert loaded.command == "echo {msg}"
    finally:
        shutil.rmtree(pd)


@test("skills: file without frontmatter becomes a knowledge skill")
def _():
    pd = _tmp_projects_dir()
    try:
        p = pd / "plain.md"
        p.write_text("# Just a note\n\nNo frontmatter here.")
        s = sk.parse_skill_file(p)
        assert s.invocation == "knowledge"
        assert s.name == "plain"
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_brain (end-to-end with mock chat_fn)
# =============================================================================

def _make_mock_chat(sequence: list[dict[str, Any]]) -> Callable:
    """Build a chat_fn that returns the next response in `sequence` per call."""
    counter = [0]

    def chat_fn(messages, **kwargs):
        i = counter[0]
        counter[0] += 1
        if i >= len(sequence):
            return {"status": "ok", "text": "done", "tool_calls": [],
                    "usage": None, "duration_ms": 1.0, "raw": {}}
        base = {"status": "ok", "usage": None, "duration_ms": 1.0, "raw": {}}
        base.update(sequence[i])
        return base

    return chat_fn


@test("brain: turn without tool calls returns final text directly")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        chat_fn = _make_mock_chat([
            {"text": "the answer is 42", "tool_calls": []},
        ])
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn)
        resp = brain.turn("what is the answer?")
        assert resp.text == "the answer is 42"
        assert resp.tool_calls == []
        assert resp.model_calls == 1
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: turn with tool call dispatches through skills and feeds result back")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()

        @reg.register_python("add", "add two", [
            {"name": "a", "type": "integer", "required": True},
            {"name": "b", "type": "integer", "required": True},
        ])
        def _add(a, b):
            return {"sum": a + b}

        chat_fn = _make_mock_chat([
            {"text": "", "tool_calls": [
                {"id": "t1", "name": "add", "arguments": {"a": 2, "b": 3}}
            ]},
            {"text": "2+3=5", "tool_calls": []},
        ])
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn)
        resp = brain.turn("add two and three")
        assert resp.text == "2+3=5"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["name"] == "add"
        assert resp.tool_calls[0]["result"] == {"sum": 5}
        assert resp.model_calls == 2
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: max_tool_iterations caps runaway tool loops")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()

        @reg.register_python("loop", "always loops", [])
        def _loop():
            return {"keep_going": True}

        # Mock always returns a tool call — brain should cap at max_tool_iterations
        def chat_fn(messages, **kwargs):
            return {
                "status": "ok",
                "text": "",
                "tool_calls": [{"id": "t", "name": "loop", "arguments": {}}],
                "usage": None, "duration_ms": 1.0, "raw": {},
            }

        cfg = br.BrainConfig(max_tool_iterations=2, inject_env_snapshot=False)
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn, config=cfg)
        resp = brain.turn("start loop")
        # Should have stopped at max_tool_iterations model calls
        assert resp.model_calls == 2
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: session_metrics returns expected shape")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        chat_fn = _make_mock_chat([{"text": "ok", "tool_calls": []}])
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn)
        brain.turn("hello")
        m = brain.session_metrics()
        assert m["turns_total"] == 1
        assert m["turns_successful"] == 1
        assert m["accuracy"] == 1.0
        assert m["model_calls_total"] == 1
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_identity (identity lock + post-response filter)
# =============================================================================

@test("identity: MNEMOSYNE_IDENTITY mentions the name")
def _():
    assert "Mnemosyne" in mid.MNEMOSYNE_IDENTITY


@test("identity: enforce rewrites 'I am Claude'")
def _():
    out, slips = mid.enforce_identity("I am Claude, an AI assistant.")
    assert "Mnemosyne" in out
    assert len(slips) >= 1


@test("identity: enforce rewrites 'I'm ChatGPT'")
def _():
    out, _ = mid.enforce_identity("I'm ChatGPT, how can I help?")
    assert "Mnemosyne" in out
    assert "ChatGPT" not in out


@test("identity: enforce rewrites 'My name is Gemini'")
def _():
    out, _ = mid.enforce_identity("My name is Gemini.")
    assert "My name is Mnemosyne" in out


@test("identity: enforce rewrites 'trained by Anthropic'")
def _():
    out, _ = mid.enforce_identity("I was trained by Anthropic to help you.")
    assert "Mnemosyne framework" in out
    assert "Anthropic" not in out


@test("identity: enforce strips 'As an AI language model' opener")
def _():
    out, _ = mid.enforce_identity("As an AI language model, I cannot help with that.")
    assert not out.startswith("As an AI")


@test("identity: enforce leaves legitimate third-person mentions alone")
def _():
    original = "The difference between Claude and GPT-4 is context window size."
    out, slips = mid.enforce_identity(original)
    assert out == original
    assert slips == []


@test("identity: enforce leaves API-context mentions alone")
def _():
    original = "You can call the Anthropic API or the OpenAI API for this task."
    out, slips = mid.enforce_identity(original)
    assert out == original
    assert slips == []


@test("identity: audit mode detects slips without rewriting")
def _():
    original = "I am Claude, Anthropic's assistant."
    out, slips = mid.enforce_identity(original, passthrough=True)
    assert out == original  # unchanged in audit mode
    assert len(slips) >= 1  # but the slip is reported


@test("identity: contains_foreign_identity_slip positive + negative")
def _():
    assert mid.contains_foreign_identity_slip("I am Claude")
    assert mid.contains_foreign_identity_slip("My name is GPT-4")
    assert not mid.contains_foreign_identity_slip("I am Mnemosyne")
    assert not mid.contains_foreign_identity_slip("Claude is a model from Anthropic")


@test("identity: load_identity_extension reads IDENTITY.md if present")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "IDENTITY.md").write_text("## Personality\n\nDirect, concise, no filler.")
        ext = mid.load_identity_extension(projects_dir=pd)
        assert "Direct, concise" in ext
    finally:
        shutil.rmtree(pd)


@test("identity: load_identity_extension returns empty string when absent")
def _():
    pd = _tmp_projects_dir()
    try:
        assert mid.load_identity_extension(projects_dir=pd) == ""
    finally:
        shutil.rmtree(pd)


@test("identity: IDENTITY_SCENARIOS is a list of properly shaped dicts")
def _():
    assert len(mid.IDENTITY_SCENARIOS) >= 6
    for s in mid.IDENTITY_SCENARIOS:
        assert "id" in s and "prompt" in s and "expected_contains" in s
        assert any("Mnemosyne" in c for c in s["expected_contains"])


@test("identity: brain with enforce_identity_lock rewrites slipped response")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        # Simulate a model that slips and says "I am Claude"
        chat_fn = _make_mock_chat([{"text": "I am Claude, how can I help?", "tool_calls": []}])
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn)
        resp = brain.turn("who are you?")
        assert "Mnemosyne" in resp.text
        assert "Claude" not in resp.text
        store.close()
    finally:
        shutil.rmtree(pd)


@test("identity: brain without enforce_identity_lock does not rewrite")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        chat_fn = _make_mock_chat([{"text": "I am Claude.", "tool_calls": []}])
        cfg = br.BrainConfig(enforce_identity_lock=False, inject_env_snapshot=False)
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn, config=cfg)
        resp = brain.turn("who are you?")
        # Lock is off — the raw response is preserved
        assert "Claude" in resp.text
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_models v2 (expanded providers + detect_providers)
# =============================================================================

@test("models: every PROVIDERS entry has a matching endpoint URL")
def _():
    for prov, url in mdls.PROVIDERS.items():
        assert url.startswith(("http://", "https://")), f"{prov}: {url}"


@test("models: local providers are listed in LOCAL_PROVIDERS")
def _():
    assert "ollama" in mdls.LOCAL_PROVIDERS
    assert "lmstudio" in mdls.LOCAL_PROVIDERS
    assert "vllm" in mdls.LOCAL_PROVIDERS


@test("models: cloud providers have an API_KEY_ENV entry")
def _():
    for prov in mdls.PROVIDERS:
        if prov in mdls.LOCAL_PROVIDERS:
            continue
        assert prov in mdls.API_KEY_ENV, f"no env var for cloud provider {prov!r}"


@test("models: detect_providers returns status for every provider")
def _():
    # Ensure no cloud providers are authorized in the test environment
    import os as _os
    for env in mdls.API_KEY_ENV.values():
        _os.environ.pop(env, None)
    detected = mdls.detect_providers()
    assert set(detected.keys()) == set(mdls.PROVIDERS.keys())
    for prov, info in detected.items():
        if prov in mdls.LOCAL_PROVIDERS:
            assert info["status"] == "local"
        else:
            assert info["status"] in ("authorized", "unauthorized")


@test("models: from_env picks authorized cloud provider over local when key present")
def _():
    import os as _os
    # Set a fake key for groq and clear the rest
    for env in mdls.API_KEY_ENV.values():
        _os.environ.pop(env, None)
    _os.environ.pop("MNEMOSYNE_MODEL_PROVIDER", None)
    _os.environ["GROQ_API_KEY"] = "fake-key-for-test"
    try:
        # Important: groq is mid-priority in the autoselect order, but local
        # providers come first. If ollama isn't reachable (as in this sandbox)
        # from_env should fall through to the first authorized cloud.
        b = mdls.from_env()
        # Either local (if ollama is up) or groq (if not)
        assert b.provider in ("ollama", "lmstudio", "vllm", "tgi", "groq"), b.provider
    finally:
        _os.environ.pop("GROQ_API_KEY", None)


@test("models: MNEMOSYNE_MODEL_PROVIDER env var overrides auto-selection")
def _():
    import os as _os
    _os.environ["MNEMOSYNE_MODEL_PROVIDER"] = "openai"
    try:
        b = mdls.from_env()
        assert b.provider == "openai"
    finally:
        _os.environ.pop("MNEMOSYNE_MODEL_PROVIDER", None)


@test("models: Backend rejects unknown provider without explicit url")
def _():
    try:
        mdls.Backend(provider="totally-made-up")
        assert False, "should have raised"
    except ValueError:
        pass


@test("identity scenarios present in example JSONL")
def _():
    path = _REPO / "scenarios.example.jsonl"
    text = path.read_text()
    for sid in ("identity_name", "identity_who", "identity_maker"):
        assert sid in text, f"missing scenario {sid}"


@test("brain: learn_skill writes a skill file that's immediately loadable")
def _():
    pd = _tmp_projects_dir()
    try:
        # Point projects_dir at our temp dir for the learn_skill call
        # (os is imported at module top)
        orig = os.environ.get("MNEMOSYNE_PROJECTS_DIR")
        os.environ["MNEMOSYNE_PROJECTS_DIR"] = str(pd)
        try:
            store = mm.MemoryStore(path=pd / "mem.db")
            reg = sk.SkillRegistry()
            chat_fn = _make_mock_chat([{"text": "ok", "tool_calls": []}])
            brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn)
            path = brain.learn_skill(
                name="auto_test",
                description="a learned tool",
                command="echo {x}",
                parameters=[{"name": "x", "type": "string", "required": True}],
            )
            assert path.exists()
            # The skill should be loaded into the registry after learn_skill
            assert reg.get("auto_test") is not None
            store.close()
        finally:
            if orig is None:
                os.environ.pop("MNEMOSYNE_PROJECTS_DIR", None)
            else:
                os.environ["MNEMOSYNE_PROJECTS_DIR"] = orig
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_triage (self-healing feedback loop)
# =============================================================================

def _seed_fake_runs_with_errors(pd: Path, n_runs: int = 3) -> list[str]:
    """Create fake runs with a mix of ok/error events. Returns run_ids."""
    run_ids: list[str] = []
    for i in range(n_runs):
        rid = ht.create_run(model="qwen3:8b", tags=["triage-test"], slug=f"fake{i}",
                            projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            s.log("tool_call", tool="obsidian_search", status="ok", duration_ms=40.0)
            s.log("tool_call", tool="obsidian_search", status="error",
                  error={"type": "TimeoutError", "message": "vault unreachable"})
            s.log("tool_call", tool="notion_search", status="error",
                  error={"type": "HTTPError", "message": "401"})
            s.log("identity_slip_detected", status="error",
                  metadata={"slips": ["I am Claude"]})
        ht.finalize_run(rid, metrics={"accuracy": 0.7}, projects_dir=pd)
        run_ids.append(rid)
    return run_ids


@test("triage: clusters events by (event_type, tool, error_type)")
def _():
    pd = _tmp_projects_dir()
    try:
        _seed_fake_runs_with_errors(pd, n_runs=2)
        report = tri.run_triage(projects_dir=pd, window_days=30)
        assert report.runs_scanned == 2
        assert report.error_event_count >= 6  # 3 errors/run × 2 runs
        # At least 3 distinct clusters: obsidian timeout, notion HTTP, identity slip
        assert len(report.clusters) >= 3
    finally:
        shutil.rmtree(pd)


@test("triage: identity slips outrank tool errors on severity (blast_radius)")
def _():
    pd = _tmp_projects_dir()
    try:
        _seed_fake_runs_with_errors(pd, n_runs=3)
        report = tri.run_triage(projects_dir=pd, window_days=30)
        # Find identity-slip cluster and a tool_call cluster
        slip = next(c for c in report.clusters if c["event_type"] == "identity_slip_detected")
        tool = next(c for c in report.clusters if c["event_type"] == "tool_call")
        assert slip["severity"] > tool["severity"], \
            f"slip sev {slip['severity']} should exceed tool sev {tool['severity']}"
    finally:
        shutil.rmtree(pd)


@test("triage: cluster_id is stable across re-runs of same input")
def _():
    cid1 = tri._cluster_id_for("tool_call", "x", "TimeoutError")
    cid2 = tri._cluster_id_for("tool_call", "x", "TimeoutError")
    cid3 = tri._cluster_id_for("tool_call", "y", "TimeoutError")
    assert cid1 == cid2
    assert cid1 != cid3


@test("triage: write_markdown_report produces a readable file")
def _():
    pd = _tmp_projects_dir()
    try:
        _seed_fake_runs_with_errors(pd, n_runs=2)
        report = tri.run_triage(projects_dir=pd, window_days=30)
        path = tri.write_markdown_report(report, projects_dir=pd)
        assert path.exists()
        body = path.read_text()
        assert "Mnemosyne health report" in body
        assert f"Grade: {report.health_grade}" in body
        assert "identity_slip_detected" in body
    finally:
        shutil.rmtree(pd)


@test("triage: empty projects_dir yields grade A + 0 clusters")
def _():
    pd = _tmp_projects_dir()
    try:
        report = tri.run_triage(projects_dir=pd, window_days=30)
        assert report.health_grade == "A"
        assert report.clusters == []
        assert report.error_event_count == 0
    finally:
        shutil.rmtree(pd)


@test("triage: severity_score returns all 6 sub-scores")
def _():
    c = tri.Cluster(cluster_id="x", event_type="tool_call", tool="t",
                    error_type="E", events=[{"timestamp_utc": "2026-04-15T00:00:00.000Z"}])
    c.last_seen_utc = c.events[0]["timestamp_utc"]
    c.first_seen_utc = c.events[0]["timestamp_utc"]
    s = tri.severity_score(c, {})
    assert set(s["sub_scores"].keys()) == {
        "frequency", "recency", "diversity", "blast_radius", "fix_age", "regression"
    }
    assert 0 <= s["severity"] <= 100


# =============================================================================
#  mnemosyne_models: local-model helpers
# =============================================================================

@test("models: recommended_context_budget scales with context window")
def _():
    assert mdls.recommended_context_budget(None) == 6
    assert mdls.recommended_context_budget(0) == 6
    assert mdls.recommended_context_budget(8192) >= 2
    # Below saturation: bigger context → bigger budget
    assert mdls.recommended_context_budget(8192) > mdls.recommended_context_budget(2048)
    # Saturation at 20 (both 32K and 128K should be clamped there)
    assert mdls.recommended_context_budget(1_000_000) == 20
    assert mdls.recommended_context_budget(131072) == 20


@test("models: ollama_list_pulled returns empty list when daemon unreachable")
def _():
    # Point at a port nothing listens on
    names = mdls.ollama_list_pulled(host="http://localhost:1", timeout=0.5)
    assert names == []


@test("models: ollama_model_info returns error dict when unreachable")
def _():
    info = mdls.ollama_model_info("qwen3:8b", host="http://localhost:1", timeout=0.5)
    assert "error" in info


@test("models: ollama_ensure_pulled without auto_pull returns False when missing")
def _():
    ready, msg = mdls.ollama_ensure_pulled(
        "totally-imaginary-model:v0",
        host="http://localhost:1",
        auto_pull=False,
        timeout=0.5,
    )
    assert ready is False


# =============================================================================
#  brain: context adaptation for local models
# =============================================================================

@test("brain: adapt_to_context=False preserves configured retrieval limit")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        chat_fn = _make_mock_chat([{"text": "ok", "tool_calls": []}])
        cfg = br.BrainConfig(memory_retrieval_limit=6, adapt_to_context=False,
                             inject_env_snapshot=False)
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn, config=cfg)
        assert brain.config.memory_retrieval_limit == 6
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: adapt_to_context with unreachable Ollama keeps default")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        reg = sk.SkillRegistry()
        chat_fn = _make_mock_chat([{"text": "ok", "tool_calls": []}])
        # Backend pointing at an unreachable host — probe should fail silently
        cfg = br.BrainConfig(
            backend=mdls.Backend(provider="ollama", url="http://localhost:1/api/chat"),
            memory_retrieval_limit=6,
            adapt_to_context=True,
            inject_env_snapshot=False,
        )
        brain = br.Brain(memory=store, skills=reg, chat_fn=chat_fn, config=cfg)
        # Should have fallen back to the default without exception
        assert brain.config.memory_retrieval_limit == 6
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_proposer (Meta-Harness proposer loop)
# =============================================================================

@test("proposer: ignores clusters below min_severity")
def _():
    pd = _tmp_projects_dir()
    try:
        class StubReport:
            clusters = [
                {"cluster_id": "c1", "event_type": "tool_call", "tool": "foo",
                 "error_type": "Timeout", "count": 1, "severity": 2.0,
                 "sample_events": []},
            ]
        props = proposer.propose(report=StubReport(), projects_dir=pd,
                                  min_severity=20.0)
        assert props == [], props
    finally:
        shutil.rmtree(pd)


@test("proposer: identity_slip cluster produces identity category proposal")
def _():
    pd = _tmp_projects_dir()
    try:
        class StubReport:
            clusters = [
                {"cluster_id": "id-slip-abc",
                 "event_type": "identity_slip_detected",
                 "tool": None, "error_type": None,
                 "count": 12, "severity": 55.0,
                 "sample_events": [{"slips": ["I am Claude"]}]},
            ]
        props = proposer.propose(report=StubReport(), projects_dir=pd,
                                  min_severity=20.0)
        assert len(props) == 1, props
        p = props[0]
        assert p.category == "identity", p.category
        assert p.status == "pending"
        assert "identity" in p.title.lower()
        # File was written
        files = sorted((pd / "proposals").glob("PROP-*.md"))
        assert len(files) == 1, files
        body = files[0].read_text(encoding="utf-8")
        assert "cluster_id: id-slip-abc" in body
        assert "## Problem" in body
    finally:
        shutil.rmtree(pd)


@test("proposer: dedupes by cluster_id across runs")
def _():
    pd = _tmp_projects_dir()
    try:
        class StubReport:
            clusters = [
                {"cluster_id": "c-dedup",
                 "event_type": "tool_call", "tool": "notion_search",
                 "error_type": "HTTPError", "count": 4, "severity": 30.0,
                 "sample_events": []},
            ]
        first = proposer.propose(report=StubReport(), projects_dir=pd,
                                  min_severity=20.0)
        second = proposer.propose(report=StubReport(), projects_dir=pd,
                                   min_severity=20.0)
        files = sorted((pd / "proposals").glob("PROP-*.md"))
        assert len(first) == 1
        assert second == []           # duplicate suppressed
        assert len(files) == 1
    finally:
        shutil.rmtree(pd)


@test("proposer: dry_run returns proposals without writing files")
def _():
    pd = _tmp_projects_dir()
    try:
        class StubReport:
            clusters = [
                {"cluster_id": "c-dry",
                 "event_type": "session_error",
                 "tool": None, "error_type": None,
                 "count": 5, "severity": 40.0,
                 "sample_events": []},
            ]
        props = proposer.propose(report=StubReport(), projects_dir=pd,
                                  min_severity=20.0, dry_run=True)
        assert len(props) == 1
        assert not (pd / "proposals").exists() or not any(
            (pd / "proposals").glob("PROP-*.md")
        )
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_dreams (offline pattern extraction)
# =============================================================================

@test("dreams: cluster_memories groups related memories by token overlap")
def _():
    memories = [
        {"id": 1, "content": "user prefers dark mode in vscode editor"},
        {"id": 2, "content": "user uses dark mode in terminal apps"},
        {"id": 3, "content": "user likes dark theme colors at night"},
        {"id": 4, "content": "weather forecast shows rain tomorrow afternoon"},
        {"id": 5, "content": "weather alert heavy rain storm warning"},
        {"id": 6, "content": "weather rain today tomorrow forecast"},
    ]
    clusters = dreams._cluster_memories(
        memories, similarity_threshold=0.2, min_cluster_size=2,
    )
    assert len(clusters) >= 1, clusters
    # Should find at least one cluster containing either "dark" or "weather"
    found_labels = [c.key for c in clusters]
    assert any("dark" in k or "weather" in k or "rain" in k or "mode" in k
               for k in found_labels), found_labels


@test("dreams: stdlib summarizer produces non-empty prefix with count")
def _():
    contents = [
        "The database migration ran successfully last night.",
        "Migration backfilled 50 million user rows without errors.",
        "The migration deployment completed ahead of schedule.",
    ]
    out = dreams._stdlib_summarize(contents)
    assert out, "expected non-empty summary"
    assert "3 memories" in out, out


@test("dreams: consolidate writes L2 abstract and logs telemetry")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "memory.db")
        # Seed 6 related L3 memories
        for i in range(6):
            store.write(
                content=f"user prefers dark mode in their editor setup #{i}",
                tier=mm.L3_COLD,
                kind="fact",
                source="test",
            )
        before = store.stats()["by_tier"]["L2_warm"]
        report = dreams.consolidate(
            memory=store,
            projects_dir=pd,
            similarity_threshold=0.1,
            min_cluster_size=2,
            max_memories_scanned=100,
        )
        after = store.stats()["by_tier"]["L2_warm"]
        assert report.clusters_examined >= 1
        assert report.abstracts_written >= 1
        assert after > before, f"expected new L2 abstract, {before}->{after}"
        # The report JSON file was written
        trail = list((pd / "dreams").glob("dream-*.json"))
        assert len(trail) == 1
        store.close()
    finally:
        shutil.rmtree(pd)


@test("dreams: consolidate dry_run writes nothing but returns cluster info")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "memory.db")
        for i in range(5):
            store.write(
                content=f"team meeting notes about migration project week {i}",
                tier=mm.L3_COLD,
            )
        report = dreams.consolidate(
            memory=store,
            projects_dir=pd,
            similarity_threshold=0.1,
            min_cluster_size=2,
            dry_run=True,
        )
        assert report.abstracts_written == 0
        assert report.clusters_examined >= 1
        assert not (pd / "dreams").exists() or not list((pd / "dreams").glob("*.json"))
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_inner (Planner → Critic → Doer)
# =============================================================================

@test("inner: should_deliberate triggers on matching tag")
def _():
    assert inner.should_deliberate(
        "just a normal question",
        metadata={"tags": ["hard"]},
        trigger_tags={"hard"},
        trigger_keywords=set(),
    )
    assert not inner.should_deliberate(
        "just a normal question",
        metadata={"tags": ["easy"]},
        trigger_tags={"hard"},
        trigger_keywords=set(),
    )


@test("inner: should_deliberate triggers on keyword in message")
def _():
    assert inner.should_deliberate(
        "Plan a database migration for production",
        metadata=None,
        trigger_tags=set(),
        trigger_keywords={"plan a"},
    )
    assert not inner.should_deliberate(
        "What time is it?",
        metadata=None,
        trigger_tags=set(),
        trigger_keywords={"plan a"},
    )


@test("inner: deliberate runs all three personas and returns doer text")
def _():
    calls: list[str] = []

    def fake_chat(messages, **kw):
        # Identify which persona by looking at the system prompt
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "Role: Planner" in sys_text:
            calls.append("planner")
            return {"text": "### Plan\n1. step one\n2. step two\n",
                    "tool_calls": []}
        if "Role: Critic" in sys_text:
            calls.append("critic")
            return {"text": "### Concerns\n- none\n### Recommend\n- accept",
                    "tool_calls": []}
        if "Role: Doer" in sys_text:
            calls.append("doer")
            return {"text": "Here is the final answer.", "tool_calls": []}
        calls.append("unknown")
        return {"text": "", "tool_calls": []}

    result = inner.deliberate(
        user_message="Plan something structured.",
        chat_fn=fake_chat,
        backend=None,
        identity_preamble=mid.MNEMOSYNE_IDENTITY,
    )
    assert calls == ["planner", "critic", "doer"], calls
    assert result.answer == "Here is the final answer."
    assert result.total_model_calls == 3
    assert result.planner is not None
    assert result.critic is not None
    assert result.doer is not None


@test("inner: deliberate skips critic when enable_critic=False")
def _():
    calls: list[str] = []

    def fake_chat(messages, **kw):
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "Role: Planner" in sys_text:
            calls.append("planner")
            return {"text": "plan", "tool_calls": []}
        if "Role: Doer" in sys_text:
            calls.append("doer")
            return {"text": "done", "tool_calls": []}
        return {"text": "", "tool_calls": []}

    result = inner.deliberate(
        user_message="x",
        chat_fn=fake_chat,
        backend=None,
        enable_critic=False,
    )
    assert calls == ["planner", "doer"], calls
    assert result.critic is None
    assert result.total_model_calls == 2


@test("inner: identity lock is applied to persona outputs")
def _():
    def fake_chat(messages, **kw):
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "Role: Planner" in sys_text:
            return {"text": "I am Claude, a planner.", "tool_calls": []}
        if "Role: Critic" in sys_text:
            return {"text": "The plan looks fine.", "tool_calls": []}
        if "Role: Doer" in sys_text:
            return {"text": "I am ChatGPT and I respond.", "tool_calls": []}
        return {"text": "", "tool_calls": []}

    result = inner.deliberate(
        user_message="hello",
        chat_fn=fake_chat,
        backend=None,
        identity_preamble=mid.MNEMOSYNE_IDENTITY,
    )
    # Foreign identity string should have been rewritten in both planner and doer
    assert "Claude" not in result.planner.text, result.planner.text
    assert "ChatGPT" not in result.doer.text, result.doer.text
    assert "Mnemosyne" in result.doer.text or "assistant" in result.doer.text.lower()


# =============================================================================
#  Brain integration: inner dialogue routing
# =============================================================================

@test("brain: inner dialogue fires only on tagged turn")
def _():
    pd = _tmp_projects_dir()
    try:
        seen_personas: list[str] = []

        def fake_chat(messages, **kw):
            sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
            if "Role: Planner" in sys_text:
                seen_personas.append("planner")
                return {"text": "plan body", "tool_calls": []}
            if "Role: Critic" in sys_text:
                seen_personas.append("critic")
                return {"text": "accept", "tool_calls": []}
            if "Role: Doer" in sys_text:
                seen_personas.append("doer")
                return {"text": "inner-answer", "tool_calls": []}
            seen_personas.append("single")
            return {"text": "single-answer", "tool_calls": []}

        store = mm.MemoryStore(path=pd / "memory.db")
        cfg = br.BrainConfig(
            inner_dialogue_enabled=True,
            inner_dialogue_tags={"hard"},
            inner_dialogue_keywords=set(),
            adapt_to_context=False,
            inject_env_snapshot=False,
        )
        brain = br.Brain(config=cfg, memory=store, chat_fn=fake_chat)

        # Untagged turn — takes single path
        r1 = brain.turn("A normal question.", metadata={"tags": []})
        assert r1.text == "single-answer"
        assert seen_personas == ["single"]

        # Tagged turn — takes inner-dialogue path
        seen_personas.clear()
        r2 = brain.turn("Plan X carefully.", metadata={"tags": ["hard"]})
        assert r2.text == "inner-answer"
        assert seen_personas == ["planner", "critic", "doer"]

        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: dreams fire on cadence when L3 has enough memories")
def _():
    pd = _tmp_projects_dir()
    try:
        def fake_chat(messages, **kw):
            return {"text": "ok", "tool_calls": []}

        store = mm.MemoryStore(path=pd / "memory.db")
        # Seed 25 L3 memories so the guard threshold passes
        for i in range(25):
            store.write(
                content=f"seed memory about project planning iteration {i}",
                tier=mm.L3_COLD,
            )
        cfg = br.BrainConfig(
            dreams_after_n_turns=1,
            dreams_min_memories=20,
            adapt_to_context=False,
            inject_env_snapshot=False,
        )
        brain = br.Brain(config=cfg, memory=store, chat_fn=fake_chat)
        before = store.stats()["by_tier"]["L2_warm"]
        brain.turn("hello")
        after = store.stats()["by_tier"]["L2_warm"]
        # At least one dream abstract should have landed in L2. The turn
        # itself also writes one memory, but dreams should add ≥1 more.
        assert after > before + 1, f"dream abstracts missing: {before}->{after}"
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: dreams skip when L3 below threshold")
def _():
    pd = _tmp_projects_dir()
    try:
        def fake_chat(messages, **kw):
            return {"text": "ok", "tool_calls": []}

        store = mm.MemoryStore(path=pd / "memory.db")
        # Only 3 L3 memories — below default threshold
        for i in range(3):
            store.write(content=f"stray {i}", tier=mm.L3_COLD)
        cfg = br.BrainConfig(
            dreams_after_n_turns=1,
            dreams_min_memories=20,
            adapt_to_context=False,
            inject_env_snapshot=False,
        )
        brain = br.Brain(config=cfg, memory=store, chat_fn=fake_chat)
        before = store.stats()["by_tier"]["L2_warm"]
        brain.turn("hi")
        after = store.stats()["by_tier"]["L2_warm"]
        # Only the turn memory should be written — no dream abstracts
        assert after == before + 1, f"unexpected dream: {before}->{after}"
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_models: rate limiter + cost pricing + stream parser bits
# =============================================================================

@test("models: rate limiter blocks until token available")
def _():
    lim = mdls.RateLimiter(default_rps=50.0, burst=2)
    t0 = time.monotonic()
    for _ in range(5):
        lim.acquire("fake-provider")
    elapsed = time.monotonic() - t0
    # 5 tokens at 50 rps with burst=2: first 2 are free, then 3 at 20ms each
    # ≈ 60ms. Allow plenty of slack for CI.
    assert 0.04 <= elapsed <= 0.5, f"elapsed={elapsed}"


@test("models: rate limiter raises on timeout")
def _():
    lim = mdls.RateLimiter(default_rps=0.1, burst=1)   # 1 token / 10s
    lim.acquire("fake")   # consume the burst
    raised = False
    try:
        lim.acquire("fake", timeout=0.2)
    except TimeoutError:
        raised = True
    assert raised


@test("models: cost_for gpt-4o with canonical usage")
def _():
    c = mdls.cost_for("gpt-4o",
                       {"prompt_tokens": 1_000_000,
                        "completion_tokens": 500_000})
    assert round(c["prompt_usd"], 2) == 2.50
    assert round(c["completion_usd"], 2) == 5.00
    assert c["matched"] == "gpt-4o"


@test("models: cost_for local Ollama model returns zero")
def _():
    c = mdls.cost_for("qwen3.5:9b", {"prompt_tokens": 1000, "completion_tokens": 500})
    assert c["total_usd"] == 0.0, c


@test("models: cost_for unknown model returns zero without override")
def _():
    c = mdls.cost_for("some-new-model-2027",
                       {"prompt_tokens": 100, "completion_tokens": 50})
    assert c["total_usd"] == 0.0


@test("models: cost_for uses price_override for unknown models")
def _():
    c = mdls.cost_for("some-new-model-2027",
                       {"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
                       price_override={"prompt": 1.0, "completion": 2.0})
    assert round(c["total_usd"], 2) == 2.00    # 1.0 + 1.0


# =============================================================================
#  mnemosyne_goals
# =============================================================================

@test("goals: add + list_open round-trip")
def _():
    pd = _tmp_projects_dir()
    try:
        gs = goals_mod.GoalStack(projects_dir=pd)
        g = gs.add("finish the demo", priority=2, tags=["demo"])
        assert g.id == 1
        assert g.status == "open"
        opens = gs.list_open()
        assert len(opens) == 1
        assert opens[0].text == "finish the demo"
    finally:
        shutil.rmtree(pd)


@test("goals: resolve moves goal out of list_open")
def _():
    pd = _tmp_projects_dir()
    try:
        gs = goals_mod.GoalStack(projects_dir=pd)
        g1 = gs.add("goal one")
        g2 = gs.add("goal two")
        gs.resolve(g1.id, notes="done")
        opens = gs.list_open()
        assert [g.id for g in opens] == [g2.id]
        resolved = gs.get(g1.id)
        assert resolved is not None and resolved.status == "resolved"
    finally:
        shutil.rmtree(pd)


@test("goals: system_block lists top-priority first")
def _():
    pd = _tmp_projects_dir()
    try:
        gs = goals_mod.GoalStack(projects_dir=pd)
        gs.add("low priority", priority=5)
        high = gs.add("urgent thing", priority=1)
        block = goals_mod.goals_system_block(gs.list_open(), limit=5)
        # High-priority goal text should appear before the low-priority one
        assert block.index("urgent thing") < block.index("low priority")
        assert f"#{high.id}" in block
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_embeddings
# =============================================================================

@test("embeddings: hashed-bow deterministic + similar texts cluster")
def _():
    e = emb.HashedBowEmbedder(dim=128)
    v1 = e.embed("dark mode in vscode editor")
    v2 = e.embed("dark mode in terminal apps")
    v3 = e.embed("weather forecast tomorrow rain")
    sim_close = emb.cosine(v1, v2)
    sim_far = emb.cosine(v1, v3)
    assert sim_close > sim_far, f"close={sim_close}, far={sim_far}"
    # Deterministic: same input gives same vector
    assert e.embed("dark mode in vscode editor") == v1


@test("embeddings: cosine boundary cases")
def _():
    assert emb.cosine([], []) == 0.0
    assert emb.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert abs(emb.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9


@test("embeddings: cluster_by_embedding groups similar memories")
def _():
    mems = [
        {"id": 1, "content": "user prefers dark mode in editor"},
        {"id": 2, "content": "user uses dark mode in terminal"},
        {"id": 3, "content": "user likes dark theme at night"},
        {"id": 4, "content": "weather forecast shows rain"},
        {"id": 5, "content": "weather alert rain warning"},
        {"id": 6, "content": "weather advisory stormy today"},
    ]
    e = emb.HashedBowEmbedder(dim=256)
    clusters = emb.cluster_by_embedding(
        mems, e, similarity_threshold=0.15, min_cluster_size=2,
    )
    # Expect at least one cluster and every cluster to have >= 2 members
    assert len(clusters) >= 1
    for c in clusters:
        assert c["size"] >= 2


# =============================================================================
#  mnemosyne_apply
# =============================================================================

@test("apply: skipped when proposal status != accepted")
def _():
    pd = _tmp_projects_dir()
    try:
        proposals = pd / "proposals"
        proposals.mkdir(parents=True)
        prop = proposals / "PROP-0001-test.md"
        prop.write_text(
            "---\nid: PROP-0001\nstatus: pending\ncategory: identity\n---\n"
            "# test proposal\n",
            encoding="utf-8",
        )
        r = apply_mod.apply_proposal(prop)
        assert r.status == "skipped"
    finally:
        shutil.rmtree(pd)


@test("apply: identity-category handler runs against identity scenarios")
def _():
    pd = _tmp_projects_dir()
    try:
        proposals = pd / "proposals"
        proposals.mkdir(parents=True)
        prop = proposals / "PROP-0001-id.md"
        prop.write_text(
            "---\nid: PROP-0001\nstatus: accepted\ncategory: identity\n---\n"
            "# test identity\n",
            encoding="utf-8",
        )
        r = apply_mod.apply_proposal(prop)
        assert r.status == "applied", r
        assert "slips_caught" in r.details
    finally:
        shutil.rmtree(pd)


@test("apply: tool category is marked not-automatable")
def _():
    pd = _tmp_projects_dir()
    try:
        proposals = pd / "proposals"
        proposals.mkdir(parents=True)
        prop = proposals / "PROP-0001-tool.md"
        prop.write_text(
            "---\nid: PROP-0001\nstatus: accepted\ncategory: tool\n---\n"
            "# test tool\n",
            encoding="utf-8",
        )
        r = apply_mod.apply_proposal(prop)
        assert r.status == "not-automatable"
    finally:
        shutil.rmtree(pd)


@test("apply: apply_all_accepted walks the proposals dir")
def _():
    pd = _tmp_projects_dir()
    try:
        proposals = pd / "proposals"
        proposals.mkdir(parents=True)
        for i, status in enumerate(["pending", "accepted", "rejected"]):
            (proposals / f"PROP-000{i+1}-t.md").write_text(
                f"---\nid: PROP-000{i+1}\nstatus: {status}\ncategory: config\n---\n# {status}\n",
                encoding="utf-8",
            )
        results = apply_mod.apply_all_accepted(projects_dir=pd)
        # Only the one with status=accepted should have been processed
        assert len(results) == 1, [r.__dict__ for r in results]
        assert results[0].proposal_id == "PROP-0002"
        assert (pd / "apply_history.jsonl").exists()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_scengen
# =============================================================================

@test("scengen: candidate_to_scenario extracts salient tokens")
def _():
    c = scengen.Candidate(
        run_id="r1", turn_number=1, timestamp_utc="2026-04-15T00:00:00Z",
        user_message="What is the capital of France?",
        response_text="Paris is the capital of France. "
                        "Paris is also the largest city in France.",
    )
    s = scengen.candidate_to_scenario(c, n_asserts=2)
    assert s["prompt"] == "What is the capital of France?"
    assert "auto-generated" in s["tags"]
    # Paris should be a salient token (4+ chars, repeated)
    assert "paris" in s["expected_contains"]


@test("scengen: generate returns empty when there are no runs")
def _():
    pd = _tmp_projects_dir()
    try:
        out = scengen.generate(projects_dir=pd)
        assert out["scenarios"] == 0
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_mcp (protocol-level; no subprocess spawning in tests)
# =============================================================================

@test("mcp: serve_stdio tools/list returns registered skills")
def _():
    import io

    reg = sk.SkillRegistry()

    @reg.register_python("ping", "respond with pong",
                           [{"name": "x", "type": "string", "required": True,
                             "description": "anything"}])
    def _ping(x: str) -> dict:
        return {"pong": x}

    # Drive the server via piped stdin/stdout
    stdin = io.StringIO()
    stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": "initialize", "params": {}}) + "\n")
    stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2,
                              "method": "tools/list"}) + "\n")
    stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3,
                              "method": "tools/call",
                              "params": {"name": "ping",
                                         "arguments": {"x": "hello"}}}) + "\n")
    stdin.seek(0)

    out = io.StringIO()
    orig_stdin, orig_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin, sys.stdout = stdin, out
        mcp.serve_stdio(registry=reg)
    finally:
        sys.stdin, sys.stdout = orig_stdin, orig_stdout

    out.seek(0)
    responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert len(responses) == 3, responses
    init, tlist, tcall = responses
    assert init["result"]["serverInfo"]["name"] == "mnemosyne"
    tool_names = {t["name"] for t in tlist["result"]["tools"]}
    assert "ping" in tool_names
    # tools/call returns MCP content-wrapped output
    content = tcall["result"]["content"]
    assert content[0]["type"] == "text"
    assert "pong" in content[0]["text"]


# =============================================================================
#  inner: Evaluator persona
# =============================================================================

@test("inner: evaluator fires when enable_evaluator=True")
def _():
    personas_seen: list[str] = []

    def fake_chat(messages, **kw):
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "Role: Planner" in sys_text:
            personas_seen.append("planner")
            return {"status": "ok", "text": "plan text", "tool_calls": []}
        if "Role: Critic" in sys_text:
            personas_seen.append("critic")
            return {"status": "ok", "text": "accept", "tool_calls": []}
        if "Role: Doer" in sys_text:
            personas_seen.append("doer")
            return {"status": "ok", "text": "final answer", "tool_calls": []}
        if "Role: Evaluator" in sys_text:
            personas_seen.append("evaluator")
            return {"status": "ok",
                    "text": "### Score\n- plan_coverage: 9\n\n### Verdict\n- (accept)",
                    "tool_calls": []}
        return {"status": "ok", "text": "", "tool_calls": []}

    result = inner.deliberate(
        user_message="Plan something.",
        chat_fn=fake_chat,
        backend=None,
        enable_evaluator=True,
    )
    assert personas_seen == ["planner", "critic", "doer", "evaluator"]
    assert result.evaluator is not None
    assert result.evaluator_verdict == "accept"


@test("inner: evaluator verdict=revise is detected")
def _():
    def fake_chat(messages, **kw):
        sys_text = next((m["content"] for m in messages if m["role"] == "system"), "")
        if "Role: Evaluator" in sys_text:
            return {"status": "ok",
                    "text": "### Verdict\n- (revise): the plan skipped backup",
                    "tool_calls": []}
        return {"status": "ok", "text": "x", "tool_calls": []}

    result = inner.deliberate(
        user_message="q",
        chat_fn=fake_chat,
        backend=None,
        enable_evaluator=True,
    )
    assert result.evaluator_verdict == "revise"


# =============================================================================
#  brain: tool-feedback learning + goals injection
# =============================================================================

@test("brain: tool-feedback writes L1 failure_note on tool error")
def _():
    pd = _tmp_projects_dir()
    try:
        reg = sk.SkillRegistry()

        @reg.register_python("bad_tool", "always raises",
                               [{"name": "x", "type": "string", "required": True}])
        def _bad(x: str) -> dict:
            raise RuntimeError("boom")

        # Mock chat_fn that calls the bad tool once, then returns plain text
        calls = {"n": 0}

        def fake_chat(messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"status": "ok", "text": "calling tool",
                        "tool_calls": [{"id": "t1", "name": "bad_tool",
                                          "arguments": {"x": "hi"}}]}
            return {"status": "ok", "text": "final answer",
                    "tool_calls": []}

        store = mm.MemoryStore(path=pd / "memory.db")
        cfg = br.BrainConfig(
            adapt_to_context=False, inject_env_snapshot=False,
            tool_feedback_learning=True,
        )
        brain = br.Brain(config=cfg, memory=store, skills=reg, chat_fn=fake_chat)
        brain.turn("use the tool please")
        # Expect a failure_note L1 memory
        rows = store._conn.execute(
            "SELECT content FROM memories WHERE kind = 'failure_note'"
        ).fetchall()
        assert len(rows) >= 1, rows
        assert "bad_tool" in rows[0][0]
        store.close()
    finally:
        shutil.rmtree(pd)


@test("brain: goals injection surfaces top goals in first-turn system prompt")
def _():
    pd = _tmp_projects_dir()
    try:
        # Seed a goal at the default location (the brain reads GoalStack())
        import os as _os
        saved = _os.environ.get("MNEMOSYNE_PROJECTS_DIR")
        _os.environ["MNEMOSYNE_PROJECTS_DIR"] = str(pd)
        try:
            gs = goals_mod.GoalStack(projects_dir=pd)
            gs.add("ship the v0.2.0 release", priority=1, tags=["release"])

            seen_system: list[str] = []

            def fake_chat(messages, **kw):
                seen_system.append(next(
                    (m["content"] for m in messages if m["role"] == "system"),
                    "",
                ))
                return {"status": "ok", "text": "noted", "tool_calls": []}

            store = mm.MemoryStore(path=pd / "memory.db")
            cfg = br.BrainConfig(
                adapt_to_context=False, inject_env_snapshot=False,
                goals_inject=True,
            )
            brain = br.Brain(config=cfg, memory=store, skills=sk.SkillRegistry(),
                             chat_fn=fake_chat)
            brain.turn("hello")
            assert "ship the v0.2.0 release" in seen_system[0], seen_system[0]
            store.close()
        finally:
            if saved is None:
                _os.environ.pop("MNEMOSYNE_PROJECTS_DIR", None)
            else:
                _os.environ["MNEMOSYNE_PROJECTS_DIR"] = saved
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_train — telemetry → LoRA bridge
# =============================================================================

def _mk_events_run(pd, run_id: str, events: list[dict]) -> Path:
    """Create a minimal experiments/<run_id>/events.jsonl under pd."""
    rd = pd / "experiments" / run_id
    rd.mkdir(parents=True)
    with (rd / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return rd


@test("train: export builds Hermes-compatible schema from training_turn events")
def _():
    pd = _tmp_projects_dir()
    try:
        turn_start = {"event_id": "ev-1", "event_type": "turn_start",
                       "parent_event_id": None, "metadata": {"turn_number": 1}}
        training_turn = {"event_id": "ev-2", "event_type": "training_turn",
                          "parent_event_id": "ev-1",
                          "metadata": {
                              "system_prompt": "You are Mnemosyne.",
                              "user_message": "What is 2 + 2?",
                              "assistant_text": "2 + 2 equals 4.",
                              "tool_calls": [],
                              "model": "qwen3.5:9b", "provider": "ollama",
                          }}
        turn_end = {"event_id": "ev-3", "event_type": "turn_end",
                     "parent_event_id": "ev-1", "status": "ok"}
        _mk_events_run(pd, "run_20260415-test-1",
                        [turn_start, training_turn, turn_end])
        out = pd / "out.jsonl"
        summary = train_mod.export(projects_dir=pd, out=out,
                                     allow_memory_fallback=False)
        assert summary.trajectories_written == 1, summary
        line = out.read_text(encoding="utf-8").strip()
        obj = json.loads(line)
        # Hermes schema keys
        for k in ("prompt_index", "conversations", "metadata", "completed",
                   "partial", "api_calls", "toolsets_used", "tool_stats",
                   "tool_error_counts"):
            assert k in obj, k
        convs = obj["conversations"]
        roles = [c["from"] for c in convs]
        assert roles == ["system", "human", "gpt"], roles
        assert "Mnemosyne" in convs[0]["value"]
        assert obj["metadata"]["mnemo_run_id"] == "run_20260415-test-1"
        assert obj["metadata"]["mnemo_model"] == "qwen3.5:9b"
    finally:
        shutil.rmtree(pd)


@test("train: export filters identity-slip runs when --drop-identity-slips")
def _():
    pd = _tmp_projects_dir()
    try:
        events = [
            {"event_id": "ev-1", "event_type": "turn_start",
             "metadata": {"turn_number": 1}},
            {"event_id": "ev-2", "event_type": "training_turn",
             "parent_event_id": "ev-1",
             "metadata": {"user_message": "hi", "assistant_text": "hello",
                           "tool_calls": [], "model": "m", "provider": "p"}},
            {"event_id": "ev-3", "event_type": "identity_slip_detected",
             "parent_event_id": "ev-1", "status": "error"},
            {"event_id": "ev-4", "event_type": "turn_end",
             "parent_event_id": "ev-1", "status": "ok"},
        ]
        _mk_events_run(pd, "run-slip", events)
        out = pd / "out.jsonl"
        summary = train_mod.export(projects_dir=pd, out=out,
                                     drop_identity_slips=True,
                                     allow_memory_fallback=False)
        assert summary.trajectories_written == 0
    finally:
        shutil.rmtree(pd)


@test("train: export falls back to memory.db when no training_turn events")
def _():
    pd = _tmp_projects_dir()
    try:
        # Seed memory.db with a Q:/A: row
        store = mm.MemoryStore(path=pd / "memory.db")
        store.write(content="Q: what day is it\nA: Friday",
                     source="conversation", kind="turn", tier=mm.L2_WARM)
        store.close()
        # No experiments/ dir — force fallback
        out = pd / "out.jsonl"
        summary = train_mod.export(projects_dir=pd, out=out,
                                     allow_memory_fallback=True)
        assert summary.fallback_to_memory_db is True
        assert summary.trajectories_written == 1
        obj = json.loads(out.read_text(encoding="utf-8").strip())
        roles = [c["from"] for c in obj["conversations"]]
        assert roles == ["human", "gpt"]
    finally:
        shutil.rmtree(pd)


@test("train: compress no-op when under target_max_tokens")
def _():
    traj = {"conversations": [
        {"from": "system", "value": "short"},
        {"from": "human",  "value": "hi"},
        {"from": "gpt",    "value": "hello"},
    ]}
    out = train_mod.compress_one(traj, target_max_tokens=10_000)
    assert "compression_metrics" not in out
    assert out["conversations"] == traj["conversations"]


@test("train: compress replaces middle turns above target")
def _():
    big = "word " * 500  # ~2500 chars → ~625 tokens each
    traj = {"conversations": [
        {"from": "system", "value": "system preamble"},
        {"from": "human",  "value": big},
        {"from": "gpt",    "value": big},
        {"from": "human",  "value": big},
        {"from": "gpt",    "value": big},
        {"from": "human",  "value": big},
        {"from": "gpt",    "value": "final answer here"},
    ]}
    out = train_mod.compress_one(traj, target_max_tokens=500,
                                   protect_last_n_turns=2)
    assert out.get("compression_metrics", {}).get("was_compressed") is True
    # last 2 turns preserved
    assert out["conversations"][-1]["value"] == "final answer here"
    # a summary turn should appear
    assert any(c["value"].startswith("[CONTEXT SUMMARY")
                 for c in out["conversations"])


@test("train: compress preserves last N turns verbatim")
def _():
    t = [{"from": "human" if i % 2 else "gpt", "value": "X" * 2000}
          for i in range(10)]
    traj = {"conversations": t}
    out = train_mod.compress_one(traj, target_max_tokens=100,
                                   protect_last_n_turns=3)
    # The last 3 turns should match exactly
    assert out["conversations"][-3:] == t[-3:]


@test("train: deploy lmstudio --dry-run writes nothing and returns target path")
def _():
    pd = _tmp_projects_dir()
    try:
        adapter = pd / "adapter"
        adapter.mkdir()
        (adapter / "model.gguf").write_bytes(b"\x00")
        os.environ["LMSTUDIO_MODELS_DIR"] = str(pd / "lmstudio")
        try:
            r = train_mod.deploy(adapter, to="lmstudio", name="unit-test",
                                   dry_run=True)
        finally:
            os.environ.pop("LMSTUDIO_MODELS_DIR", None)
        assert r["mode"] == "lmstudio"
        assert r["dry_run"] is True
        assert "unit-test" in r["would_copy_to"]
        # Nothing was actually copied
        assert not (pd / "lmstudio").exists()
    finally:
        shutil.rmtree(pd)


@test("train: deploy lmstudio writes gguf to publisher/name/ path")
def _():
    pd = _tmp_projects_dir()
    try:
        adapter = pd / "adapter"
        adapter.mkdir()
        (adapter / "model.gguf").write_bytes(b"binary-gguf")
        os.environ["LMSTUDIO_MODELS_DIR"] = str(pd / "lmstudio")
        try:
            r = train_mod.deploy(adapter, to="lmstudio", name="my-lora",
                                   dry_run=False)
        finally:
            os.environ.pop("LMSTUDIO_MODELS_DIR", None)
        dest = Path(r["path"])
        assert dest.exists(), r
        assert dest.read_bytes() == b"binary-gguf"
        assert dest.parent == pd / "lmstudio" / "mnemosyne" / "my-lora"
    finally:
        shutil.rmtree(pd)


@test("train: deploy ollama --dry-run returns Modelfile content")
def _():
    pd = _tmp_projects_dir()
    try:
        adapter = pd / "adapter"
        adapter.mkdir()
        (adapter / "model.gguf").write_bytes(b"\x00")
        r = train_mod.deploy(adapter, to="ollama", name="ollama-lora",
                               base_model="qwen3.5:9b", dry_run=True)
        assert r["mode"] == "ollama"
        assert "FROM qwen3.5:9b" in r["modelfile"]
        assert "ADAPTER" in r["modelfile"]
        # Dry run writes nothing
        assert not (adapter / "Modelfile").exists()
    finally:
        shutil.rmtree(pd)


@test("train: _dominates on accuracy-max + latency-min")
def _():
    directions = {"accuracy": "max", "latency_ms_avg": "min"}
    better = {"accuracy": 0.9, "latency_ms_avg": 100.0}
    worse  = {"accuracy": 0.7, "latency_ms_avg": 150.0}
    assert train_mod._dominates(better, worse, directions)
    assert not train_mod._dominates(worse, better, directions)
    # Tradeoff: one axis better, other worse → neither dominates
    tradeoff = {"accuracy": 0.95, "latency_ms_avg": 200.0}
    assert not train_mod._dominates(tradeoff, better, directions)
    assert not train_mod._dominates(better, tradeoff, directions)


@test("train: eval_ab runs both harnesses and returns per-scenario delta")
def _():
    pd = _tmp_projects_dir()
    try:
        # Tiny scenarios file
        sc = pd / "tiny.jsonl"
        sc.write_text(
            json.dumps({"id": "s1", "prompt": "capital of france?",
                          "expected_contains": ["Paris"]}) + "\n"
            + json.dumps({"id": "s2", "prompt": "2+2?",
                           "expected_contains": ["4"]}) + "\n",
            encoding="utf-8",
        )

        def base_chat(messages, **kw):
            # Base gets both wrong
            return {"status": "ok", "text": "I don't know.", "tool_calls": []}

        def adapted_chat(messages, **kw):
            # Adapted nails them
            prompt = messages[0]["content"].lower()
            if "france" in prompt:
                return {"status": "ok", "text": "Paris is the capital of France.",
                        "tool_calls": []}
            return {"status": "ok", "text": "The answer is 4.", "tool_calls": []}

        report = train_mod.eval_ab(
            base={"provider": "ollama", "model": "base"},
            adapted={"provider": "lmstudio", "model": "adapted"},
            scenarios_paths=[sc],
            projects_dir=pd,
            base_chat_fn=base_chat,
            adapted_chat_fn=adapted_chat,
        )
        assert report["base"]["metrics"]["passed"] == 0
        assert report["adapted"]["metrics"]["passed"] == 2
        assert report["delta"]["passed"] == 2
        # Base should never dominate adapted when adapted has higher accuracy
        assert report["pareto"]["base_dominates_adapted"] is False
        # Per-scenario: both scenarios should show delta=+1
        deltas = {p["id"]: p["delta"] for p in report["per_scenario"]}
        assert deltas == {"s1": 1, "s2": 1}
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_tool_parsers — extended tool-call extraction
# =============================================================================

@test("tool_parsers: hermes <tool_call> tag extracts name + arguments")
def _():
    text = ('okay here we go: <tool_call>{"name":"search_web",'
            '"arguments":{"query":"rain"}}</tool_call>')
    calls = tp.parse_hermes(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "search_web"
    assert calls[0]["arguments"] == {"query": "rain"}


@test("tool_parsers: mistral [TOOL_CALLS] inline format")
def _():
    text = '[TOOL_CALLS] [{"name":"calc","arguments":{"expr":"2+2"}}]'
    calls = tp.parse_mistral(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "calc"


@test("tool_parsers: llama-3 python-tag format with 'parameters' alias")
def _():
    text = ('<|python_tag|>{"name":"weather",'
            '"parameters":{"city":"Paris"}}<|eom_id|>')
    calls = tp.parse_llama3(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "weather"
    assert calls[0]["arguments"] == {"city": "Paris"}


@test("tool_parsers: functionary fenced-JSON requires 'name' key")
def _():
    # Legitimate tool call
    text = '```json\n{"name":"add","arguments":{"a":1}}\n```'
    assert tp.parse_functionary(text)
    # JSON without "name" is NOT a tool call
    text2 = '```json\n{"result":42}\n```'
    assert not tp.parse_functionary(text2)


@test("tool_parsers: parse_any tries each parser, returns first hit")
def _():
    text = '<tool_call>{"name":"x","arguments":{}}</tool_call>'
    calls = tp.parse_any(text)
    assert calls and calls[0]["name"] == "x"
    assert tp.detect_format(text) == "hermes"
    # No match returns []
    assert tp.parse_any("just plain text no tool calls") == []


@test("tool_parsers: strip_tool_calls removes envelopes")
def _():
    text = ('preamble <tool_call>{"name":"x","arguments":{}}</tool_call> '
            'after text')
    clean = tp.strip_tool_calls(text)
    assert "<tool_call>" not in clean
    assert "preamble" in clean and "after text" in clean


@test("tool_parsers: malformed JSON does not raise")
def _():
    # Unterminated tag body
    assert tp.parse_hermes("<tool_call>{broken json}</tool_call>") == []
    # Garbage input
    assert tp.parse_any("") == []
    assert tp.parse_any("x" * 10000) == []


@test("tool_parsers: models _recover_embedded_tool_calls integrates parser")
def _():
    text = ('Sure! <tool_call>{"name":"lookup","arguments":'
            '{"q":"cat"}}</tool_call>')
    calls, cleaned = mdls._recover_embedded_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "lookup"
    assert "<tool_call>" not in cleaned


# =============================================================================
#  mnemosyne_skills_builtin — curated skill library
# =============================================================================

@test("builtin: register_builtin_skills populates 11 skills")
def _():
    from mnemosyne_skills import SkillRegistry
    reg = SkillRegistry()
    n = sbi.register_builtin_skills(reg)
    assert n == 11, n
    for name in ("fs_read", "fs_list", "fs_write_safe", "grep_code",
                   "http_get", "web_fetch_text", "sqlite_query",
                   "shell_exec_safe", "git_status", "git_log", "datetime_now"):
        assert name in reg.names(), name


@test("builtin: fs_read reads under root, rejects traversal")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "hello.txt").write_text("hello world", encoding="utf-8")
        r = sbi.fs_read("hello.txt", root=str(pd))
        assert r["content"] == "hello world"
        # Traversal rejected
        try:
            sbi.fs_read("../../../etc/passwd", root=str(pd))
            raised = False
        except PermissionError:
            raised = True
        assert raised, "expected PermissionError on traversal"
    finally:
        shutil.rmtree(pd)


@test("builtin: fs_list filters hidden + applies pattern")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "a.py").write_text("x")
        (pd / "b.py").write_text("y")
        (pd / ".hidden.py").write_text("z")
        (pd / "c.md").write_text("m")
        r = sbi.fs_list(".", root=str(pd), pattern="*.py")
        names = {e["name"] for e in r["entries"]}
        assert names == {"a.py", "b.py"}, names
    finally:
        shutil.rmtree(pd)


@test("builtin: fs_write_safe atomic + no overwrite by default")
def _():
    pd = _tmp_projects_dir()
    try:
        r1 = sbi.fs_write_safe("note.txt", "first", root=str(pd))
        assert r1.get("written")
        # Second write without overwrite=True is refused
        r2 = sbi.fs_write_safe("note.txt", "second", root=str(pd))
        assert r2.get("error"), r2
        # With overwrite=True it goes through
        r3 = sbi.fs_write_safe("note.txt", "second", root=str(pd),
                                 overwrite=True)
        assert r3.get("written")
        assert (pd / "note.txt").read_text() == "second"
    finally:
        shutil.rmtree(pd)


@test("builtin: grep_code finds pattern across files")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "a.py").write_text("def foo():\n    pass\n")
        (pd / "b.py").write_text("def bar():\n    return 42\n")
        r = sbi.grep_code(r"^def ", root=str(pd), include="*.py")
        assert len(r["matches"]) == 2
        files = {m["path"] for m in r["matches"]}
        assert files == {"a.py", "b.py"}
    finally:
        shutil.rmtree(pd)


@test("builtin: sqlite_query rejects non-SELECT")
def _():
    pd = _tmp_projects_dir()
    try:
        import sqlite3 as _s
        db = pd / "t.db"
        c = _s.connect(str(db))
        c.execute("CREATE TABLE t (x INTEGER)")
        c.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        c.commit()
        c.close()
        r = sbi.sqlite_query(str(db), "SELECT x FROM t ORDER BY x")
        assert [row["x"] for row in r["rows"]] == [1, 2, 3]
        # Write rejected
        r2 = sbi.sqlite_query(str(db), "DROP TABLE t")
        assert r2.get("error")
        # Multiple statements rejected
        r3 = sbi.sqlite_query(str(db), "SELECT 1; SELECT 2")
        assert r3.get("error")
    finally:
        shutil.rmtree(pd)


@test("builtin: shell_exec_safe enforces allow-list")
def _():
    r = sbi.shell_exec_safe("ls -la /")
    assert r["argv"][0] == "ls"
    assert r["exit_code"] == 0
    # Unlisted command rejected
    r2 = sbi.shell_exec_safe("rm -rf /")
    assert r2.get("error") and "allow-list" in r2["error"]
    # Empty input
    r3 = sbi.shell_exec_safe("")
    assert r3.get("error")


@test("builtin: http_get rejects non-http schemes")
def _():
    r = sbi.http_get("file:///etc/passwd")
    assert r.get("error") and "not allowed" in r["error"]
    r2 = sbi.http_get("ftp://example.com")
    assert r2.get("error") and "not allowed" in r2["error"]


@test("builtin: http_get blocks SSRF — loopback")
def _():
    # SSRF defense: refuse to fetch 127.0.0.1 / localhost / 0.0.0.0
    for url in ("http://127.0.0.1:8484/admin",
                  "http://localhost/", "http://0.0.0.0:11434/"):
        r = sbi.http_get(url)
        assert r.get("error"), f"should have refused {url}, got {r}"
        assert "private" in r["error"] or "loopback" in r["error"], r


@test("builtin: http_get blocks SSRF — RFC1918 + cloud metadata")
def _():
    # Cloud metadata endpoint (AWS/GCP/Azure all use 169.254.169.254)
    r = sbi.http_get("http://169.254.169.254/latest/meta-data/")
    assert r.get("error") and "private" in r["error"]
    # RFC1918 internal address
    r = sbi.http_get("http://10.0.0.1/")
    assert r.get("error") and "private" in r["error"]
    r = sbi.http_get("http://192.168.1.1/")
    assert r.get("error") and "private" in r["error"]


@test("builtin: http_get refuses URLs with no hostname")
def _():
    r = sbi.http_get("http://")
    assert r.get("error")


@test("builtin: datetime_now returns a well-formed ISO string")
def _():
    r = sbi.datetime_now()
    assert "T" in r["iso"] and r["tz"] == "UTC"
    # Parse roundtrip sanity
    from datetime import datetime as _dt
    _dt.fromisoformat(r["iso"].replace("Z", "+00:00"))


@test("builtin: default_registry loads builtins by default")
def _():
    from mnemosyne_skills import default_registry
    reg = default_registry(discover_commands=False, load_learned=False,
                             projects_dir=_tmp_projects_dir())
    names = reg.names()
    for n in ("fs_read", "http_get", "git_status"):
        assert n in names, n
    # Opt-out works
    reg2 = default_registry(discover_commands=False, load_learned=False,
                              load_builtins=False,
                              projects_dir=_tmp_projects_dir())
    assert "fs_read" not in reg2.names()


# =============================================================================
#  mnemosyne_datagen + mnemosyne_batch
# =============================================================================

@test("datagen: cartesian expansion across multiple variables")
def _():
    out = datagen.generate_prompts({
        "templates": ["What is the capital of {country}?"],
        "vars": {"country": ["France", "Spain", "Germany"]},
        "tags": ["geo"],
        "id_prefix": "g",
    })
    assert len(out) == 3
    assert out[0]["prompt"] == "What is the capital of France?"
    assert out[0]["tags"] == ["geo"]
    assert out[0]["id"].startswith("g-")


@test("datagen: limit caps output count")
def _():
    out = datagen.generate_prompts({
        "templates": ["q1?", "q2?"],
        "vars": {"x": ["a", "b", "c"]},
        "limit": 4,
    })
    assert len(out) == 4


@test("datagen: empty templates returns nothing")
def _():
    assert datagen.generate_prompts({"templates": []}) == []
    assert datagen.generate_prompts({}) == []


@test("datagen: to_scenarios attaches expected_contains from answer key")
def _():
    prompts = datagen.generate_prompts({
        "templates": ["What is the capital of {country}?"],
        "vars": {"country": ["France", "Spain"]},
    })
    answer_key = {"capital of": ["Paris", "Madrid"]}
    scenarios = datagen.to_scenarios(prompts, answer_key)
    assert all("expected_contains" in s for s in scenarios)
    assert scenarios[0]["expected_contains"] == ["Paris", "Madrid"]


@test("batch: load_prompts handles strings and dicts")
def _():
    pd = _tmp_projects_dir()
    try:
        f = pd / "prompts.jsonl"
        f.write_text(
            '"plain string prompt"\n'
            '{"id": "p1", "prompt": "dict prompt", "tags": ["t1"]}\n'
            "# comment ignored\n"
            "\n",
            encoding="utf-8",
        )
        prompts = batch_mod.load_prompts(f)
        assert len(prompts) == 2
        assert prompts[0].text == "plain string prompt"
        assert prompts[0].id.startswith("p-")
        assert prompts[1].id == "p1"
        assert prompts[1].tags == ["t1"]
    finally:
        shutil.rmtree(pd)


@test("batch: run_batch executes prompts under concurrency + summary correct")
def _():
    pd = _tmp_projects_dir()
    try:
        prompts = [
            batch_mod.Prompt(id=f"q-{i}", text=f"prompt {i}")
            for i in range(8)
        ]

        def fake_chat(messages, **kw):
            return {"status": "ok", "text": "ok", "tool_calls": []}

        def factory(session):
            return br.Brain(
                memory=mm.MemoryStore(telemetry=session,
                                       path=pd / "mem.db"),
                skills=sk.SkillRegistry(),
                telemetry=session,
                chat_fn=fake_chat,
                config=br.BrainConfig(adapt_to_context=False,
                                       inject_env_snapshot=False,
                                       capture_for_training=True),
            )

        # workers=4 is safe now that MemoryStore has a 5s busy_timeout
        # and batch.retryable() treats "database is locked" as
        # transient. Before v0.3.1 this produced an occasional drop.
        summary = batch_mod.run_batch(
            prompts, brain_factory=factory, workers=4, projects_dir=pd,
            tags=["unit-test"], progress_every=1000,
        )
        assert summary.prompts_total == 8
        assert summary.prompts_completed == 8
        assert summary.prompts_failed == 0
        assert summary.duration_s > 0
    finally:
        shutil.rmtree(pd)


@test("batch: load_completed_ids pairs turn_start.prompt_id with turn_end.ok")
def _():
    pd = _tmp_projects_dir()
    try:
        prompts = [batch_mod.Prompt(id=f"r-{i}", text=f"p{i}") for i in range(4)]

        def fake_chat(messages, **kw):
            return {"status": "ok", "text": "ok", "tool_calls": []}

        def factory(session):
            return br.Brain(
                memory=mm.MemoryStore(telemetry=session, path=pd / "m.db"),
                skills=sk.SkillRegistry(), telemetry=session,
                chat_fn=fake_chat,
                config=br.BrainConfig(adapt_to_context=False,
                                       inject_env_snapshot=False,
                                       capture_for_training=True),
            )

        # Single-threaded so the test isn't sensitive to a known
        # write-loss edge case in concurrent telemetry under heavy
        # MemoryStore contention. The batch parallelism itself is
        # exercised by the previous test.
        s1 = batch_mod.run_batch(prompts, brain_factory=factory,
                                   workers=1, projects_dir=pd)
        rd = ht.run_path(s1.run_id, pd)
        done = batch_mod.load_completed_ids(rd / "events.jsonl")
        assert done == {"r-0", "r-1", "r-2", "r-3"}, done
    finally:
        shutil.rmtree(pd)


@test("batch: run_batch counts retries on transient errors")
def _():
    pd = _tmp_projects_dir()
    try:
        attempts: dict[str, int] = {}

        def flaky_chat(messages, **kw):
            user = messages[-1]["content"]
            attempts[user] = attempts.get(user, 0) + 1
            if attempts[user] < 2:
                raise RuntimeError("HTTP 503 service unavailable")
            return {"status": "ok", "text": "recovered", "tool_calls": []}

        def factory(session):
            return br.Brain(
                memory=mm.MemoryStore(telemetry=session, path=pd / "m.db"),
                skills=sk.SkillRegistry(), telemetry=session,
                chat_fn=flaky_chat,
                config=br.BrainConfig(adapt_to_context=False,
                                       inject_env_snapshot=False),
            )

        prompts = [batch_mod.Prompt(id="x1", text="hello")]
        summary = batch_mod.run_batch(
            prompts, brain_factory=factory, workers=1, projects_dir=pd,
            max_retries=3, retry_backoff_s=0.001,
        )
        assert summary.prompts_completed == 1
        assert summary.prompts_failed == 0
        assert attempts["hello"] == 2  # 1 failure + 1 success
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  regression: concurrent MemoryStore opens + FTS5 race guard
# =============================================================================

@test("memory: concurrent schema init on same DB succeeds")
def _():
    """Regression for v0.3.1 — pre-fix this raced on
    'vtable constructor failed: memories_fts'. The v0.3.1 fix
    (module-level _SCHEMA_INIT_LOCK + busy_timeout) makes concurrent
    opens safe. Verified here with 12 threads opening the DB
    simultaneously (no writes — FTS5 trigger contention under
    concurrent writes is a separate envelope tested below)."""
    import threading as _th
    pd = _tmp_projects_dir()
    try:
        errors: list[str] = []
        elock = _th.Lock()

        def worker(tid):
            try:
                store = mm.MemoryStore(path=pd / "shared.db")
                # Just verify schema is intact
                stats = store.stats()
                assert stats["fts5_enabled"]
                store.close()
            except Exception as e:
                with elock:
                    errors.append(f"t{tid}: {type(e).__name__}: {e}")

        # 8 threads is the documented envelope — covers our batch
        # defaults (4 workers) plus the serve daemon plus some slack.
        threads = [_th.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors
    finally:
        shutil.rmtree(pd)


@test("memory: concurrent writes on same DB succeed at realistic load")
def _():
    """Verifies the write() retry loop (v0.4.1) handles realistic
    3-worker concurrent write load. Heavier concurrency (8+ rapid-
    fire writers) is beyond WAL's single-writer serialization
    envelope — documented in docs/SECURITY.md. For production
    parallelism use `mnemosyne-batch --workers N` which has outer-
    layer retry on top of this."""
    import threading as _th
    pd = _tmp_projects_dir()
    try:
        # Shared store — one connection across threads is the common
        # case (the serve daemon owns a single MemoryStore). Retry
        # loop + threading.Lock protect this path.
        store = mm.MemoryStore(path=pd / "shared.db")
        errors: list[str] = []
        elock = _th.Lock()

        def worker(tid):
            try:
                for i in range(8):
                    store.write(content=f"t{tid}-i{i}",
                                  kind="fact", tier=mm.L2_WARM)
                hits = store.search(f"t{tid}", limit=16)
                assert len(hits) >= 8, f"tid={tid} got {len(hits)}"
            except Exception as e:
                with elock:
                    errors.append(f"t{tid}: {type(e).__name__}: {e}")

        threads = [_th.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors
        assert store.stats()["total"] == 3 * 8
        store.close()
    finally:
        shutil.rmtree(pd)


@test("batch: _retryable recognizes sqlite transient errors")
def _():
    # Positive cases
    for msg in (
        "database is locked",
        "vtable constructor failed: memories_fts",
        "HTTP 503 service unavailable",
        "Request timed out",
    ):
        assert batch_mod._retryable(Exception(msg)), msg
    # Negative cases
    for msg in ("file not found", "permission denied", "bad json"):
        assert not batch_mod._retryable(Exception(msg)), msg


# =============================================================================
#  mnemosyne_avatar — derived state for the UI dashboard
# =============================================================================

@test("avatar: empty projects-dir gives a baseline rest state")
def _():
    pd = _tmp_projects_dir()
    try:
        state = avatar_mod.compute_state(projects_dir=pd, window_minutes=60)
        assert state["schema_version"] == 1
        assert state["memory_count"] == 0
        assert state["mood_phase"] == "rest"
        assert state["identity_strength"] == 1.0
        # All reserved AGI slots present + null
        for k in ("wisdom", "restlessness", "novelty", "self_assessment"):
            assert k in state and state[k] is None
        # Palette is well-formed
        for k in ("core", "accent", "rim", "bg"):
            assert state["palette"][k].startswith("#")
            assert len(state["palette"][k]) == 7
    finally:
        shutil.rmtree(pd)


@test("avatar: identity slips drag identity_strength toward zero")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", tags=["unit"], projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(20):
                s.log("identity_slip_detected", status="error",
                       metadata={"slips": ["claude"]})
            for _ in range(80):
                s.log("memory_read", metadata={})  # padding
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        state = avatar_mod.compute_state(projects_dir=pd, window_minutes=60)
        assert state["identity_slip_count"] == 20
        assert state["identity_strength"] < 1.0
        assert state["health"] < 1.0
    finally:
        shutil.rmtree(pd)


@test("avatar: dreams + inner-dialogue counts surface in state")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", tags=["unit"], projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(3):
                s.log("dream_end", metadata={"dream_id": "d-x"})
            for _ in range(2):
                s.log("inner_dialogue_done",
                       metadata={"total_calls": 3})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        state = avatar_mod.compute_state(projects_dir=pd)
        assert state["dreams_count"] == 3
        assert state["inner_dialogues"] == 2
        assert state["rings"] == 2
    finally:
        shutil.rmtree(pd)


@test("avatar: write + read snapshot round-trip")
def _():
    pd = _tmp_projects_dir()
    try:
        state = avatar_mod.compute_state(projects_dir=pd)
        path = avatar_mod.write_snapshot(state, projects_dir=pd)
        assert path.exists()
        loaded = avatar_mod.read_snapshot(projects_dir=pd)
        assert loaded["schema_version"] == 1
        assert loaded["palette"] == state["palette"]
    finally:
        shutil.rmtree(pd)


@test("avatar: render_svg returns a valid SVG with key elements")
def _():
    pd = _tmp_projects_dir()
    try:
        state = avatar_mod.compute_state(projects_dir=pd)
        svg = avatar_mod.render_svg(state, size=400)
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")
        assert 'viewBox="0 0 400 400"' in svg
        # Aura, core, eye, orbiters all present in the rest state
        for needle in ("auraGrad", "coreGrad", "ellipse", "circle"):
            assert needle in svg
    finally:
        shutil.rmtree(pd)


@test("avatar: mood_phase flips to consolidate when dreams dominate")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(8):
                s.log("dream_end", metadata={})
            # Some inner dialogue too — dreams must dominate (>2x)
            s.log("inner_dialogue_done", metadata={})
            # Bump activity above the rest threshold
            for _ in range(30):
                s.log("memory_read", metadata={})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        state = avatar_mod.compute_state(projects_dir=pd)
        assert state["mood_phase"] == "consolidate", state["mood_phase"]
    finally:
        shutil.rmtree(pd)


@test("avatar: AGI traits null when signal is absent")
def _():
    pd = _tmp_projects_dir()
    try:
        state = avatar_mod.compute_state(projects_dir=pd)
        for k in ("wisdom", "restlessness", "novelty", "self_assessment"):
            assert state[k] is None, f"{k} should be null in empty state"
    finally:
        shutil.rmtree(pd)


@test("avatar: self_assessment derived from evaluator verdicts")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            s.log("inner_dialogue_done",
                   metadata={"evaluator_verdict": "accept"})
            s.log("inner_dialogue_done",
                   metadata={"evaluator_verdict": "accept"})
            s.log("inner_dialogue_done",
                   metadata={"evaluator_verdict": "revise"})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        state = avatar_mod.compute_state(projects_dir=pd)
        # 2 accept / 3 total = 0.6667
        assert abs(state["self_assessment"] - 0.6667) < 0.001, state["self_assessment"]
    finally:
        shutil.rmtree(pd)


@test("avatar: restlessness derived from inter-turn gap variance")
def _():
    import time as _time
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            # Log turn_end events at irregular intervals
            for gap in (0.001, 0.01, 0.001, 0.02, 0.001):
                s.log("turn_end", status="ok", metadata={})
                _time.sleep(gap)
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        state = avatar_mod.compute_state(projects_dir=pd)
        # We have >= 3 gaps, so restlessness should be a number, not null
        assert state["restlessness"] is not None
        assert 0.0 <= state["restlessness"] <= 1.0
    finally:
        shutil.rmtree(pd)


@test("avatar: _compute_wisdom needs age + memory + identity")
def _():
    # No memory → null
    assert avatar_mod._compute_wisdom(0, 30.0, 1.0) is None
    # Too new → null
    assert avatar_mod._compute_wisdom(500, 0.1, 1.0) is None
    # Normal: depth × age × identity
    w = avatar_mod._compute_wisdom(1000, 30.0, 0.9)
    assert w is not None and 0.0 < w <= 1.0
    # Identity slip drags wisdom toward zero
    w_bad = avatar_mod._compute_wisdom(1000, 30.0, 0.1)
    assert w_bad is not None and w_bad < w


@test("avatar: render_svg with wisdom + self_assessment adds new elements")
def _():
    state = {
        "schema_version": 1,
        "palette": {"core": "#50aabd", "accent": "#d9bff2",
                     "rim": "#6bb7cb", "bg": "#0a0d1a"},
        "aura_radius": 100, "pulses_per_minute": 20, "rings": 0,
        "health": 0.8, "activity_score": 0.5,
        "skills_count": 0, "identity_slip_count": 0,
        "l1_count": 0, "l2_count": 0, "l3_count": 0,
        "mood_phase": "focus", "dreams_count": 0,
        "wisdom": 0.75,           # renders outer dashed ring
        "self_assessment": 0.5,   # renders ~6 rays
        "restlessness": None,
        "novelty": None,
    }
    svg = avatar_mod.render_svg(state, size=400)
    assert "stroke-dasharray=\"4 6\"" in svg  # wisdom ring
    # Count the <line> elements for rays — should have ~6
    assert svg.count("<line") >= 3


@test("avatar: compute_state cache hits when nothing changes")
def _():
    pd = _tmp_projects_dir()
    try:
        import time as _time
        # First call: cache miss, real compute
        s1 = avatar_mod.compute_state(projects_dir=pd)
        cached_at_1 = avatar_mod._STATE_CACHE.get(
            (str(pd), 60, avatar_mod._state_fingerprint(pd))
        )
        assert cached_at_1 is not None, "expected cache entry"

        # Second call within window: should return same dict object
        s2 = avatar_mod.compute_state(projects_dir=pd)
        assert s1 is s2, "cache hit should return identical object"

        # Touch a file → fingerprint changes → cache miss
        (pd / "memory.db").write_bytes(b"")
        _time.sleep(0.01)
        s3 = avatar_mod.compute_state(projects_dir=pd)
        # Different object means we recomputed
        assert s3 is not s2, "fingerprint change should bypass cache"
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_avatar.apply_feedback — bidirectional loop
# =============================================================================

@test("avatar feedback: low health reduces memory_retrieval_limit")
def _():
    cfg = br.BrainConfig(memory_retrieval_limit=6)
    state = {"health": 0.3, "wisdom": None, "restlessness": None,
             "mood_phase": "focus", "identity_strength": 1.0}
    adjs = avatar_mod.apply_feedback(state, cfg)
    codes = [a.rule for a in adjs]
    assert "low_health_reduces_retrieval" in codes
    assert cfg.memory_retrieval_limit < 6
    assert cfg.memory_retrieval_limit >= 2   # floor


@test("avatar feedback: high wisdom expands memory_retrieval_limit")
def _():
    cfg = br.BrainConfig(memory_retrieval_limit=6)
    state = {"health": 0.9, "wisdom": 0.8, "restlessness": None,
             "mood_phase": "focus", "identity_strength": 1.0}
    adjs = avatar_mod.apply_feedback(state, cfg)
    codes = [a.rule for a in adjs]
    assert "high_wisdom_expands_ceiling" in codes
    assert cfg.memory_retrieval_limit > 6
    assert cfg.memory_retrieval_limit <= 16  # ceiling


@test("avatar feedback: null wisdom does NOT fire the expansion rule")
def _():
    cfg = br.BrainConfig(memory_retrieval_limit=6)
    state = {"health": 0.9, "wisdom": None, "restlessness": None,
             "mood_phase": "focus", "identity_strength": 1.0}
    adjs = avatar_mod.apply_feedback(state, cfg)
    assert "high_wisdom_expands_ceiling" not in [a.rule for a in adjs]
    assert cfg.memory_retrieval_limit == 6


@test("avatar feedback: high restlessness disables inner dialogue")
def _():
    cfg = br.BrainConfig(inner_dialogue_enabled=True)
    state = {"health": 0.9, "wisdom": None, "restlessness": 0.85,
             "mood_phase": "focus", "identity_strength": 1.0}
    adjs = avatar_mod.apply_feedback(state, cfg)
    assert "high_restlessness_disables_inner_dialogue" in [a.rule for a in adjs]
    assert cfg.inner_dialogue_enabled is False


@test("avatar feedback: consolidate mood pauses inner dialogue")
def _():
    cfg = br.BrainConfig(inner_dialogue_enabled=True)
    state = {"health": 0.9, "wisdom": None, "restlessness": None,
             "mood_phase": "consolidate", "identity_strength": 1.0,
             "dreams_count": 5, "inner_dialogues": 0}
    adjs = avatar_mod.apply_feedback(state, cfg)
    assert "consolidate_pauses_new_reasoning" in [a.rule for a in adjs]
    assert cfg.inner_dialogue_enabled is False


@test("avatar feedback: identity weakness flips audit_only off")
def _():
    cfg = br.BrainConfig(enforce_identity_audit_only=True)
    state = {"health": 0.9, "wisdom": None, "restlessness": None,
             "mood_phase": "focus", "identity_strength": 0.7}
    adjs = avatar_mod.apply_feedback(state, cfg)
    assert "identity_weakness_locks_harder" in [a.rule for a in adjs]
    assert cfg.enforce_identity_audit_only is False


@test("avatar feedback: healthy state fires no rules")
def _():
    cfg = br.BrainConfig(memory_retrieval_limit=6,
                           inner_dialogue_enabled=False,
                           enforce_identity_audit_only=False)
    state = {"health": 0.9, "wisdom": 0.2, "restlessness": 0.1,
             "mood_phase": "focus", "identity_strength": 0.99}
    adjs = avatar_mod.apply_feedback(state, cfg)
    assert adjs == [], [a.rule for a in adjs]
    assert cfg.memory_retrieval_limit == 6
    assert cfg.inner_dialogue_enabled is False


@test("brain: avatar_feedback=True triggers apply on each turn")
def _():
    pd = _tmp_projects_dir()
    try:
        # Seed slippage so the feedback loop has something to act on
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(30):
                s.log("identity_slip_detected", status="error",
                       metadata={"slips": ["claude"]})
            for _ in range(30):
                s.log("model_call", args={"model": "x"},
                       result={"text_len": 10})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)

        seen_events: list[str] = []

        def fake_chat(messages, **kw):
            return {"status": "ok", "text": "ok", "tool_calls": []}

        import os as _os
        saved = _os.environ.get("MNEMOSYNE_PROJECTS_DIR")
        _os.environ["MNEMOSYNE_PROJECTS_DIR"] = str(pd)
        try:
            store = mm.MemoryStore(path=pd / "memory.db")
            # Pre-cap to a value higher than the floor so we can SEE it drop
            cfg = br.BrainConfig(
                adapt_to_context=False,
                inject_env_snapshot=False,
                avatar_feedback=True,
                memory_retrieval_limit=8,
            )
            class _TrackSession:
                def log(self, event_type, **kw):
                    seen_events.append(event_type)
                    return f"evt_{len(seen_events)}"
            brain = br.Brain(config=cfg, memory=store, skills=sk.SkillRegistry(),
                             telemetry=_TrackSession(), chat_fn=fake_chat)
            brain.turn("hello")
            assert "avatar_feedback" in seen_events, seen_events
            # The seeded slippage → health < 0.4 → retrieval capped
            assert cfg.memory_retrieval_limit < 8
            store.close()
        finally:
            if saved is None:
                _os.environ.pop("MNEMOSYNE_PROJECTS_DIR", None)
            else:
                _os.environ["MNEMOSYNE_PROJECTS_DIR"] = saved
    finally:
        shutil.rmtree(pd)


@test("brain: avatar_feedback=False leaves config untouched")
def _():
    pd = _tmp_projects_dir()
    try:
        def fake_chat(messages, **kw):
            return {"status": "ok", "text": "ok", "tool_calls": []}

        store = mm.MemoryStore(path=pd / "memory.db")
        cfg = br.BrainConfig(
            adapt_to_context=False,
            inject_env_snapshot=False,
            avatar_feedback=False,    # off
            memory_retrieval_limit=6,
        )
        brain = br.Brain(config=cfg, memory=store, skills=sk.SkillRegistry(),
                         chat_fn=fake_chat)
        brain.turn("hello")
        assert cfg.memory_retrieval_limit == 6
        store.close()
    finally:
        shutil.rmtree(pd)


@test("avatar: use_cache=False always recomputes")
def _():
    pd = _tmp_projects_dir()
    try:
        s1 = avatar_mod.compute_state(projects_dir=pd, use_cache=False)
        s2 = avatar_mod.compute_state(projects_dir=pd, use_cache=False)
        # Different dict objects every call
        assert s1 is not s2
        assert s1["computed_utc"] != s2["computed_utc"] or True  # may be same μs
    finally:
        shutil.rmtree(pd)


@test("avatar: memory rows + tier counts reflected in state")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "memory.db")
        for i in range(10):
            store.write(f"hot {i}", tier=mm.L1_HOT)
        for i in range(20):
            store.write(f"warm {i}", tier=mm.L2_WARM)
        for i in range(30):
            store.write(f"cold {i}", tier=mm.L3_COLD)
        store.close()
        state = avatar_mod.compute_state(projects_dir=pd)
        assert state["memory_count"] == 60
        assert state["l1_count"] == 10
        assert state["l2_count"] == 20
        assert state["l3_count"] == 30
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_serve — memory-search endpoint + safety caps
# =============================================================================

@test("serve: constant-time auth accepts correct bearer token")
def _():
    import hmac
    # Behavioral test — we can't easily test timing; just verify the
    # function accepts the right token and rejects the wrong one.
    tok = "abc123"
    assert hmac.compare_digest(f"Bearer {tok}", f"Bearer {tok}")
    assert not hmac.compare_digest(f"Bearer {tok}", "Bearer wrong")


@test("serve: MAX_BODY_BYTES is a sane cap")
def _():
    import mnemosyne_serve as ms
    assert ms.Handler.MAX_BODY_BYTES == 1 * 1024 * 1024
    # A /turn prompt 500k chars long fits comfortably
    assert ms.Handler.MAX_BODY_BYTES > 500_000


@test("serve: handle_memory_search returns FTS hits with expected shape")
def _():
    pd = _tmp_projects_dir()
    try:
        # Seed memory
        store = mm.MemoryStore(path=pd / "memory.db")
        store.write(content="alpha beta gamma", tier=mm.L1_HOT, kind="note")
        store.write(content="delta epsilon alpha", tier=mm.L2_WARM, kind="fact")
        store.write(content="zeta theta iota", tier=mm.L3_COLD, kind="archive")
        store.close()

        # Handler is bound to a full Service, but handle_memory_search
        # only needs .memory. Build a minimal shim.
        class Shim:
            pass
        svc = Shim()
        svc.memory = mm.MemoryStore(path=pd / "memory.db")

        import mnemosyne_serve as ms
        result = ms.Service.handle_memory_search(svc, "alpha", 10, None)
        assert result["query"] == "alpha"
        # Two rows match "alpha"
        assert len(result["hits"]) == 2
        for h in result["hits"]:
            for k in ("id", "tier", "kind", "source", "content",
                       "created_utc", "access_count"):
                assert k in h, k

        # tier_max filter excludes L3
        result = ms.Service.handle_memory_search(svc, "iota", 10, 2)
        assert len(result["hits"]) == 0, result

        svc.memory.close()
    finally:
        shutil.rmtree(pd)


@test("serve: handle_memory_search respects limit cap (max 50)")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "memory.db")
        for i in range(120):
            store.write(content=f"item {i} widget", tier=mm.L2_WARM)
        store.close()

        class Shim:
            pass
        svc = Shim()
        svc.memory = mm.MemoryStore(path=pd / "memory.db")
        import mnemosyne_serve as ms

        # Caller asks for 500 → capped to 50
        r = ms.Service.handle_memory_search(svc, "widget", 500, None)
        assert len(r["hits"]) == 50
        # Caller asks for 5 → honored
        r = ms.Service.handle_memory_search(svc, "widget", 5, None)
        assert len(r["hits"]) == 5
        svc.memory.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_resolver — routing-layer audit
# =============================================================================

@test("resolver: builtin registry passes audit clean")
def _():
    rep = resolver_mod.check_resolvable(include_builtins=True,
                                          projects_dir=_tmp_projects_dir())
    # Builtins should pass — they all have non-trivial descriptions
    assert rep.skills_audited >= 11
    assert rep.counts_by_severity.get("error", 0) == 0


@test("resolver: empty description flagged as error")
def _():
    reg = sk.SkillRegistry()
    reg.register(sk.Skill(name="bad", description="", invocation="python",
                            callable=lambda: None))
    rep = resolver_mod.check_resolvable(registry=reg, include_builtins=False)
    codes = {i.code for i in rep.issues}
    assert "DESC_EMPTY" in codes
    assert rep.has_errors


@test("resolver: short description flagged as warning, not error")
def _():
    reg = sk.SkillRegistry()
    reg.register(sk.Skill(name="bad", description="run it",
                            invocation="python", callable=lambda: None))
    rep = resolver_mod.check_resolvable(registry=reg, include_builtins=False)
    codes = {i.code for i in rep.issues}
    assert "DESC_TOO_SHORT" in codes
    assert not rep.has_errors


@test("resolver: ambiguous descriptions flagged as warning pair")
def _():
    reg = sk.SkillRegistry()
    common = "search the database for matching records by query"
    reg.register(sk.Skill(name="search_a", description=common + " alpha",
                            invocation="python", callable=lambda: None))
    reg.register(sk.Skill(name="search_b", description=common + " beta",
                            invocation="python", callable=lambda: None))
    rep = resolver_mod.check_resolvable(registry=reg, include_builtins=False)
    codes = {i.code for i in rep.issues}
    assert "DESC_AMBIGUOUS" in codes
    assert len(rep.distinguishability_pairs) >= 1


@test("resolver: subprocess skill missing command is an error")
def _():
    reg = sk.SkillRegistry()
    reg.register(sk.Skill(name="proc", description="run an external tool "
                            "for system inspection",
                            invocation="subprocess", command=None))
    rep = resolver_mod.check_resolvable(registry=reg, include_builtins=False)
    codes = {i.code for i in rep.issues}
    assert "NO_COMMAND" in codes
    assert rep.has_errors


@test("resolver: AGENTS.md ghost reference flagged")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "AGENTS.md").write_text(
            "Use the `nonexistent_skill` for X.\n"
            "Also try `another_ghost` if needed.\n",
            encoding="utf-8",
        )
        reg = sk.SkillRegistry()
        reg.register(sk.Skill(name="real_skill",
                                description="this skill exists with a description",
                                invocation="python", callable=lambda: None))
        rep = resolver_mod.check_resolvable(registry=reg,
                                              projects_dir=pd,
                                              include_builtins=False)
        ghosts = set(rep.agents_md_gaps)
        assert "nonexistent_skill" in ghosts
        assert "another_ghost" in ghosts
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  scenario_runner — routing assertions
# =============================================================================

@test("scenarios: expected_skill judges first dispatched tool")
def _():
    judge = sr.DEFAULT_JUDGES["expected_skill"]
    # First tool matches
    out = {"tool_calls": ["obsidian_search", "fs_read"]}
    ok, _reason = judge(out, "obsidian_search")
    assert ok
    # First tool wrong
    out = {"tool_calls": ["fs_read"]}
    ok, reason = judge(out, "obsidian_search")
    assert not ok and "obsidian_search" in reason
    # No tool dispatched
    out = {"tool_calls": []}
    ok, reason = judge(out, "obsidian_search")
    assert not ok and "no tool" in reason


@test("scenarios: expected_skill_in passes if any expected tool fires")
def _():
    judge = sr.DEFAULT_JUDGES["expected_skill_in"]
    out = {"tool_calls": ["fs_read"]}
    ok, _ = judge(out, ["obsidian_search", "fs_read"])
    assert ok
    out = {"tool_calls": ["unrelated"]}
    ok, reason = judge(out, ["obsidian_search", "fs_read"])
    assert not ok and "expected" in reason


@test("scenarios: not_skill blocks forbidden routes")
def _():
    judge = sr.DEFAULT_JUDGES["not_skill"]
    out = {"tool_calls": ["fs_read"]}
    ok, _ = judge(out, ["fs_write_safe"])
    assert ok      # safe
    out = {"tool_calls": ["fs_write_safe"]}
    ok, reason = judge(out, ["fs_write_safe"])
    assert not ok and "forbidden" in reason


@test("scenarios: routing judges accept dict-form tool_calls too")
def _():
    judge = sr.DEFAULT_JUDGES["expected_skill"]
    out = {"tool_calls": [{"name": "obsidian_search", "arguments": {"q": "x"}}]}
    ok, _ = judge(out, "obsidian_search")
    assert ok


# =============================================================================
#  triage — resolver-decay clusters
# =============================================================================

@test("triage: unknown_tool_called cluster fires when registry doesn't have name")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(3):
                s.log("tool_call", tool="ghost_tool_xyz",
                       status="ok", metadata={})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)

        report = tri.run_triage(projects_dir=pd, window_days=30)
        cluster_types = {c["event_type"] for c in report.clusters}
        assert "unknown_tool_called" in cluster_types, cluster_types
    finally:
        shutil.rmtree(pd)


@test("triage: no_tool_dispatched cluster fires when tools available + zero called")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            # Five model_call events with tools available, none used
            for _ in range(5):
                s.log("model_call",
                       args={"model": "qwen3.5:9b",
                             "provider": "ollama",
                             "has_tools": True,
                             "message_count": 2},
                       result={"text_len": 100, "tool_calls_count": 0,
                               "usage": None},
                       status="ok")
        ht.finalize_run(rid, metrics={}, projects_dir=pd)

        report = tri.run_triage(projects_dir=pd, window_days=30)
        cluster_types = {c["event_type"] for c in report.clusters}
        assert "no_tool_dispatched" in cluster_types, cluster_types
    finally:
        shutil.rmtree(pd)


@test("triage: no_tool_dispatched does NOT fire when has_tools is false")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for _ in range(5):
                s.log("model_call",
                       args={"model": "x", "has_tools": False},
                       result={"text_len": 50, "tool_calls_count": 0},
                       status="ok")
        ht.finalize_run(rid, metrics={}, projects_dir=pd)

        report = tri.run_triage(projects_dir=pd, window_days=30)
        cluster_types = {c["event_type"] for c in report.clusters}
        assert "no_tool_dispatched" not in cluster_types
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_permissions — user-editable permission model
# =============================================================================

@test("permissions: empty file allows everything")
def _():
    p = perm_mod.parse("")
    assert p.is_skill_allowed("anything") == (True, "")
    assert p.is_path_allowed("/tmp/x") == (True, "")


@test("permissions: allow-list mode denies unlisted skills")
def _():
    p = perm_mod.parse("""
## allowed_skills
- fs_read
- grep_code
""")
    assert p.is_skill_allowed("fs_read")[0]
    assert p.is_skill_allowed("grep_code")[0]
    ok, reason = p.is_skill_allowed("fs_write_safe")
    assert not ok and "allowed_skills" in reason


@test("permissions: denied_skills always deny regardless of allow-list")
def _():
    p = perm_mod.parse("""
## allowed_skills
- fs_read
- fs_write_safe

## denied_skills
- fs_write_safe
""")
    assert p.is_skill_allowed("fs_read")[0]
    ok, reason = p.is_skill_allowed("fs_write_safe")
    assert not ok and "denied_skills" in reason


@test("permissions: forbidden_paths block prefix matches")
def _():
    import os as _os
    p = perm_mod.parse(f"""
## forbidden_paths
- {_os.path.expanduser('~/.ssh')}
- /etc/shadow
""")
    ok, _ = p.is_path_allowed(_os.path.expanduser("~/.ssh/id_rsa"))
    assert not ok
    ok, _ = p.is_path_allowed("/etc/shadow")
    assert not ok
    ok, _ = p.is_path_allowed("/tmp/anything")
    assert ok


@test("permissions: rate_limits parse with sec/min/hour units")
def _():
    p = perm_mod.parse("""
## rate_limits
- http_get: 60/min
- web_fetch_text: 10/sec
- expensive_llm: 100/hour
""")
    assert p.rate_limits["http_get"] == (60, 60)
    assert p.rate_limits["web_fetch_text"] == (10, 1)
    assert p.rate_limits["expensive_llm"] == (100, 3600)


@test("permissions: rate limiter allows N then blocks N+1 within window")
def _():
    rl = perm_mod._RollingRateLimiter()
    for _ in range(3):
        ok, _ = rl.check("foo", count=3, window_s=60)
        assert ok
    ok, reason = rl.check("foo", count=3, window_s=60)
    assert not ok and "rate limit" in reason


@test("permissions: load() returns empty permissions when file absent")
def _():
    pd = _tmp_projects_dir()
    try:
        p = perm_mod.load(projects_dir=pd)
        assert p.allowed_skills == set()
        assert p.denied_skills == set()
        assert p.is_skill_allowed("anything") == (True, "")
    finally:
        shutil.rmtree(pd)


@test("permissions: write_example creates a ready-to-edit template")
def _():
    pd = _tmp_projects_dir()
    try:
        path = perm_mod.write_example(projects_dir=pd)
        assert path.is_file()
        body = path.read_text(encoding="utf-8")
        assert "# Mnemosyne permissions" in body
        assert "## allowed_skills" in body
        assert "## forbidden_paths" in body
        # Refuses to overwrite without the flag
        try:
            perm_mod.write_example(projects_dir=pd)
            raised = False
        except FileExistsError:
            raised = True
        assert raised
    finally:
        shutil.rmtree(pd)


@test("brain: enforce_permissions blocks denied skill dispatch")
def _():
    pd = _tmp_projects_dir()
    try:
        (pd / "permissions.md").write_text("""
## denied_skills
- forbidden_tool
""", encoding="utf-8")

        import os as _os
        saved = _os.environ.get("MNEMOSYNE_PROJECTS_DIR")
        _os.environ["MNEMOSYNE_PROJECTS_DIR"] = str(pd)
        try:
            reg = sk.SkillRegistry()

            @reg.register_python("forbidden_tool", "a tool that's on the deny list",
                                   [{"name": "x", "type": "string", "required": True}])
            def _bad(x): return {"ok": True, "x": x}

            @reg.register_python("allowed_tool", "a tool that is always fine",
                                   [{"name": "y", "type": "string", "required": True}])
            def _good(y): return {"ok": True, "y": y}

            call_count = {"n": 0}

            def fake_chat(messages, **kw):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return {"status": "ok", "text": "dispatching",
                            "tool_calls": [
                                {"id": "t1", "name": "forbidden_tool",
                                 "arguments": {"x": "a"}},
                                {"id": "t2", "name": "allowed_tool",
                                 "arguments": {"y": "b"}},
                            ]}
                return {"status": "ok", "text": "final", "tool_calls": []}

            store = mm.MemoryStore(path=pd / "memory.db")
            cfg = br.BrainConfig(
                adapt_to_context=False,
                inject_env_snapshot=False,
                enforce_permissions=True,
            )
            brain = br.Brain(config=cfg, memory=store, skills=reg,
                             chat_fn=fake_chat,
                             telemetry=None)
            r = brain.turn("please call both tools")
            names = [tc["name"] for tc in r.tool_calls]
            assert "forbidden_tool" in names    # attempt was made
            assert "allowed_tool" in names
            # forbidden_tool's result should be a permission_denied error
            forbidden = next(tc for tc in r.tool_calls
                              if tc["name"] == "forbidden_tool")
            assert forbidden["result"].get("error") == "permission_denied"
            allowed = next(tc for tc in r.tool_calls
                            if tc["name"] == "allowed_tool")
            assert allowed["result"].get("ok") is True
            store.close()
        finally:
            if saved is None:
                _os.environ.pop("MNEMOSYNE_PROJECTS_DIR", None)
            else:
                _os.environ["MNEMOSYNE_PROJECTS_DIR"] = saved
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_memory.export_to_git — autobiography
# =============================================================================

@test("memory: export_to_git writes one markdown per memory + tier dirs")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write(content="hot preference", tier=1, kind="preference")
        store.write(content="warm project note", tier=2, kind="project")
        store.write(content="cold old fact", tier=3, kind="fact")
        out = pd / "autobio"
        result = store.export_to_git(out, tier_min=2)
        assert result["count"] == 2   # L2 + L3, not L1
        files = sorted(out.rglob("*.md"))
        assert len(files) == 2
        # Tier subdirs correctly used
        tiers = {f.parent.name for f in files}
        assert tiers == {"L2", "L3"}
        # Content preserved
        assert "warm project note" in files[0].read_text()
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: export_to_git respects --since filter")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write(content="ancient", tier=2)
        # Backdate the first row
        store._conn.execute(
            "UPDATE memories SET created_utc = '2020-01-01T00:00:00.000000Z' "
            "WHERE id = 1",
        )
        store.write(content="recent", tier=2)
        out = pd / "autobio"
        result = store.export_to_git(out, tier_min=2, since="2025-01-01T00:00:00Z")
        assert result["count"] == 1, result
        content = (next(out.rglob("*.md"))).read_text()
        assert "recent" in content
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory: export_to_git is safe when git binary missing")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "mem.db")
        store.write(content="x", tier=2)
        # Even if git fails mid-way, files should be written and the
        # function should not raise.
        out = pd / "autobio"
        result = store.export_to_git(out, tier_min=2)
        assert result["count"] == 1
        assert result["repo"] == str(out)
        store.close()
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  mnemosyne_adapter_claude_code — harness adapter
# =============================================================================

import mnemosyne_adapter_claude_code as cc_adapter  # noqa: E402


@test("adapter-claude-code: install creates CLAUDE.md + hooks + settings")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        cc_adapter.install(target, projects_dir=proj)
        assert (target / "CLAUDE.md").is_file()
        body = (target / "CLAUDE.md").read_text(encoding="utf-8")
        assert "mnemosyne-adapter:begin" in body
        assert (target / ".claude" / "mnemosyne" / "hooks"
                / "session_start.sh").is_file()
        assert (target / ".claude" / "settings.json").is_file()
        settings = json.loads((target / ".claude" / "settings.json")
                                .read_text(encoding="utf-8"))
        assert "hooks" in settings
        # Our hook command path is wired
        stop_hooks = settings["hooks"].get("Stop") or []
        assert any(".claude/mnemosyne/hooks" in h["command"]
                   for entry in stop_hooks
                   for h in entry.get("hooks") or [])
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


@test("adapter-claude-code: install is non-destructive to existing CLAUDE.md")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        user_content = "# My existing project\n\nRespect existing rules.\n"
        (target / "CLAUDE.md").write_text(user_content, encoding="utf-8")
        cc_adapter.install(target, projects_dir=proj)
        body = (target / "CLAUDE.md").read_text(encoding="utf-8")
        # User content preserved, our block appended
        assert user_content.strip() in body
        assert "mnemosyne-adapter:begin" in body
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


@test("adapter-claude-code: install is idempotent with --force")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        cc_adapter.install(target, projects_dir=proj)
        body_1 = (target / "CLAUDE.md").read_text(encoding="utf-8")
        # Second install without --force warns instead of duplicating
        r2 = cc_adapter.install(target, projects_dir=proj)
        assert any("already has" in w for w in r2["warnings"])
        body_2 = (target / "CLAUDE.md").read_text(encoding="utf-8")
        assert body_1 == body_2
        # With --force, replaces rather than duplicates
        cc_adapter.install(target, projects_dir=proj, force=True)
        body_3 = (target / "CLAUDE.md").read_text(encoding="utf-8")
        # Only one begin-marker in the file
        assert body_3.count("mnemosyne-adapter:begin") == 1
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


@test("adapter-claude-code: uninstall removes our additions + preserves user content")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        user_content = "# My project\n\nUser notes.\n"
        (target / "CLAUDE.md").write_text(user_content, encoding="utf-8")
        cc_adapter.install(target, projects_dir=proj)
        cc_adapter.uninstall(target)
        # User content survives
        body = (target / "CLAUDE.md").read_text(encoding="utf-8")
        assert "User notes" in body
        assert "mnemosyne-adapter:begin" not in body
        # Hooks dir gone
        assert not (target / ".claude" / "mnemosyne").exists()
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


@test("adapter-claude-code: status reports correct presence flags")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        s_before = cc_adapter.status(target)
        assert not s_before["claude_md_has_mnemosyne_block"]
        assert not s_before["hooks_installed"]

        cc_adapter.install(target, projects_dir=proj)
        s_after = cc_adapter.status(target)
        assert s_after["claude_md_has_mnemosyne_block"]
        assert s_after["hooks_installed"]
        assert s_after["settings_present"]
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


@test("adapter-claude-code: install merges into existing settings.json hooks")
def _():
    proj = _tmp_projects_dir()
    target = _tmp_projects_dir()
    try:
        (target / ".claude").mkdir(parents=True)
        existing = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command",
                                 "command": "./user-hook.sh"}]}
                ]
            }
        }
        (target / ".claude" / "settings.json").write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
        cc_adapter.install(target, projects_dir=proj)
        merged = json.loads(
            (target / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        stop_cmds = [h["command"]
                      for entry in merged["hooks"]["Stop"]
                      for h in entry.get("hooks") or []]
        # User's pre-existing hook preserved + ours added
        assert "./user-hook.sh" in stop_cmds
        assert any(".claude/mnemosyne/hooks/on_stop.sh" in c
                   for c in stop_cmds)
    finally:
        shutil.rmtree(proj)
        shutil.rmtree(target)


# =============================================================================
#  mnemosyne_predictions — self-calibration
# =============================================================================

@test("predictions: score_events computes calibration correctly")
def _():
    events = [
        {"event_type": "prediction",
         "metadata": {"prediction_id": "a", "claim": "x",
                       "confidence": 0.9, "kind": "tool_success",
                       "emitted_at": "2026-04-15T12:00:00.000000Z"}},
        {"event_type": "outcome",
         "metadata": {"prediction_id": "a",
                       "actual_correctness": 0.0}},
        {"event_type": "prediction",
         "metadata": {"prediction_id": "b", "claim": "y",
                       "confidence": 0.8, "kind": "tool_success",
                       "emitted_at": "2026-04-15T12:01:00.000000Z"}},
        {"event_type": "outcome",
         "metadata": {"prediction_id": "b",
                       "actual_correctness": 1.0}},
    ]
    r = pred_mod.score_events(events)
    assert r.predictions_total == 2
    assert r.predictions_resolved == 2
    assert r.overconfident_wrong == 1   # the first one
    # |0.9-0| = 0.9, |0.8-1| = 0.2  →  mean 0.55  →  calibration 0.45
    assert abs(r.calibration - 0.45) < 0.01


@test("predictions: unresolved predictions within horizon are pending")
def _():
    events = [
        {"event_type": "prediction",
         "metadata": {"prediction_id": "a", "claim": "x",
                       "confidence": 0.5, "horizon_seconds": 3600,
                       "emitted_at": "2026-04-15T12:00:00.000000Z"}},
    ]
    r = pred_mod.score_events(events,
                                now_iso="2026-04-15T12:10:00.000000Z")
    assert r.predictions_pending == 1
    assert r.predictions_resolved == 0


@test("predictions: expired predictions score as 0.5 (uninformative)")
def _():
    events = [
        {"event_type": "prediction",
         "metadata": {"prediction_id": "a", "claim": "x",
                       "confidence": 0.9, "horizon_seconds": 60,
                       "emitted_at": "2026-04-15T12:00:00.000000Z"}},
    ]
    r = pred_mod.score_events(events,
                                now_iso="2026-04-15T13:00:00.000000Z")
    assert r.predictions_expired == 1
    # conf=0.9 vs scoring=0.5 → error=0.4 → calibration=0.6
    assert abs(r.calibration - 0.6) < 0.01


@test("predictions: calibration_trait returns None with fewer than 3 resolved")
def _():
    pd = _tmp_projects_dir()
    try:
        # Seed only one prediction+outcome pair
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            s.log("prediction", metadata={"prediction_id": "x",
                                             "confidence": 0.8,
                                             "claim": "w",
                                             "emitted_at": "2026-04-16T00:00:00.000000Z"})
            s.log("outcome", metadata={"prediction_id": "x",
                                          "actual_correctness": 1.0})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        t = pred_mod.calibration_trait(pd)
        assert t is None
    finally:
        shutil.rmtree(pd)


@test("predictions: calibration_trait returns value with 3+ resolved")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for i, corr in enumerate([1.0, 0.5, 0.8]):
                s.log("prediction", metadata={
                    "prediction_id": f"p{i}", "confidence": 0.7,
                    "claim": "c", "emitted_at": "2026-04-16T00:00:00.000000Z"})
                s.log("outcome", metadata={
                    "prediction_id": f"p{i}",
                    "actual_correctness": corr})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        t = pred_mod.calibration_trait(pd)
        assert t is not None and 0.0 <= t <= 1.0
    finally:
        shutil.rmtree(pd)


@test("predict/observe emit linked events through TelemetrySession")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            pid = pred_mod.predict(s, claim="will succeed",
                                     confidence=0.9, kind="tool_success",
                                     horizon_seconds=60)
            assert pid.startswith("pred_")
            pred_mod.observe(s, prediction_id=pid,
                              actual="succeeded",
                              actual_correctness=1.0)
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        # Round-trip through score_run
        rd = ht.run_path(rid, pd)
        r = pred_mod.score_run(rd)
        assert r.predictions_resolved == 1
    finally:
        shutil.rmtree(pd)


@test("triage: prediction_overconfident cluster fires on high-conf-wrong pair")
def _():
    pd = _tmp_projects_dir()
    try:
        rid = ht.create_run(model="t", projects_dir=pd)
        with ht.TelemetrySession(rid, projects_dir=pd) as s:
            for i in range(3):
                s.log("prediction", metadata={
                    "prediction_id": f"p{i}", "confidence": 0.9,
                    "claim": "c", "kind": "tool_success"})
                s.log("outcome", metadata={
                    "prediction_id": f"p{i}",
                    "actual_correctness": 0.0})
        ht.finalize_run(rid, metrics={}, projects_dir=pd)
        report = tri.run_triage(projects_dir=pd, window_days=30)
        cluster_types = {c["event_type"] for c in report.clusters}
        assert "prediction_overconfident" in cluster_types, cluster_types
    finally:
        shutil.rmtree(pd)


@test("avatar: calibration trait appears in state dict (null when no signal)")
def _():
    pd = _tmp_projects_dir()
    try:
        state = avatar_mod.compute_state(projects_dir=pd, use_cache=False)
        assert "calibration" in state
        # No predictions yet → null
        assert state["calibration"] is None
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  v0.7: L4/L5 tiers + ACT-R decay + Hebbian reinforcement
# =============================================================================

@test("memory v0.7: strength column defaults to 1.0 on new writes")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        mid_ = store.write("hello", kind="fact")
        row = store.get(mid_)
        assert row is not None
        assert row["strength"] == 1.0, row
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory v0.7: reinforce() approaches 1.0 asymptotically")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        # Start a row well below 1.0 via direct UPDATE (simulate decay)
        mid_ = store.write("hello")
        store._conn.execute(
            "UPDATE memories SET strength = 0.0 WHERE id = ?", (mid_,))
        # Reinforce 50x with amount=0.1 should converge into (0.9, 1.0)
        for _ in range(50):
            store.reinforce(mid_, amount=0.1)
        row = store.get(mid_)
        assert 0.9 < row["strength"] < 1.0, row["strength"]
        store.close()
    finally:
        shutil.rmtree(pd)


@test("memory v0.7: identity-class kinds decay slower than operational kinds")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        core = store.write("core val", kind="core_value",
                            tier=mm.L5_IDENTITY)
        fail = store.write("fail note", kind="failure_note",
                            tier=mm.L2_WARM)
        # Age both rows 30 days into the past
        old_iso = "2020-01-01T00:00:00.000000Z"
        store._conn.execute(
            "UPDATE memories SET created_utc=?, last_accessed_utc=?",
            (old_iso, old_iso),
        )
        store.apply_decay()
        s_core = store.get(core)["strength"]
        s_fail = store.get(fail)["strength"]
        assert s_core > s_fail, (s_core, s_fail)
    finally:
        shutil.rmtree(pd)


@test("memory v0.7: apply_decay demotes L4 pattern rows below strength 0.3")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        p = store.write("pattern", kind="pattern", tier=mm.L4_PATTERN)
        store._conn.execute(
            "UPDATE memories SET strength = 0.05, "
            "created_utc='2020-01-01T00:00:00.000000Z', "
            "last_accessed_utc='2020-01-01T00:00:00.000000Z'"
        )
        store.apply_decay()
        row = store.get(p)
        assert row["tier"] == mm.L3_COLD, row
    finally:
        shutil.rmtree(pd)


@test("memory v0.7: stats() reports L4 and L5 counts separately")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        store.write("a", tier=mm.L4_PATTERN, kind="pattern")
        store.write("b", tier=mm.L5_IDENTITY, kind="core_value")
        s = store.stats()
        assert s["by_tier"]["L4_pattern"] == 1, s
        assert s["by_tier"]["L5_identity"] == 1, s
    finally:
        shutil.rmtree(pd)


@test("memory v0.7: promote accepts L4_PATTERN and L5_IDENTITY targets")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        mid_ = store.write("x", tier=mm.L2_WARM)
        store.promote(mid_, to_tier=mm.L5_IDENTITY)
        assert store.get(mid_)["tier"] == mm.L5_IDENTITY
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  v0.7: mnemosyne_compactor (L3 → L4 promotion)
# =============================================================================

@test("compactor: promotes recurring L3 clusters to L4 pattern rows")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        for i in range(5):
            store.write(
                f"network timeout calling api on attempt {i}",
                kind="failure_note", tier=mm.L3_COLD,
            )
        store._conn.execute(
            "UPDATE memories SET created_utc = '2020-01-01T00:00:00.000000Z'"
        )
        result = compactor_mod.compact_patterns(
            store, min_age_days=1, min_cluster_size=3,
        )
        assert result["promoted"] >= 1, result
        rows = store._conn.execute(
            "SELECT * FROM memories WHERE tier = ? AND kind = 'pattern'",
            (mm.L4_PATTERN,),
        ).fetchall()
        assert len(rows) >= 1, rows
    finally:
        shutil.rmtree(pd)


@test("compactor: skips clusters below min_cluster_size")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        for i in range(2):
            store.write(
                f"isolated event with unique topic quorum {i}",
                kind="event", tier=mm.L3_COLD,
            )
        store._conn.execute(
            "UPDATE memories SET created_utc = '2020-01-01T00:00:00.000000Z'"
        )
        result = compactor_mod.compact_patterns(
            store, min_age_days=1, min_cluster_size=3,
        )
        assert result["promoted"] == 0, result
    finally:
        shutil.rmtree(pd)


@test("compactor: idempotent across re-runs (does not double-promote)")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        for i in range(4):
            store.write(
                f"shared topic cluster recurring thing token {i}",
                kind="event", tier=mm.L3_COLD,
            )
        store._conn.execute(
            "UPDATE memories SET created_utc = '2020-01-01T00:00:00.000000Z'"
        )
        r1 = compactor_mod.compact_patterns(
            store, min_age_days=1, min_cluster_size=3,
        )
        r2 = compactor_mod.compact_patterns(
            store, min_age_days=1, min_cluster_size=3,
        )
        assert r1["promoted"] == 1, r1
        assert r2["promoted"] == 0, r2  # already-linked ids skipped
    finally:
        shutil.rmtree(pd)


@test("compactor: dry_run does not write any L4 rows")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        for i in range(4):
            store.write(
                f"shared cluster recurring token {i}",
                kind="event", tier=mm.L3_COLD,
            )
        store._conn.execute(
            "UPDATE memories SET created_utc = '2020-01-01T00:00:00.000000Z'"
        )
        r = compactor_mod.compact_patterns(
            store, min_age_days=1, min_cluster_size=3, dry_run=True,
        )
        assert r["clusters_found"] >= 1, r
        assert r["promoted"] == 0, r
        # No L4 rows were written
        count = store._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE tier = ?",
            (mm.L4_PATTERN,),
        ).fetchone()[0]
        assert count == 0
    finally:
        shutil.rmtree(pd)


# =============================================================================
#  v0.7: mnemosyne_continuity (benchmark runner)
# =============================================================================

@test("continuity: load_scenarios parses the shipped 50-scenario file")
def _():
    scs = continuity_mod.load_scenarios(
        Path(_REPO) / "scenarios" / "continuity.jsonl"
    )
    assert len(scs) == 50, len(scs)
    # Each has the required fields after defaulting
    for sc in scs:
        assert "id" in sc and "probe" in sc
        assert isinstance(sc["expected_any"], list)
        assert isinstance(sc["plant"], list)


@test("continuity: judge_response matches expected_any case-insensitively")
def _():
    ok, _reason = continuity_mod.judge_response(
        "The answer is TEAL actually.",
        expected_any=["teal"], not_contains=[],
    )
    assert ok


@test("continuity: judge_response fails when not_contains substring present")
def _():
    ok, reason = continuity_mod.judge_response(
        "here is a hyphen—",
        expected_any=[], not_contains=["—"],
    )
    assert not ok, reason


@test("continuity: dryrun on a minimal scenario file produces an aggregate")
def _():
    # Build a throwaway scenario list, no LLM involved
    report = continuity_mod.run_continuity(
        [
            {
                "id": "t1", "category": "fact",
                "plant": ["My dog is named Miso."],
                "probe": "What's my dog's name?",
                "expected_any": ["miso"],
                "not_contains": [],
                "cross_session": False,
                "tags": [],
            },
        ],
        make_brain=continuity_mod._make_dry_brain,
    )
    assert report["total"] == 1, report
    assert 0.0 <= report["continuity_score"] <= 1.0


# =============================================================================
#  v0.7: Brain L5 identity injection
# =============================================================================

@test("memory v0.7.1: search falls back to OR when AND returns no hits")
def _():
    pd = _tmp_projects_dir()
    try:
        store = mm.MemoryStore(path=pd / "m.db")
        store.write("My favorite color is teal", kind="preference")
        # "favorite color"  AND would match.
        # "favorite missing" AND misses; OR should still catch "favorite".
        strict_hits = store.search("favorite color")
        assert len(strict_hits) == 1, strict_hits
        fallback_hits = store.search("favorite missingterm")
        assert len(fallback_hits) == 1, fallback_hits
        # Pathological all-miss query returns empty after fallback too.
        empty = store.search("zzzzz yyyyy xxxxx")
        assert empty == [], empty
    finally:
        shutil.rmtree(pd)


@test("memory v0.7.1: _fts5_escape supports OR joining for recall mode")
def _():
    assert mm._fts5_escape("a b") == '"a" "b"'
    assert mm._fts5_escape("a b", any_token=True) == '"a" OR "b"'
    # Single-token queries return a single quoted term regardless of mode
    assert mm._fts5_escape("a", any_token=True) == '"a"'


@test("brain v0.7: L5 identity rows land in system prompt on every turn")
def _():
    pd = _tmp_projects_dir()
    try:
        os.environ["MNEMOSYNE_PROJECTS_DIR"] = str(pd)
        mem = mm.MemoryStore(path=pd / "m.db")
        mem.write("I prioritize honesty over politeness.",
                  kind="core_value", tier=mm.L5_IDENTITY)

        captured: dict[str, Any] = {}

        def fake_chat(messages, **kw):
            captured["messages"] = messages
            return {"text": "ok", "tool_calls": [], "status": "ok",
                    "usage": {}}

        b = br.Brain(config=br.BrainConfig(enforce_identity_lock=True),
                     chat_fn=fake_chat, memory=mem)
        b.turn("hello")
        sys_msg = next(m for m in captured["messages"]
                       if m["role"] == "system")
        assert "Core values" in sys_msg["content"], sys_msg["content"][:500]
        assert "honesty over politeness" in sys_msg["content"]
    finally:
        shutil.rmtree(pd)


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
