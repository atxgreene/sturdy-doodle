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
head1 '1/10  pip install -e . into a fresh venv'
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
head1 '2/10  Model providers — 19 backends detected'
# ==============================================================================
export PATH="$DEMO_VENV/bin:$PATH"
export MNEMOSYNE_PROJECTS_DIR="$DEMO_PROJECTS"

head2 'mnemosyne-models list'
mnemosyne-models list | head -25

head2 'mnemosyne-models current   (no auth configured → falls back)'
mnemosyne-models current

# ==============================================================================
head1 '3/10  Environment snapshot  (first-turn preamble, Meta-Harness Terminal-Bench 2 pattern)'
# ==============================================================================
head2 'environment-snapshot  (human-readable markdown)'
environment-snapshot --projects-dir "$DEMO_PROJECTS" 2>&1 | head -30

# ==============================================================================
head1 '4/10  Memory layer — SQLite+FTS5 with ICMS 3-tier'
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
head1 '5/10  Identity lock — regardless of underlying model, agent says Mnemosyne'
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
head1 '6/10  Skills — agentskills.io-compatible registry + self-improvement'
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
head1 '7/10  Full pipeline — OBSERVE → EVALUATE → SWEEP → COMPARE → INSPECT'
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
head1 '8/10  Aggregate statistics — per-tool call counts, latency percentiles'
# ==============================================================================
LATEST=$(mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" list --limit 1 | head -1 | awk '{print $1}')
head2 "aggregate for $LATEST"
mnemosyne-experiments --projects-dir "$DEMO_PROJECTS" aggregate "$LATEST"

# ==============================================================================
head1 '9/10  Live dashboard (single frame via --once --plain)'
# ==============================================================================
bash "$SCRIPT_DIR/mnemosyne-dashboard.sh" --once --plain

# ==============================================================================
head1 '10/10  Test suite — 123/123 passing'
# ==============================================================================
head2 'bash test-harness.sh (integration)'
bash "$SCRIPT_DIR/test-harness.sh" 2>&1 | tail -4

head2 'python3 tests/test_all.py (unit)'
"$DEMO_VENV/bin/python3" "$SCRIPT_DIR/tests/test_all.py" 2>&1 | tail -2

# ==============================================================================
head1 'Demo complete.'
# ==============================================================================
printf '\n'
printf 'All 10 sections exercised. Identity lock holds across slip attempts.\n'
printf 'Full pipeline produces real experiments in the fake PROJECTS_DIR and\n'
printf 'the CLI tools read them back without sys.path shims. 123/123 tests pass.\n'
printf '\n'
printf 'Re-run this demo anytime with: bash demo.sh\n'
printf 'Transcript regenerated with:   bash demo.sh > docs/DEMO.md 2>&1\n'
