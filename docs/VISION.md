# Vision: Mnemosyne as a cognitive OS

This document defines what "cognitive OS" means to us, operationally.
It's the target. It's not marketing — it's a checklist we can audit
against the code.

## The operational definition

A **cognitive OS** is an agent runtime with all five of:

1. **Persistent identity.** The agent's name, voice, values, and
   learned preferences survive a total working-context wipe. Measured
   by the Continuity Score (see `docs/BENCHMARKS.md` §3).

2. **Layered memory with upward compaction.** Memory flows from
   working (seconds) → episodic (days) → semantic (weeks) →
   patterns (months) → identity (lifetime). Compaction is driven by
   concept extraction + reinforcement, not just recency. False
   patterns decay; reinforced patterns promote.

3. **Observable self-regulation.** The runtime's internal state is
   visible to the user *and* influences runtime behavior through a
   documented pathway. No magic, no opaque personality engine. Every
   adjustment maps to one telemetry event.

4. **Self-calibration.** The runtime emits predictions as first-class
   events, observes outcomes, and adjusts confidence over time.
   Calibration score (1 − mean prediction error) is itself a
   measurable agent trait.

5. **Self-auditing.** The runtime checks its own routing layer,
   permissions model, memory compaction, and identity integrity on a
   schedule. Drift surfaces as structured events the user can act
   on, not silent degradation.

## Why this bar, not a lower one

There are many weaker definitions of "cognitive" that would make
the claim easier and emptier:

- "Uses memory retrieval" (everyone does this)
- "Chains tool calls" (everyone does this)
- "Learns user preferences" (by "learns" they usually mean stores)

We set the bar at *measurable self-directed behavior*: the agent is
cognitive if it can observe itself, compact its own experience into
higher-order structure, test its predictions against reality, and
route its own attention — and if we can audit each of those
capabilities with a specific command.

## Why this bar, not a higher one

There are also stronger claims we explicitly refuse:

- **Consciousness.** Unfalsifiable. Save the word.
- **AGI.** Marketing-driven, definitionally unstable, dangerous
  framing that attracts grift.
- **Sentience / awareness / emotions.** We show mood-phase as a
  derived trait, but mood-phase is a function of telemetry, not a
  felt experience. Users who forget this lose calibration.

The cognitive-OS bar is: *does the system exhibit the five
properties above, each verifiable by a specific command?* Nothing
more.

## Current status

See `docs/COGNITIVE_OS.md` for the live checklist. As of v0.5.0:

| Property | Status | Verify with |
|---|---|---|
| Persistent identity | partial | `mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl` + (L5 memory, v0.7) |
| Layered memory + compaction | partial | L1/L2/L3 shipped (`mnemosyne-memory stats`); L4/L5 in v0.6–0.7 |
| Observable self-regulation | ✓ | `mnemosyne-serve` + `/ui` + `mnemosyne-avatar state` |
| Self-calibration | ✗ → ✓ in v0.6 | `mnemosyne-experiments show <run> --metric calibration` |
| Self-auditing | ✓ | `mnemosyne-resolver check && mnemosyne-triage scan` |

When all five are ✓, Mnemosyne is a cognitive OS by the definition on
this page. That's a claim we can defend.

## How this translates to marketing

- **Technical pages** (README quickstart, API docs, CHANGELOG) lead
  with concrete capabilities: "local-first LLM agent framework,
  stdlib only, 22 console scripts, 4-layer identity lock." These
  stay grounded.
- **Positioning / vision pages** (this doc, `docs/ARTICLE.md`) use
  "cognitive OS" as the organizing frame with the definition above
  pinned at the top.
- **README tagline** gets one extra line: *"The cognitive substrate
  for local-first agents."* Substrate, not OS — acknowledges we're
  the layer underneath, not claiming the whole stack.
- **Anti-patterns to avoid:** "your personal AGI," "fully conscious
  agent," "mind-like," "true intelligence." If you see those words
  in a pull request, reject it.

## Why "substrate" and not "OS" in the tagline

Because on day one of adopting Mnemosyne you're not running an OS —
you're pointing a dashboard at a local model. The *ambition* is OS;
the *shipping product* is substrate. Positioning should match what
the user actually experiences in the first ten minutes, not what
the architecture enables on day 100.

When L4 patterns + L5 identity + self-calibration all ship (v0.6–0.7),
the tagline can honestly become: *"A cognitive OS for local-first
agents."* Not a day before.

## What happens when we hit all five

When the cognitive-OS checklist reads ✓ across all five rows:

1. `docs/COGNITIVE_OS.md` gets the green banner at the top.
2. README tagline upgrades from "substrate" → "OS".
3. We cut v1.0.0 and publish to PyPI without qualifiers.
4. Article gets a follow-up: "We now meet the operational definition
   of a cognitive OS. Here's the evidence."
5. We don't stop. The bar just shifts: next target is
   *measurably-improving-over-time* (the Continuity Score climbing
   week-over-week as the agent accumulates).

Everything past that is research.

## This is not aspirational hand-waving

Every line in the "operational definition" section maps to a
command you can run on the installed package:

| Claim | Command |
|---|---|
| Persistent identity | `mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl` |
| Layered memory | `mnemosyne-memory stats` + `mnemosyne-dreams` |
| Observable self-regulation | `mnemosyne-serve` → dashboard |
| Self-calibration | `mnemosyne-experiments show <run> --metric calibration` (v0.6) |
| Self-auditing | `mnemosyne-resolver check` + `mnemosyne-triage scan` + `mnemosyne-apply` |

If a future maintainer adds a property that *looks* cognitive but
doesn't map to a verify-able command, it belongs in `ROADMAP.md`
under "aspirational" until it does.
