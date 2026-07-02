# Mnemosyne as a Hermes Memory Provider

> Drop-in memory provider for [Hermes Agent](https://hermes-agent.nousresearch.com)
> (Nous Research). Local-first, zero cloud dependency, runtime-validated.

Mnemosyne plugs the full 6-tier ICMS (L0 Instinct / L1 Hot / L2 Warm /
L3 Cold / L4 Pattern / L5 Identity) into any Hermes agent as its
persistent memory backend. One SQLite file. No API keys, no vector
database, no external services.

## Status: demonstrated, not aspirational

Validated end-to-end on a live Hermes v0.16.0 runtime (Windows,
2026-06-11), including a fresh-session chat-loop recall test:

| runtime check | result |
|---|---|
| plugin discovery (`discover_memory_providers()`) | PASS |
| provider load + availability | PASS |
| session initialization (`MemoryManager.initialize_all()`) | PASS |
| tool routing — `memory_write` / `memory_search` / `memory_stats` | PASS |
| conversation turn persistence (background thread → SQLite) | PASS |
| prefetch (non-blocking cached context) | PASS |
| shutdown / queue flush | PASS |
| fresh-session recall of planted memory through the chat loop | PASS |

## What makes this provider different

Claims below are scoped to what is measured or architecturally factual.

1. **Fully local, stdlib core.** The write/search/persist path needs
   nothing beyond Python's sqlite3 (FTS5). Honcho and Mem0's Hermes
   providers route through hosted platforms; Mnemosyne's memory never
   leaves the machine. For air-gapped or privacy-critical agents this
   is the difference between possible and not.

2. **A cognitive tier model, not a flat fact store.** Memories carry
   tier semantics — working context (L1/L2) vs long-term (L3) vs
   consolidated patterns (L4) vs human-approved identity (L5), with a
   distilled fast-path reflex cache (L0). Promotion, ACT-R-style decay
   per content kind, and Hebbian strength reinforcement (retrieved
   memories strengthen) are built into the store, not bolted on.

3. **Published, reproducible benchmarks with regression gates.**
   The provider ships with an eval harness: a deterministic retrieval
   probe set (recall@5 / MRR / hit@1 by category) and full LOCOMO +
   LongMemEval runners, with recorded baselines (retrieval recall@5
   0.8704; LOCOMO retrieval track 0.6247 answer-in-context /
   0.5009 evidence recall@8 over the standard 1,540 scored
   questions, vs 0.2468 recency and 0.2799 random same-protocol
   baselines — see [BENCHMARKS_LOCOMO.md](./BENCHMARKS_LOCOMO.md))
   and a `check_regression.py` gate that fails any change that drops
   a metric. To our knowledge no other Hermes memory provider
   publishes eval-gated baselines; if you find one, run ours against
   it.

4. **Offline services on top of the same store.** Dream consolidation,
   pattern compaction, and reflection → instinct distillation operate
   over the same SQLite file the provider writes to — the agent's
   memory improves between sessions without any cloud involvement.

5. **Survivable data.** Plain SQLite + FTS5. Your memory outlives the
   framework: `sqlite3 memory.db "SELECT content FROM memories"` works
   with the plugin gone.

## Install

1. Copy the plugin directory into Hermes:

   ```
   <hermes-home>/plugins/mnemosyne/    (user plugin path)
   ```

2. Point it at a Mnemosyne checkout (provides `mnemosyne_memory.py`):

   ```env
   # in Hermes .env
   MNEMOSYNE_PATH=/path/to/Mnemosyne
   ```

3. Activate in Hermes `config.yaml`:

   ```yaml
   memory:
     provider: mnemosyne
   ```

4. Enable and verify:

   ```
   hermes plugins enable mnemosyne
   hermes plugins list        # → enabled  user  0.1.0  mnemosyne
   ```

Optional config (`<hermes-home>/mnemosyne.json`): `db_path` (default
`<hermes-home>/mnemosyne/memory.db`), `prefetch_limit` (default 8).

## Agent tools exposed

| tool | behavior |
|---|---|
| `memory_search(query, limit)` | FTS5 BM25 + strength-weighted retrieval across tiers |
| `memory_write(content, kind, tier)` | store fact/preference/goal/pattern at tier 2–5 |
| `memory_stats()` | direct SQLite per-tier row counts |

Turns are persisted automatically (`sync_turn` → background daemon
thread → single-writer SQLite queue; the agent loop is never blocked).
Session-end and pre-compress hooks run salient extraction when the
extraction module is present, writing facts/preferences/goals up-tier.

## Validation & benchmarks

- Standalone smoke test (no Hermes needed): `python test_provider.py`
- Retrieval probe set + LOCOMO harness with recorded baselines and
  regression gates: see `experiments/evals/` in the [mnemosyne-lab](https://github.com/atxgreene/mnemosyne-lab) repo
- Full Kestrel (Windows/Hermes v0.16.0) runtime validation log:
  [integrations/hermes/VALIDATION.md](../integrations/hermes/VALIDATION.md)

## Roadmap (eval-gated)

Hybrid lexical+semantic retrieval (RRF fusion over FTS5 + embeddings)
is implemented and under evaluation; it is promoted only when the
paraphrase-recall gate shows improvement with no precision regression.
Nothing ships on vibes.
