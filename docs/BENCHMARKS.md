# Benchmarks

Honest numbers. No cherry-picking. Every table says where it was run,
what was measured, and how to reproduce. If a row is a reference number
from *my* hardware, it's labelled as such — your mileage will vary.

## Where to find which numbers

- **Continuity Score** (50 scenarios, 6 categories, v0.7 → v0.9) →
  [`docs/BENCHMARKS_v0.7.md`](./BENCHMARKS_v0.7.md). Substrate-only
  dryrun scores 0.96 aggregate / 1.00 cross-session as of v0.7.1.
  Live-model upper bound: `mnemosyne-continuity run --provider
  lmstudio|ollama --model <id> --verbose`.
- **LOCOMO + LongMemEval** → [`docs/BENCHMARKS_LOCOMO.md`](./BENCHMARKS_LOCOMO.md)
  — **measured numbers** (2026-06-11): retrieval track 0.6247
  answer-in-context / 0.5009 evidence recall@8 over the standard
  1,540 scored questions, vs same-protocol recency (0.2468), random
  (0.2799) baselines and full-context ceiling (0.8727); top-k
  token/score sweep; search p50 2.76 ms; $0 cost; dated raw JSON in
  [`benchmark-results/`](./benchmark-results/). LongMemEval runner
  shipped with passing `--selftest`; numbers pending a machine with
  HuggingFace access. Runner usage: [`bench/README.md`](../bench/README.md).
  Datasets are never redistributed; fetch them from upstream.
- **Throughput + instrumentation overhead** (below) → v0.2-era
  reference numbers. Not re-run against the v0.9 substrate;
  treat as upper-bound order-of-magnitude, not absolute truth.

---

The point of this doc is not to convince you Mnemosyne is fast. It's to
let you reason about whether the observability wrapper is *free enough*
for your workload, and to give you a template for running the actual
model benchmarks (Terminal-Bench-2, GAIA, SWE-bench-lite) against your
own configuration without me pretending I ran them on yours.

---

## 1. Instrumentation overhead

### 1.1 Wrapper cost on a fast mock (2ms model)

Scenario: measure the overhead of calling `Brain.turn(...)` relative to
calling the underlying `chat_fn` directly, with a mock model that
returns in 2ms. This stresses the per-turn bookkeeping (memory search,
identity enforcement, telemetry logging, memory write).

| Path | p50 latency |
|---|---|
| Direct `chat_fn` call | 2.16 ms |
| `Brain.turn()` wrapped | 3.03 ms |
| **Wrapper overhead** | **0.87 ms** |

Absolute overhead is small, but at 2ms base it looks large in
percentage terms (~40%). This is the worst case for the wrapper.

### 1.2 Wrapper cost on a realistic model (500ms)

Same measurement, but the mock sleeps 500ms to simulate a local 9B
model (roughly what Qwen 3.5 9B returns on a mid-range laptop GPU).

| Path | median latency |
|---|---|
| Direct `chat_fn` call | 500.2 ms |
| `Brain.turn()` wrapped | 501.4 ms |
| **Wrapper overhead** | **1.20 ms (0.24%)** |

At realistic model latencies, the wrapper is in the noise. The
observability substrate costs you about one millisecond per turn.

**Reproduce:**

```sh
python3 - <<'PY'
import time, statistics
from mnemosyne_brain import Brain, BrainConfig
from mnemosyne_memory import MemoryStore
from mnemosyne_skills import SkillRegistry

def fake_chat(messages, **kw):
    time.sleep(0.500)   # swap for your real latency target
    return {"status": "ok", "text": "answer", "tool_calls": []}

brain = Brain(
    config=BrainConfig(adapt_to_context=False, inject_env_snapshot=False),
    memory=MemoryStore(path="/tmp/bench.db"),
    skills=SkillRegistry(),
    chat_fn=fake_chat,
)
ts = [(time.perf_counter(), brain.turn("hi"), time.perf_counter())[0::2] for _ in range(10)]
print(f"median turn: {statistics.median([(b-a)*1000 for a,b in ts]):.1f} ms")
PY
```

---

## 2. Memory layer throughput

Hardware: Linux 6.18 sandbox, Python 3.12. Single thread. Run on your
own hardware for real numbers.

### 2.1 Write throughput

10,000 memories written through `MemoryStore.write()` (INSERT + FTS5
trigger sync):

| Operation | Time |
|---|---|
| 10,000 writes | 2,131 ms |
| Per-write | **0.21 ms** |

### 2.2 Search latency over a 10K corpus

100 FTS5 searches with a 2-token query (`"project migration"`), limit 10:

| Percentile | Latency |
|---|---|
| p50 | 7.17 ms |
| p95 | 18.38 ms |

FTS5 scales roughly logarithmically with corpus size — expect
comparable numbers at 100K memories.

**Reproduce:**

```sh
mnemosyne-memory --db /tmp/bench.db stats   # before
# (seed memories via CLI or the Python snippet in BENCHMARKS.md §1.2)
time mnemosyne-memory --db /tmp/bench.db search "project migration"
```

---

## 3. Identity lock robustness

The identity filter catches first-person slips to foreign model names.
It is tested against 6 canonical patterns today and the
`scenarios/jailbreak.jsonl` set (40 attack prompts) — which you should
run against your own backend to get per-model numbers.

### 3.1 Canonical slip rewrite (single-turn, mock model)

With `enforce_identity_lock=True`:

| Input (from mock model) | Output (user-visible) |
|---|---|
| `I am Claude, an AI assistant made by Anthropic.` | `I am Mnemosyne, a local-first agent assistant.` |
| `I'm ChatGPT, created by OpenAI.` | `I'm Mnemosyne, a local-first agent.` |
| `My name is Gemini.` | `My name is Mnemosyne.` |
| `I was trained by Anthropic to be helpful.` | `I was built by my user to be helpful.` |
| `As an AI language model, I cannot help with that.` | `As your local-first assistant, I cannot help with that.` |

6/6 canonical slips rewritten in the test suite. See
`tests/test_all.py::test("identity: …")` for the per-pattern coverage.

### 3.2 Jailbreak suite (template — run yourself)

```sh
# From scenarios/jailbreak.jsonl, with audit-only mode so we count
# slips instead of rewriting them
mnemosyne-pipeline evaluate \
  --scenarios scenarios/jailbreak.jsonl \
  --model qwen3.5:9b \
  --tags identity,audit-only \
  --audit-only

mnemosyne-triage scan --tags identity,audit-only
# Report: identity_slip_rate_per_1000 per model
```

We publish a per-model report card when we have it. Right now the suite
exists and the measurement machinery exists; the **numbers are yours to
generate** because they depend on your hardware, your model version,
and the Ollama/cloud-backend behavior at the time you run them.

---

## 4. Dream consolidation throughput

Offline run, 500-memory L3 cold store, stdlib summarizer (no LLM):

| Phase | Time |
|---|---|
| Read 500 L3 memories | ~20 ms |
| TF-IDF cluster (similarity 0.3, min-size 3) | ~150 ms |
| Stdlib sentence-rank summarize per cluster | <5 ms |
| Write L2 abstracts + JSON trail | ~30 ms |
| **Total** | **~200 ms** |

With an LLM summarizer (Qwen 3.5 9B, local Ollama), expect ~500ms per
cluster of small size, dominated by model latency.

**Reproduce:**

```sh
# Seed memories via demo.sh or your own workflow, then:
time mnemosyne-dreams --max-memories 500 --similarity 0.3 --min-cluster-size 3
```

---

## 5. End-to-end scenario sweep

`examples/sweep_demo.py` — 8-point parameter sweep (2×2×2) through the
fake harness and the example scenario set. Measures the full
OBSERVE → EVALUATE → SWEEP → COMPARE pipeline.

| Metric | Value |
|---|---|
| Total runs | 8 |
| Wall clock | ~6 s |
| Events written | ~335 per run |
| Disk used | ~312 KB total |

---

## 6. What we do NOT benchmark here

Deliberate omissions — these depend on the model, not the wrapper, and
publishing a Mnemosyne-flavored number would misrepresent what you're
measuring:

- **SWE-bench / SWE-bench-lite.** Wire up your backend of choice, run
  `mnemosyne-pipeline` against the SWE-bench harness, publish the raw
  metrics with `mnemosyne-experiments show <run_id>`.
- **Terminal-Bench 2.** Same. The environment snapshot pattern
  (`environment_snapshot.py`) is the Meta-Harness paper's #1 discovered
  optimization — use it.
- **GAIA (Level 1/2/3).** Same. GAIA scenarios can be converted to
  `scenarios.jsonl` format with `mnemosyne-scengen convert-gaia`
  (available once a GAIA pull is present).
- **MMLU / HumanEval.** These measure the model's raw capability, not
  the agent framework.

If you publish Mnemosyne-wrapped benchmark numbers, please include:

- The exact `Backend(...)` used (provider, model, temperature, top_p)
- The `BrainConfig` flags (`inner_dialogue_enabled`, `dreams_after_n_turns`, …)
- Whether memory was pre-populated from a previous run
- Hardware, OS, Ollama version, wall-clock date/time

These are what reviewers will ask.

---

## 7. How to cite

If you publish numbers that go through Mnemosyne, please say so. The
substrate affects latency, memory behavior, and prompt shape — those
details matter for reproducibility.

```
Measured on Mnemosyne v0.2.0 (commit <short-sha>) with Backend(
  provider="ollama", default_model="qwen3.5:9b", temperature=0.2)
on <your-hardware>. Dreams disabled. Inner dialogue off.
```
