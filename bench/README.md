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

# 2. Run the Continuity Score benchmark against it:
mnemosyne-continuity run \
    --scenarios scenarios/continuity.jsonl \
    --provider lmstudio \
    --model <model-id-from-step-1> \
    --out /tmp/mnemosyne-continuity-lmstudio.json

# 3. The summary JSON prints to stdout. Full per-scenario report is
#    written to --out. Paste the summary into an issue or share it.
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

LOCOMO is the standard public long-conversation memory recall
benchmark (arxiv 2402.09727). Ten ~600-turn dialogues, four question
types, LLM-as-judge scoring.

This needs the full optional deps (`datasets` for loading, `openai`
for the judge, `mem0ai` if you want a head-to-head):

```sh
# 1. Set up the optional venv (keeps stdlib-only invariant intact)
python3 -m venv bench/.venv
bench/.venv/bin/pip install -r bench/requirements.txt

# 2. Mnemosyne, retrieval-only baseline (no LLM)
bench/.venv/bin/python bench/locomo.py \
    --substrate mnemosyne \
    --max-conversations 2 \
    --out bench/results/mnemosyne-locomo-retrieval.json

# 3. Mnemosyne, LM Studio grounded
bench/.venv/bin/python bench/locomo.py \
    --substrate mnemosyne \
    --llm-grounded \
    --provider lmstudio \
    --model <your-lmstudio-model-id> \
    --out bench/results/mnemosyne-locomo-lmstudio.json

# 4. Mem0 head-to-head (requires OPENAI_API_KEY in env)
bench/.venv/bin/python bench/locomo.py \
    --substrate mem0 \
    --max-conversations 2 \
    --out bench/results/mem0-locomo.json
```

The judge is `gpt-4o-mini` at `temperature=0` by default; override
in `bench/locomo.py:llm_judge` if you want to pin a different
snapshot.

Reports land in `bench/results/` (gitignored — don't commit raw
conversation samples).

---

## What lives here

| File | Purpose |
|---|---|
| `locomo.py` | LOCOMO runner with `MnemosyneSubstrate` + `Mem0Substrate` adapters. Supports retrieval-only and LLM-grounded modes. |
| `requirements.txt` | Optional deps — `datasets`, `mem0ai`, `openai`, `sentence-transformers`, `tiktoken`. NOT in main pyproject. |
| `README.md` | This file. |
| (planned) `longmemeval.py` | LongMemEval runner once the LOCOMO comparison is stable. |

---

## Reporting your numbers

If you run the benchmarks on your hardware, please share the result.
Open an issue at github.com/atxgreene/sturdy-doodle/issues with:

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

## Status as of v0.8.0

- `locomo.py` — runner shipped; LM Studio + Mem0 adapters wired.
- Mnemosyne LM Studio Continuity numbers — recommend running tonight
  via the Quick Path above. Expected: live model > 0.96 aggregate.
- Mem0 head-to-head — runner ready; numbers blocked on real run.
- LongMemEval — not yet stubbed.
