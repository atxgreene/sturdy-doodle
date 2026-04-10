#!/usr/bin/env bash
# ==============================================================================
#  test-harness.sh
#  Integration test + live demo for the Mnemosyne observability stack.
#
#  Exercises all four components end-to-end against an ephemeral fake
#  PROJECTS_DIR, with no network dependencies and no impact on the real
#  ~/projects/mnemosyne tree:
#
#    1. harness_telemetry.py    — creates three runs, logs tool events,
#                                 verifies secret redaction, finalizes with
#                                 diverse metrics
#    2. mnemosyne-experiments   — list, show, top-k, pareto, diff, events
#    3. environment-snapshot    — markdown + JSON output, secret safety check
#    4. obsidian-search         — verified separately in prior commits
#
#  Exits 0 on success, non-zero on any failure with a clear message.
#  Safe to run repeatedly. Cleans up on exit (even on failure).
#
#  Usage:
#    bash test-harness.sh [--keep]
#
#    --keep    leave the ephemeral PROJECTS_DIR in place for inspection
#              (path is printed on exit)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0
KEEP=0

for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_blue=$'\033[1;34m'
c_dim=$'\033[2m'; c_off=$'\033[0m'

hr()  { printf '%s────────────────────────────────────────────────%s\n' "$c_dim" "$c_off"; }
log() { printf '%s==>%s %s\n' "$c_blue" "$c_off" "$*"; }
ok()  { printf '%s  ✓%s %s\n' "$c_green" "$c_off" "$*"; PASS=$((PASS+1)); }
bad() { printf '%s  ✗%s %s\n' "$c_red" "$c_off" "$*"; FAIL=$((FAIL+1)); }

FAKE=$(mktemp -d /tmp/mnemo-harness-test-XXXXXX)
# shellcheck disable=SC2317  # cleanup() is called via trap, shellcheck can't see that
cleanup() {
  if [ "$KEEP" = 1 ]; then
    echo
    log "left fake PROJECTS_DIR at: $FAKE"
  else
    rm -rf "$FAKE"
  fi
}
trap cleanup EXIT

hr
printf '%s  Mnemosyne observability — integration test%s\n' "$c_green" "$c_off"
hr
log "fake PROJECTS_DIR: $FAKE"
echo

# ---- 1. harness_telemetry: create + log + finalize --------------------------
log "1/4  harness_telemetry: create three runs, log events, finalize"

# Create three runs with different models + tags
python3 - <<PY
import os, sys
os.environ["MNEMOSYNE_PROJECTS_DIR"] = "$FAKE"
sys.path.insert(0, "$SCRIPT_DIR")
import harness_telemetry as ht

# Run A: baseline with qwen3:8b
run_a = ht.create_run(
    model="qwen3:8b",
    notes="baseline qwen run",
    tags=["baseline", "qwen"],
    slug="baseline",
)

# Run B: gemma4:e4b experiment
run_b = ht.create_run(
    model="gemma4:e4b",
    notes="gemma4 A/B test",
    tags=["experiment", "gemma4"],
    slug="gemma4ab",
)

# Run C: another gemma4 run with different params
run_c = ht.create_run(
    model="gemma4:e4b",
    notes="gemma4 with wider context",
    tags=["experiment", "gemma4", "wide"],
    slug="widectx",
)

# Log events in run A: a few tool calls, including one with a secret in args
with ht.TelemetrySession(run_a) as sess:
    @sess.trace
    def obsidian_search(query, limit=10):
        return {"matches": [{"path": "daily/2026-04-08.md", "line": 12}]}

    @sess.trace
    def dangerous_tool(api_key, query):
        return {"ok": True}

    obsidian_search("project alpha")
    obsidian_search("quarterly review", limit=5)
    dangerous_tool(api_key="SUPER_SECRET_SHOULD_BE_REDACTED", query="test")

    # Also log a prompt/response pair manually
    sess.log("prompt", metadata={"turn": 1},
             args={"prompt": "What did I work on yesterday?"})
    sess.log("response", metadata={"turn": 1},
             result={"text": "Based on Obsidian notes, you worked on Mnemosyne wizard."})

# Run B: fewer events, different tool
with ht.TelemetrySession(run_b) as sess:
    @sess.trace
    def obsidian_search(query):
        return {"matches": []}
    obsidian_search("x")
    obsidian_search("y")

# Run C: also events
with ht.TelemetrySession(run_c) as sess:
    @sess.trace
    def notion_search(query):
        return {"results": [{"id": "abc123", "title": "Foo"}]}
    notion_search("project")
    notion_search("meeting")
    notion_search("review")

# Finalize with diverse metrics for Pareto analysis
# Run A: higher accuracy, higher latency
ht.finalize_run(run_a, metrics={
    "accuracy": 0.82,
    "latency_ms_avg": 1800.0,
    "turns_successful": 4,
    "turns_failed": 1,
})
# Run B: lower accuracy, much lower latency (Pareto-frontier candidate)
ht.finalize_run(run_b, metrics={
    "accuracy": 0.76,
    "latency_ms_avg": 950.0,
    "turns_successful": 2,
    "turns_failed": 0,
})
# Run C: dominated (lower than A on accuracy, higher than B on latency)
ht.finalize_run(run_c, metrics={
    "accuracy": 0.75,
    "latency_ms_avg": 1900.0,
    "turns_successful": 3,
    "turns_failed": 0,
})

print(run_a)
print(run_b)
print(run_c)
PY
mapfile -t RUNS < <(python3 -c "
import os, sys
sys.path.insert(0, '$SCRIPT_DIR')
os.environ['MNEMOSYNE_PROJECTS_DIR'] = '$FAKE'
import harness_telemetry as ht
for rid, _ in ht.list_runs():
    print(rid)
")

if [ "${#RUNS[@]}" -eq 3 ]; then
  ok "created 3 runs: ${RUNS[*]}"
else
  bad "expected 3 runs, got ${#RUNS[@]}"
fi

# Verify events.jsonl has lines
for rid in "${RUNS[@]}"; do
  events="$FAKE/experiments/$rid/events.jsonl"
  count=$(wc -l < "$events" 2>/dev/null || echo 0)
  if [ "$count" -gt 0 ]; then
    ok "$rid: events.jsonl has $count events"
  else
    bad "$rid: events.jsonl empty or missing"
  fi
done

# Verify "latest" symlink exists
if [ -L "$FAKE/experiments/latest" ]; then
  ok "'latest' symlink created"
else
  bad "'latest' symlink missing"
fi

# ---- 2. secret redaction verification ---------------------------------------
log "2/4  verify secret redaction in events.jsonl"

# The dangerous_tool call above had api_key="SUPER_SECRET_SHOULD_BE_REDACTED".
# That string must NOT appear anywhere in any events.jsonl.
echo "SUPER_SECRET_SHOULD_BE_REDACTED" > /tmp/harness-needle.txt
if cat "$FAKE/experiments"/*/events.jsonl 2>/dev/null | grep -F -f /tmp/harness-needle.txt >/dev/null; then
  bad "SECRET LEAKED in events.jsonl"
else
  ok "secret redacted (not present in any events.jsonl)"
fi

# The word "<redacted>" should appear at least once
if grep -rq "<redacted>" "$FAKE/experiments"/*/events.jsonl; then
  ok "redaction marker present in event stream"
else
  bad "no redaction marker found — redaction may not have fired"
fi
rm -f /tmp/harness-needle.txt

# ---- 3. mnemosyne-experiments CLI --------------------------------------------
log "3/4  mnemosyne-experiments: list, show, top-k, pareto, diff, events"

export MNEMOSYNE_PROJECTS_DIR="$FAKE"
MEX="$SCRIPT_DIR/mnemosyne-experiments.py"

# list
if python3 "$MEX" list 2>/dev/null | grep -q "run_"; then
  ok "list: shows runs"
else
  bad "list: no runs shown"
fi

# list --json (valid JSON?)
if python3 "$MEX" list --json 2>/dev/null | python3 -m json.tool >/dev/null; then
  ok "list --json: valid JSON"
else
  bad "list --json: invalid JSON"
fi

# show
FIRST_RUN="${RUNS[0]}"
if python3 "$MEX" show "$FIRST_RUN" 2>/dev/null | grep -q "metadata"; then
  ok "show: metadata section present"
else
  bad "show: missing metadata"
fi

# top-k by accuracy (max)
TOP_OUT=$(python3 "$MEX" top-k 1 --metric accuracy --direction max 2>&1)
if echo "$TOP_OUT" | grep -q "accuracy=0.82"; then
  ok "top-k: correctly picks highest accuracy (0.82)"
else
  bad "top-k: expected accuracy=0.82 as #1, got: $TOP_OUT"
fi

# top-k by latency (min)
LAT_OUT=$(python3 "$MEX" top-k 1 --metric latency_ms_avg --direction min 2>&1)
if echo "$LAT_OUT" | grep -q "latency_ms_avg=950"; then
  ok "top-k: correctly picks lowest latency (950.0)"
else
  bad "top-k: expected latency_ms_avg=950 as #1, got: $LAT_OUT"
fi

# Pareto frontier on accuracy (max) × latency (min)
# Expected: Run A (high acc, high latency) and Run B (lower acc, low latency) on frontier.
# Run C is dominated by A.
PARETO_OUT=$(python3 "$MEX" pareto --axes accuracy,latency_ms_avg --directions max,min 2>&1)
PARETO_COUNT=$(echo "$PARETO_OUT" | grep -c "^  run_" || true)
if [ "$PARETO_COUNT" = "2" ]; then
  ok "pareto: 2 runs on frontier (expected)"
else
  bad "pareto: expected 2 runs on frontier, got $PARETO_COUNT"
  echo "$PARETO_OUT"
fi

# Pareto --json is valid
if python3 "$MEX" pareto --axes accuracy,latency_ms_avg --directions max,min --json 2>/dev/null \
    | python3 -m json.tool >/dev/null; then
  ok "pareto --json: valid JSON"
else
  bad "pareto --json: invalid JSON"
fi

# diff two runs
DIFF_OUT=$(python3 "$MEX" diff "${RUNS[0]}" "${RUNS[1]}" 2>&1)
if echo "$DIFF_OUT" | grep -q "metadata changes"; then
  ok "diff: metadata section present"
else
  bad "diff: missing metadata section"
fi
if echo "$DIFF_OUT" | grep -q "accuracy"; then
  ok "diff: metrics compared"
else
  bad "diff: metrics not compared"
fi

# events — filter by tool
# (RUNS is sorted descending so [2] is the oldest / run_a baseline)
BASELINE_RUN="${RUNS[2]}"
if python3 "$MEX" events "$BASELINE_RUN" --tool obsidian_search 2>/dev/null | grep -q "obsidian_search"; then
  ok "events: can filter by tool name"
else
  bad "events: tool filter failed"
fi

# events — count
EVENT_COUNT=$(python3 "$MEX" events "$BASELINE_RUN" 2>/dev/null | wc -l)
if [ "$EVENT_COUNT" -gt 5 ]; then
  ok "events: baseline run has multiple events ($EVENT_COUNT)"
else
  bad "events: baseline run has too few events ($EVENT_COUNT)"
fi

# aggregate: per-tool stats
AGG_OUT=$(python3 "$MEX" aggregate "$BASELINE_RUN" 2>&1)
if echo "$AGG_OUT" | grep -q "obsidian_search"; then
  ok "aggregate: lists obsidian_search in per-tool table"
else
  bad "aggregate: obsidian_search missing from per-tool table"
fi
if echo "$AGG_OUT" | grep -q "100.00%"; then
  ok "aggregate: computes success_rate"
else
  bad "aggregate: success_rate not shown"
fi

# aggregate --json
if python3 "$MEX" aggregate "$BASELINE_RUN" --json 2>/dev/null | python3 -m json.tool >/dev/null; then
  ok "aggregate --json: valid JSON"
else
  bad "aggregate --json: invalid JSON"
fi

# pareto --plot (requires exactly 2 axes)
PLOT_OUT=$(python3 "$MEX" pareto --axes accuracy,latency_ms_avg --directions max,min --plot 2>&1)
if echo "$PLOT_OUT" | grep -q "Pareto frontier"; then
  ok "pareto --plot: frontier header present"
else
  bad "pareto --plot: frontier header missing"
fi
if echo "$PLOT_OUT" | grep -q "legend:"; then
  ok "pareto --plot: ASCII plot rendered"
else
  bad "pareto --plot: plot legend missing"
fi
# Must contain at least one * (on-frontier marker) and one . (dominated marker)
if echo "$PLOT_OUT" | grep -q '\*' && echo "$PLOT_OUT" | grep -qE '\.$|\.\s|^\.'; then
  ok "pareto --plot: both frontier (*) and dominated (.) markers present"
else
  bad "pareto --plot: missing frontier or dominated markers"
fi

# ---- 4. environment-snapshot -------------------------------------------------
log "4/4  environment-snapshot: markdown + JSON + secret safety"

SNAP="$SCRIPT_DIR/environment-snapshot.py"

# Pre-seed a .env with a bot token we'll assert is NEVER emitted
cat > "$FAKE/.env" <<'INNER_ENV'
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
TELEGRAM_BOT_TOKEN=NEEDLE_TOKEN_MUST_NOT_LEAK
NOTION_API_KEY=secret_NEEDLE_NEEDLE_NEEDLE
OBSIDIAN_VAULT_PATH=/tmp/notreal
INNER_ENV
chmod 600 "$FAKE/.env"

MD=$(python3 "$SNAP" --projects-dir "$FAKE" 2>&1)
JSN=$(python3 "$SNAP" --projects-dir "$FAKE" --json 2>&1)

# Valid JSON?
if echo "$JSN" | python3 -m json.tool >/dev/null 2>&1; then
  ok "snapshot --json: valid JSON"
else
  bad "snapshot --json: invalid JSON"
fi

# Key names should be emitted
if echo "$MD" | grep -q "TELEGRAM_BOT_TOKEN"; then
  ok "snapshot markdown: lists key names"
else
  bad "snapshot markdown: key names missing"
fi

# Secret VALUES should NEVER be emitted
if echo "$MD$JSN" | grep -q "NEEDLE_TOKEN_MUST_NOT_LEAK\|secret_NEEDLE_NEEDLE_NEEDLE"; then
  bad "snapshot LEAKED secret value(s)"
else
  ok "snapshot: no secret values in output"
fi

# OBSIDIAN_VAULT_PATH is not a secret, should be shown
if echo "$MD" | grep -q "/tmp/notreal"; then
  ok "snapshot: non-secret vault path surfaced"
else
  bad "snapshot: vault path missing"
fi

# Skill discovery
if echo "$MD" | grep -q "obsidian-search" && echo "$MD" | grep -q "notion-search"; then
  ok "snapshot: discovered obsidian-search + notion-search"
else
  bad "snapshot: skill discovery failed"
fi

# ---- summary -----------------------------------------------------------------
echo
hr
if [ "$FAIL" -eq 0 ]; then
  printf '%s  ✓ %d checks passed, 0 failed%s\n' "$c_green" "$PASS" "$c_off"
  hr
  exit 0
else
  printf '%s  ✗ %d checks passed, %d failed%s\n' "$c_red" "$PASS" "$FAIL" "$c_off"
  hr
  exit 1
fi
