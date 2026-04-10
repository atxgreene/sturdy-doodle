# Mnemosyne Architecture

*System design document for the Mnemosyne harness stack. Synthesizes the Stanford Meta-Harness paper (Lee et al., 2026), hybrid DeltaNet inference research, and the Luce megakernel into a coherent design rationale for each component in this repo.*

---

## Thesis

**The harness matters more than the model.**

Three independent pieces of evidence from the last 30 days converge on the same conclusion:

1. **Meta-Harness (Stanford/MIT/KRAFTON, March 2026):** Changing the harness around a fixed LLM produces a **6x performance gap** on the same benchmark. Automated harness optimization outperforms hand-tuned baselines on TerminalBench-2, and the #1 discovery was environment bootstrapping — pre-computing the agent's context instead of letting it explore. ([paper](https://arxiv.org/abs/2603.28052), [project page](https://yoonholee.com/meta-harness/))

2. **Luce megakernel (April 2026):** Fusing all 24 DeltaNet+Attention layers of Qwen 3.5-0.8B into a single CUDA kernel launch delivers **1.55x over llama.cpp** on the same GPU. A 2020 RTX 3090 beats an M5 Max on both throughput and efficiency — not from better hardware, but from better software. ([repo](https://github.com/Luce-Org/luce-megakernel))

3. **Mamba-3 (ICLR 2026):** State-space models now **beat transformers by 4% on language modeling while running 7x faster** at long sequences. The optimal hybrid ratio is 5:1 linear-to-attention layers. This is the architecture direction that Qwen 3.5, Kimi Linear, and Qwen3-Next are adopting. ([paper](https://arxiv.org/abs/2603.15569))

The model is the engine. The harness — retrieval, memory, tool dispatch, prompt construction, observation, inference runtime — is the car. You can put a V8 in a shopping cart or a four-cylinder in a well-designed chassis. The chassis wins.

---

## Four-layer stack

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1: Harness deployment + observability            │
│  (this repo: sturdy-doodle)                             │
│                                                         │
│  install-mnemosyne.sh     bootstrap, idempotent         │
│  mnemosyne-wizard.sh      credential config (TUI)       │
│  validate-mnemosyne.sh    health check                  │
│  obsidian-search.py       vault skill helper             │
│  notion-search.py         workspace skill helper         │
│  harness_telemetry.py     observability library          │
│  mnemosyne-experiments.py CLI over experiment history    │
│  environment-snapshot.py  first-turn context injection   │
│  harness_sweep.py         deterministic grid search      │
│  scenario_runner.py       JSONL-driven evaluation        │
└─────────────────────────────┬───────────────────────────┘
                              │ clones + configures + observes
                              ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 2: Meta-harness                                  │
│  (fantastic-disco / mnemosyne-consciousness)            │
│                                                         │
│  ConsciousnessLoop         wraps the base harness       │
│  TurboQuant                quantized meta-reasoning     │
│  Metacognition             self-monitoring between turns │
│  Dream consolidation       offline memory compression   │
│  Autobiography             persistent identity state    │
│  Behavioral coupling       cross-session consistency    │
└─────────────────────────────┬───────────────────────────┘
                              │ instruments + reshapes
                              ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 3: Base harness                                  │
│  (eternal-context)                                      │
│                                                         │
│  ICMS 3-tier memory        L1 hot / L2 warm / L3 cold  │
│  SDI selection             context window management    │
│  11 tools                  search, read, compute, etc.  │
│  Channel adapters          Telegram/Slack/Discord/REST  │
│  Prompt assembly           system + user + retrieval    │
└─────────────────────────────┬───────────────────────────┘
                              │ issues tool calls + prompts
                              ▼
┌─────────────────────────────────────────────────────────┐
│  LAYER 4: Engine                                        │
│  (Ollama + local model)                                 │
│                                                         │
│  qwen3:8b         standard attention, 32K context       │
│  qwen3.5:9b       DeltaNet hybrid + MoE, ~3B active    │
│  gemma4:e4b       standard attention, 128K context      │
│  (future)         Mamba-3 hybrid, 5:1 linear:attn      │
└─────────────────────────────────────────────────────────┘
```

### Why this layering?

Most agent stacks collapse layers 1–3 into a single repo. Mnemosyne separates them because:

- **Layer 1 can evolve without touching the agent.** Adding a new skill helper, changing the wizard's credential flow, or upgrading the observability layer doesn't require a commit to `eternal-context`. This is the same separation-of-concerns principle that makes Docker Compose separate from the application.

- **Layer 2 (meta-harness) operates ON Layer 3 (base harness), not inside it.** The consciousness extensions observe and reshape the base agent between turns — dream consolidation, metacognition, behavioral coupling. This is the key architectural decision that makes Mnemosyne more than a standard agent: the meta-harness is a feedback loop over the harness, which is exactly what the Meta-Harness paper describes automating.

- **Layer 4 (engine) is the cheapest part to swap.** Changing from `qwen3:8b` to `gemma4:e4b` is one environment variable. The harness stays the same. This is the 6x-gap principle: the model matters less than what surrounds it.

---

## Model architecture implications

### The DeltaNet inflection point

Standard multi-head attention scales quadratically with context length: O(n^2) in both compute and memory. For an ICMS system that needs to maintain long retrieval contexts (Mnemosyne's L1/L2/L3 tiers), this means the context window is a hard ceiling on how much the agent can "remember" per turn.

DeltaNet (and its gated variant) replaces attention with a learned recurrence that scales **linearly** with context: O(n). The state is a fixed-size matrix that gets updated token-by-token via the delta rule. Retrieval from this state is approximate but fast.

**Qwen 3.5 uses Gated DeltaNet + sparse MoE.** It's available now on Ollama:

```bash
ollama pull qwen3.5:9b
```

Only ~3B parameters are activated per token (sparse MoE routing), despite 9B total. This means:

- **Inference is faster per-token** than a dense 8B model (fewer active weights)
- **Context scales linearly** (DeltaNet layers dominate; 3:1 ratio is near-optimal per hybrid-architecture research)
- **ICMS retrieval can go deeper** without hitting the quadratic wall

### Model comparison matrix

| Model | Architecture | Context | Active params | Ollama tag | ICMS suitability |
|---|---|---|---|---|---|
| `qwen3:8b` | Standard MHA | 32K | 8B (dense) | `qwen3:8b` | Good baseline; 32K limits L1 |
| `qwen3.5:9b` | **DeltaNet + MoE** | Long (linear) | ~3B activated | `qwen3.5:9b` | **Best fit** — linear context + fast inference |
| `gemma4:e4b` | Standard MHA | 128K | ~4.5B eff | `gemma4:e4b` | Large context via brute-force; quadratic at scale |
| `gemma4:26b` | MoE | 256K | ~4B activated | `gemma4:26b` | Max context; needs ~16GB VRAM |
| (future) Mamba-3 | SSM hybrid | Very long | Varies | Not on Ollama yet | 7x faster at long seq vs transformers (ICLR 2026) |

**Recommendation:** Try `qwen3.5:9b` as the primary model. Same Qwen family as the current default (familiar tool-calling behavior), but with DeltaNet for linear context scaling and sparse MoE for inference speed. Use `gemma4:e4b` as a comparison point — its 128K context is competitive but scales quadratically. The sweep infrastructure (`harness_sweep.py`) can A/B them on your actual workload.

### Inference runtime as harness

The Luce megakernel demonstrates that **the inference runtime is part of the harness**, not a given. A single CUDA kernel that fuses all 24 layers of a DeltaNet+Attention model extracts 1.55x more throughput than the generic llama.cpp path. This is a harness-level optimization: same weights, same model, different scaffold.

Ollama currently uses llama.cpp under the hood. As DeltaNet-optimized kernels mature (Luce, flash-linear-attention, vLLM Triton), the inference harness for Mnemosyne will get faster without any changes to the model or the agent code. `environment-snapshot.py` now reports GPU model, VRAM, CUDA version, and compute capability so that a future optimizer can reason about which inference path to use.

---

## Observability design rationale

### Why not Langfuse / OpenLLMetry / Phoenix?

The existing LLM observability landscape in 2026 is mature: Langfuse, OpenLLMetry, Arize Phoenix, TruLens, Helicone, PostHog, LangSmith. They're all good tools. They solve a different problem.

Those tools are **monitoring platforms** designed for production SaaS applications that call cloud LLM APIs. They optimize for: dashboards, cost tracking, latency percentiles, alerting, team collaboration, A/B experiment management via a web UI.

Mnemosyne's observability layer optimizes for something different: **harness optimization substrate for a local-first agent.**

| | Monitoring platforms | Mnemosyne observability |
|---|---|---|
| **Primary user** | Human SRE reading dashboards | Human developer or agentic optimizer reading filesystem |
| **Data format** | OTEL spans → cloud DB | JSONL files → local filesystem |
| **Compression** | Yes (aggregated metrics, sampled traces) | **No** (raw events, the paper's core argument) |
| **Navigability** | Web UI with filters | `grep`, `cat`, `mnemosyne-experiments` CLI |
| **Cloud dependency** | Yes (most require an API key + internet) | **None** (runs in `/tmp`, no network) |
| **Designed for** | Monitoring production reliability | **Optimizing harness code** (the Meta-Harness loop) |

The Meta-Harness paper explicitly argues that monitoring-style compression (reducing each run to a scalar like "accuracy: 0.82") is the core failure mode of prior optimizers. Raw event traces — full args, full results, full tracebacks — are the input the optimizer needs. Our filesystem-as-database approach aligns with the paper; dashboard tools do not.

This doesn't mean you can't use both. An optional OTEL export could let you send the same events to Langfuse for dashboarding while keeping the raw filesystem for optimization. That's a future enhancement.

### Environment snapshot = the paper's #1 discovery

The Meta-Harness paper's most concrete result came from TerminalBench-2. After multiple failed iterations, the optimizer discovered:

> *"Before the agent loop begins, the harness runs a compound shell command to gather a snapshot of the sandbox environment and injects it into the initial prompt."*

This eliminated 2–4 exploratory turns the agent otherwise spent discovering its environment. It was the single change that beat the hand-engineered baseline.

`environment-snapshot.py` implements this pattern. It pre-computes:

- Projects directory contents
- `.env` key names (never values)
- Ollama reachability + model list
- **Model architecture classification** (DeltaNet hybrid vs standard attention)
- **GPU capabilities** (model, VRAM, CUDA, compute capability)
- Venv health + Python version
- Available skill helpers
- Obsidian vault status
- Disk free + platform

A skill wrapper can inject the markdown output as a system prompt preamble. The agent starts every session knowing its environment — no exploratory tool calls needed.

---

## Future: the optimization loop

The infrastructure in this repo forms the substrate for a full Meta-Harness optimization loop:

```
1. OBSERVE     harness_telemetry → events.jsonl
2. EVALUATE    scenario_runner → metrics dict
3. SWEEP       harness_sweep → experiments/ tree
4. COMPARE     mnemosyne-experiments pareto → frontier
5. INSPECT     mnemosyne-experiments diff → delta
6. PROPOSE     (future) agentic proposer reads filesystem, writes new harness code
7. GOTO 1
```

Steps 1–5 are shipped and tested (78 assertions). Step 6 — the agentic proposer that reads traces and rewrites harness code — is the missing piece. The paper uses Claude Code with filesystem tools for this; a future `mnemosyne-optimizer` repo could do the same, reading from the experiments directory and writing modified harness scripts.

The key insight from the paper is that step 6 requires **unrestricted access to all previous history** — not summaries, not top-k, not just the Pareto frontier. The optimizer needs to `grep` across all prior code and traces to find patterns. Our filesystem-as-database design supports this by construction.

---

## References

- Lee, Y., Nair, R., Zhang, Q., Lee, K., Khattab, O., & Finn, C. (2026). Meta-Harness: End-to-End Optimization of Model Harnesses. [arxiv.org/abs/2603.28052](https://arxiv.org/abs/2603.28052)
- Yang, S. et al. (2024). Parallelizing Linear Transformers with the Delta Rule over Sequence Length. NeurIPS 2024. [arxiv.org/abs/2406.06484](https://arxiv.org/abs/2406.06484)
- Gu, A. & Dao, T. (2026). Mamba-3: Improved Sequence Modeling using State Space Principles. ICLR 2026. [arxiv.org/abs/2603.15569](https://arxiv.org/abs/2603.15569)
- Raschka, S. (2025). Beyond Standard LLMs. [magazine.sebastianraschka.com/p/beyond-standard-llms](https://magazine.sebastianraschka.com/p/beyond-standard-llms)
- AVB (@neural_avb). Meta-Harness review. [x.com/neural_avb/article/2039709486538260583](https://x.com/neural_avb/article/2039709486538260583)
- Luce-Org. Megakernel for DeltaNet+Attention hybrid models. [github.com/Luce-Org/luce-megakernel](https://github.com/Luce-Org/luce-megakernel)
- Stanford IRIS Lab. Meta-Harness TerminalBench-2 artifact. [github.com/stanford-iris-lab/meta-harness-tbench2-artifact](https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact)
