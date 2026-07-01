# LOCOMO & LongMemEval — measured numbers

Dated, reproducible, zero-API-cost benchmark results for the Mnemosyne
memory substrate on the two standard public long-term-memory
benchmarks. Every number on this page was produced by a command you
can run yourself in under a minute of setup; the raw report JSONs
(with dataset sha256, git commit, argv, and per-question records) are
in [`benchmark-results/2026-06-11-locomo-retrieval-track.json`](./benchmark-results/2026-06-11-locomo-retrieval-track.json).

**TL;DR (LOCOMO retrieval track, 2026-06-11):**

| | answer-in-context | evidence recall@8 | context tokens/probe | search p50 | cost |
|---|---:|---:|---:|---:|---:|
| **Mnemosyne FTS top-8** | **0.6247** | **0.5009** | 319 | 2.76 ms | $0 |
| recency-8 baseline | 0.2468 | 0.0054 | 299 | ~0 | $0 |
| random-8 baseline | 0.2799 | 0.0198 | 302 | ~0 | $0 |
| full-context ceiling | 0.8727 | 0.9961 | 22,576 | 0.13 ms | $0 |

The substrate retains **72% of the full-context ceiling score using
1.4% of the tokens** (319 vs 22,576 per probe), and beats the
same-protocol recency/random baselines by **2.2–2.5×** on answers and
**25–93×** on evidence recall.

---

## 1. What was measured

**Benchmark:** LOCOMO ([snap-research/locomo](https://github.com/snap-research/locomo),
arXiv 2402.17753) — 10 long multi-session conversations (5,882 turns
total, ~26k tokens each), 1,986 human-annotated QA pairs.

**Track:** *retrieval-only*. Each conversation turn is written to
Mnemosyne's `MemoryStore` (SQLite + FTS5, tier L2); each question is
answered by retrieving top-k rows and checking whether the gold
answer appears in the retrieved context. **No LLM anywhere** — this
isolates the substrate (what Mnemosyne actually ships) from model
quality, runs deterministically, and costs nothing. The
`--llm-grounded` mode that measures the full agent stack is also
shipped; see §6.

**Two scores per run:**

- **answer-in-context** — case-insensitive substring/token match of
  the gold answer against the retrieved context (a deterministic
  lower bound; the judge a model would need to beat).
- **evidence recall@k** — judge-free: fraction of the gold `evidence`
  dia_ids that appear among the top-k retrieved rows. This is the
  cleanest pure-retrieval metric LOCOMO supports.

**Scoring protocol.** The headline score covers the **1,540
non-adversarial questions** (categories 1–4) — the same denominator
Mem0's published evaluation uses (their repo reports n=1540).
Category 5 ("adversarial", 446 questions) has no gold answer — the
correct behavior is *abstention*, which only exists when a model
generates an answer; the runner scores it via abstention detection in
`--llm-grounded` mode and reports it separately.

> **Erratum for previously published numbers.** The earlier
> "LOCOMO retrieval-only **0.4849** across 1,986 questions" figure is
> the *same run quality* as today's 0.6247 — it divided by all 1,986
> questions, silently counting 446 adversarial questions as failures
> that retrieval-only mode can never pass (962/1986 = 0.4844).
> Additionally, runner versions before v0.9.8 had category labels
> permuted (1↔4, 2↔3 relative to the snap-research evaluation code);
> totals were unaffected but old per-category rows are mislabeled.

## 2. Reproducible setup

```sh
git clone https://github.com/atxgreene/Mnemosyne && cd Mnemosyne
curl -L https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
     -o bench/data/locomo10.json
# sha256: 79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4

python3 bench/locomo.py --substrate mnemosyne --retrieval-mode fts \
    --out bench/results/locomo-fts-full.json          # headline, ~15 s
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode recency \
    --out bench/results/locomo-recency-full.json      # baseline
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode random \
    --out bench/results/locomo-random-full.json       # baseline
python3 bench/locomo.py --substrate mnemosyne --retrieval-mode full \
    --out bench/results/locomo-full-full.json         # ceiling
```

Environment for the published run: Linux 6.18.5 x86_64 cloud sandbox,
Python 3.11.15, single thread, stdlib only (no optional deps
installed — token counts use the chars/4 estimate). Substrate:
Mnemosyne v0.9.7, runner `bench/locomo.py` v0.9.8. Date: 2026-06-11.

## 3. Results

### 3.1 By category (FTS top-8, answer-in-context)

Categories use the snap-research evaluation-code mapping
(1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop):

| category | n | Mnemosyne FTS-8 | recency-8 | random-8 | full ceiling |
|---|---:|---:|---:|---:|---:|
| single-hop | 841 | **0.7717** | 0.3020 | 0.3222 | 0.9905 |
| multi-hop | 282 | **0.5851** | 0.3014 | 0.3582 | 0.9574 |
| temporal | 321 | **0.3302** | 0.0343 | 0.0935 | 0.5358 |
| open-domain | 96 | **0.4375** | 0.3125 | 0.3021 | 0.7188 |
| **overall (1,540)** | | **0.6247** | 0.2468 | 0.2799 | 0.8727 |

Reading: single-hop recall is strong; temporal is the substrate's
weakest category (FTS5 has no date arithmetic — even the full-context
ceiling only hits 0.536 on substring match because answers like
"7 May 2023" are phrased differently in the dialog). That gap is the
clearest argument for the dream/consolidation layers and a
temporal-index — see §7.

### 3.2 Evidence recall@8 (judge-free)

| category | Mnemosyne FTS-8 | recency-8 | random-8 | full |
|---|---:|---:|---:|---:|
| single-hop | 0.5979 | 0.0071 | 0.0188 | 0.9996 |
| temporal | 0.5968 | 0.0031 | 0.0275 | 0.9969 |
| multi-hop | 0.1909 | 0.0035 | 0.0177 | 0.9943 |
| open-domain | 0.2304 | 0.0027 | 0.0082 | 0.9674 |
| **mean (n=1,536)** | **0.5009** | 0.0054 | 0.0198 | 0.9961 |

All-gold-evidence-found rate: 0.4583 (FTS-8). Multi-hop evidence
recall (0.19) is the bottleneck — single FTS query, multiple
scattered evidence turns. Query decomposition or multi-probe
retrieval is the obvious lever.

### 3.3 Token / score trade-off (top-k sweep, FTS)

| k | answer-in-context | evidence recall | context tokens/probe | search p50 | search p95 |
|---:|---:|---:|---:|---:|---:|
| 2 | 0.4377 | 0.3436 | 81 | 1.84 ms | 3.64 ms |
| 4 | 0.5422 | 0.4201 | 160 | 2.23 ms | 4.73 ms |
| 8 | 0.6247 | 0.5009 | 319 | 2.76 ms | 21.87 ms |
| 16 | 0.6948 | 0.5602 | 643 | 3.74 ms | 23.67 ms |
| 32 | 0.7552 | 0.6264 | 1,296 | 6.57 ms | 28.08 ms |
| full | 0.8727 | 0.9961 | 22,576 | — | — |

k=32 reaches 87% of the ceiling at 5.7% of the tokens. Pick your
point on the curve per your model's context budget.

### 3.4 Throughput, latency, cost

| metric | value |
|---|---|
| ingest | 5,882 turns in 2.38 s (**2,467 writes/s**, INSERT + FTS5 sync) |
| search latency (FTS-8) | p50 **2.76 ms** · p95 21.87 ms · max 392 ms (n=1,986) |
| full benchmark wall clock | **13.1 s** (ingest + 1,986 probes) |
| total ingested | ~179.5k tokens (est) |
| API cost | **$0.00** — no network calls at all |

For scale: Mem0's paper (arXiv 2504.19413, Table 2) reports *search*
p50 of 0.148 s (148 ms) and total-response p95 of 1.44 s for Mem0,
and 17.1 s p95 for full-context — those include API round-trips and
answer generation, so they are not like-for-like with our local
substrate search; cite accordingly. The like-for-like point is
architectural: a local FTS5 search is ~50× faster than a hosted
memory-API search before any answer model runs.

## 3.5 Efficiency frontier — breaking the norm

The agent-memory leaderboard (Mem0, Zep, Honcho) races one number: LLM-judged
accuracy. That race hides what decides whether memory is usable in a live agent
loop — tokens, cost, latency, and locality per answer. `bench/efficiency_frontier.py`
reports those directly (run 2026-07-01, artifact
[`benchmark-results/2026-07-01-locomo-efficiency-frontier.json`](./benchmark-results/2026-07-01-locomo-efficiency-frontier.json)):

| k | answer-in-context | tokens/probe | **utility per 1k tok** | % of full-context tokens | $/1k questions¹ | search p50 |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 0.4377 | 81 | **5.40** | 0.31% | $0.012 | 1.7 ms |
| 4 | 0.5422 | 160 | 3.39 | 0.62% | $0.024 | 2.1 ms |
| 8 | 0.6247 | 319 | 1.96 | 1.23% | $0.048 | 2.6 ms |
| 16 | 0.6948 | 643 | 1.08 | 2.47% | $0.096 | 3.7 ms |
| 32 | 0.7552 | 1,296 | 0.58 | 4.99% | $0.194 | 6.4 ms |
| full-context | 0.8727 | 22,576 | — | 100% | $3.386 | — |

¹ cost to feed the retrieved context to an answer LLM at $0.15/1M input tokens
(illustrative; retrieval itself is **$0** — no API, no network). Set your
provider's price with `--input-price-per-1m`.

**The norm-breaking result:** k=32 reaches **86.5% of the full-context accuracy
on 94% fewer tokens**; feeding full history to the answer model costs **17.4× more
per 1,000 questions** for a 16-point accuracy gain. Shallow retrieval (k=2) buys
the most answer-coverage per token (**5.4 correct-answer-units per 1k tokens**) —
pick your point on the curve by your model's context budget and your cost target.

This is the axis Mnemosyne is built to win: competitive recall at 1–5% of the
token load, locally, at zero retrieval cost, with p50 search in single-digit ms.
Accuracy-at-any-cost is the norm; **usable memory per token/dollar/millisecond**
is the frontier.

## 4. How this relates to published LOCOMO numbers

Published memory-system numbers (Mem0 paper, arXiv 2504.19413; all
with gpt-4o-mini answering and an LLM judge, n=1,540) — **not
directly comparable** to our retrieval track, reproduced here for
context only:

| system (their protocol: LLM answers + LLM judge) | overall J |
|---|---:|
| full-context (~26k tokens/conv) | 72.90 |
| Mem0-graph | 68.44 |
| Mem0 | 66.88 |
| Zep (as run by Mem0 — contested, see below) | 65.99 |
| best RAG config | 60.97 |
| LangMem | 58.10 |
| OpenAI memory | 52.90 |

Why we don't put our 0.6247 in that table: different judge
(substring vs LLM), different answerer (none vs gpt-4o-mini). A
substring match on retrieved context and an LLM-judged generated
answer measure different things, and pretending otherwise is how
benchmark fights start. The honest comparisons available today:

1. **Same-protocol baselines (this page):** Mnemosyne 0.6247 vs
   recency 0.2468 / random 0.2799 / ceiling 0.8727 — the substrate
   demonstrably does the retrieval work.
2. **Token economics (like-for-like):** Mem0 claims >90% token
   savings vs full context; Mnemosyne's FTS-8 uses **98.6% fewer
   tokens** than full context (319 vs 22,576) at 72% of the ceiling
   score, with no extraction LLM, no API, and no cloud.
3. **Apples-to-apples runs (runner shipped, blocked on keys):**
   `bench/locomo.py --substrate mem0` runs Mem0 through the *same*
   ingest/probe/judge pipeline, and `--llm-grounded --judge openai`
   reproduces the published LLM-judge protocol on Mnemosyne. Both
   need `OPENAI_API_KEY` at run time; neither was available in this
   run's environment. When those numbers exist they belong in this
   table with the same dated-JSON treatment.

> **Caveat on cross-vendor numbers.** Vendor-run LOCOMO scores for
> *competitors'* systems are contested in both directions: Mem0's
> issue against Zep's 84% claim ([zep-papers#5](https://github.com/getzep/zep-papers/issues/5),
> alleging a numerator/denominator mismatch on category 5 and
> reporting Zep at 58.44% under their config) and Zep's rebuttal
> (alleging Mem0 misconfigured Zep's roles and timestamps, reporting
> 75.14%). Treat every cross-vendor cell — including Zep's row above —
> as disputed, and prefer each vendor's number for its own system.
> Also note Mem0's published per-category *labels* appear permuted
> relative to the snap-research evaluation code's mapping (their
> table's n-counts pin category 1 to "single-hop" where the LOCOMO
> authors' `evaluation.py` treats category 1 as multi-hop); align by
> question counts (282/321/96/841) when comparing per-category cells.

## 5. LongMemEval — runner shipped, numbers pending

LongMemEval (arXiv 2410.10813, ICLR 2025) is the second standard:
500 questions over timestamped multi-session histories
(`longmemeval_s` ≈ 115k tokens/question), five abilities including
knowledge updates and abstention. Published reference points: the
paper's headline is that long-context LLMs drop ~30% accuracy on
sustained interactions; Zep (arXiv 2501.13956) reports 71.2% with
gpt-4o vs 60.2% full-context baseline, using ~1.6k context tokens vs
~115k.

`bench/longmemeval.py` ships now with:

- **session-level recall@k** (gold `answer_session_ids` found in
  top-k) and **turn-level recall@k** (`has_answer` turns found) —
  LongMemEval's standard judge-free memory-recall metrics;
- abstention (`*_abs`) handling identical to LOCOMO category 5;
- the same retrieval-mode baselines, top-k sweep, and
  latency/token/cost instrumentation;
- `--selftest` — a synthetic, schema-faithful 3-question fixture that
  validates the full pipeline with no dataset, network, or LLM
  (currently passing 7/7 checks).

The dataset is distributed via HuggingFace/Google Drive only, which
the sandbox that produced this page cannot reach — so unlike §3 we do
not publish guessed numbers. Produce them on any machine with HF
access:

```sh
mkdir -p bench/data
wget -P bench/data https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
python3 bench/longmemeval.py --selftest                # verify runner
python3 bench/longmemeval.py \
    --dataset bench/data/longmemeval_s_cleaned.json \
    --verbose --out bench/results/longmemeval-s-retrieval.json
```

The output JSON lands in the same dated format; PRs adding a
`docs/benchmark-results/<date>-longmemeval-*.json` from a real run
are welcome and will be credited.

## 6. Measuring the full stack (LLM-grounded)

Retrieval-only is the floor. To measure Mnemosyne + your model:

```sh
python3 bench/locomo.py --substrate mnemosyne --llm-grounded \
    --provider lmstudio --model <id> --verbose \
    --out bench/results/locomo-grounded.json
# add --judge openai --judge-model gpt-4o-mini to reproduce the
# published LLM-judge protocol (needs OPENAI_API_KEY)
```

In grounded runs the report also scores the 446 adversarial questions
via abstention detection, records per-question LLM latency separately
from search latency, and tracks prompt/response sizes so cost is
computable for any provider's pricing.

## 7. What these numbers say to improve (eval-gated roadmap)

1. **Multi-hop evidence recall (0.19)** — single-query FTS misses
   scattered evidence. Candidate: multi-probe retrieval / query
   decomposition. Gate: evidence recall@8 multi-hop ≥ 0.35 with no
   single-hop regression.
2. **Temporal (0.33)** — FTS5 can't do date arithmetic. Candidate:
   index `session_date_time` and resolve relative-time queries at
   probe time. Gate: temporal answer-in-context ≥ 0.45.
3. **Hybrid retrieval** — the RRF fusion (FTS5 + embeddings) already
   under evaluation should move paraphrase-heavy categories; promote
   only on this page's metrics, per the existing rule: nothing ships
   on vibes.
