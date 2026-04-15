Mnemosyne end-to-end demo
Generated:   2026-04-15T04:06:31+00:00
Commit:      2171283
Branch:      claude/setup-mnemosyne-consciousness-NZqQE
Python:      Python 3.11.15

────────────────────────────────────────────────────────────────
 1/18  pip install -e . into a fresh venv
────────────────────────────────────────────────────────────────
Successfully built mnemosyne-harness
Installing collected packages: mnemosyne-harness
Successfully installed mnemosyne-harness-0.2.0

── Installed console entry points on $PATH:
  environment-snapshot
  harness-telemetry
  mnemosyne-apply
  mnemosyne-dreams
  mnemosyne-experiments
  mnemosyne-goals
  mnemosyne-mcp
  mnemosyne-memory
  mnemosyne-models
  mnemosyne-pipeline
  mnemosyne-proposer
  mnemosyne-scengen
  mnemosyne-serve
  mnemosyne-train
  mnemosyne-triage
  notion-search
  obsidian-search

── Library imports (no sys.path hacks):
  ✓ all 7 library surfaces import cleanly

────────────────────────────────────────────────────────────────
 2/18  Model providers — 19 backends detected
────────────────────────────────────────────────────────────────

── mnemosyne-models list
provider        kind    status         env var                 endpoint
------------------------------------------------------------------------------------------------------------------------
anthropic       cloud   unauthorized   ANTHROPIC_API_KEY       https://api.anthropic.com/v1/messages
cerebras        cloud   unauthorized   CEREBRAS_API_KEY        https://api.cerebras.ai/v1/chat/completions
cohere          cloud   unauthorized   COHERE_API_KEY          https://api.cohere.ai/compatibility/v1/chat/completions
deepseek        cloud   unauthorized   DEEPSEEK_API_KEY        https://api.deepseek.com/v1/chat/completions
fireworks       cloud   unauthorized   FIREWORKS_API_KEY       https://api.fireworks.ai/inference/v1/chat/completions
google          cloud   unauthorized   GOOGLE_API_KEY          https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
groq            cloud   unauthorized   GROQ_API_KEY            https://api.groq.com/openai/v1/chat/completions
hyperbolic      cloud   unauthorized   HYPERBOLIC_API_KEY      https://api.hyperbolic.xyz/v1/chat/completions
lmstudio        local   local/unreachable  -                       http://localhost:1234/v1/chat/completions
mistral         cloud   unauthorized   MISTRAL_API_KEY         https://api.mistral.ai/v1/chat/completions
nous            cloud   unauthorized   NOUS_PORTAL_API_KEY     https://inference-api.nousresearch.com/v1/chat/completions
novita          cloud   unauthorized   NOVITA_API_KEY          https://api.novita.ai/v3/openai/chat/completions
ollama          local   local/unreachable  -                       http://localhost:11434/api/chat
openai          cloud   unauthorized   OPENAI_API_KEY          https://api.openai.com/v1/chat/completions
openrouter      cloud   unauthorized   OPENROUTER_API_KEY      https://openrouter.ai/api/v1/chat/completions
perplexity      cloud   unauthorized   PERPLEXITY_API_KEY      https://api.perplexity.ai/chat/completions
tgi             local   local/unreachable  -                       http://localhost:8080/v1/chat/completions
together        cloud   unauthorized   TOGETHER_API_KEY        https://api.together.xyz/v1/chat/completions
vllm            local   local/unreachable  -                       http://localhost:8000/v1/chat/completions
xai             cloud   unauthorized   XAI_API_KEY             https://api.x.ai/v1/chat/completions

── mnemosyne-models current   (no auth configured → falls back)
provider:       ollama
endpoint:       http://localhost:11434/api/chat
default_model:  qwen3:8b
has_api_key:    False

────────────────────────────────────────────────────────────────
 3/18  Environment snapshot  (first-turn preamble, Meta-Harness Terminal-Bench 2 pattern)
────────────────────────────────────────────────────────────────

── environment-snapshot  (human-readable markdown)
# Mnemosyne environment snapshot

**Projects dir:** /tmp/mnemo-demo-IzPRYK/projects (0 entries)

**.env:** not found (run mnemosyne-wizard.sh)

**Ollama:** NOT reachable at http://localhost:11434 (URLError)

**GPU:** none detected (CPU inference)

**venv:** NOT FOUND at /tmp/mnemo-demo-IzPRYK/projects/.venv

**Skills available:** notion-search, obsidian-search

**Obsidian vault:** not configured (.env missing)

**Disk:** 32.0 GB free of 270.6 GB (11.8% free)

**Platform:** Linux 6.18.5, Python 3.11.15

────────────────────────────────────────────────────────────────
 4/18  Memory layer — SQLite+FTS5 with ICMS 3-tier
────────────────────────────────────────────────────────────────

── Writing 4 memories across all three tiers
1
2
3
4

── search 'gemma' — FTS5-accelerated

── search 'rust' with --tier-max 2 (excludes cold memories)
[L2] Project alpha uses rust and tokio  (project, cli)

── Stats:
{
  "total": 4,
  "by_tier": {
    "L1_hot": 2,
    "L2_warm": 1,
    "L3_cold": 1
  },
  "by_kind": {
    "fact": 1,
    "preference": 2,
    "project": 1
  },
  "fts5_enabled": true,
  "db_path": "/tmp/mnemo-demo-IzPRYK/projects/memory.db",
  "schema_version": 1
}

────────────────────────────────────────────────────────────────
 5/18  Identity lock — regardless of underlying model, agent says Mnemosyne
────────────────────────────────────────────────────────────────

── Testing enforce_identity() against 5 slip patterns + 3 legitimate uses
  ✓ [SLIP] CHANGED: I am Claude, an AI assistant made by Anthropic.
                   → I am Mnemosyne, an AI assistant made by Anthropic.
  ✓ [SLIP] CHANGED: I'm ChatGPT, created by OpenAI.
                   → I am Mnemosyne, created by OpenAI.
  ✓ [SLIP] CHANGED: My name is Gemini.
                   → My name is Mnemosyne.
  ✓ [SLIP] CHANGED: I was trained by Anthropic to be helpful.
                   → I was built from the Mnemosyne framework to be helpful.
  ✓ [SLIP] CHANGED: As an AI language model, I cannot help with that.
                   → I cannot help with that.
  ✓ [KEEP] kept   : The difference between Claude and GPT-4 is context windo
  ✓ [KEEP] kept   : You can call the Anthropic API or the OpenAI API for thi
  ✓ [KEEP] kept   : Mnemosyne supports Claude, Gemini, Qwen, and many other 

── Brain end-to-end (mock LLM that slips to "I am Claude")
  user        : Who are you?
  model said  : I am Claude, an AI assistant made by Anthropic. How can I help you today?
  brain output: I am Mnemosyne, an AI assistant made by Anthropic. How can I help you today?
  identity lock: HELD ✓

────────────────────────────────────────────────────────────────
 6/18  Skills — agentskills.io-compatible registry + self-improvement
────────────────────────────────────────────────────────────────
  Registered skills: ['add']
  OpenAI tool-spec shape:
{
  "type": "function",
  "function": {
    "name": "add",
    "description": "add two integers",
    "parameters": {
      "type": "object",
      "properties": {
        "a": {
          "type": "integer",
          "description": ""
        },
        "b": {
          "type": "integer",
          "description": ""
        }
      },
      "required": [
        "a",
        "b"
      ]
    }
  }

  Discovered 2 $PATH skills: ['notion_search', 'obsidian_search']

  Learned skill written to: projects/skills/learned/search-and-summarize-20260415-040639.md
  Parsed back:  name=search-and-summarize  learned=True

────────────────────────────────────────────────────────────────
 7/18  Full pipeline — OBSERVE → EVALUATE → SWEEP → COMPARE → INSPECT
────────────────────────────────────────────────────────────────

── Running examples/sweep_demo.py (8-point sweep, fake harness, ~6 seconds)
sweep complete: 8 runs in 8.9s

Demo sweep finished: 8 runs created.

Inspect the results:

  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-IzPRYK/projects ./mnemosyne-experiments.py list
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-IzPRYK/projects ./mnemosyne-experiments.py top-k 3 --metric accuracy
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-IzPRYK/projects ./mnemosyne-experiments.py top-k 3 --metric latency_ms_avg --direction min
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-IzPRYK/projects ./mnemosyne-experiments.py pareto \
      --axes accuracy,latency_ms_avg --directions max,min --plot
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-IzPRYK/projects ./mnemosyne-experiments.py aggregate run_20260415-040639-mode-qwen3-8b-retr-5-temp-00

── mnemosyne-experiments list  (newest first)
run_20260415-040647-mode-gemma4-e-retr-15-temp-05  [completed]  gemma4:e4b          2026-04-15 04:06:47  events=42     tags=sweep,demo,example
run_20260415-040646-mode-gemma4-e-retr-15-temp-00  [completed]  gemma4:e4b          2026-04-15 04:06:46  events=42     tags=sweep,demo,example
run_20260415-040645-mode-gemma4-e-retr-5-temp-05  [completed]  gemma4:e4b          2026-04-15 04:06:45  events=42     tags=sweep,demo,example
run_20260415-040644-mode-gemma4-e-retr-5-temp-00  [completed]  gemma4:e4b          2026-04-15 04:06:44  events=42     tags=sweep,demo,example
run_20260415-040643-mode-qwen3-8b-retr-15-temp-05  [completed]  qwen3:8b            2026-04-15 04:06:43  events=42     tags=sweep,demo,example
run_20260415-040642-mode-qwen3-8b-retr-15-temp-00  [completed]  qwen3:8b            2026-04-15 04:06:42  events=42     tags=sweep,demo,example
run_20260415-040640-mode-qwen3-8b-retr-5-temp-05  [completed]  qwen3:8b            2026-04-15 04:06:40  events=42     tags=sweep,demo,example
run_20260415-040639-mode-qwen3-8b-retr-5-temp-00  [completed]  qwen3:8b            2026-04-15 04:06:39  events=42     tags=sweep,demo,example

── Top 3 by accuracy:
Top 3 runs by accuracy (max):
  run_20260415-040646-mode-gemma4-e-retr-15-temp-00  accuracy=0.5  model=gemma4:e4b
  run_20260415-040644-mode-gemma4-e-retr-5-temp-00  accuracy=0.5  model=gemma4:e4b
  run_20260415-040642-mode-qwen3-8b-retr-15-temp-00  accuracy=0.5  model=qwen3:8b

── Pareto frontier on accuracy × latency  (ASCII plot):
Pareto frontier on (accuracy, latency_ms_avg) with directions (max, min):
  run_20260415-040646-mode-gemma4-e-retr-15-temp-00  accuracy=0.5  latency_ms_avg=54.232567687492406  model=gemma4:e4b

  latency_ms_avg
    85.39 |.                         .                         
    83.32 |                                                    
    81.24 |                                                    
    79.16 |                                                    
    77.08 |                                                   .
    75.01 |                                                   .
    72.93 |                                                    
    70.85 |                                                    
    68.77 |                                                    
    66.70 |                                                    
    64.62 |                                                    
    62.54 |                          .                         
    60.46 |                          .                         
    58.39 |                                                    
    56.31 |                                                    
    54.23 |                                                   #
          +----------------------------------------------------
          0.38                                            0.50
                                accuracy

  legend:  * = on Pareto frontier   . = dominated   # = overlap

────────────────────────────────────────────────────────────────
 8/18  Aggregate statistics — per-tool call counts, latency percentiles
────────────────────────────────────────────────────────────────

── aggregate for run_20260415-040647-mode-gemma4-e-retr-15-temp-05
# aggregate for run_20260415-040647-mode-gemma4-e-retr-15-temp-05

total events: 42
  scenario_end   16
  scenario_start 16
  scenario_summary 1
  session_end    1
  session_start  1
  tool_call      7

## overall tool_call stats
  calls:        7
  ok:           7
  errors:       0
  success_rate: 100.00%
  duration_ms:  avg=20.8  p50=23.2  p95=30.8  p99=30.8  total=145.9

## per-tool
  tool                           calls      ok     err     rate    avg_ms    p95_ms
  notion_search                      3       3       0  100.0%      26.5      30.8
  obsidian_search                    4       4       0  100.0%      16.6      28.7

────────────────────────────────────────────────────────────────
 9/18  Self-healing triage engine (Peter Pang / CREAO pattern, local-first)
────────────────────────────────────────────────────────────────

── mnemosyne-triage scan --window-days 30  (reads events.jsonl from our demo runs)
Mnemosyne health — grade D
  window:      30d
  runs:        8
  events:      336
  errors:      69
  identity slip rate:  0.0 per 1000 events
  tool failure rate:   0.0 per 1000 tool_calls
  clusters:    1

  top 1 clusters:
    [ 61.2]  scenario_end            -                   -  (n=69, runs=8)

  report written: /tmp/mnemo-demo-IzPRYK/projects/health/2026-04-15.md

── Daily health report was written to:
  2026-04-15.md

── First 20 lines of the report:
  # Mnemosyne health report — 2026-04-15
  
  **Grade: D**  ·  window: 30d  ·  runs: 8  ·  events: 336  ·  generated: 2026-04-15T04:06:48.825564Z
  
  ## Headline metrics
  
  - Error events:           69
  - Identity-slip rate:     0.0 per 1000 events
  - Tool-failure rate:      0.0 per 1000 tool_calls
  - Distinct clusters:      1
  
  ## Top 1 clusters (by severity)
  
  ### cluster `14aeda1c0b83` — severity 61.2
  
  - event_type: `scenario_end`
  - tool: `-`
  - error_type: `-`
  - count: 69  ·  runs: 8
  - first seen: `2026-04-15T04:06:40.223463Z`

────────────────────────────────────────────────────────────────
 10/18  Meta-Harness proposer — triage → proposals (rule-based v1)
────────────────────────────────────────────────────────────────

── Seed an identity-slip event so the proposer has something to react to
  seeded run: run_20260415-040648-4394d6

── mnemosyne-proposer --min-severity 0  (rule engine reads triage clusters)
Generated 2 proposal(s):
  [ 61.2] PROP-0001 skill    Scenario failures clustered (69 events, severity 61.2)
  [ 52.5] PROP-0002 identity Identity slips recurring (12 events, severity 52.5)

Written to: /tmp/mnemo-demo-IzPRYK/projects/proposals/

── Proposal written to disk:
  PROP-0001-scenario-failures-clustered-69-events-se.md
  PROP-0002-identity-slips-recurring-12-events-sever.md

── First 25 lines of the newest proposal:
  ---
  id: PROP-0002
  created_utc: 2026-04-15T04:06:48.921831Z
  status: pending
  severity: 52.5
  cluster_id: 75efbe3366fa
  category: identity
  ---
  # Identity slips recurring (12 events, severity 52.5)
  
  ## Problem
  
  The identity filter has caught 12 first-person slips to foreign model names across recent runs. This means the model is trying to self-identify as Claude/GPT/etc. despite the MNEMOSYNE_IDENTITY system preamble.
  
  ## Proposal
  
  Two non-exclusive options:
  
  1. Lower `temperature` on the current Backend — high temperature correlates with prompt-obedience drift. Sweep [0.0, 0.2, 0.5] and compare slip rate.
  2. Extend `mnemosyne_identity._SLIP_PATTERNS` if new phrasings are appearing (check `Sample events` below for patterns not yet caught).
  3. If a specific model is the main offender per `affected_models`, switch to a different model with better prompt adherence for routing-heavy turns (Qwen 3.5, Gemma 4 tend to be strong).
  
  ## How to verify
  
  Run the 6 identity scenarios in `scenarios.example.jsonl` against the candidate configuration via `harness_sweep`. Target: `identity_slip_rate_per_1000 == 0` on the follow-up triage scan.

────────────────────────────────────────────────────────────────
 11/18  Dream consolidation — offline pattern extraction from L3 cold
────────────────────────────────────────────────────────────────

── Seed 12 related L3 memories (user-preference pattern)
  seeded 12 L3 memories
  L3 count: 13

── mnemosyne-dreams  (stdlib summarizer, no LLM calls)
  dream: dream-20260415T040649Z
    scanned:   13 memories
    clusters:  2
    abstracts: 2 (stdlib)
  
    [6x] weather report rainy
      Pattern across 6 memories: weather rain today tomorrow forecast
    [6x] dark palette requested
      Pattern across 6 memories: user uses dark mode in vscode editor

── Dream report JSON:
  dream-20260415T040649Z.json

────────────────────────────────────────────────────────────────
 12/18  Inner dialogue — Planner → Critic → Doer on tagged turns
────────────────────────────────────────────────────────────────
  ── untagged turn (single-pass path)
    answer: single-pass answer
    model calls: 1

  ── tagged turn (inner-dialogue path)
    answer: Plan: (1) take an off-host backup, (2) apply the migration inside a transaction, (3) validate row counts. If any step fails, roll back.
    model calls: 3  (planner + critic + doer)

────────────────────────────────────────────────────────────────
 13/18  Goal stack — persistent TODOs across sessions
────────────────────────────────────────────────────────────────

── Seed two goals via the CLI
  added: #1  P1  ship v0.2.0 release notes
  added: #2  P3  review Peter Pang article for loop ideas

── List open goals (priority-sorted)
    [ ] #1    P1  ship v0.2.0 release notes [release, docs]
    [ ] #2    P3  review Peter Pang article for loop ideas [reading]

── Brain with goals_inject=True surfaces them in the first-turn system prompt
  ## Open goals (across sessions)
  - (P1) #1: ship v0.2.0 release notes [release, docs]
  - (P3) #2: review Peter Pang article for loop ideas [reading]

────────────────────────────────────────────────────────────────
 14/18  Apply-agent — closes the Meta-Harness loop
────────────────────────────────────────────────────────────────

── Mark one identity proposal as accepted, then run mnemosyne-apply
  marked accepted: PROP-0002-identity-slips-recurring-12-events-sever.md
    [applied         ] PROP-0002    identity   identity scenarios re-scanned. slips_caught=0

── Proposal status after apply:
  ---
  id: PROP-0002
  created_utc: 2026-04-15T04:06:48.921831Z
  status: applied
  severity: 52.5
  cluster_id: 75efbe3366fa
  category: identity
  applied_utc: 2026-04-15T04:06:49.379188Z
  apply_notes: identity scenarios re-scanned. slips_caught=0
  ---

────────────────────────────────────────────────────────────────
 15/18  MCP bridge — Mnemosyne skills exposed as Model Context Protocol tools
────────────────────────────────────────────────────────────────

── mnemosyne-mcp serve reads JSON-RPC from stdin; we drive it inline
  id=1  result: {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "mnemosyne", "version": "0.2.0"}...
  id=2  result: {"tools": [{"name": "echo", "description": "return the input unchanged", "inputSchema": {"type": "object", "properties":...
  id=3  result: {"content": [{"type": "text", "text": "{\"echoed\": \"hi from MCP\"}"}]}

────────────────────────────────────────────────────────────────
 16/18  Live dashboard (single frame via --once --plain)
────────────────────────────────────────────────────────────────
Mnemosyne dashboard   2026-04-15T04:06:49+00:00
$PROJECTS_DIR: /tmp/mnemo-demo-IzPRYK/projects
────────────────────────────────────────────────────────────────
Ollama: not reachable at http://localhost:11434
Experiments: 9 runs, 312K on disk
────────────────────────────────────────────────────────────────
Last 5 runs:
  run_20260415-040648-4394d6  [completed]  demo-model          2026-04-15 04:06:48  events=14     tags=proposer-demo
  run_20260415-040647-mode-gemma4-e-retr-15-temp-05  [completed]  gemma4:e4b          2026-04-15 04:06:47  events=42     tags=sweep,demo,example
  run_20260415-040646-mode-gemma4-e-retr-15-temp-00  [completed]  gemma4:e4b          2026-04-15 04:06:46  events=42     tags=sweep,demo,example
  run_20260415-040645-mode-gemma4-e-retr-5-temp-05  [completed]  gemma4:e4b          2026-04-15 04:06:45  events=42     tags=sweep,demo,example
  run_20260415-040644-mode-gemma4-e-retr-5-temp-00  [completed]  gemma4:e4b          2026-04-15 04:06:44  events=42     tags=sweep,demo,example
────────────────────────────────────────────────────────────────
Memory:
  {
    "total": 18,
    "by_tier": {
      "L1_hot": 2,
      "L2_warm": 3,
      "L3_cold": 13
    },
    "by_kind": {
      "dream_abstract": 2,
      "fact": 13,
      "preference": 2,
      "project": 1
    },
    "fts5_enabled": true,
    "db_path": "/tmp/mnemo-demo-IzPRYK/projects/memory.db",
    "schema_version": 1
  }
────────────────────────────────────────────────────────────────
Recent events (latest run):
  identity_slip_detected  -                              -  error
  identity_slip_detected  -                              -  error
  identity_slip_detected  -                              -  error
  identity_slip_detected  -                              -  error
  session_end     -                              -  ok
────────────────────────────────────────────────────────────────
Disk: /dev/vda        252G  7.3G   30G  20% /

────────────────────────────────────────────────────────────────
 17/18  Training bridge — telemetry → Hermes-compatible ShareGPT (LoRA ready)
────────────────────────────────────────────────────────────────

── Seed two successful training_turn events in a fresh run
  seeded 6 events in run_train_demo

── mnemosyne-train export   (Hermes-compatible ShareGPT)
{
  "trajectories_written": 2,
  "runs_scanned": 1,
  "runs_skipped": 10,
  "turns_total": 2,
  "fallback_to_memory_db": false,
  "out_path": "/tmp/mnemo-demo-IzPRYK/projects/training/20260415-040649.jsonl",
  "warnings": []
}

── First exported trajectory — Hermes schema:
  {
      "prompt_index": 0,
      "conversations": [
          {
              "from": "system",
              "value": "You are Mnemosyne."
          },
          {
              "from": "human",
              "value": "What is the capital of France?"
          },
          {
              "from": "gpt",
              "value": "Paris is the capital of France."
          }
      ],
      "metadata": {
          "mnemo_run_id": "run_train_demo",
          "mnemo_turn_number": 1,
          "mnemo_model": "qwen3.5:9b",
          "mnemo_provider": "ollama",
          "mnemo_path": "single",
          "mnemo_tags": []
      },
      "completed": true,
      "partial": false,
      "api_calls": 1,
      "toolsets_used": [],
      "tool_stats": {},
      "tool_error_counts": {}
  }

── Schema check: Hermes-compatible keys present:
  ✓ all Hermes keys present
  ✓ mnemo metadata: ['mnemo_run_id', 'mnemo_turn_number', 'mnemo_model', 'mnemo_provider', 'mnemo_path', 'mnemo_tags']

── mnemosyne-train deploy --to lmstudio --dry-run  (shows target path, no install)
  {
    "mode": "lmstudio",
    "dry_run": true,
    "would_copy_from": "/tmp/fake-adapter/model.gguf",
    "would_copy_to": "/root/.lmstudio/models/mnemosyne/mnemo-demo-v1/model.gguf"
  }

────────────────────────────────────────────────────────────────
 18/18  Test suite
────────────────────────────────────────────────────────────────

── bash test-harness.sh (integration)

[2m────────────────────────────────────────────────[0m
[1;32m  ✓ 29 checks passed, 0 failed[0m
[2m────────────────────────────────────────────────[0m

── python3 tests/test_all.py (unit)

[1;32m156/156 tests passed[0m in 1.34s

────────────────────────────────────────────────────────────────
 Demo complete.
────────────────────────────────────────────────────────────────

All 18 sections exercised. Identity lock holds across slip attempts.
Triage → proposer → apply closes the Meta-Harness loop end-to-end.
Dreams compress L3 cold memories; inner dialogue fires on hard turns.
Goal stack persists across sessions; MCP bridge exposes skills.
Training bridge emits Hermes-compatible ShareGPT JSONL.
Full pipeline produces real experiments in the fake PROJECTS_DIR and
the CLI tools read them back without sys.path shims. All tests pass.

Re-run this demo anytime with: bash demo.sh
Transcript regenerated with:   bash demo.sh > docs/DEMO.md 2>&1
