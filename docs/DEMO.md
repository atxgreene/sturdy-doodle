# Mnemosyne — end-to-end demo transcript

*Regenerate with `bash demo.sh > docs/DEMO.md 2>&1` (then wrap in the markdown preamble below). This is the actual captured output of `demo.sh` — no hand-editing, no faking. Re-runnable in any fresh clone with zero API keys and zero network access.*

**What the demo exercises:**

1. `pip install -e .` into a clean venv — verifies all 8 console entry points land on `$PATH` and every library surface imports with no `sys.path` hacks
2. `mnemosyne-models list` and `current` — enumerates all 19 supported providers with their env-var and auth status, picks the right backend via `from_env()`
3. `environment-snapshot` — first-turn preamble the brain injects (Meta-Harness Terminal-Bench-2 pattern; `.env` *values* never leak, only key names)
4. Memory layer — writes across L1/L2/L3 tiers, FTS5 search, `--tier-max` filtering, stats breakdown
5. **Identity lock (the critical section)** — regex filter rewrites 5 slip patterns without mangling 3 legitimate third-party references, then a full brain turn with a mock LLM that deliberately says "I am Claude" gets rewritten to "I am Mnemosyne"
6. Skills — `@register_python` decorator, `$PATH` command discovery, `record_learned_skill()` self-improvement writing a new markdown skill file
7. Full pipeline — 8-point parameter sweep × 10 scenarios through a fake harness, runs finalized with metrics, Pareto frontier computed
8. Aggregate statistics — per-tool call counts, success rate, latency p50/p95/p99
9. Live dashboard — single frame via `--once --plain` showing Ollama/experiments/memory/events/disk
10. Full test suite — 29 integration + 94 unit = **123/123 green**

**What you should look for:**

- Section 1: `Successfully installed mnemosyne-harness-0.1.0` + all 8 entry points listed
- Section 5: `identity lock: HELD ✓` after the mock LLM's "I am Claude" prompt
- Section 7: a real ASCII Pareto plot with `.` markers (dominated runs)
- Section 10: `29/29` integration + `94/94` unit

---

```
Mnemosyne end-to-end demo
Generated:   2026-04-15T00:37:02+00:00
Commit:      8810554
Branch:      claude/setup-mnemosyne-consciousness-NZqQE
Python:      Python 3.11.15

────────────────────────────────────────────────────────────────
 1/10  pip install -e . into a fresh venv
────────────────────────────────────────────────────────────────
Successfully built mnemosyne-harness
Installing collected packages: mnemosyne-harness
Successfully installed mnemosyne-harness-0.1.0

── Installed console entry points on $PATH:
  environment-snapshot
  harness-telemetry
  mnemosyne-experiments
  mnemosyne-memory
  mnemosyne-models
  mnemosyne-pipeline
  notion-search
  obsidian-search

── Library imports (no sys.path hacks):
  ✓ all 7 library surfaces import cleanly

────────────────────────────────────────────────────────────────
 2/10  Model providers — 19 backends detected
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
 3/10  Environment snapshot  (first-turn preamble, Meta-Harness Terminal-Bench 2 pattern)
────────────────────────────────────────────────────────────────

── environment-snapshot  (human-readable markdown)
# Mnemosyne environment snapshot

**Projects dir:** /tmp/mnemo-demo-i0G0XI/projects (0 entries)

**.env:** not found (run mnemosyne-wizard.sh)

**Ollama:** NOT reachable at http://localhost:11434 (URLError)

**GPU:** none detected (CPU inference)

**venv:** NOT FOUND at /tmp/mnemo-demo-i0G0XI/projects/.venv

**Skills available:** notion-search, obsidian-search

**Obsidian vault:** not configured (.env missing)

**Disk:** 32.1 GB free of 270.6 GB (11.8% free)

**Platform:** Linux 6.18.5, Python 3.11.15

────────────────────────────────────────────────────────────────
 4/10  Memory layer — SQLite+FTS5 with ICMS 3-tier
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
  "db_path": "/tmp/mnemo-demo-i0G0XI/projects/memory.db",
  "schema_version": 1
}

────────────────────────────────────────────────────────────────
 5/10  Identity lock — regardless of underlying model, agent says Mnemosyne
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
 6/10  Skills — agentskills.io-compatible registry + self-improvement
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

  Learned skill written to: projects/skills/learned/search-and-summarize-20260415-003710.md
  Parsed back:  name=search-and-summarize  learned=True

────────────────────────────────────────────────────────────────
 7/10  Full pipeline — OBSERVE → EVALUATE → SWEEP → COMPARE → INSPECT
────────────────────────────────────────────────────────────────

── Running examples/sweep_demo.py (8-point sweep, fake harness, ~6 seconds)
sweep complete: 8 runs in 8.9s

Demo sweep finished: 8 runs created.

Inspect the results:

  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-i0G0XI/projects ./mnemosyne-experiments.py list
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-i0G0XI/projects ./mnemosyne-experiments.py top-k 3 --metric accuracy
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-i0G0XI/projects ./mnemosyne-experiments.py top-k 3 --metric latency_ms_avg --direction min
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-i0G0XI/projects ./mnemosyne-experiments.py pareto \
      --axes accuracy,latency_ms_avg --directions max,min --plot
  MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo-i0G0XI/projects ./mnemosyne-experiments.py aggregate run_20260415-003710-mode-qwen3-8b-retr-5-temp-00

── mnemosyne-experiments list  (newest first)
run_20260415-003718-mode-gemma4-e-retr-15-temp-05  [completed]  gemma4:e4b          2026-04-15 00:37:18  events=42     tags=sweep,demo,example
run_20260415-003718-mode-gemma4-e-retr-15-temp-00  [completed]  gemma4:e4b          2026-04-15 00:37:18  events=42     tags=sweep,demo,example
run_20260415-003717-mode-gemma4-e-retr-5-temp-05  [completed]  gemma4:e4b          2026-04-15 00:37:17  events=42     tags=sweep,demo,example
run_20260415-003716-mode-gemma4-e-retr-5-temp-00  [completed]  gemma4:e4b          2026-04-15 00:37:16  events=42     tags=sweep,demo,example
run_20260415-003714-mode-qwen3-8b-retr-15-temp-05  [completed]  qwen3:8b            2026-04-15 00:37:14  events=42     tags=sweep,demo,example
run_20260415-003713-mode-qwen3-8b-retr-15-temp-00  [completed]  qwen3:8b            2026-04-15 00:37:13  events=42     tags=sweep,demo,example
run_20260415-003712-mode-qwen3-8b-retr-5-temp-05  [completed]  qwen3:8b            2026-04-15 00:37:12  events=42     tags=sweep,demo,example
run_20260415-003710-mode-qwen3-8b-retr-5-temp-00  [completed]  qwen3:8b            2026-04-15 00:37:10  events=42     tags=sweep,demo,example

── Top 3 by accuracy:
Top 3 runs by accuracy (max):
  run_20260415-003718-mode-gemma4-e-retr-15-temp-00  accuracy=0.5  model=gemma4:e4b
  run_20260415-003716-mode-gemma4-e-retr-5-temp-00  accuracy=0.5  model=gemma4:e4b
  run_20260415-003713-mode-qwen3-8b-retr-15-temp-00  accuracy=0.5  model=qwen3:8b

── Pareto frontier on accuracy × latency  (ASCII plot):
Pareto frontier on (accuracy, latency_ms_avg) with directions (max, min):
  run_20260415-003718-mode-gemma4-e-retr-15-temp-00  accuracy=0.5  latency_ms_avg=54.19363418750223  model=gemma4:e4b

  latency_ms_avg
    85.35 |.                         .                         
    83.28 |                                                    
    81.20 |                                                    
    79.12 |                                                    
    77.05 |                                                   .
    74.97 |                                                   .
    72.89 |                                                    
    70.81 |                                                    
    68.74 |                                                    
    66.66 |                                                    
    64.58 |                                                    
    62.50 |                          .                         
    60.43 |                          .                         
    58.35 |                                                    
    56.27 |                                                    
    54.19 |                                                   #
          +----------------------------------------------------
          0.38                                            0.50
                                accuracy

  legend:  * = on Pareto frontier   . = dominated   # = overlap

────────────────────────────────────────────────────────────────
 8/10  Aggregate statistics — per-tool call counts, latency percentiles
────────────────────────────────────────────────────────────────

── aggregate for run_20260415-003718-mode-gemma4-e-retr-15-temp-05
# aggregate for run_20260415-003718-mode-gemma4-e-retr-15-temp-05

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
  duration_ms:  avg=20.8  p50=23.1  p95=30.8  p99=30.8  total=145.8

## per-tool
  tool                           calls      ok     err     rate    avg_ms    p95_ms
  notion_search                      3       3       0  100.0%      26.4      30.8
  obsidian_search                    4       4       0  100.0%      16.6      28.7

────────────────────────────────────────────────────────────────
 9/10  Live dashboard (single frame via --once --plain)
────────────────────────────────────────────────────────────────
Mnemosyne dashboard   2026-04-15T00:37:20+00:00
$PROJECTS_DIR: /tmp/mnemo-demo-i0G0XI/projects
────────────────────────────────────────────────────────────────
Ollama: not reachable at http://localhost:11434
Experiments: 8 runs, 292K on disk
────────────────────────────────────────────────────────────────
Last 5 runs:
  run_20260415-003718-mode-gemma4-e-retr-15-temp-05  [completed]  gemma4:e4b          2026-04-15 00:37:18  events=42     tags=sweep,demo,example
  run_20260415-003718-mode-gemma4-e-retr-15-temp-00  [completed]  gemma4:e4b          2026-04-15 00:37:18  events=42     tags=sweep,demo,example
  run_20260415-003717-mode-gemma4-e-retr-5-temp-05  [completed]  gemma4:e4b          2026-04-15 00:37:17  events=42     tags=sweep,demo,example
  run_20260415-003716-mode-gemma4-e-retr-5-temp-00  [completed]  gemma4:e4b          2026-04-15 00:37:16  events=42     tags=sweep,demo,example
  run_20260415-003714-mode-qwen3-8b-retr-15-temp-05  [completed]  qwen3:8b            2026-04-15 00:37:14  events=42     tags=sweep,demo,example
────────────────────────────────────────────────────────────────
Memory:
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
    "db_path": "/tmp/mnemo-demo-i0G0XI/projects/memory.db",
    "schema_version": 1
  }
────────────────────────────────────────────────────────────────
Recent events (latest run):
  scenario_end    -                        52.0ms  ok
  scenario_start  -                              -  ok
  scenario_end    -                        51.9ms  error
  scenario_summary  -                              -  ok
  session_end     -                              -  ok
────────────────────────────────────────────────────────────────
Disk: /dev/vda        252G  7.2G   30G  20% /

────────────────────────────────────────────────────────────────
 10/10  Test suite — 123/123 passing
────────────────────────────────────────────────────────────────

── bash test-harness.sh (integration)

────────────────────────────────────────────────
  ✓ 29 checks passed, 0 failed
────────────────────────────────────────────────

── python3 tests/test_all.py (unit)

94/94 tests passed in 1.17s

────────────────────────────────────────────────────────────────
 Demo complete.
────────────────────────────────────────────────────────────────

All 10 sections exercised. Identity lock holds across slip attempts.
Full pipeline produces real experiments in the fake PROJECTS_DIR and
the CLI tools read them back without sys.path shims. 123/123 tests pass.

Re-run this demo anytime with: bash demo.sh
Transcript regenerated with:   bash demo.sh > docs/DEMO.md 2>&1
```
