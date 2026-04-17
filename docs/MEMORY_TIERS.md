# Mnemosyne memory tiers — the canonical reference

This is the single source of truth for how many tiers Mnemosyne has,
what they're called, and what they do. If any other doc disagrees,
this page wins and the other doc has a bug.

The repo has **one** memory layer: a single SQLite table called
`memories` with `tier`, `kind`, and `strength` columns (plus FTS5
indexing). Everything else — "dreams," "compactor," "instinct
distiller" — is an *offline process* that reads the lower tiers and
writes to higher or lower ones. No vector DB. No graph. No second
backend.

## The six tiers (v0.9+)

| Constant | Tier | Name | Checked first? | Populated by | Decay |
| :--- | :---: | :--- | :---: | :--- | :--- |
| `L0_INSTINCT`  | 0 | instinct | ✓ fastest | `mnemosyne_instinct.distill()` | medium (0.4×) |
| `L1_HOT`       | 1 | hot      | working memory | live writes | fast |
| `L2_WARM`      | 2 | warm     | default write tier | live writes | medium |
| `L3_COLD`      | 3 | cold     | long-term | `demote_unused()` from L2 | slow |
| `L4_PATTERN`   | 4 | pattern  | learned behaviors | `mnemosyne_compactor.compact_patterns()` | slow (0.5×) |
| `L5_IDENTITY`  | 5 | identity | injected every turn | human approval only | very slow (0.1×) |

Retrieval order on every turn: **L0 → L1 → L2 → L3 → L4 → L5**, with
L5 also injected into the system prompt regardless of the user query
(because identity shouldn't be query-relevance-gated).

## The Reflection → Instinct loop (v0.9)

Three offline processes collectively constitute "reflection":

1. **`mnemosyne_dreams.consolidate()`** — TF-IDF clustering of L2/L3
   content into L3 abstractions. Runs every N turns.
2. **`mnemosyne_compactor.compact_patterns()`** — Jaccard clustering
   of aged L3 rows into L4 pattern rows. Runs on a cron or manually.
3. **`mnemosyne_instinct.distill()`** — Jaccard clustering of recent
   user-pattern-bearing rows (from L5 and below) into L0 user_instinct
   rows. This is the Reflection → Instinct transfer.

Process #3 is what makes the v0.9 architecture a "cognitive OS" by
cognitive-science standards: slow deliberate consolidation gradually
shapes fast automatic reaction. Procedural memory in humans; L0
instinct in code.

## What the tier system is NOT

Things external LLM summaries have (incorrectly) claimed:

- **Not "L4 = archival / L5 = meta-memory."** That terminology does
  not appear anywhere in the code. If you read it in a generated
  summary, the summary is wrong.
- **Not a vector database.** We use SQLite FTS5 full-text search
  with an AND → OR recall fallback. Optional embeddings exist
  (`mnemosyne_embeddings`) but are not required for the default
  substrate.
- **Not a knowledge graph.** No nodes, no edges, no PageRank. Rows
  cluster by Jaccard token overlap into patterns; relationships are
  implicit in shared vocabulary.
- **Not tiered by LLM cost.** Tier number reflects access speed and
  decay rate, not which model sees which tier. All tiers are
  readable from the same SQLite queries.
- **Not based on Atkinson-Shiffrin literally.** It's *inspired by*
  Atkinson-Shiffrin multi-store + ACT-R base-level activation. The
  analogy is pedagogical scaffolding; the load-bearing claim is the
  `docs/COGNITIVE_OS.md` operational checklist, not the human-memory
  mapping.

## What each tier actually holds (examples)

- **L0 instinct**: *"[INSTINCT x 4] terse, direct, responses, user,
  prefers: user prefers terse direct responses no hedging style."*
  (cluster signature + representative row; ~10-20 of these at most,
  total ~500 tokens)
- **L1 hot**: *"Tool `obsidian_search` failed with 'timeout' when
  called with args={'query': 'project alpha'}. Consider an
  alternative or guard the call."* (tool-feedback learning row)
- **L2 warm**: *"User said their favorite async runtime is tokio."*
  (any normal write defaults here)
- **L3 cold**: *"Meeting with team on April 5 covered Q2 roadmap."*
  (demoted from L2 after `threshold_days` of no access)
- **L4 pattern**: *"[PATTERN x 7] timeout, api, network, call,
  error: pattern of network timeouts in obsidian_search."*
  (promoted from L3 clusters by the compactor)
- **L5 identity**: *"I prioritize honesty over politeness — I will
  disagree with a user when warranted."* (written only via explicit
  API calls; never auto-promoted; human-approved)

## Decay multipliers

`mnemosyne_memory.KIND_DECAY_MULTIPLIERS` (module-scope; override at
import time):

| kind | multiplier | 7-day half-life? |
|---|---:|---|
| `core_value`, `identity`, `identity_value` | 0.1× | 70 days |
| `preference` | 0.3× | ~23 days |
| `user_instinct`, `trait` | 0.4×, 0.3× | ~17-23 days |
| `pattern` | 0.5× | 14 days |
| `interest` | 0.8× | ~9 days |
| `fact`, `dream_abstract`, `project` | 1.0× | 7 days |
| `turn`, `event` | 2.0× | 3.5 days |
| `failure_note`, `tool_result` | 3.0× | ~2.3 days |

Rows below `strength = 0.3` get demoted (L0 → L4, L4 → L3, L1/L2 →
next tier when strength < 0.1). Rows never auto-delete — the
substrate never forgets; it just downgrades access priority.

## Verify this doc against the code

```python
>>> from mnemosyne_memory import (
...     L0_INSTINCT, L1_HOT, L2_WARM, L3_COLD, L4_PATTERN, L5_IDENTITY,
...     KIND_DECAY_MULTIPLIERS, _TIER_NAMES,
... )
>>> L0_INSTINCT, L1_HOT, L2_WARM, L3_COLD, L4_PATTERN, L5_IDENTITY
(0, 1, 2, 3, 4, 5)
>>> _TIER_NAMES
{0: 'L0_instinct', 1: 'L1_hot', 2: 'L2_warm', 3: 'L3_cold',
 4: 'L4_pattern', 5: 'L5_identity'}
```

If any of the above assertions fails, that's the canonical signal
this doc is stale and should be updated. Nothing else here is
normative; the Python constants are.
