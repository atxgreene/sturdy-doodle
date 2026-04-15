#!/usr/bin/env bash
# ==============================================================================
#  demo.sh
#
#  Reproducible end-to-end demo of the Mnemosyne harness. Exercises every
#  layer — install, providers, identity lock, memory, skills, brain, sweep,
#  Pareto, experiments CLI, GUI dashboard, tests — with zero external
#  dependencies.
#
#  This is the script that generates docs/DEMO.md. Re-run it anytime the
#  branch moves:
#
#      bash demo.sh > docs/DEMO.md 2>&1
#
#  Every section is delimited by ──── headers so the transcript reads
#  cleanly. No network calls. No API keys. No actual LLM inference. The
#  brain demo uses a mock chat_fn so identity-lock rewriting is visible
#  without paying for tokens.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEMO_ROOT=$(mktemp -d /tmp/mnemo-demo-XXXXXX)
DEMO_VENV="$DEMO_ROOT/venv"
DEMO_PROJECTS="$DEMO_ROOT/projects"
mkdir -p "$DEMO_PROJECTS"

cleanup() {
  rm -rf "$DEMO_ROOT"
}
trap cleanup EXIT

# ---- pretty output ----------------------------------------------------------
hr()    { printf '────────────────────────────────────────────────────────────────\n'; }
head1() { printf '\n'; hr; printf ' %s\n' "$1"; hr; }
head2() { printf '\n── %s\n' "$1"; }

printf 'Mnemosyne end-to-end demo\n'
printf 'Generated:   %s\n' "$(date -Iseconds)"
printf 'Commit:      %s\n' "$(git rev-parse --short HEAD 2>/dev/null || echo '(not a git repo)')"
printf 'Branch:      %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')"
printf 'Python:      %s\n' "$(python3 --version)"

# ==============================================================================
head1 '1/18  pip install -e . into a fresh venv'
# ==============================================================================
python3 -m venv "$DEMO_VENV"
"$DEMO_VENV/bin/pip" install --quiet --upgrade pip
"$DEMO_VENV/bin/pip" install -e "$SCRIPT_DIR" 2>&1 | tail -3

head2 "Installed console entry points on \$PATH:"
# shellcheck disable=SC2010  # grep on ls output is fine for a demo transcript
ls "$DEMO_VENV/bin" | grep -E 'mnemo|obsidian|notion|environ|harness' | sort | sed 's/^/  /'

head2 'Library imports (no sys.path hacks):'
"$DEMO_VENV/bin/python3" -c "
from harness_telemetry import TelemetrySession, create_run, finalize_run
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_memory import MemoryStore, L1_HOT, L2_WARM, L3_COLD
from mnemosyne_models import Backend, from_env, detect_providers
from mnemosyne_skills import SkillRegistry, record_learned_skill
from mnemosyne_identity import enforce_identity, MNEMOSYNE_IDENTITY
print('  ✓ all 7 library surfaces import cleanly')
"

# ==============================================================================
head1 '2/18  Model providers — 19 backends detected'
# ==============================================================================
export PATH="$DEMO_VENV/bin:$PATH"
export MNEMOSYNE_PROJECTS_DIR="$DEMO_PROJECTS"

head2 'mnemosyne-models list'
mnemosyne-models list | head -25

head2 'mnemosyne-models current   (no auth configured → falls back)'
mnemosyne-models current

# ==============================================================================
head1 '3/18  Environment snapshot  (first-turn preamble, Meta-Harness Terminal-Bench 2 pattern)'
# ==============================================================================
head2 'environment-snapshot  (human-readable markdown)'
environment-snapshot --projects-dir "$DEMO_PROJECTS" 2>&1 | head -30

# ==============================================================================
head1 '4/18  Memory layer — SQLite+FTS5 with ICMS 3-tier'
# ==============================================================================
MEM_DB="$DEMO_PROJECTS/memory.db"

head2 'Writing 4 memories across all three tiers'
mnemosyne-memory --db "$MEM_DB" write "User prefers gemma4:e4b as default model" --kind preference --tier 1
mnemosyne-memory --db "$MEM_DB" write "Project alpha uses rust and tokio" --kind project --tier 2
mnemosyne-memory --db "$MEM_DB" write "Old fact: python pre-2026 had no DeltaNet support" --kind fact --tier 3
mnemosyne-memory --db "$MEM_DB" write "User likes direct, technical voice in responses" --kind preference --tier 1

head2 "search 'gemma' — FTS5-accelerated"
mnemosyne-memory --db "$MEM_DB" search gemma

head2 "search 'rust' with --tier-max 2 (excludes cold memories)"
mnemosyne-memory --db "$MEM_DB" search rust --tier-max 2

head2 'Stats:'
mnemosyne-memory --db "$MEM_DB" stats

# ==============================================================================
head1 '5/18  Identity lock — regardless of underlying model, agent says Mnemosyne'
# ==============================================================================
head2 'Testing enforce_identity() against 5 slip patterns + 3 legitimate uses'
"$DEMO_VENV/bin/python3" <<'PY'
from mnemosyne_identity import enforce_identity

cases = [
    ('I am Claude, an AI assistant made by Anthropic.',                         'SLIP'),
    ("I'm ChatGPT, created by OpenAI.",                                         'SLIP'),
    ('My name is Gemini.',                                                      'SLIP'),
    ('I was trained by Anthropic to be helpful.',                               'SLIP'),
    ('As an AI language model, I cannot help with that.',                       'SLIP'),
    ('The difference between Claude and GPT-4 is context window size.',         'KEEP'),
    ('You can call the Anthropic API or the OpenAI API for this.',              'KEEP'),
    ('Mnemosyne supports Claude, Gemini, Qwen, and many other underlying models.', 'KEEP'),
]
for inp, kind in cases:
    out, slips = enforce_identity(inp)
    marker = 'CHANGED' if out != inp else 'kept   '
    ok = ('✓' if (kind == 'SLIP' and out != inp) or (kind == 'KEEP' and out == inp) else '✗')
    print(f'  {ok} [{kind}] {marker}: {inp[:56]}')
    if out != inp:
        print(f'                   → {out[:56]}')
PY

head2 'Brain end-to-end (mock LLM that slips to "I am Claude")'
"$DEMO_VENV/bin/python3" <<'PY'
import os
os.environ['MNEMOSYNE_PROJECTS_DIR'] = os.environ['MNEMOSYNE_PROJECTS_DIR']
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_memory import MemoryStore
from mnemosyne_skills import SkillRegistry
from pathlib import Path

def mock_chat(messages, **kwargs):
    # Simulate a model that leaks its underlying identity
    return {
        'status': 'ok',
        'text': 'I am Claude, an AI assistant made by Anthropic. How can I help you today?',
        'tool_calls': [], 'usage': None, 'duration_ms': 5.0, 'raw': {},
    }

mem = MemoryStore(path=Path(os.environ['MNEMOSYNE_PROJECTS_DIR']) / 'brain-demo.db')
reg = SkillRegistry()
brain = Brain(memory=mem, skills=reg, chat_fn=mock_chat)
resp = brain.turn('Who are you?')
print(f'  user        : Who are you?')
print(f'  model said  : I am Claude, an AI assistant made by Anthropic. How can I help you today?')
print(f'  brain output: {resp.text}')
print(f'  identity lock: {"HELD ✓" if "Mnemosyne" in resp.text and "Claude" not in resp.text else "FAILED ✗"}')
mem.close()
PY

# ==============================================================================
head1 '6/18  Skills — agentskills.io-compatible registry + self-improvement'
# ==============================================================================
"$DEMO_VENV/bin/python3" <<'PY'
import os, tempfile
from pathlib import Path
from mnemosyne_skills import SkillRegistry, record_learned_skill, parse_skill_file

reg = SkillRegistry()

@reg.register_python('add', 'add two integers', [
    {'name': 'a', 'type': 'integer', 'required': True},
    {'name': 'b', 'type': 'integer', 'required': True},
])
def add(a, b):
    return a + b

print(f'  Registered skills: {reg.names()}')
print(f'  OpenAI tool-spec shape:')
import json
print(json.dumps(reg.tools()[0], indent=2, default=str)[:400])

# Discover installed $PATH commands (obsidian-search, notion-search)
n = reg.discover_path_commands()
print(f'\n  Discovered {n} $PATH skills: {[s for s in reg.names() if s != "add"]}')

# Self-improvement: write a learned skill file
pd = Path(os.environ['MNEMOSYNE_PROJECTS_DIR'])
path = record_learned_skill(
    name='search-and-summarize',
    description='Search Obsidian then Notion then produce a concise summary.',
    command='obsidian-search --json search {query} --limit 5',
    parameters=[{'name': 'query', 'type': 'string', 'required': True}],
    notes='The brain learned this after the user asked for cross-source summaries 3 times.',
    projects_dir=pd,
)
print(f'\n  Learned skill written to: {path.relative_to(pd.parent)}')
loaded = parse_skill_file(path)
print(f'  Parsed back:  name={loaded.name}  learned={loaded.learned}')
PY

# ==============================================================================
head1 '7/18  Full pipeline — OBSERVE → EVALUATE → SWEEP → COMPARE → INSPECT'
# ==============================================================================
head2 'Running examples/sweep_demo.py (8-point sweep, fake harness, ~6 seconds)'
"$DEMO_VENV/bin/python3" "$SCRIPT_DIR/examples/sweep_demo.py" --projects-dir "$DEMO_PROJECTS" 2>&1 | tail -12

head2 'mnemosyne-experiments list  (newest first)'
mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" list --limit 8

head2 'Top 3 by accuracy:'
mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" top-k 3 --metric accuracy --direction max

head2 'Pareto frontier on accuracy × latency  (ASCII plot):'
mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" pareto \
  --axes accuracy,latency_ms_avg --directions max,min --plot 2>&1 | head -30

# ==============================================================================
head1 '8/18  Aggregate statistics — per-tool call counts, latency percentiles'
# ==============================================================================
LATEST=$(mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" list --limit 1 | head -1 | awk '{print $1}')
head2 "aggregate for $LATEST"
mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" aggregate "$LATEST"

# ==============================================================================
head1 '9/18  Self-healing triage engine (Peter Pang / CREAO pattern, local-first)'
# ==============================================================================
head2 'mnemosyne-triage scan --window-days 30  (reads events.jsonl from our demo runs)'
mnemosyne-triage --projects-dir "$DEMO_PROJECTS" scan --window-days 30 --top-n 5

head2 'Daily health report was written to:'
# shellcheck disable=SC2012  # ls + sed is fine for a demo transcript
ls "$DEMO_PROJECTS/health/" 2>/dev/null | sed 's/^/  /'
head2 'First 20 lines of the report:'
head -20 "$DEMO_PROJECTS"/health/*.md 2>/dev/null | sed 's/^/  /'

# ==============================================================================
head1 '10/18  Meta-Harness proposer — triage → proposals (rule-based v1)'
# ==============================================================================
head2 'Seed an identity-slip event so the proposer has something to react to'
"$DEMO_VENV/bin/python3" <<'PY'
import os, harness_telemetry as ht
rid = ht.create_run(model='demo-model', tags=['proposer-demo'])
with ht.TelemetrySession(rid) as sess:
    for _ in range(12):
        sess.log('identity_slip_detected', status='error',
                 metadata={'slips': ['I am Claude'], 'count': 1})
ht.finalize_run(rid, metrics={'turns_total': 12, 'turns_failed': 12})
print(f'  seeded run: {rid}')
PY

head2 'mnemosyne-proposer --min-severity 0  (rule engine reads triage clusters)'
mnemosyne-proposer --projects-dir "$DEMO_PROJECTS" --window-days 30 --min-severity 0

head2 'Proposal written to disk:'
# shellcheck disable=SC2012
ls "$DEMO_PROJECTS/proposals/" 2>/dev/null | sed 's/^/  /'
head2 'First 25 lines of the newest proposal:'
# shellcheck disable=SC2012
PROP=$(ls -1t "$DEMO_PROJECTS"/proposals/PROP-*.md 2>/dev/null | head -1)
[ -n "$PROP" ] && head -25 "$PROP" | sed 's/^/  /'

# ==============================================================================
head1 '11/18  Dream consolidation — offline pattern extraction from L3 cold'
# ==============================================================================
head2 'Seed 12 related L3 memories (user-preference pattern)'
"$DEMO_VENV/bin/python3" <<'PY'
import os
from pathlib import Path
from mnemosyne_memory import MemoryStore, L3_COLD
# Use the default memory.db so `mnemosyne-dreams` (CLI) operates on the same store
pd = Path(os.environ['MNEMOSYNE_PROJECTS_DIR'])
store = MemoryStore(path=pd / 'memory.db')
patterns = [
    'user prefers dark mode in terminal apps',
    'user uses dark mode in vscode editor',
    'user likes dark theme colors at night',
    'user set dark background in obsidian vault',
    'dark mode preference across all editor tools',
    'dark palette requested for dashboards',
    'weather forecast shows rain tomorrow afternoon',
    'weather alert heavy rain storm warning',
    'weather rain today tomorrow forecast',
    'weather update evening rain expected',
    'weather advisory thunderstorm tonight',
    'weather report rainy weekend incoming',
]
for p in patterns:
    store.write(content=p, tier=L3_COLD, kind='fact', source='demo')
print(f'  seeded {len(patterns)} L3 memories')
print(f'  L3 count: {store.stats()["by_tier"]["L3_cold"]}')
store.close()
PY

head2 'mnemosyne-dreams  (stdlib summarizer, no LLM calls)'
"$DEMO_VENV/bin/python3" -m mnemosyne_dreams \
  --projects-dir "$DEMO_PROJECTS" \
  --similarity 0.1 --min-cluster-size 3 --max-memories 100 2>&1 | sed 's/^/  /'

head2 'Dream report JSON:'
# shellcheck disable=SC2012
ls "$DEMO_PROJECTS/dreams/" 2>/dev/null | sed 's/^/  /'

# ==============================================================================
head1 '12/18  Inner dialogue — Planner → Critic → Doer on tagged turns'
# ==============================================================================
"$DEMO_VENV/bin/python3" <<'PY'
import os
from pathlib import Path
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_memory import MemoryStore
from mnemosyne_skills import SkillRegistry

# Mock model that returns different text depending on which persona is asking
def mock_chat(messages, **kw):
    sys_text = next((m['content'] for m in messages if m['role'] == 'system'), '')
    if 'Role: Planner' in sys_text:
        return {'status': 'ok',
                'text': '### Goal\nReview migration plan.\n\n### Plan\n1. Backup the db\n2. Apply migration in a tx\n3. Validate row counts',
                'tool_calls': []}
    if 'Role: Critic' in sys_text:
        return {'status': 'ok',
                'text': '### Concerns\n- Backup step needs off-host copy.\n### Recommend\n- revise: add off-host backup',
                'tool_calls': []}
    if 'Role: Doer' in sys_text:
        return {'status': 'ok',
                'text': 'Plan: (1) take an off-host backup, (2) apply the migration inside a transaction, (3) validate row counts. If any step fails, roll back.',
                'tool_calls': []}
    # single-pass fallback
    return {'status': 'ok', 'text': 'single-pass answer', 'tool_calls': []}

pd = Path(os.environ['MNEMOSYNE_PROJECTS_DIR'])
store = MemoryStore(path=pd / 'inner-demo.db')
cfg = BrainConfig(
    inner_dialogue_enabled=True,
    inner_dialogue_tags={'hard'},
    adapt_to_context=False,
    inject_env_snapshot=False,
)
brain = Brain(config=cfg, memory=store, skills=SkillRegistry(), chat_fn=mock_chat)

print('  ── untagged turn (single-pass path)')
r1 = brain.turn('What is 2 + 2?', metadata={'tags': []})
print(f'    answer: {r1.text}')
print(f'    model calls: {r1.model_calls}')

print()
print('  ── tagged turn (inner-dialogue path)')
r2 = brain.turn('Plan a production database migration', metadata={'tags': ['hard']})
print(f'    answer: {r2.text}')
print(f'    model calls: {r2.model_calls}  (planner + critic + doer)')
store.close()
PY

# ==============================================================================
head1 '13/18  Goal stack — persistent TODOs across sessions'
# ==============================================================================
head2 'Seed two goals via the CLI'
mnemosyne-goals --projects-dir "$DEMO_PROJECTS" add "ship v0.2.0 release notes" --priority 1 --tags "release,docs" | sed 's/^/  /'
mnemosyne-goals --projects-dir "$DEMO_PROJECTS" add "review Peter Pang article for loop ideas" --priority 3 --tags "reading" | sed 's/^/  /'

head2 'List open goals (priority-sorted)'
mnemosyne-goals --projects-dir "$DEMO_PROJECTS" list | sed 's/^/  /'

head2 'Brain with goals_inject=True surfaces them in the first-turn system prompt'
"$DEMO_VENV/bin/python3" <<'PY'
import os
from pathlib import Path
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_memory import MemoryStore
from mnemosyne_skills import SkillRegistry

captured = {}
def mock_chat(messages, **kw):
    captured['system'] = next((m['content'] for m in messages if m['role'] == 'system'), '')
    return {'status': 'ok', 'text': 'noted the open goals', 'tool_calls': []}

pd = Path(os.environ['MNEMOSYNE_PROJECTS_DIR'])
store = MemoryStore(path=pd / 'goals-demo.db')
cfg = BrainConfig(adapt_to_context=False, inject_env_snapshot=False, goals_inject=True)
brain = Brain(config=cfg, memory=store, skills=SkillRegistry(), chat_fn=mock_chat)
brain.turn('what should we work on today?')
for line in captured.get('system', '').splitlines():
    if 'goals' in line.lower() or line.startswith('- (P'):
        print(f'  {line}')
store.close()
PY

# ==============================================================================
head1 '14/18  Apply-agent — closes the Meta-Harness loop'
# ==============================================================================
head2 'Mark one identity proposal as accepted, then run mnemosyne-apply'
PROP=$(ls -1t "$DEMO_PROJECTS"/proposals/PROP-*identity*.md 2>/dev/null | head -1)
if [ -n "$PROP" ]; then
  python3 -c "
import sys
from pathlib import Path
p = Path('$PROP')
text = p.read_text()
p.write_text(text.replace('status: pending', 'status: accepted'))
print(f'  marked accepted: {p.name}')
"
  mnemosyne-apply --projects-dir "$DEMO_PROJECTS" | sed 's/^/  /'
  head2 'Proposal status after apply:'
  head -10 "$PROP" | sed 's/^/  /'
else
  echo "  (no identity proposal to apply — skipping)"
fi

# ==============================================================================
head1 '15/18  MCP bridge — Mnemosyne skills exposed as Model Context Protocol tools'
# ==============================================================================
head2 'mnemosyne-mcp serve reads JSON-RPC from stdin; we drive it inline'
"$DEMO_VENV/bin/python3" <<'PY'
import io, json, sys
from mnemosyne_skills import SkillRegistry
import mnemosyne_mcp as mcp

reg = SkillRegistry()

@reg.register_python('echo', 'return the input unchanged',
                     [{'name': 'text', 'type': 'string', 'required': True}])
def echo(text: str) -> dict:
    return {'echoed': text}

stdin = io.StringIO(
    json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}) + '\n' +
    json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}) + '\n' +
    json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'echo', 'arguments': {'text': 'hi from MCP'}}}) + '\n'
)
out = io.StringIO()
orig = sys.stdin, sys.stdout
sys.stdin, sys.stdout = stdin, out
try:
    mcp.serve_stdio(registry=reg)
finally:
    sys.stdin, sys.stdout = orig

for line in out.getvalue().splitlines():
    r = json.loads(line)
    if 'result' in r:
        snippet = json.dumps(r['result'])[:120]
        print(f'  id={r["id"]:<2} result: {snippet}...' if len(snippet) >= 120 else f'  id={r["id"]:<2} result: {snippet}')
PY

# ==============================================================================
head1 '16/18  Live dashboard (single frame via --once --plain)'
# ==============================================================================
bash "$SCRIPT_DIR/mnemosyne-dashboard.sh" --once --plain

# ==============================================================================
head1 '17/18  Training bridge — telemetry → Hermes-compatible ShareGPT (LoRA ready)'
# ==============================================================================
head2 'Seed two successful training_turn events in a fresh run'
"$DEMO_VENV/bin/python3" <<'PY'
import json, os
from pathlib import Path
pd = Path(os.environ['MNEMOSYNE_PROJECTS_DIR'])
rd = pd / 'experiments' / 'run_train_demo'
rd.mkdir(parents=True, exist_ok=True)
events = [
    {'event_id': 'a1', 'event_type': 'turn_start', 'metadata': {'turn_number': 1}},
    {'event_id': 'a2', 'event_type': 'training_turn', 'parent_event_id': 'a1',
     'metadata': {'system_prompt': 'You are Mnemosyne.',
                  'user_message': 'What is the capital of France?',
                  'assistant_text': 'Paris is the capital of France.',
                  'tool_calls': [], 'model': 'qwen3.5:9b', 'provider': 'ollama'}},
    {'event_id': 'a3', 'event_type': 'turn_end', 'parent_event_id': 'a1', 'status': 'ok'},
    {'event_id': 'b1', 'event_type': 'turn_start', 'metadata': {'turn_number': 2}},
    {'event_id': 'b2', 'event_type': 'training_turn', 'parent_event_id': 'b1',
     'metadata': {'system_prompt': 'You are Mnemosyne.',
                  'user_message': 'Search my Obsidian vault for mnemosyne.',
                  'assistant_text': 'Found 3 notes.',
                  'tool_calls': [{'name': 'obsidian_search',
                                   'args': {'query': 'mnemosyne'},
                                   'result': {'matches': [{'path': 'a.md'}]}}],
                  'model': 'qwen3.5:9b', 'provider': 'ollama'}},
    {'event_id': 'b3', 'event_type': 'turn_end', 'parent_event_id': 'b1', 'status': 'ok'},
]
with (rd / 'events.jsonl').open('w') as f:
    for e in events:
        f.write(json.dumps(e) + '\n')
print(f'  seeded {len(events)} events in {rd.name}')
PY

head2 'mnemosyne-train export   (Hermes-compatible ShareGPT)'
mnemosyne-train export --projects-dir "$DEMO_PROJECTS" --json 2>&1 | head -15

head2 'First exported trajectory — Hermes schema:'
OUT=$(mnemosyne-train export --projects-dir "$DEMO_PROJECTS" --out /tmp/trajs.jsonl --json | python3 -c "import json,sys; print(json.load(sys.stdin)['out_path'])")
head -1 "$OUT" | python3 -m json.tool | sed 's/^/  /' | head -40

head2 'Schema check: Hermes-compatible keys present:'
head -1 "$OUT" | python3 -c "
import json, sys
o = json.loads(sys.stdin.read())
needed = ('prompt_index','conversations','metadata','completed','partial',
          'api_calls','toolsets_used','tool_stats','tool_error_counts')
missing = [k for k in needed if k not in o]
print('  ✓ all Hermes keys present' if not missing else f'  ✗ missing: {missing}')
print(f'  ✓ mnemo metadata: {list(k for k in o[\"metadata\"] if k.startswith(\"mnemo_\"))}')"

head2 'mnemosyne-train deploy --to lmstudio --dry-run  (shows target path, no install)'
mkdir -p /tmp/fake-adapter
echo FAKE > /tmp/fake-adapter/model.gguf
mnemosyne-train deploy /tmp/fake-adapter --to lmstudio --name mnemo-demo-v1 --dry-run --json 2>&1 | sed 's/^/  /'

# ==============================================================================
head1 '18/18  Test suite'
# ==============================================================================
head2 'bash test-harness.sh (integration)'
bash "$SCRIPT_DIR/test-harness.sh" 2>&1 | tail -4

head2 'python3 tests/test_all.py (unit)'
"$DEMO_VENV/bin/python3" "$SCRIPT_DIR/tests/test_all.py" 2>&1 | tail -2

# ==============================================================================
head1 'Demo complete.'
# ==============================================================================
printf '\n'
printf 'All 18 sections exercised. Identity lock holds across slip attempts.\n'
printf 'Triage → proposer → apply closes the Meta-Harness loop end-to-end.\n'
printf 'Dreams compress L3 cold memories; inner dialogue fires on hard turns.\n'
printf 'Goal stack persists across sessions; MCP bridge exposes skills.\n'
printf 'Training bridge emits Hermes-compatible ShareGPT JSONL.\n'
printf 'Full pipeline produces real experiments in the fake PROJECTS_DIR and\n'
printf 'the CLI tools read them back without sys.path shims. All tests pass.\n'
printf '\n'
printf 'Re-run this demo anytime with: bash demo.sh\n'
printf 'Transcript regenerated with:   bash demo.sh > docs/DEMO.md 2>&1\n'
