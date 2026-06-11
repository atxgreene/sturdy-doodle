# Quickstart

Ten lines from `pip install` to your first conversation with an evolving avatar.

## 1. Install

```sh
pip install mnemosyne-harness
```

That's it. Stdlib only — no torch, no transformers, no langchain.

## 2. Pick a backend

You need *something* that speaks the OpenAI chat-completions API. Easiest is
local Ollama:

```sh
# install ollama if you don't have it (https://ollama.com/install.sh)
ollama pull qwen3:8b   # ~5GB; one-time
```

Or LM Studio, or any cloud provider. Mnemosyne speaks 19 backends out of the
box — Ollama, LM Studio, OpenAI, Anthropic, OpenRouter, Together, Fireworks,
Groq, DeepSeek, Cerebras, Hyperbolic, Perplexity, Novita, Nous, Google, xAI,
Mistral, Cohere, vLLM, TGI.

## 3. Start the daemon

```sh
mnemosyne-serve --port 8484 &
```

This is a long-running process that owns your memory store, runs dream
consolidation on a schedule, and serves the dashboard. It binds to
`127.0.0.1` only — never exposed unless you explicitly `--host 0.0.0.0`.

## 4. Open the dashboard

```
http://127.0.0.1:8484/ui
```

You'll see the avatar (resting at first), a chat panel, live event stream,
goal list, memory tier bars, and a memory browser. Type a message in chat;
the avatar evolves as you talk.

## 5. Have your first conversation

In the chat box:

```
What is the capital of France?
```

The avatar's pulse picks up. The event stream shows `turn_start` →
`model_call` → `turn_end`. A memory lands in L2 warm. After a few turns,
the activity score rises and the mood shifts from `rest` to `explore`.

## What just happened

You ran a stdlib-only daemon that:

1. **Locked your agent's identity** — even if Qwen replies "I am Qwen,"
   the four-layer identity filter rewrites it to "I am Mnemosyne." Your
   agent has one identity across 19 backends.
2. **Logged everything** to `~/projects/mnemosyne/experiments/<run-id>/events.jsonl`.
   Every turn, every tool call, every memory operation. Replayable.
3. **Computed avatar state** deterministically from observable signals.
   No magic — every visual property maps to a number you can grep out of
   `~/projects/mnemosyne/avatar.json`.
4. **Stored the turn** in a SQLite + FTS5 memory store with the
   6-tier ICMS policy (L0 instinct / L1 hot / L2 warm / L3 cold /
   L4 pattern / L5 identity). Searchable from the dashboard's
   memory browser panel.

## Common next moves

```sh
# Inspect what just happened
mnemosyne-experiments list                    # see every run
mnemosyne-experiments show <run_id>           # the full event tree

# Watch the memory grow
mnemosyne-memory stats
mnemosyne-memory search "France"

# Set a goal — the agent surfaces it in its system prompt next session
mnemosyne-goals add "ship the v1 demo" --priority 1 --tags release
mnemosyne-goals list

# Audit the routing layer (which skills the agent can actually pick)
mnemosyne-resolver check
```

## Sharing your data with friends — but locally

Mnemosyne is local-first. Your `~/projects/mnemosyne/` is yours: a SQLite
database, plain-text JSONL events, markdown skill files, a JSON goal stack.
No telemetry, no callbacks, no auto-updates. If Mnemosyne disappeared
tomorrow your knowledge survives as plain files in a directory you control.

## Where to go next

| Want to… | Read |
|---|---|
| understand how the framework is organized | [`docs/ARCHITECTURE.md`](./ARCHITECTURE.md) |
| see the avatar's full visual contract | [`docs/UI.md`](./UI.md) |
| run benchmarks against your backend | [`docs/BENCHMARKS.md`](./BENCHMARKS.md) |
| pick a local model and tune retrieval | [`docs/LOCAL_MODELS.md`](./LOCAL_MODELS.md) |
| fine-tune a LoRA on your own captured turns | [`docs/TRAINING.md`](./TRAINING.md) |
| see what's shipped vs. research vs. aspirational | [`docs/ROADMAP.md`](./ROADMAP.md) |
| watch a 45-second screen-recording of all of this | `docs/demo.gif` |
