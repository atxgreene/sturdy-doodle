# Training — LoRA adapters from captured turns

Closes the last loop: captured conversations → fine-tuning dataset →
LoRA adapter → deploy to LM Studio or Ollama → A/B-eval against the
base. The pipeline is deliberately narrow — export, compress, train,
deploy, eval — one subcommand per phase.

See `mnemosyne_train.py` for the bridge. See Hermes Agent's
`batch_runner.py` and `trajectory_compressor.py` for the format
specification we match.

## What this is

An opinionated `mnemosyne-train` CLI that turns `events.jsonl` +
`memory.db` into a **LoRA adapter** you can ship to LM Studio or
Ollama. This is not a training framework — training itself shells out
to [Unsloth](https://github.com/unslothai/unsloth), which does the
heavy lifting (LoRA + QLoRA, chat templates, GGUF export) in about
twice the speed of vanilla PEFT/TRL on a single consumer GPU.

What it is **not**:

- A replacement for Unsloth, Axolotl, or any other training framework.
- A full-weights fine-tuner. LoRA only. Merged-GGUF export uses
  Unsloth's merge so the deployed model is standalone.
- A path to "AGI." LoRA sharpens existing model capabilities; it does
  not add skills the base model cannot already do.

## Minimum viable dataset

Rough guidance from observed behavior:

| Turns captured | What you can expect |
|---|---|
| < 500 | Smoke test only. The adapter loads and runs; don't expect behavior change. |
| 500–2 000 | Vocabulary and tone shift toward your corpus. Routing improvements start to show on tool-heavy turns. |
| 2 000–10 000 | Meaningful specialization on your workload. Honest A/B signals on scenario sets. |
| 10 000+ | Strong workload-specialization. Consider full supervised fine-tuning (SFT) as an alternative. |

LoRA rank matters less than you'd think at small sizes. Default is
`rank=16`; bump to 32 once you're past 5 000 turns.

## Capturing training data

Turn on full-turn capture in `BrainConfig`:

```python
from mnemosyne_brain import Brain, BrainConfig
brain = Brain(config=BrainConfig(capture_for_training=True, ...), ...)
```

This emits a `training_turn` telemetry event per successful turn with
the full verbatim system prompt, user message, assistant text, and
tool calls. Storage cost: ~2× the size of a training-off events.jsonl.

Leave capture off for privacy-sensitive runs — there's no way to
scrub verbatim text out of events.jsonl retroactively without editing
the raw file.

## Pipeline

```
events.jsonl + memory.db
    │
    ▼
export    → trajectories.jsonl              (Hermes-compatible ShareGPT)
    │
    ▼
compress  → trajectories.compressed.jsonl   (context summarization)
    │
    ▼
train     → ./adapters/mnemo-v1/            (GGUF adapter via Unsloth)
    │
    ▼
deploy    → ~/.lmstudio/models/mnemosyne/…  (or Ollama Modelfile)
    │
    ▼
eval      → per-scenario A/B vs. the base model, Pareto verdict
```

Commands in order:

```sh
# 1. Export (captures everything completed successfully, drops slips)
mnemosyne-train export --drop-identity-slips \
    --out trajectories.jsonl

# 2. Compress long trajectories to fit your training context window
mnemosyne-train compress trajectories.jsonl \
    --out trajectories.compressed.jsonl \
    --target-max-tokens 15250

# 3. Train (requires [train] extra; shells out to Unsloth)
pip install -e '.[train]'
mnemosyne-train train \
    --data trajectories.compressed.jsonl \
    --base-model unsloth/Qwen2.5-7B-Instruct \
    --out-dir ./adapters/mnemo-v1 \
    --max-steps 500

# 4. Deploy into LM Studio (default) or Ollama
mnemosyne-train deploy ./adapters/mnemo-v1 \
    --to lmstudio \
    --name mnemo-qwen3.5-9b-lora-v1

# 5. A/B eval
mnemosyne-train eval \
    --base-model qwen3.5:9b                    --base-provider ollama \
    --adapted-model mnemo-qwen3.5-9b-lora-v1   --adapted-provider lmstudio \
    --scenarios scenarios.example.jsonl scenarios/jailbreak.jsonl \
    --out /tmp/ab.json
```

## Chat templates — read this before training

Unsloth's own docs are blunt about this: *"use the same chat template
that was used when training."* A mismatch silently produces garbage
output at inference — no error, just a confused model.

Default is `chatml` because it's what Hermes uses and what most modern
instruction-tuned Qwen/Mistral checkpoints ship with. Override only if
you know the base model uses something else (e.g. `llama-3` for
Llama-3 chat models, `qwen-2.5` for Qwen 2.5 Instruct).

## LM Studio vs. Ollama

Both consume the same GGUF file. Pick the one you already run.

- **LM Studio**: recommended for local dev. GUI model browser, clean
  OpenAI-compatible endpoint at `http://localhost:1234/v1`. `deploy`
  drops the GGUF into `~/.lmstudio/models/mnemosyne/<name>/` and it
  shows up in the model picker on restart.
- **Ollama**: preferred for headless and serving. `deploy` writes a
  `Modelfile` with `FROM <base>` + `ADAPTER <gguf>` and runs
  `ollama create <name> -f Modelfile`.

Switching between them is one config change in `mnemosyne_models`:

```python
Backend(provider="lmstudio", default_model="mnemo-qwen3.5-9b-lora-v1")
# or
Backend(provider="ollama",   default_model="mnemo-qwen3.5-9b-lora-v1")
```

## Interop with Hermes

Our exported JSONL is a **drop-in** for Hermes's
`trajectory_compressor.py` and anything downstream of it. Every line
has the exact keys Hermes writes — `prompt_index`, `conversations`
(with `from`/`value` + tool_calls), `metadata`, `completed`, `partial`,
`api_calls`, `toolsets_used`, `tool_stats`, `tool_error_counts`.

Our extra fields live under `metadata.mnemo_*` (`mnemo_run_id`,
`mnemo_turn_number`, `mnemo_model`, `mnemo_provider`, `mnemo_path`,
`mnemo_tags`). Hermes tooling ignores unknown keys, so the files
move freely across ecosystems.

The converse works too: if you have Hermes trajectories, drop them
into `compress` and onward — the pipeline is schema-compatible.

## Honest caveats

- **LoRA is not a skill generator.** It re-weights capabilities the
  base model already has. If your base can't reason about SQL, no
  amount of SQL-trajectory training will fix that.
- **Data quality dominates.** 1 000 clean, representative turns beat
  10 000 noisy ones. The `--drop-identity-slips` flag is not optional
  — identity-slipped turns are actively misleading.
- **Eval on held-out scenarios.** Train on your real workload, eval on
  a held-out set. The `eval` subcommand doesn't split your data for
  you; that's your job.
- **Quantization trades quality for size.** Default is `q4_k_m` — good
  balance. For serious work use `q5_k_m` or `q8_0`. `q4_k_m` is fine
  for iterative dev.
- **LM Studio paths differ by OS version.** If `deploy --dry-run`
  prints a path that looks wrong for your setup, override with
  `LMSTUDIO_MODELS_DIR=/path/to/models mnemosyne-train deploy …`.

## When not to use this

- If the base model already handles your workload well, don't train —
  use skills, memory, and prompt engineering first. LoRA is expensive
  and easy to overfit.
- If you have less than ~500 turns of real conversation data,
  synthesize scenarios via `mnemosyne-scengen`, run your base model
  against them, and only then consider training.
- If you need the adapter to survive a base-model upgrade (e.g.
  Qwen 3.5 → 4.0), LoRA is the wrong tool. Adapters are tied to the
  specific base weights they were trained against.
