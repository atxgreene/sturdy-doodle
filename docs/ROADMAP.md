# Mnemosyne Roadmap

Honest account of what is shipped, what is research-grade, and what is
aspirational. No AGI claims. Capabilities are listed as **verifiable**
(covered by a test or a reproducible demo), **experimental** (lands in
main but the behavior can regress without a loud signal), or
**aspirational** (on the list, not yet implemented).

The yardstick is "could a stranger reproduce this with `git clone && pip
install -e . && ./demo.sh` on a fresh laptop?" If yes, it's verifiable.

Current release: **v0.9.7** (`mnemosyne-harness`, Beta). The cognitive-OS
checklist is 5/5 ✓ (v0.7.0); the 12-component harness audit is 9 ✓ + 3
partial (v0.8.0); the Reflection → Instinct loop shipped in v0.9.

---

## Shipped and verifiable

| Area | Module(s) | Coverage |
|---|---|---|
| Telemetry (run + event model, secret redaction, FTS-friendly JSONL) | `harness_telemetry` | unit tests + `test-harness.sh` |
| Parameter sweeps + Pareto analysis | `harness_sweep`, `mnemosyne_experiments` | unit tests, `sweep_demo` |
| Scenario runner with expectations DSL | `scenario_runner` | unit tests |
| Environment snapshot (first-turn context) | `environment_snapshot` | integration test |
| **6-tier ICMS memory (L0–L5)** with ACT-R decay + Hebbian strength | `mnemosyne_memory` | unit tests |
| **L3 → L4 pattern compaction + audit** | `mnemosyne_compactor` | unit tests |
| **Reflection → Instinct loop (L0 fast-path distillation)** | `mnemosyne_instinct` | unit tests; brain injects L0 rows each turn |
| **Continuity Score benchmark (50 scenarios, 6 categories)** | `mnemosyne_continuity` | substrate dryrun 0.96 agg / 1.00 cross-session (v0.7.1) |
| Model backend (19 providers, stdlib-only, streaming) | `mnemosyne_models` | unit tests (mocked HTTP) |
| Text-embedded tool-call parsers (Hermes/Qwen/Mistral/Llama-3) | `mnemosyne_tool_parsers` | unit tests |
| Skill registry (agentskills.io-compatible) + builtin library | `mnemosyne_skills`, `mnemosyne_skills_builtin` | unit tests |
| 4-layer identity lock + audit mode | `mnemosyne_identity` | unit tests; 6/6 canonical slips rewritten |
| Brain routing orchestrator + local-model context adaptation | `mnemosyne_brain` | unit tests, mocked `chat_fn` |
| Triage / clustering + severity scoring | `mnemosyne_triage` | unit tests |
| **Meta-Harness proposer (rule-based)** | `mnemosyne_proposer` | unit tests |
| **Apply agent — closes the loop (execute + measure)** | `mnemosyne_apply` | unit tests |
| **Dream consolidation (stdlib + optional LLM summarizer)** | `mnemosyne_dreams` | unit tests; ~200 ms / 500 L3 memories |
| **Multi-persona inner dialogue (Planner / Critic / Doer / Evaluator)** | `mnemosyne_inner` | unit + brain-integration tests |
| **Goal stack (persistent TODO across sessions)** | `mnemosyne_goals` | unit tests |
| **Self-calibration / predictions** | `mnemosyne_predictions` | unit tests |
| **Routing-layer audit (Resolvers)** | `mnemosyne_resolver` | unit tests |
| **User-editable permission model + harness adapter** | `mnemosyne_permissions`, `mnemosyne_adapter_claude_code` | unit tests |
| Embeddings (hashed-BOW fallback + optional sentence-transformers) | `mnemosyne_embeddings` | unit tests |
| MCP bridge (both directions) | `mnemosyne_mcp` | protocol-level test (piped stdio) |
| Scenario auto-generator | `mnemosyne_scengen` | unit tests |
| Cost accounting (`mnemosyne-experiments cost`) | `mnemosyne_experiments` + `mnemosyne_models.cost_for` | unit tests |
| **Training bridge (ShareGPT export → Unsloth LoRA → deploy → A/B eval)** | `mnemosyne_train` | export/compress/deploy/eval covered; training needs the `[train]` extra |
| **Synthetic-data pipeline (parallel trajectory runner + prompt gen)** | `mnemosyne_batch`, `mnemosyne_datagen` | unit tests |
| **Avatar dashboard (29 derived traits) + static UI** | `mnemosyne_avatar`, `mnemosyne_ui` | derived-state tests; served by `mnemosyne-serve` |
| Long-running daemon (dream / triage / proposer crons) | `mnemosyne_serve` | HTTP smoke test |
| Jailbreak scenario suite (40 prompts) | `scenarios/jailbreak.jsonl` | run with `mnemosyne-pipeline` on your backend |
| **Hermes memory provider — runtime-validated** | `integrations/hermes/` | 8/8 checks on live Hermes v0.16.0 (see `docs/HERMES.md`) |
| 25-command CLI (via `pip install -e .`) | `pyproject.toml` `[project.scripts]` | CI install-smoke |
| GitHub Actions CI | `.github/workflows/ci.yml` | runs on every push |

Run `python3 tests/test_all.py` (full unit suite, <2 s on a laptop),
`bash test-harness.sh` (end-to-end integration), and `./demo.sh` (captured
multi-section transcript) to verify any row above.

### Published, eval-gated benchmarks

- **Retrieval recall@5 = 0.8704** on a deterministic probe set (recall@5 /
  MRR / hit@1 by category).
- **LOCOMO retrieval track = 0.6247** answer-in-context / **0.5009**
  evidence recall@8 over the standard 1,540 scored questions
  (`snap-research/locomo`), vs 0.2468 recency / 0.2799 random
  same-protocol baselines and a 0.8727 full-context ceiling — at 319
  context tokens/probe vs 22,576. Full reproducible setup, top-k
  sweep, and token/latency/cost: [`docs/BENCHMARKS_LOCOMO.md`](./BENCHMARKS_LOCOMO.md).
  (Supersedes the earlier 0.4849/1,986 figure, which divided the same
  run by a denominator including 446 unanswerable-without-a-model
  adversarial questions.)
- **Continuity = 0.96 aggregate / 1.00 cross-session** (substrate dryrun, v0.7.1).
- Throughput (single-thread reference): **0.21 ms/write**, **7.17 ms** search
  p50 over a 10K corpus, **1.20 ms (0.24%)** Brain wrapper overhead at
  realistic model latency.
- A `check_regression.py` gate fails any change that drops a tracked metric.
  Harness + datasets live in [`atxgreene/mnemosyne-lab`](https://github.com/atxgreene/mnemosyne-lab).

---

## Shipped but experimental

These work in the happy path but haven't been pressure-tested at scale.

- **Hybrid lexical + semantic retrieval (RRF fusion over FTS5 +
  embeddings).** Implemented and under evaluation; promoted to default only
  when the paraphrase-recall gate improves with no precision regression.
- **Dream consolidation with a live LLM summarizer.** Clustering and the
  stdlib fallback are verified; the model-backed summarizer is wired
  (`dreams.make_brain_summarizer(brain)`) but we have not yet measured
  whether the L2 abstracts improve downstream retrieval. That's the next A/B.
- **Inner-dialogue routing.** `should_deliberate` is a keyword + tag
  heuristic; it does not yet learn which turns benefit.
- **19 model providers.** OpenAI-compatible + Anthropic native + Ollama
  native are exercised via mocked HTTP; full cloud end-to-end runs require
  credentials and are gated in CI.
- **Training bridge end-to-end.** Export/compress/deploy/eval are covered;
  a full Unsloth LoRA run depends on your GPU and the `[train]` extra.

---

## Research-grade — documented, not yet shipped

Directions the codebase is *shaped to accept* but which are not implemented.

- **LLM-driven proposer.** Today `mnemosyne_proposer` uses hand-written
  rules over 5 cluster shapes. The Meta-Harness paper uses a coding agent to
  generate proposal code directly; the `PROP-NNNN-slug.md` filesystem
  interface is designed for drop-in replacement.
- **Manifold-aware L4 pattern memory.** Upgrade L4 from flat summaries to
  connected concept neighbourhoods (relations, boundaries, failure modes),
  guided by neural-geometry research. Gated by retrieval/continuity evals.
- **Learned inner-dialogue router.** A classifier over
  `(user_message, memory_context)` predicting whether deliberation improves
  accuracy. Needs a labelled scenario set first.
- **Long-context exploitation.** DeltaNet / Mamba-style models are supported
  as Ollama targets, but the brain does not yet exploit their context-length
  advantage.

---

## Aspirational (on the list, not yet scoped)

- **Behavioral coupling**: two Mnemosyne instances negotiating over a shared
  memory store. Needs a protocol spec first.
- **Dream-driven skill synthesis**: the dream loop proposes *new skill files*
  when it detects a recurring procedural pattern.
- **Continuous identity audit via statistical control charts**: treat the
  identity-slip rate as a process variable and alarm on drift.
- **Federated personal agents**: a consent-token wire protocol so one user's
  Mnemosyne can query another's.

---

## What this project is NOT

- **Not AGI, not a path to AGI.** These are engineering primitives for
  building usable local-first agents that are observable, tunable, and
  identity-stable.
- **Not a benchmarks-chaser.** We do not tune against SWE-bench,
  Terminal-Bench, or GAIA. The scenarios file is a smoke test, not a
  leaderboard submission. The benchmarks we *do* publish (retrieval, LOCOMO,
  continuity) measure the memory substrate, and ship with reproduction
  commands.
- **Not a replacement for the frontier labs' SDKs.** Mnemosyne wraps those
  APIs as 1 of 19 backends; it does not reimplement them. Want the frontier?
  Use the frontier. Want local-first observability around it? This is that
  layer.

---

## How to verify anything on this page

```sh
git clone https://github.com/atxgreene/Mnemosyne.git
cd Mnemosyne
pip install -e .
python3 tests/test_all.py          # full unit suite, <2s on laptops
bash test-harness.sh                # end-to-end integration assertions
./demo.sh                           # captured multi-section transcript
./validate-mnemosyne.sh             # environment health check
```

To exercise the Meta-Harness loop locally without real failures:

```sh
MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo \
  python3 -c "
import harness_telemetry as ht
rid = ht.create_run(model='test', tags=['roadmap-demo'])
with ht.TelemetrySession(rid) as sess:
    sess.log('identity_slip_detected', status='error',
             metadata={'slips': ['I am Claude'], 'count': 1})
ht.finalize_run(rid, metrics={})
"
MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo mnemosyne-triage scan --json
MNEMOSYNE_PROJECTS_DIR=/tmp/mnemo-demo mnemosyne-proposer --min-severity 0
ls /tmp/mnemo-demo/proposals/
```

If these run and emit proposals, the loop is verified for you. If they
don't, open an issue — the docs are the first thing to fix.
