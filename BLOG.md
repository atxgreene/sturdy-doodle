# The Harness Is the Product: Building a Meta-Harness-Ready Local Agent

*Draft — review, re-voice, and publish at your discretion. X thread version at the bottom.*

---

Three papers landed in the same week and said the same thing: **the harness matters more than the model.**

- Stanford's [Meta-Harness paper](https://arxiv.org/abs/2603.28052): changing the harness around a fixed LLM produces a **6x performance gap** on the same benchmark.
- The [Luce megakernel](https://github.com/Luce-Org/luce-megakernel): fusing DeltaNet+Attention layers into one CUDA kernel makes a 2020 RTX 3090 **1.8x faster than an M5 Max** — same weights, different software.
- [Mamba-3](https://arxiv.org/abs/2603.15569) at ICLR 2026: state-space models beat transformers by 4% while running **7x faster at long sequences**.

I've been building a local agent stack called Mnemosyne. When these papers hit, I realized I was already doing harness engineering — I just wasn't measuring it. So in one overnight session I built the observability substrate that a Meta-Harness-style optimizer would need to operate on my stack. Here's what shipped and what I learned.

## The headline numbers

Stanford's Meta-Harness paper (Lee, Nair, Zhang, Lee, Khattab, Finn — Stanford + MIT + KRAFTON) includes this claim:

> *Changing the harness around a fixed LLM can produce a 6x performance gap on the same benchmark.*

And this result: Meta-Harness achieves **76.4% on TerminalBench-2** with Opus 4.6, ranking #2 on the leaderboard. On Haiku 4.5, it scores **37.6%** — #1 among all Haiku agents, outperforming even hand-tuned baselines on the stronger model.

The paper's description of the #1 discovery the optimizer made:

> *"The main modification discovered by Meta-Harness is environment bootstrapping: before the agent loop begins, the harness runs a compound shell command to gather a snapshot of the sandbox environment and injects it into the initial prompt."*

I had already built that. `environment-snapshot.py` — shipped the night before I read the paper's full results — does exactly this: pre-computes the projects directory, configured services, available models, GPU capabilities, and installed skills, then produces a markdown preamble the agent can consume on turn one instead of spending 2–4 turns discovering its environment via tool calls.

I'm not claiming independent discovery of a grand idea. The Terminal-Bench 2 pattern was described in AVB's review and I built it from there. But the fact that a Stanford optimizer converged on the same modification as its highest-value find validates the design: **environment bootstrapping is not a nice-to-have, it's the #1 lever the optimizer reaches for.**

## The architecture that clicked

```
┌──────────────────────────────────────────────┐
│  Layer 1: Deployment + Observability          │
│  (this repo)                                  │
│  bootstrap / wizard / telemetry / sweep /     │
│  scenarios / experiments CLI / env snapshot   │
├──────────────────────────────────────────────┤
│  Layer 2: Meta-harness                        │
│  TurboQuant / metacognition / dream           │
│  consolidation / behavioral coupling          │
├──────────────────────────────────────────────┤
│  Layer 3: Base harness                        │
│  ICMS memory / SDI / 11 tools / channels      │
├──────────────────────────────────────────────┤
│  Layer 4: Engine (Ollama + local model)        │
│  qwen3.5:9b / gemma4:e4b / qwen3:8b          │
└──────────────────────────────────────────────┘
```

Layer 4 is replaceable with one environment variable. The model is the cheapest part. Everything else is harness.

## Why Qwen 3.5 changes the model recommendation

The engine layer just got an upgrade. Qwen 3.5 (available now on Ollama as `qwen3.5:9b`) uses **Gated DeltaNet + sparse MoE**:

- DeltaNet layers scale **linearly** with context length (vs quadratic for standard attention)
- Only ~3B parameters activated per token (sparse routing), so it's actually *faster* per-token than a dense 8B model
- Same Qwen family as the prior default, so tool-calling behavior is familiar

For an ICMS system with three memory tiers, linear context scaling is the architectural property that matters most. It means the agent can look at more retrieved context per turn without hitting a latency wall. The Luce megakernel demonstrated that fusing DeltaNet layers into a single CUDA kernel extracts 1.55x more throughput — and that's just the beginning of DeltaNet kernel optimization.

This doesn't mean Gemma 4 is wrong (its 128K context window is still useful for brute-force long-context via standard attention). It means there's now a principled architectural argument for the model choice, not just benchmark numbers.

## What shipped overnight

**19 files, ~7400 lines, 78 passing test assertions, zero cloud dependencies.**

The core of what I built is the observability substrate: four Python modules that together implement the infrastructure the Meta-Harness paper says you need before an optimizer can operate.

**`harness_telemetry.py`** — the observation layer. A `TelemetrySession` class writes every tool call to append-only JSONL: full args, full results, full tracebacks on failure, duration, status. Secrets are redacted by key name at write time. **No summarization.** The paper's central argument is that compressing traces to scalars loses the causal information the optimizer needs. This module respects that by never aggregating.

**`mnemosyne-experiments.py`** — the navigation layer. Seven subcommands (list, show, top-k, pareto, diff, events, aggregate) that let a human or agentic proposer inspect the history. The Pareto frontier computes multi-axis dominance; `--plot` renders an ASCII scatter. `aggregate` gives per-tool latency distributions (p50/p95/p99).

**`harness_sweep.py`** — the evaluation layer. Deterministic grid search over a parameter space, one `TelemetrySession` per combination, evaluator callable returns metrics, failed evaluators don't kill the sweep.

**`scenario_runner.py`** — the scoring layer. JSONL-driven evaluation with pluggable judges (`expected_contains`, `expected_tool_calls`, `expected_regex`). Catches harness exceptions per-scenario. Returns metrics compatible with `finalize_run`.

**`environment-snapshot.py`** — the bootstrapping layer. Pre-computes everything the agent would otherwise discover: projects dir, `.env` keys (never values), Ollama models, GPU capabilities (model, VRAM, CUDA, compute capability), model architecture classification (DeltaNet hybrid vs standard attention), venv, skills, vault, disk. This is the Terminal-Bench 2 pattern — and it's the modification that the paper's optimizer independently converged on as its highest-value discovery.

Together these form steps 1–5 of the Meta-Harness optimization loop:

```
OBSERVE  →  EVALUATE  →  SWEEP  →  COMPARE  →  INSPECT  →  (PROPOSE)
   ↑                                                            ↓
   └────────────────────────────────────────────────────────────┘
```

Step 6 (PROPOSE) — the agentic proposer that reads traces and rewrites harness code — is the missing piece. That's its own project, needing a compute budget, an eval suite, and a code-writing agent operating in a loop. What's here is the substrate it would run against.

## The testing discipline

78 assertions across two test suites:

- **`test-harness.sh`** (29 integration): creates fake runs, logs events, verifies secret redaction at the filesystem level, exercises every CLI subcommand including Pareto multi-axis dominance and ASCII plot rendering, runs environment-snapshot and verifies no planted secret ever escapes.
- **`tests/test_all.py`** (49 unit): stdlib-only, covers redaction (nested dicts, false-positive avoidance), run lifecycle, trace decorator (ok + error + re-raise), sweep (cartesian product, failure handling, stop-on-error), all three scenario judges, Pareto dominance logic, percentile edge cases, ASCII scatter rendering.

The tests caught bugs I'd have shipped otherwise: a broken `--json` flag that only worked in one position, a re-run path that nuked working tokens on a network flake, and `set -e` tail-fall-through errors in the wizard.

## Why local-first, zero-cloud matters

The existing LLM observability landscape is mature (Langfuse, OpenLLMetry, Arize Phoenix, etc.). Those are monitoring platforms designed for cloud-API SaaS applications. They optimize for dashboards, cost tracking, and team collaboration via web UIs.

Mnemosyne's observability is designed for something different: **an optimization substrate readable by `grep`.** The Meta-Harness paper's proposer agent navigates the filesystem with standard terminal tools. Cloud dashboards can't be `grep`'d. OTEL spans can't be `cat`'d. JSONL files on a local filesystem can.

This isn't anti-cloud dogma. It's alignment with the paper's architecture: the optimizer needs raw, navigable history. Monitoring platforms compress it. We don't.

## What I learned

**Stop summarizing.** Before reading the paper, I would have logged "tool: obsidian_search, ok, 42ms" and called it observability. After: raw args, raw results, raw tracebacks, raw everything. The gap between those two is the gap between "I know it ran" and "I can see why it failed."

**Environment bootstrapping is the highest-leverage single change you can make to an agent harness.** Stanford's optimizer found it. The megakernel paper demonstrated the same principle at the kernel level (pre-loading weights instead of re-fetching). It's the same idea at every layer of the stack: don't let the system discover what you already know.

**The model architecture matters more than the model benchmark.** Qwen 3.5's DeltaNet hybrid scales linearly with context. Gemma 4's standard attention has a higher context ceiling (128K) but hits it quadratically. For an ICMS system that needs deep retrieval every turn, the scaling property matters more than the MMLU score.

## What's next

1. Wire `harness_telemetry` into `eternal-context` (needs one existing skill file as reference — four concrete wiring patterns documented in `docs/WIRING.md`)
2. A/B `qwen3.5:9b` vs `qwen3:8b` vs `gemma4:e4b` on real workloads using the sweep infrastructure
3. Build a real eval suite from conversation logs (~50 scenarios)
4. Eventually: close the loop with an agentic proposer that reads the experiments directory and writes improved harness code

Everything is on [`atxgreene/sturdy-doodle`](https://github.com/atxgreene/sturdy-doodle) on the `claude/setup-mnemosyne-consciousness-NZqQE` branch. `bash test-harness.sh && python3 tests/test_all.py` proves it works in three seconds.

---

## X thread version

> 1/ Three papers in one week said the same thing: the harness matters more than the model. Stanford's Meta-Harness: 6x performance gap, same model, different harness. The Luce megakernel: 1.55x from fusing DeltaNet layers. Mamba-3 at ICLR: 7x faster than transformers at long sequences.

> 2/ So I built the observability substrate a Meta-Harness optimizer would need. 19 files, ~7400 lines, 78 passing tests, zero cloud dependencies. Telemetry library + experiments CLI + parameter sweep + scenario runner + environment snapshot.

> 3/ The paper's #1 discovery was "environment bootstrapping" — pre-compute the agent's context instead of letting it explore for 2-4 turns. I had already built this as environment-snapshot.py before reading the full paper results. Stanford's optimizer converged on the same pattern independently.

> 4/ The 6x gap number is real (Lee et al., Stanford/MIT/KRAFTON, 2026). On TerminalBench-2, Meta-Harness scores 76.4% with Opus 4.6 and 37.6% with Haiku 4.5 — #1 among all Haiku agents. The harness code matters more than which model you're running.

> 5/ Meanwhile Qwen 3.5 landed on Ollama with DeltaNet hybrid attention — scales LINEARLY with context length instead of quadratically. For an ICMS memory system that needs deep retrieval, this is the architecture property that matters. `ollama pull qwen3.5:9b`

> 6/ The Luce megakernel fuses all 24 DeltaNet+Attention layers of Qwen 3.5-0.8B into a single CUDA kernel launch. Result: 1.55x over llama.cpp, a 2020 RTX 3090 beating an M5 Max on both speed AND efficiency. The inference runtime IS part of the harness.

> 7/ Our observability is deliberately NOT Langfuse/Phoenix/OpenLLMetry. Those are monitoring platforms. We built an optimization substrate — raw JSONL traces navigable by `grep`, because the Meta-Harness paper's proposer agent reads the filesystem directly. You can't `grep` a dashboard.

> 8/ 78 test assertions caught bugs I'd have shipped otherwise: a --json flag that only worked before the subcommand, a re-run path that nuked working tokens on network flakes, set -e tail-fall-through in the wizard. Testing is the harness for the harness.

> 9/ The optimization loop has 6 steps: observe, evaluate, sweep, compare, inspect, propose. We shipped steps 1-5. Step 6 — the agentic proposer — is its own project. But the substrate is ready.

> 10/ Full architecture doc, wiring guide, model comparison matrix (Qwen 3.5 DeltaNet vs Gemma 4 vs Mamba-3), and a runnable end-to-end demo. Branch: atxgreene/sturdy-doodle. Paper: arxiv.org/abs/2603.28052. Research credits: @neural_avb's review was the catalyst.

---

*Sources: [Meta-Harness paper](https://arxiv.org/abs/2603.28052) · [Meta-Harness project page](https://yoonholee.com/meta-harness/) · [Luce megakernel](https://github.com/Luce-Org/luce-megakernel) · [Mamba-3 (ICLR 2026)](https://arxiv.org/abs/2603.15569) · [DeltaNet (NeurIPS 2024)](https://arxiv.org/abs/2406.06484) · [Beyond Standard LLMs (Raschka)](https://magazine.sebastianraschka.com/p/beyond-standard-llms) · [AVB's Meta-Harness review](https://x.com/neural_avb/article/2039709486538260583) · [Stanford Meta-Harness artifact](https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact)*
