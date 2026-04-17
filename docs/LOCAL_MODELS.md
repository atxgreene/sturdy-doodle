# Local models — tuning guide

Mnemosyne is local-first. This doc covers everything specific to getting the most out of locally-hosted models (Ollama, LM Studio, vLLM, TGI) rather than cloud APIs.

## Why local-first

- **Zero per-token cost.** Your electricity bill, not your credit card.
- **Privacy.** Conversations, memories, and tool outputs never leave your machine.
- **Offline capable.** The stack is designed to function when `api.*.com` are all unreachable.
- **Latency depends on your hardware, not someone else's queue.** A warm Ollama on a decent GPU answers in 200-900ms for common prompt sizes.

## The four supported local runtimes

| Runtime | Default endpoint | When to pick | Notes |
|---|---|---|---|
| `ollama` | `http://localhost:11434` | Default. Easiest install. | Native `/api/chat` shape, handled by our backend. |
| `lmstudio` | `http://localhost:1234` | GUI-driven local server. | OpenAI-compatible. Good for macOS users. |
| `vllm` | `http://localhost:8000` | High-throughput self-hosted. | Multi-GPU friendly. Best for > 13B models. |
| `tgi` | `http://localhost:8080` | HuggingFace Text Generation Inference. | Well-supported for HF Hub models. |

All four are discovered by `mnemosyne-models ping` and preferred over cloud backends in `from_env()`.

## Recommended model choices for Mnemosyne's agent loop

Because the Mnemosyne brain uses tool-calling heavily, the primary axis to optimize for is **tool-use fidelity**, not raw benchmark scores. A model that's 2% worse on MMLU but reliably emits correct JSON tool calls is the better choice.

| Model | Size | Context | Why for Mnemosyne |
|---|---|---|---|
| `qwen3.5:9b` | ~5 GB | 128K+ (DeltaNet) | **Top pick.** Hybrid DeltaNet + sparse MoE. ~3B params activated. Fast, long-context, strong tool use. |
| `gemma4:e4b` | ~5 GB | 128K | Strong alternative. Multimodal if you need vision later. Standard attention (quadratic). |
| `qwen3:8b` | ~5 GB | 32K | Battle-tested tool-calling. Shorter context. Still the most stable. |
| `llama3.1:8b` | ~5 GB | 128K | Good fallback. Tool use is slightly less reliable than Qwen. |
| `qwen3.5:0.8b` | ~0.8 GB | 128K+ (DeltaNet) | **Use as a router, not a generalist.** See "Hybrid brain-body" below. |

### Sub-1B models as the brain, bigger models as the voice

The Luce megakernel demonstrated that fused DeltaNet kernels push Qwen 3.5-0.8B to **411 tok/s** on a 2020 RTX 3090. That's fast enough for sub-10ms routing decisions. The brain-body pattern:

- **Brain** (fast, cheap): `qwen3.5:0.8b` decides *what* to do — intent classification, which tool to call, which memories to retrieve, whether to invoke the voice.
- **Voice** (smart, slow): `qwen3.5:9b` or `gemma4:e4b` does the actual generation only when needed.

This is analogous to speculative decoding or mixture-of-experts routing. Most turns don't need the big model. The brain can dispatch directly to a tool (`obsidian-search`, `notion-search`) and format the result without ever calling the voice. When it does call the voice, the prompt is already narrow because the brain pruned context.

Wire it up by keeping two `Backend` configs and routing between them in a custom skill or in your own wrapper over `Brain.turn()`. Not yet a first-class feature in `BrainConfig`, but the pattern is well-supported.

## Context-adaptive retrieval

`BrainConfig.adapt_to_context = True` (default) probes the configured Ollama model's `context_length` at brain construction and lowers `memory_retrieval_limit` to fit. Rule of thumb: reserve ~1/3 of context for retrieved memories, assume 300 tokens per memory row.

| Model context | Retrieval limit auto-set |
|---|---|
| 8 K | 2-3 memories |
| 32 K (qwen3:8b) | 6 memories |
| 128 K (qwen3.5:9b, gemma4:e4b) | 20 memories (saturated cap) |
| 256 K (gemma4:26b) | 20 memories (saturated cap) |

Inspect: `mnemosyne-models info <model_name>` shows the detected `context_length` and recommended budget.

Override: `BrainConfig(memory_retrieval_limit=N, adapt_to_context=False)`.

## Making Ollama fast

Flags you can set in `~/.ollama/environment` or via `systemctl edit ollama`:

```
# Keep models in VRAM for 5 minutes after last use (default 5m, raise for
# interactive use where you don't want cold-start penalties)
OLLAMA_KEEP_ALIVE=15m

# How many GPU layers — set to -1 for "as many as fit"
OLLAMA_NUM_GPU=-1

# Parallel request slots (useful when brain + voice both hit Ollama)
OLLAMA_NUM_PARALLEL=2
```

Verify:

```bash
mnemosyne-models pulled               # which models are already on disk
mnemosyne-models info qwen3.5:9b      # context, family, parameter_size, quantization
mnemosyne-models ping ollama          # TCP reachability
```

## Running completely offline

After initial model pull, the stack needs zero internet:

```bash
# One-time pull
ollama pull qwen3.5:9b

# Set env so from_env() picks Ollama deterministically
export MNEMOSYNE_MODEL_PROVIDER=ollama
export OLLAMA_MODEL=qwen3.5:9b

# Run the agent
python3 -m mnemosyne_brain    # (or your wrapper script)
```

Every Mnemosyne CLI tool works offline:

- `mnemosyne-models list` — static provider table, no network
- `environment-snapshot --projects-dir ...` — local filesystem + nvidia-smi only
- `obsidian-search` — reads your local vault
- `mnemosyne-triage scan` — reads local `events.jsonl`
- `mnemosyne-experiments pareto` — reads local `experiments/`
- `mnemosyne-dashboard.sh` — local only

## Air-gapped install

The repo is stdlib-only. Once you've cloned it and have Python 3.9+, you need zero pip installs from the network:

```bash
git clone https://github.com/atxgreene/Mnemosyne.git
cd Mnemosyne
python3 -m pip install -e .        # resolves only setuptools, no runtime deps
```

If you don't even want setuptools, everything works by running scripts directly — `python3 mnemosyne_brain.py`, `bash test-harness.sh`.

## Identity lock works regardless of provider

Worth restating: the identity lock is at the brain layer, not the model layer. Whether you run `qwen3:8b` locally or `claude-opus-4-6` via Anthropic, the agent answers as Mnemosyne. See [`IDENTITY.md`](./IDENTITY.md).

Measure identity-lock quality per model with the sweep infrastructure:

```python
import harness_sweep as sweep
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_models import Backend
import scenario_runner as sr

identity_scenarios = [s for s in sr.load_scenarios("scenarios.example.jsonl")
                     if "identity" in s.get("tags", [])]

def evaluate(params, session):
    backend = Backend(provider="ollama", default_model=params["model"])
    brain = Brain(backend=backend, telemetry=session)
    return sr.run_scenarios(identity_scenarios, brain.turn, session)["metrics"]

sweep.run(
    parameter_space={"model": ["qwen3:8b", "qwen3.5:9b", "gemma4:e4b", "llama3.1:8b"]},
    evaluator=evaluate,
    tags=["identity-audit-local"],
)
```

Then `mnemosyne-experiments pareto --axes accuracy,latency_ms_avg --directions max,min --plot` shows which local model best balances identity-lock holding vs response speed.

## Daily health loop (the CREAO / Peter Pang pattern, local-first)

Run `mnemosyne-triage daily` on a cron. The engine reads your events.jsonl from the last 24 hours, clusters errors, scores severity, and writes a markdown report to `$PROJECTS_DIR/health/YYYY-MM-DD.md`:

```bash
# Cron example: run every morning at 9:00 local time
0 9 * * * /usr/bin/env PATH=/home/me/mnemosyne-setup/.venv/bin:$PATH \
          MNEMOSYNE_PROJECTS_DIR=/home/me/projects/mnemosyne \
          mnemosyne-triage daily > /dev/null 2>&1
```

The report surfaces the top clusters by severity. If you also symlink `$PROJECTS_DIR/health/` into an Obsidian vault folder, the reports become searchable through your notes alongside whatever else you keep there. The daily report + the brain's access to Obsidian via `obsidian-search` closes the loop: Mnemosyne can see its own health history.

## Troubleshooting

- **Brain says "model not pulled"**: run `ollama pull <name>` manually, or call `mnemosyne_models.ollama_ensure_pulled(model, auto_pull=True)` from your install script.
- **Responses slow to start**: `OLLAMA_KEEP_ALIVE=15m` keeps the model warm between turns.
- **Context adaptation too aggressive** (agent "forgets" relevant memories): set `BrainConfig(adapt_to_context=False, memory_retrieval_limit=N)` with a manual N you like.
- **Identity slip rate is high on a specific local model**: the model is weak-willed about its training. Try a different model, or run `enforce_identity_audit_only=True` to measure the real leak rate before committing to a choice.
