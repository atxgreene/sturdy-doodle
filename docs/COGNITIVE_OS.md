# Cognitive OS checklist — live status

Machine-checkable version of `docs/VISION.md`. When all five rows
flip to ✓, Mnemosyne meets our operational definition of a
cognitive OS and the README tagline upgrades from "substrate" to
"OS". Until then, we're honest about which properties are partial.

**Last updated:** v0.7.0 (2026-04-16)

---

## The five properties

### 1. Persistent identity · **✓ shipped (v0.7)**

The agent's name, voice, values, and preferences must survive a
total working-context wipe.

**Shipped:**
- `mnemosyne_identity.MNEMOSYNE_IDENTITY` (1.4 KB preamble)
- `mnemosyne_identity.enforce_identity()` post-filter regex
- `IDENTITY.md` user extension
- `scenarios/jailbreak.jsonl` — 40 attack prompts measuring slip rate
- **v0.7:** L5 identity-memory tier — learned core values injected
  into the system prompt on every turn, independent of retrieval
  (`mnemosyne_brain._build_l5_identity_block`)
- **v0.7:** `mnemosyne_continuity.py` + `scenarios/continuity.jsonl`
  — 50-scenario Continuity Score benchmark with same-session,
  cross-session, and multi-plant subsets
- **v0.7:** Kind-differentiated decay in
  `KIND_DECAY_MULTIPLIERS` — `core_value`/`identity_value` decay at
  0.1× baseline so core values persist longer than operational notes

**Verify command:**
```sh
mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl
mnemosyne-continuity dryrun --scenarios scenarios/continuity.jsonl
# with a live model:
mnemosyne-continuity run --scenarios scenarios/continuity.jsonl \
    --model qwen2.5:7b --provider ollama --out /tmp/report.json
```

---

### 2. Layered memory with upward compaction · **✓ shipped (v0.7)**

Memory flows working → episodic → semantic → patterns → identity,
promoted by concept extraction + reinforcement, not just recency.
False patterns decay; reinforced patterns promote.

**Shipped:**
- L1 hot / L2 warm / L3 cold base tiers (`mnemosyne_memory`) — since
  grown to the full 6-tier ICMS (L0 instinct … L5 identity; see the
  v0.7 entries below for L4/L5)
- SQLite + FTS5 with `strength` column in schema
- `mnemosyne_dreams.consolidate()` — L2→L3 concept extraction via
  TF-IDF clustering (optional LLM summarizer)
- Tier promotion/demotion APIs (`promote`, `demote_unused`, `evict_l3_older_than`)
- Git-backed autobiography export (`mnemosyne-memory export --to-git`)
- **v0.7:** L4 patterns tier + `mnemosyne_compactor.py` — promotes
  recurring L3 clusters (Jaccard-thresholded token overlap,
  min_cluster_size configurable) into L4 pattern rows. Idempotent
  across re-runs via `source_ids` metadata check.
- **v0.7:** L5 identity tier — human-approved core values. Compactor
  refuses to write L5 directly (requires explicit API call or UI
  action; documented as the human-in-the-loop boundary).
- **v0.7:** `apply_decay()` with ACT-R base-level activation + kind
  multiplier (7-day half-life at `mult=1.0`, scaled by 0.1× for
  identity-class kinds, 3.0× for operational-class). Demotes rows
  below `strength=0.3`. `mnemosyne-memory decay` CLI.
- **v0.7:** Hebbian reinforcement on `search()` — every retrieval
  pushes strength toward 1.0 asymptotically (`amount=0.05`), so
  used memories naturally outrank unused ones.

**Verify command:**
```sh
mnemosyne-memory stats             # L0–L5 counts
mnemosyne-memory decay             # one ACT-R decay pass
mnemosyne-compactor run --dry-run  # preview L3 → L4 promotions
mnemosyne-compactor run            # actually promote
```

---

### 3. Observable self-regulation · **✓ shipped**

The runtime's internal state must be visible to the user *and*
influence runtime behavior through a documented pathway.

**Shipped:**
- Browser dashboard (`mnemosyne-serve` → http://127.0.0.1:8484/ui)
- SVG avatar with 16 derived traits, each mapping 1:1 to a number
  in `$PROJECTS_DIR/avatar.json`
- Bidirectional feedback (v0.4.1): 5 rules in
  `mnemosyne_avatar.FEEDBACK_RULES` adjust the BrainConfig at
  turn-start based on observable state
- Every adjustment logs `avatar_feedback` telemetry event with
  {rule, field, old, new, reason}

**Verify command:**
```sh
mnemosyne-avatar state
mnemosyne-serve &
open http://127.0.0.1:8484/ui
# Browse the trait grid on the left of the dashboard
```

---

### 4. Self-calibration · **✓ shipped (v0.6)**

The runtime must emit predictions as first-class events, observe
outcomes, adjust confidence over time. Calibration itself becomes a
measurable agent trait.

**Shipped:**
- `mnemosyne_predictions.py` — telemetry event types `prediction`
  + `outcome` with shared `prediction_id`
- Avatar trait: `calibration` = 1 − mean(|confidence − actual|)
- Triage rule: `prediction_overconfident` clusters when confidence
  ≥ 0.8 and error ≥ 0.5
- Horizon-bounded scoring: unresolved predictions past horizon auto-
  score 0.5 so callbacks don't stall the pipeline

**Verify command:**
```sh
mnemosyne-experiments show <run-id> --metric calibration
mnemosyne-triage scan    # surfaces prediction_overconfident clusters
```

---

### 5. Self-auditing · **✓ shipped**

The runtime must check its own routing layer, permissions model,
and identity integrity on a schedule. Drift surfaces as structured
events.

**Shipped:**
- `mnemosyne_resolver.check_resolvable()` — static audit of every
  skill's description quality, distinguishability, AGENTS.md refs
- `mnemosyne_triage` — 8 cluster rules including identity slips,
  unknown-tool-called, no-tool-dispatched, session errors
- `mnemosyne_proposer` — converts high-severity clusters into
  reviewable change proposals
- `mnemosyne_apply` — executes accepted proposals, marks outcomes
- `mnemosyne_permissions` — user-editable `permissions.md` gate
  checked before every skill dispatch

**Verify command:**
```sh
mnemosyne-resolver check
mnemosyne-triage scan --window-days 30
mnemosyne-proposer --min-severity 20
```

---

## Summary table

| # | Property | Status | Blocker |
|---|---|---|---|
| 1 | Persistent identity | ✓ (v0.7) | — |
| 2 | Layered memory + compaction | ✓ (v0.7) | — |
| 3 | Observable self-regulation | ✓ | — |
| 4 | Self-calibration | ✓ (v0.6) | — |
| 5 | Self-auditing | ✓ | — |

**All five green as of v0.7.0.** Mnemosyne now meets the operational
definition in `docs/VISION.md`. The README tagline in the next minor
release is permitted to say "cognitive OS" — not as marketing, but as
a claim backed by five commands a user can run.

Stability of the checklist is load-bearing. A ✓ → ✗ transition
requires a GitHub issue naming the specific verify command that fails
and a linked commit reverting the affected feature — we don't downgrade
the tagline quietly.

---

## Gatekeeping: who decides when a row flips

This file is versioned. Changes require a commit + a CHANGELOG entry.
No quiet rewrites. If you see a ✗ → ✓ transition in a commit, the
commit should also include:

1. The code that provides the capability.
2. The test that verifies the capability.
3. The verify command (from the row) that a human can run.

No capability gets a ✓ without a command a user can type to confirm
it. That's the whole point of the definition.

---

## Not on the list — but worth mentioning

Properties we sometimes get asked about that are *out of scope* for
our definition of cognitive OS:

- **Reasoning / chain-of-thought.** That's a *model* property. We
  wrap models; we don't do reasoning ourselves. Inner dialogue is
  structured multi-model orchestration, which is a cognitive-OS
  behavior, but the *reasoning itself* happens in the model.
- **Multi-agent coordination.** Interesting but orthogonal. Multiple
  Mnemosyne instances negotiating would be a *network* of cognitive
  OSes, not a requirement for one to qualify.
- **Learning model weights.** Explicitly out of scope — that's what
  `mnemosyne-train` exports data for, but fine-tuning itself is a
  different problem with its own tools (Unsloth).
- **Emotion / sentience.** Intentionally out. Mood-phase is a
  derived trait from telemetry, not a felt experience.

If a PR tries to add one of these to the cognitive-OS checklist,
reject it — or promote it to a new checklist with its own definition
and its own verify commands.
