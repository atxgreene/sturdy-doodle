# Mnemosyne — Hermes Memory Provider Plugin

Local-first, privacy-preserving persistent memory for the Hermes Agent
(Nous Research). Backed by Mnemosyne's 6-tier SQLite ICMS (Integrated
Cognitive Memory System).

## What it does

- Persists every conversation turn to a local SQLite database (FTS5 search).
- Exposes three agent tools: `memory_search`, `memory_write`, `memory_stats`.
- Injects a `prefetch` context block before each turn (async, non-blocking).
- On session end / pre-compression: extracts facts, preferences, goals, and
  patterns from the transcript and promotes them to higher ICMS tiers.
- No cloud, no API key, no external service required.

## Setup

### 1. Install Mnemosyne

```bash
git clone https://github.com/atxgreene/Mnemosyne ~/Mnemosyne
export MNEMOSYNE_PATH=~/Mnemosyne
```

### 2. Drop into Hermes

```bash
cp -r experiments/hermes_plugin/mnemosyne \
      /path/to/hermes-agent/plugins/memory/mnemosyne
```

### 3. Configure Hermes

In your Hermes `config.yaml`:
```yaml
memory:
  provider: mnemosyne
```

Optional `$HERMES_HOME/mnemosyne.json`:
```json
{
  "db_path": "/home/user/.mnemosyne/hermes.db",
  "prefetch_limit": 8
}
```

### 4. Run

```bash
MNEMOSYNE_PATH=~/Mnemosyne hermes chat
```

## Agent tools

| Tool | Description |
|---|---|
| `memory_search(query, limit=8)` | FTS5 search across all tiers |
| `memory_write(content, kind, tier)` | Store a new memory |
| `memory_stats()` | Tier distribution summary |

`kind` options: `fact`, `preference`, `goal`, `pattern`, `identity`  
`tier` levels: 2 (event/turn) → 3 (fact) → 4 (pattern) → 5 (identity)

## Running the smoke test

```bash
MNEMOSYNE_PATH=~/Mnemosyne python3 experiments/hermes_plugin/mnemosyne/test_provider.py
```

All checks should pass in under 2 seconds with no network access.

## Retrieval baseline (2026-06-10)

Measured against the eval harness in `experiments/evals/`:

| metric | value |
|---|---|
| recall@5 overall | 0.87 |
| paraphrase recall@5 | 0.625 |
| LOCOMO score | 0.4849 |

The paraphrase gap is the primary optimization target: wiring
`mnemosyne_embeddings.py` into `MemoryStore.search()` with rank fusion
should lift paraphrase hit@1 from 0.50 to ≥0.75. Gate: run
`python3 experiments/evals/check_regression.py` before merging any
retrieval changes.

## Broader application roadmap

**mnemosyne-os ISO** — The mnemosyne-os live-build ISO (separate repo) will
ship this plugin as the default agent memory backend once the stub Tugboat
router is replaced with real imports. Estimated 2–4 week project.

**LLM training signal** — The ICMS event log + structured tiers are
high-quality synthetic training data for memory-augmented LLM fine-tuning
(Honcho Neuromancer direction). Viable at scale once BEN accumulates
sufficient real-use events. Requires a data pipeline from `events.jsonl`
to instruction-tuning pairs.

**AGI trajectory** — The ICMS architecture is well above open-source
alternatives. The current ceiling is the absence of closed-loop
self-improvement: the agentic proposer is research-grade, retrieval is
keyword-only, and routing is rule-based. The eval harness + regression
gate are the foundation for closing that gap metric by metric. Progress
will be measured, not assumed.
