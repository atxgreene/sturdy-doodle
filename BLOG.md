# Building a Meta-Harness-ready local agent: Mnemosyne's observability layer

*Draft post, ~1600 words. Not yet published. Feel free to trim, re-voice, or rewrite — this is a starting point, not a final artifact.*

---

> "Everything in your AI system that is not the LLM itself is a harness."
> — AVB, reviewing the Stanford Meta-Harness paper

Last week I wired up a local agent stack on my WSL2 box. It's called Mnemosyne. It runs on Ollama, wraps `qwen3:8b` with a three-tier memory layer, tool registry, and channel adapters (Telegram, Slack, Discord, REST), and has a "consciousness layer" on top that does dream consolidation and behavioral coupling between sessions. All of this lives in two upstream repos that I clone from a small bootstrap script.

Then the Stanford [Meta-Harness paper](https://x.com/neural_avb/article/2039709486538260583) landed and reframed what I was building. Not a new concept for me — I already thought of agents as "LLM + scaffold" — but AVB's review made something crisp: **95% of the engineering is the harness, not the model**, and the existing tools for optimizing harnesses all fail in the same specific way — they compress feedback into a single score and lose the causal information an optimizer actually needs.

This post is about what I did in response. In one overnight session, I built the observability substrate that a Meta-Harness-style optimizer would need to run against Mnemosyne. Four small components, 1600+ lines of stdlib-only Python and bash, 23 passing integration tests, full secret-safety verification. Everything is in [`atxgreene/sturdy-doodle`](https://github.com/atxgreene/sturdy-doodle) on the `claude/setup-mnemosyne-consciousness-NZqQE` branch.

## The layering that finally clicked

Before I read the paper, I thought of Mnemosyne as a two-layer thing: the base agent and the consciousness extensions. After reading AVB's review, I realized it's actually a **four-layer stack**, and that the separation matters more than I'd given it credit for:

```
┌─────────────────────────────────────────────────────────┐
│  sturdy-doodle (this repo)                              │
│  Harness deployment infrastructure                      │
│  install-mnemosyne.sh, mnemosyne-wizard.sh,             │
│  validate-mnemosyne.sh, obsidian-search.py,             │
│  notion-search.py, and the new observability layer      │
└─────────────────────────────────────────────────────────┘
                         │ clones + configures
                         ▼
┌─────────────────────────────────────────────────────────┐
│  mnemosyne-consciousness (fantastic-disco)              │
│  META-HARNESS: TurboQuant, metacognition, dream         │
│  consolidation, autobiography, behavioral coupling.     │
│  Observes and reshapes the base harness between turns.  │
└─────────────────────────────────────────────────────────┘
                         │ wraps + instruments
                         ▼
┌─────────────────────────────────────────────────────────┐
│  eternal-context                                        │
│  BASE HARNESS: ICMS 3-tier memory, SDI selection,       │
│  11 tools, channel adapters (Telegram/Slack/Discord/    │
│  REST), prompt assembly.                                │
└─────────────────────────────────────────────────────────┘
                         │ issues tool calls
                         ▼
┌─────────────────────────────────────────────────────────┐
│  Ollama + qwen3:8b  (or gemma4:e4b)                     │
│  The engine. Stateless, replaceable.                    │
└─────────────────────────────────────────────────────────┘
```

The LLM is the engine. The base harness does retrieval, memory, and tool dispatch. The meta-harness operates on the base harness between turns. And the deployment layer — `sturdy-doodle` — is what stands the whole thing up on a fresh box.

None of those layers was instrumented before this session. If I wanted to compare two harness variants, I had no data. I couldn't tell you which of the 11 tools got called, how long each call took, whether the agent's retrieval was hitting relevant chunks, or how my custom ICMS policy compared to the defaults. Zero visibility.

## The paper's core argument, in one paragraph

AVB's review is worth reading in full, but the load-bearing claim for practitioners is this: **existing harness optimizers compress feedback too much**. DSPy-style tools reduce each candidate to a single scalar and try to improve from there. Meta-Harness argues you need **execution-level traces** — the exact inputs and outputs of every tool call, every failed retrieval, every prompt — because the scalar discards the causal information the optimizer needs. The proposer (in the paper, Claude Code with filesystem tools) navigates this history with `grep` and `cat`, finds patterns, and proposes new harness code. One of the paper's authors explicitly says "this directory gets very big" — they're accepting the storage cost because the alternative is the failure mode.

That reframing is the thing I didn't fully have before. Logging is usually an afterthought. Meta-Harness treats it as the load-bearing input to the whole optimization loop. If you ever want an optimizer — human or agentic — to improve your harness, you have to stop summarizing.

## What I built

Four components, all in `sturdy-doodle`, all stdlib-only Python and bash:

### 1. `harness_telemetry.py` — the observability library

A run-scoped `TelemetrySession` class that writes events to an append-only `events.jsonl` file. Each event is a full JSON object: event id, run id, UTC timestamp, event type, tool name, raw args, raw result, duration, status, error, parent event id. No summarization. Secret redaction happens by key name before the event hits disk — values under keys matching `token`, `secret`, `api_key`, `password`, `bearer`, `credential`, or `signing_key` are replaced with `<redacted>` recursively.

A `@sess.trace` decorator wraps any callable and instruments it for the lifetime of a session. The wrapped function's args, result, and duration get logged automatically; exceptions are captured with full tracebacks and re-raised. The experiments directory follows a simple, grep-friendly layout:

```
$PROJECTS_DIR/experiments/
  latest -> run_<id>/                 symlink to most recent
  run_<YYYYMMDD-HHMMSS>-<slug>/
    metadata.json                     run_id, model, status, tags, notes, git sha
    results.json                      final metrics
    events.jsonl                      append-only, one JSON per line
    harness/                          optional: frozen snapshot of harness code
    notes.md                          optional
```

Every file is plain text. You can `grep` it, `cat` it, commit it to git, and — crucially — an agentic proposer can navigate it the exact way the Meta-Harness paper describes.

### 2. `mnemosyne-experiments.py` — the CLI over the history

The paper's practical-tip #4 is "build a small CLI over the logs" with four specific operations: list the Pareto frontier, show top-k candidates, diff pairs of runs, and an index. I added `list`, `show`, and `events` on top, for a total of six subcommands:

```bash
mnemosyne-experiments.py list --tag baseline
mnemosyne-experiments.py show run_20260409-053012-baseline
mnemosyne-experiments.py top-k 5 --metric accuracy --direction max
mnemosyne-experiments.py pareto --axes accuracy,latency_ms_avg --directions max,min
mnemosyne-experiments.py diff run_A run_B
mnemosyne-experiments.py events run_A --tool obsidian_search
```

Every subcommand has a `--json` mode. The Pareto implementation is the obvious one: a run R1 dominates R2 if R1 is at least as good on every axis and strictly better on at least one; the frontier is the set of runs not dominated by any other.

One design choice worth mentioning: **for a local-first agent like Mnemosyne, token cost is effectively zero** (local Ollama, your own hardware). So where the paper uses accuracy × token cost as the Pareto frontier, I substitute **accuracy × latency**. Latency is the real constraint when the inference cost is your own compute cycles.

### 3. `environment-snapshot.py` — the Terminal-Bench 2 pattern

The paper's most striking concrete result came from Terminal-Bench 2, a benchmark of 89 long-horizon tasks. Meta-Harness iterated on two baselines, failed for a while, then discovered: instead of letting the agent spend 2–4 turns exploring its environment via tool calls (`pwd`, `ls /app`, `which python`, `df`), **pre-compute all of that at session start and inject it into the first LLM call**. Eliminates the exploration phase entirely. It feels like cheating. The optimizer found it precisely because it could see the execution traces of the failed exploration attempts.

`environment-snapshot.py` implements that pattern for Mnemosyne. It snapshots the projects directory layout, the keys configured in `.env` (**names only — never values**), Ollama reachability and available models, venv health, available skill helpers, Obsidian vault status, disk free, and platform info. A skill wrapper can inject the markdown output as a system prompt preamble, so the agent starts every session knowing its environment instead of discovering it turn by turn.

### 4. `test-harness.sh` — the integration test

23 assertions across all four components. Creates three fake runs in an ephemeral `/tmp` directory with deliberately diverse metrics (one baseline, one faster-but-less-accurate, one strictly dominated), exercises every CLI subcommand, verifies secret redaction at the filesystem level with a deliberately-planted "needle" token, runs the environment snapshot twice (markdown and JSON), and asserts that no planted secret ever escapes into any output. Runs in about two seconds. No network. Exits non-zero on any failure.

The test turned out to be the most valuable part of the session. Writing it surfaced two bugs — a broken `--json` flag that only worked before the subcommand name (shared parent-parser trick fixed that) and a validation path that could nuke a working token on a network flake — that I would have shipped without it. AVB's tip #6 is "automate eval outside the proposer" for exactly this reason.

## What this DOESN'T do (and why)

I did not ship a Meta-Harness proper. The agentic proposer that rewrites harness code in a loop is out of scope for this repo — that's its own project, needs an eval suite, needs a budget for thousands of runs, and needs Claude Code (or equivalent) with filesystem tools executing autonomously for hours. What this ships is the **substrate the optimizer runs against**. Get the observability right first; the optimizer is the follow-on.

I also did not flip Mnemosyne's default model from `qwen3:8b` to `gemma4:e4b` despite Gemma 4's 128K context window being a 4× improvement. That's a decision that needs a live A/B on my actual workload, not a speculative swap based on blog benchmarks. Gemma 4 is advertised as an alternative in `SETUP.md`; you opt in with `MODEL=gemma4:e4b bash install-mnemosyne.sh`.

## What I'd do next

The obvious follow-on is **wiring**. Right now the telemetry library lives in `sturdy-doodle`, but the agent it instruments lives in `eternal-context`. The wiring is a ~20-line shim that imports `harness_telemetry` and decorates the eternal-context tool dispatch function with `@sess.trace`. I can't write that until I see the actual shape of an existing skill file, which is pending.

After that, the shortest path to a real optimization loop is: pick 10 realistic scenarios, run the baseline harness, capture metrics, make one change (swap a retrieval strategy, widen ICMS L1, change the prompt), re-run, diff. If the `mnemosyne-experiments diff` output shows a clear win on the Pareto frontier, commit. If not, revert. That loop is entirely human-driven, but every component the paper describes for *automating* that loop is now in place.

## Why the overnight push was worth it

This session was the first time the harness abstraction felt load-bearing to me instead of academic. Before: "I have an agent and I'm tweaking it." After: "I have a harness, I can measure candidates, I can identify the Pareto frontier, I have the substrate a future optimizer would run against, and the whole thing is 900 lines of stdlib-only Python." Small code, big conceptual unlock.

Everything is on `atxgreene/sturdy-doodle@claude/setup-mnemosyne-consciousness-NZqQE`. `bash test-harness.sh` takes two seconds and proves it works.

---

*Source: [atxgreene/sturdy-doodle](https://github.com/atxgreene/sturdy-doodle). Paper reviewed: AVB's walkthrough of the Stanford Meta-Harness paper (April 2026), linked above. None of this would have happened without that review landing at the right moment.*
