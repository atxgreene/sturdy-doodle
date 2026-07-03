# Comparative benchmarks (`bench/`)

Stuff in this directory **is not part of the Mnemosyne distribution.** It
ships in the repo so the methodology is reproducible, but it is not
installed by `pip install mnemosyne-harness`, has its own optional
deps (see `requirements.txt`), and is not tested in CI.

The point: when someone asks "OK, but what does Mnemosyne actually
score on LOCOMO vs Mem0?", we have a runnable answer in the same
repo as the substrate it benchmarks.

---

## Quick path: LM Studio + Continuity Score (no extra deps)

If you have LM Studio running at `http://localhost:1234`, you can
get a real benchmark number against your local model in one
command — using only the stdlib bits already shipped with
Mnemosyne. No `bench/requirements.txt` needed.

```sh
# 1. Start LM Studio's local server. Confirm it's reachable:
curl -s http://localhost:1234/v1/models | head -20

# 2. Sanity-first: run 5 scenarios to make sure LM Studio is
#    responding correctly before committing to all 50. Expect ~30 s.
mnemosyne-continuity run \
    --scenarios scenarios/continuity.jsonl \
    --provider lmstudio \
    --model <model-id-from-step-1> \
    --max-scenarios 5 \
    --verbose

# 3. Full benchmark with streaming progress. Expect roughly
#    N_scenarios × (model's turn latency × 2) — 50 scenarios × ~5 s
#    per turn × 2 turns per scenario ≈ 8-10 minutes on a 7-8B
#    quantized model.
mnemosyne-continuity run \
    --scenarios scenarios/continuity.jsonl \
    --provider lmstudio \
    --model <model-id-from-step-1> \
    --verbose \
    --out /tmp/mnemosyne-continuity-lmstudio.json

# 4. The summary JSON prints to stdout. Full per-scenario report is
#    written to --out. Paste the summary into an issue or share it.
```

**Why `--verbose`?** Without it, a 50-scenario run is silent for
10-20 minutes and it's easy to lose faith that anything's happening.
`--verbose` streams a one-line result per scenario as they complete:

```
[ 1/50] ✓ cont-pref-01        preference
[ 2/50] ✓ cont-pref-02        preference
[ 3/50] ✗ cont-pref-03        preference
...
[31/50] ✓ cont-xses-01        preference [xsession]
```

What you'll get back:

```json
{
  "continuity_score": 0.XX,
  "passed": NN,
  "total": 50,
  "by_category": {
    "preference": {"total": 12, "passed": NN, "score": 0.XX},
    "fact":       {"total": 14, "passed": NN, "score": 0.XX},
    "project":    {"total": 12, "passed": NN, "score": 0.XX},
    "decision":   {"total":  6, "passed": NN, "score": 0.XX},
    "rule":       {"total":  6, "passed": NN, "score": 0.XX}
  },
  "cross_session": {"total": 10, "passed": NN, "score": 0.XX}
}
```

**Reference:** the substrate-only dryrun (no LLM at all) scores
**0.96 aggregate / 1.00 cross-session** (see
`docs/BENCHMARKS_v0.7.md`). Your live-model score should match or
beat the dryrun on `preference` / `fact` (LLMs handle paraphrasing
better than FTS5) and may exceed it on `project` / `rule` (where
multi-row composition or rule-following matters).

The two scenarios that the dryrun flags as structural floor —
`cont-proj-04` (cross-row composition) and `cont-rule-02`
(world-knowledge) — are exactly where a real model should give us
a delta.

---

## LOCOMO benchmark (full setup)

LOCOMO is the standard public long-conversation memory-recall
benchmark from Snap Research: ten ~600-turn dialogues, ~199
human-annotated QA pairs per sample (1,990 total), five question
categories (single-hop / multi-hop / temporal / open-domain /
adversarial).

Repo: [snap-research/locomo](https://github.com/snap-research/locomo).
Paper: arXiv 2402.17753.

**We do not redistribute the dataset** (no license declared in the
upstream repo). Download it yourself — one command:

```sh
mkdir -p bench/data
curl -L https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
    -o bench/data/locomo10.json
```

`bench/data/` is gitignored so the 2.8 MB JSON never accidentally
gets committed.

### Retrieval track (fast, no API key, deterministic)

The zero-cost path: retrieval-only with the substring judge
(case-insensitive substring / token match between expected answer
and retrieved context) plus **evidence recall@k** (judge-free —
fraction of gold `evidence` dia_ids found in the top-k retrieved
rows). Full 1,986-question run takes ~15 s and 0 USD. Published
numbers: [`docs/BENCHMARKS_LOCOMO.md`](../docs/BENCHMARKS_LOCOMO.md).

```sh
# Headline retrieval run (FTS5 top-8) — the substrate number
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode fts \
    --out bench/results/locomo-fts-full.json

# Same-protocol baselines + ceiling for the comparison table
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode recency \
    --out bench/results/locomo-recency-full.json
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode random \
    --out bench/results/locomo-random-full.json
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode full \
    --out bench/results/locomo-full-full.json

# Token/score trade-off sweep
for k in 2 4 8 16 32; do
  python3 bench/locomo.py --substrate mnemosyne --top-k $k \
      --out bench/results/locomo-fts-k$k.json
done
```

Scoring protocol: the headline `score` covers the **1,540
non-adversarial questions** (categories 1-4), matching how published
LOCOMO evals (e.g. Mem0, arXiv 2504.19413) report theirs. Category 5
(adversarial, 446 questions) requires a model to *abstain*, which is
meaningless without an LLM; it is judged by abstention-phrase
detection in `--llm-grounded` runs and reported separately. Every
report JSON embeds dataset sha256, git commit, latency p50/p95,
token estimates, ingest throughput, and the exact argv.

### LLM-grounded (measures the full agent stack)

```sh
# LM Studio-grounded smoke test (model answers each Q from retrieved context)
python3 bench/locomo.py --substrate mnemosyne \
    --llm-grounded --provider lmstudio --model <your-model-id> \
    --max-samples 1 --max-questions-per-sample 20 \
    --verbose --out bench/results/mnemo-lmstudio-smoke.json

# Full run: 10 samples × ~199 questions × model_turn_latency
# On a 7-8B q4_K_M model that's 1-3 hours. Use --verbose.
python3 bench/locomo.py --substrate mnemosyne \
    --llm-grounded --provider lmstudio --model <your-model-id> \
    --verbose --out bench/results/mnemo-lmstudio-full.json
```

### LLM-as-judge (paid, more representative of LOCOMO numbers)

Published LOCOMO numbers use GPT-4 family as the judge. Match their
methodology with `--judge openai` (requires `OPENAI_API_KEY` +
`pip install -r bench/requirements.txt`):

```sh
pip install -r bench/requirements.txt

python3 bench/locomo.py --substrate mnemosyne \
    --llm-grounded --provider lmstudio --model <your-model-id> \
    --judge openai --judge-model gpt-4o-mini \
    --verbose --out bench/results/mnemo-lmstudio-openai-judge.json
```

### Mem0 head-to-head

Run the same dataset through Mem0 for a comparison table:

```sh
# Requires OPENAI_API_KEY in env (Mem0 uses it for extraction)
python3 bench/locomo.py --substrate mem0 \
    --max-samples 1 --max-questions-per-sample 20 \
    --judge openai --verbose \
    --out bench/results/mem0-locomo-smoke.json
```

### Category interpretation

LOCOMO categories (integer 1-5 in the JSON; renamed by our runner):

| Code | Our name | n | LOCOMO meaning |
| :--: | :--- | --: | :--- |
| 1 | `multi_hop` | 282 | Answer requires combining multiple turns |
| 2 | `temporal` | 321 | Answer requires reasoning about time ("When did…") |
| 3 | `open_domain` | 96 | Answer draws on knowledge beyond the dialog |
| 4 | `single_hop` | 841 | Answer in one dialog turn |
| 5 | `adversarial` | 446 | Correct answer is abstention — question isn't answerable from the dialog (`adversarial_answer` field, no `answer`) |

> ⚠️ Runner versions before v0.9.8 had 1↔4 and 2↔3 swapped, so
> per-category numbers from older runs are mislabeled (totals were
> unaffected). The mapping above is verified against the data itself
> (category 2 = "When did…" questions) and matches Mem0's published
> evaluation code.

Reports land in `bench/results/` (gitignored — don't commit raw
conversation samples or outputs).

---

## What lives here

| File | Purpose |
|---|---|
| `locomo.py` | LOCOMO runner with `MnemosyneSubstrate` + `Mem0Substrate` adapters. Retrieval-only + LLM-grounded modes, baseline retrieval modes (recency/random/full), evidence recall@k, latency/token/cost instrumentation. |
| `longmemeval.py` | LongMemEval runner (arXiv 2410.10813). Session-level + turn-level retrieval recall@k, abstention handling, same instrumentation. `--selftest` validates the runner on a synthetic schema-faithful fixture with no dataset/network/LLM. |
| `requirements.txt` | Optional deps — `datasets`, `mem0ai`, `openai`, `sentence-transformers`, `tiktoken`. NOT in main pyproject. |
| `README.md` | This file. |

## LongMemEval benchmark

LongMemEval (Wu et al., ICLR 2025 — [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval))
tests five long-term memory abilities over timestamped multi-session
histories. 500 questions; `longmemeval_s` ≈ 115k tokens of history
per question. Dataset is released on HuggingFace (we don't
redistribute it):

```sh
mkdir -p bench/data
wget -P bench/data https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
wget -P bench/data https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

# Validate the runner first (no dataset needed):
python3 bench/longmemeval.py --selftest

# Retrieval-only run (session/turn recall@k + answer-in-context):
python3 bench/longmemeval.py \
    --dataset bench/data/longmemeval_s_cleaned.json \
    --verbose --out bench/results/longmemeval-s-retrieval.json
```

Abstention questions (`question_id` ending `_abs`) are excluded from
the headline score in retrieval-only mode and judged via abstention
detection in `--llm-grounded` runs — same protocol as LOCOMO
category 5.

---

## Reporting your numbers

If you run the benchmarks on your hardware, please share the result.
Open an issue at github.com/atxgreene/Mnemosyne/issues with:

- The exact command you ran (model id, provider, scenario count).
- The dataset hash if you ran LOCOMO (printable from `datasets`).
- The summary JSON.
- Hardware (CPU/GPU, VRAM if relevant).

We'd like to maintain a small table of community-reported numbers
in `docs/BENCHMARKS_v0.7.md` with credit to the runner.

---

## Why a separate directory

1. **Deps.** Mem0 requires its SDK + a vector backend + an embedding
   provider + an LLM API key. None belongs in the stdlib-only core.
2. **Cost.** A LOCOMO run hits a paid API. CI shouldn't run on
   every push.
3. **Honesty.** Benchmark numbers belong in a dated, cited report,
   not in a passing test that flips green every CI run regardless of
   whether the underlying systems changed.

---

## Status as of v0.9.8

- `locomo.py` — full retrieval track **run and published**
  (2026-06-11): FTS top-8 0.6247 vs recency 0.2468 / random 0.2799 /
  full-context ceiling 0.8727 over the 1,540 non-adversarial
  questions. See [`docs/BENCHMARKS_LOCOMO.md`](../docs/BENCHMARKS_LOCOMO.md)
  and [`docs/benchmark-results/2026-06-11-locomo-retrieval-track.json`](../docs/benchmark-results/2026-06-11-locomo-retrieval-track.json).
- `longmemeval.py` — **oracle-split grounded run measured (2026-07)**:
  0.6894 (324/470 scored) with LM Studio `qwen3-vl-4b-bench`; see
  [`docs/BENCHMARKS_LOCOMO.md`](../docs/BENCHMARKS_LOCOMO.md) §5.
  `longmemeval_s` haystack-split retrieval numbers still pending.
- LLM-grounded LOCOMO — **full run measured (2026-07)**: 0.3292
  (507/1540) with LM Studio `ternary-bonsai-8b-mlx-bench`; see
  [`docs/BENCHMARKS_LOCOMO.md`](../docs/BENCHMARKS_LOCOMO.md) §6.
  Mem0 head-to-head still blocked on `OPENAI_API_KEY` at run time.
- Continuity Score — multi-model results published in
  [`docs/benchmark-results/`](../docs/benchmark-results/).
