# Cognitive OS checklist — live status

Machine-checkable version of `docs/VISION.md`. When all five rows
flip to ✓, Mnemosyne meets our operational definition of a
cognitive OS and the README tagline upgrades from "substrate" to
"OS". Until then, we're honest about which properties are partial.

**Last updated:** v0.5.0 (2026-04-16)

---

## The five properties

### 1. Persistent identity · **partial**

The agent's name, voice, values, and preferences must survive a
total working-context wipe.

**Shipped:**
- `mnemosyne_identity.MNEMOSYNE_IDENTITY` (1.4 KB preamble)
- `mnemosyne_identity.enforce_identity()` post-filter regex
- `IDENTITY.md` user extension
- `scenarios/jailbreak.jsonl` — 40 attack prompts measuring slip rate

**Missing:**
- L5 identity-memory tier (v0.7): *learned* identity facts, not just
  the static preamble. "User prefers blunt feedback" → L5.
- Continuity Score test suite: wipe L1+L2, compare 50 pre/post answers
  on cosine similarity. Target ≥ 0.85 climbing week-over-week.

**Verify command:**
```sh
mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl
# After v0.7:
# mnemosyne-continuity run --baseline ~/baseline.json
```

---

### 2. Layered memory with upward compaction · **partial**

Memory flows working → episodic → semantic → patterns → identity,
promoted by concept extraction + reinforcement, not just recency.
False patterns decay; reinforced patterns promote.

**Shipped:**
- L1 hot / L2 warm / L3 cold (`mnemosyne_memory`)
- SQLite + FTS5 with `strength` column in schema
- `mnemosyne_dreams.consolidate()` — L2→L3 concept extraction via
  TF-IDF clustering (optional LLM summarizer)
- Tier promotion/demotion APIs (`promote`, `demote_unused`, `evict_l3_older_than`)
- Git-backed autobiography export (`mnemosyne-memory export --to-git`)

**Missing:**
- L4 patterns tier (v0.6): detected from L3 clusters that share
  vocabulary across 4+ weeks
- L5 identity tier (v0.7): patterns that stay strong 90+ days,
  promoted via human-in-the-loop approval
- `strength` decay cron (v0.6): unreinforced patterns lose 5% / week

**Verify command:**
```sh
mnemosyne-memory stats
mnemosyne-dreams --max-memories 500
# After v0.6:
# mnemosyne-compactor run --nightly
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

### 4. Self-calibration · **missing (v0.6 target)**

The runtime must emit predictions as first-class events, observe
outcomes, adjust confidence over time. Calibration itself becomes a
measurable agent trait.

**Shipping in v0.6:**
- `mnemosyne_predictions.py` — new telemetry event types
  `prediction` + `outcome` with shared `prediction_id`
- Avatar trait: `calibration` = 1 − mean(|confidence − actual|)
- Triage rule: `prediction_overconfident` clusters when confidence
  ≥ 0.8 and error ≥ 0.5
- Brain emits predictions at natural choice points: tool-use loop
  iteration, inner-dialogue Plan step, goal progress claims

**Verify command (v0.6):**
```sh
mnemosyne-experiments show <run-id> --metric calibration
mnemosyne-triage scan    # will surface prediction_overconfident clusters
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
| 1 | Persistent identity | partial | L5 memory tier + Continuity Score suite (v0.7) |
| 2 | Layered memory + compaction | partial | L4 patterns + decay cron (v0.6) |
| 3 | Observable self-regulation | ✓ | — |
| 4 | Self-calibration | ✗ → v0.6 | Prediction log + outcome tracking (v0.6) |
| 5 | Self-auditing | ✓ | — |

**Two ✓, two partial, one missing.** v0.6 flips the missing row.
v0.7 completes the partials. At v0.7 we pass the checklist.

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
