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
│  ICMS 5-tier memory        L1 hot / L2 warm / L3 cold  │
│                            L4 pattern / L5 identity     │
│  Instinct overlay          user-pattern fast-path       │
│                            (L4 kind=user_instinct)      │
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

## Memory architecture — ICMS 5-tier + Instinct overlay

The memory system is the most-iterated subsystem in this repo. As of v0.8 it has five persistent tiers plus a runtime overlay that gives the agent a fast, user-personalized "instinct" path. The whole thing is one SQLite + FTS5 database — `tier`, `kind`, and `strength` columns on a single `memories` table do all the work. No graph DB, no vector store (optional embeddings exist but are not required), no third-party memory backend.

### Canonical tier table

| Constant      | Tier | Name     | Purpose                                                | Decay |
| :------------ | :--: | :------- | :----------------------------------------------------- | :---- |
| `L1_HOT`      |  1   | hot      | Working memory; current session context                | fast  |
| `L2_WARM`     |  2   | warm     | Short-term; default tier for new writes                | medium|
| `L3_COLD`     |  3   | cold     | Long-term; demoted from L2 by `demote_unused`          | slow  |
| `L4_PATTERN`  |  4   | pattern  | Recurring clusters promoted by `mnemosyne_compactor`   | slow  |
| `L5_IDENTITY` |  5   | identity | Human-approved core values; injected every turn        | very slow |

These are the **only** tier constants. `mnemosyne_memory.py` exports them at module scope; `KIND_DECAY_MULTIPLIERS` modifies decay rate per content kind (e.g. `core_value` 0.1×, `failure_note` 3.0×). If a doc anywhere refers to "archival" or "meta-memory" tiers, it's wrong — that vocabulary doesn't exist in the code.

### Instinct overlay (v0.8)

Instinct is **not a sixth tier.** It's a fast-path overlay on L4 that gives the agent learned, user-specific reactions without disturbing the persistent hierarchy. Implementation:

- **Storage:** rows live in L4 with `kind="user_instinct"` and `source="instinct"`. They use the same `memories` table; no schema change.
- **Population:** `mnemosyne_instinct.distill()` runs in dream cycles (or on demand). It scans recent L1/L2/L3 rows whose kinds signal user-pattern intent (`preference`, `fact`, `event`, `tool_result`), groups by topic-token signature, and writes the top-N recurring patterns as user-instinct rows. Each pass replaces the prior batch idempotently.
- **Consumption:** `Brain._build_instinct_block()` injects user-instinct rows into the system prompt on every turn, parallel to the L5 identity block. Brain checks instinct *before* doing query-relevance retrieval against the rest of the store.
- **Decay:** user-instinct rows use the `user_instinct` kind multiplier (0.5×) — slower than ops, faster than identity. Stale preferences get demoted; reinforced ones stay.

This is the closest the architecture gets to "automatic learned behavior shaped by reflection." Reflection isn't a separate tier — it's what L5 + the dream/compactor cycle already do. Distilled output flows down into L4 Pattern (via the compactor) or into the user-instinct rows (via `mnemosyne_instinct`). On the next turn, the Brain reads them as part of its system context. That's the loop.

### Diagram

```
                                                    ┌──────────────────────┐
                                                    │  Reflection loop      │
                                                    │  (dreams + compactor) │
                                                    │                       │
                                                    │  • cluster L3 → L4    │
                                                    │  • distill user       │
                                                    │    patterns → L4      │
                                                    │    (user_instinct)    │
                                                    └──────────┬────────────┘
                                                               │  offline
                                                               ▼
   ingest                       ingest                  inject every turn
     │                            │                        │
     ▼                            ▼                        ▼
  ┌──────┐  promote   ┌──────┐  promote   ┌──────┐    ┌────────────┐    ┌──────────┐
  │  L1  ├───────────►│  L2  ├───────────►│  L3  │    │  L4        │    │  L5      │
  │  hot │            │ warm │            │ cold │    │  pattern   │    │ identity │
  │      │◄──demote───│      │◄──demote───│      │    │ +instinct  │    │  (core)  │
  └───┬──┘            └──────┘            └──────┘    └─────┬──────┘    └────┬─────┘
      │                                                     │                │
      └─────────────► query-time retrieval ◄────────────────┘                │
                                                                             │
                                       Brain system prompt ◄─────────────────┘
                                       (every turn, query-independent)
```

### Comparison to human memory models — the honest version

The system draws on Atkinson–Shiffrin (multi-store) and ACT-R (base-level activation, recency × frequency × spacing) — not a strict literal mapping, but a useful framing.

| Mnemosyne tier  | Closest human-memory analogue                          | Important caveat                              |
| :-------------- | :----------------------------------------------------- | :-------------------------------------------- |
| L1 Hot          | Working memory (Baddeley)                              | No phonological loop; it's just a hot SQL row |
| L2 Warm         | Short-term store with rehearsal                        | Rehearsal happens via reads, not vocal loop   |
| L3 Cold         | Consolidated long-term memory (semantic + episodic)    | No interference effects; clean retrieval      |
| L4 Pattern      | Procedural memory + schemas / habituation              | Built by stdlib clustering, not synaptic plasticity |
| L4 user_instinct| Priming + automatic preferences                        | Distilled deliberately; not subliminal        |
| L5 Identity     | Core self-schema / autobiographical anchor             | Human-approved-only; not auto-learned         |

**What the cognitive-science framing buys us:** ACT-R-shaped decay (`KIND_DECAY_MULTIPLIERS` × time-since-last) gives us forgetting curves that look reasonable; the multi-store progression gives users a familiar mental model.

**What it doesn't buy us:** sentience, emotion, embodied grounding. The cognitive-OS checklist in `docs/COGNITIVE_OS.md` is the operational definition we actually defend; the human-memory analogy is pedagogical scaffolding, not a load-bearing claim.

### Bottlenecks and ongoing work

- `apply_decay()` and `search()` use batched `executemany` UPDATEs as of v0.8 — full-scan decay still O(N) but with one round-trip instead of N.
- L4 quality is monitored via `mnemosyne-compactor audit` (v0.8): hit-rate, dead-pattern fraction, average age. Mem0 reportedly hits 97% junk in production audits without strict rules; the compactor's `min_cluster_size` + `source_ids` idempotency + audit pass are the defenses against the same failure mode.
- Comparative benchmarks against Mem0 / LongMemEval / LOCOMO live in `bench/` (skeleton in v0.8; first numbers TBD).

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
