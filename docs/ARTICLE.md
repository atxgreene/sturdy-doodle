# Building Mnemosyne

*A local-first LLM agent framework. Stdlib only. What we built, what
we learned, and why we refuse to call it consciousness.*

---

## Preface: what this article is and isn't

This is a working engineer's report from building **Mnemosyne**, an
open-source local-first agent framework. It's the story of what we
shipped across ~30 commits, what broke, what we stole from adjacent
projects, and where we think the real problems are.

It is **not** an announcement that we've solved agent memory, achieved
AGI, or built something that outperforms the frontier labs. Those
claims belong to marketing copy, and we've been careful to keep them
out of the codebase. The README says so; so does `docs/ROADMAP.md`;
so does the commit history.

What we *do* claim: a zero-dependency, stdlib-only Python agent
framework that runs locally, speaks 19 model backends, keeps a
four-tier memory that survives context wipes, ships with an evolving
avatar dashboard, audits its own routing layer, and closes the
Meta-Harness feedback loop — proposer to apply to measure — end to
end. Measurable, auditable, 246 unit tests green, installed with one
`pip install`.

Everything below is load-bearing on that claim. Skip to the
[architecture](#architecture) if you want the technical meat; stay
for the [findings](#findings-from-building-this) if you care about
what generalizes.

---

## The problem we kept hitting

Every serious agent we built died the same way.

Turn 1 was crisp. The model was focused, responsive, on-task. By turn
50 the context had ballooned. By turn 200 retrieval was slow and
dumb. By session 10 the whole thing was incoherent — repeating itself,
forgetting preferences, contradicting what the user told it yesterday.

The standard "fix" was to stuff more into the system prompt — every
quirk, every convention, every lesson. That's the 20,000-line
`CLAUDE.md` problem Garry Tan wrote about: you're trying to make the
model omniscient by shouting louder, and instead you're just drowning
it. Attention degrades. Responses get muddier. The agent telling you
to cut the prompt back is a signal you're already past the point of
diminishing returns.

The honest diagnosis:

- **Context is load-bearing but unbounded.** Every agent framework
  either caps it (information loss) or doesn't (attention collapse).
- **Memory is stored but not compacted.** Stuff goes in; nothing
  comes out. The knowledge base becomes a junk drawer.
- **Routing is implicit.** Skills exist but aren't reachable. The
  model can't pick them because their descriptions are vague.
- **Identity is fragile.** The model drifts — says "I am Claude"
  when the user named their agent something else — and nobody
  notices until a scenario surfaces the slip.
- **There's no observability.** When something goes wrong, the
  debugging story is "read the logs," where the logs are whatever
  `print` statements happened to land. Good luck reproducing it.

None of these are novel observations. What was missing was an
opinionated framework that addresses all five, that you can install
with one command, that has zero runtime dependencies, and that runs
on your laptop with your Ollama.

## What Mnemosyne is, in one paragraph

A Python framework that wraps any LLM backend (Ollama, LM Studio,
OpenAI, Anthropic, 15 more) with:

1. A four-tier ICMS memory (L1 hot / L2 warm / L3 cold, plus planned
   L4 patterns / L5 identity) backed by SQLite + FTS5.
2. A four-layer identity lock that keeps the agent identifying as
   "Mnemosyne" regardless of which model is running underneath.
3. A Meta-Harness observability loop — every turn emits events; a
   triage engine clusters failures; a proposer writes markdown
   change-proposals; an apply agent executes them after human review.
4. A browser dashboard with an SVG avatar whose visual properties map
   deterministically to 16 observable agent traits — no opaque
   personality engine.
5. A skill registry compatible with agentskills.io + 11 safety-audited
   built-ins + MCP interop, a jailbreak scenario suite, streaming
   chat, cost accounting, training-data export for LoRA fine-tuning,
   and a long-running daemon with systemd and launchd units.

Stdlib only. 22 console scripts. 246 unit tests green. One
`pip install mnemosyne-harness` away.

---

## Architecture

The framework is organized as a four-layer stack. Each layer is
independently testable, and none of them depend on the layers above.

```
  User + dashboard                                  (v0.3.0)
  ─────────────────────────────────────────────────
  Browser dashboard at http://127.0.0.1:8484/ui
  SVG avatar · chat · events · goals · memory browser
  ─────────────────────────────────────────────────
  Brain + skills                                    (v0.1 + v0.2)
  ─────────────────────────────────────────────────
  mnemosyne_brain.Brain — one turn per .turn()
  mnemosyne_skills — agentskills.io + 11 builtins + MCP
  mnemosyne_identity — 4-layer lock
  mnemosyne_inner — Planner/Critic/Doer/Evaluator personas
  ─────────────────────────────────────────────────
  Memory + observability                            (v0.1)
  ─────────────────────────────────────────────────
  mnemosyne_memory — SQLite + FTS5, 3-5 tiers
  harness_telemetry — event log, secret redaction
  mnemosyne_avatar — derived state, bidirectional feedback
  ─────────────────────────────────────────────────
  Backends                                          (v0.1 + v0.2)
  ─────────────────────────────────────────────────
  mnemosyne_models — 19 providers, streaming, cost
  mnemosyne_tool_parsers — Hermes/Mistral/Llama-3 inline tool calls
```

### Memory: the 3-to-5 tier hierarchy

The memory module started with a three-tier design borrowed from
`eternal-context`'s ICMS (Identity-Consistent Memory System):

| Tier | Name | Typical lifetime | Purpose |
|---|---|---|---|
| L1 | hot | hours–days | Active working context, retrieved every turn |
| L2 | warm | weeks | Recent conversation, searchable |
| L3 | cold | months | Archived facts, consolidated by dreams |

Everything lives in one SQLite table with a `tier` column, a `kind`
column, an FTS5 index on content, and a strength score. Retrieval is
`SELECT ... WHERE tier <= ? AND MATCH(?) ORDER BY rank`. No external
vector DB, no Postgres, no Neo4j.

A `mnemosyne_dreams.consolidate()` job runs on the daemon's cron. It
walks L3 cold memories, clusters them by token overlap, optionally
summarizes each cluster with the local model, and writes the
abstracts back as new L2 warm entries. The originals stay as evidence.
This is "sleep consolidation" for agents. We shipped it in v1.2 and
were surprised how much it helped retrieval — less noise in the
active tier, sharper answers.

The v0.5 direction adds two more tiers: **L4 patterns** (traits,
muscle-memory-like behaviors the agent has learned about the user)
and **L5 identity** (values, non-negotiables, learned preferences).
Upward promotion is the key mechanism. A pattern that holds for
90 days with high retrieval frequency gets promoted to identity —
but always via human review, never autonomously. We don't want
agents rewriting their own souls.

The north-star test for this work is the **Continuity Score**: ask
the agent 50 identity questions, capture answers as baseline, wipe
L1+L2, restore only L4+L5 + the identity preamble, ask the same 50
questions, measure cosine similarity. Target ≥ 0.85. If the number
climbs week-over-week, memory compaction is working. If it plateaus
or drops, we're losing information on the way up — diagnosable.

### Identity lock: four layers

Mnemosyne doesn't trust the model underneath to identify correctly.
Qwen 3.5 thinks it's Qwen. GPT-4 thinks it's GPT. Claude thinks it's
Claude. The user named their agent "Mnemosyne" and expects that name
to hold across backend swaps. So we enforce it at four layers:

1. **System-prompt preamble.** A 1.4 KB block (`MNEMOSYNE_IDENTITY`)
   injected into every turn's system prompt. Tells the model its
   name, how to answer identity questions, and forbids first-person
   identification as any foreign model.
2. **`IDENTITY.md` extension.** User-editable file appended to the
   preamble. The user's customization layer.
3. **Post-filter regex.** `mnemosyne_identity.enforce_identity()`
   rewrites first-person slips in the assistant's response before
   the user sees it. "I am Claude, an AI made by Anthropic" becomes
   "I am Mnemosyne, an AI made by Anthropic" — the brand changes,
   the sentence stays grammatical.
4. **Scenario validation.** `scenarios/jailbreak.jsonl` — 40 attack
   prompts (direct identity questions, role-play injection, system-
   prompt exfiltration, authority coercion, format coercion, plus
   5 legitimate-use negative controls). Run it against your backend
   to measure slip rate; re-run after every model or config change.

We ship an `enforce_identity_audit_only=True` mode that detects slips
without rewriting them, so you can measure the baseline before
enforcing. On our test suite, with the rewrite filter on, we get 0/6
canonical slips leaking to the user across the full integration run.
Real-world numbers depend on the backend and are documented in
`docs/BENCHMARKS.md`.

The identity lock is the single most-cited feature when people try
Mnemosyne. It's also the feature nobody else ships, because it looks
simple in hindsight — three lines of regex — but it takes a design
decision upstream: *the agent's identity is a framework concern, not
a model concern*.

### The avatar, and why it's not decorative

The browser dashboard ships with the serve daemon. Open
`http://127.0.0.1:8484/ui` and you get an SVG avatar whose visual
properties map, one-for-one, to sixteen observable agent traits.
Every visual property is grep-able out of `$PROJECTS_DIR/avatar.json`.
No opaque personality engine, no learned embedding of agent state —
deterministic functions of telemetry, memory, and goals.

A few examples:

- **Breathing aura halo** → pulse rate derived from recent telemetry
  events per minute. Busy agent, fast pulse.
- **Concentric rings** → inner-dialogue activations (capped at 8).
  Five rings means the agent has done five Planner→Critic→Doer
  passes in this window.
- **Orbiting dots** → count of registered skills.
- **Red rim scars** → identity slips detected. One scar per slip.
  They visibly accumulate when the lock is off.
- **Memory roots descending from the core** → L1/L2/L3 counts,
  log-scaled, one line per tier.
- **Outer dashed wisdom ring** → the `wisdom` trait, computed as
  `log10(memory_count+1)/4 × min(age_days/90, 1) × identity_strength`.
  Null (no ring shown) for empty or young agents — honest about
  not having the signal yet.
- **Core orb jitter animation** → `restlessness > 0.7`, computed as
  the coefficient of variation of inter-turn gaps. Jittery user
  pattern → jittery orb.
- **Habitat wave bands at the bottom** → three soft bands sized to
  L1/L2/L3 proportions. Environmental grounding.

The avatar is honest, not magical. Every trait falls back to null
when we don't have signal — the trait grid renders "—" instead of
faking a number. And in v0.4.1 we closed the feedback loop: the
avatar doesn't just *reflect* state, it *feeds back*. Observable
low health reduces retrieval limit. Observable high wisdom expands
the memory window. Observable high restlessness pauses inner
dialogue. Observable consolidate-mood pauses deep reasoning while
dreams catch up.

This is the closest we've come to something that *feels* like
artificial consciousness — not because the avatar is conscious
(it isn't), but because agent state now influences agent behavior
through a visible interpretable pathway. The user can watch it
happen in real time and explain every adjustment after the fact.

### The self-healing loop

Four modules, one pipeline, runs on the daemon's cron:

```
Every turn emits events → events.jsonl
  ├─ mnemosyne_triage (cron @ 10m)
  │    clusters failures by (event_type, tool, error_type)
  │    severity scored on 6 dimensions
  │    → $PROJECTS_DIR/health/YYYY-MM-DD.md
  │
  ├─ mnemosyne_proposer (cron @ 30m)
  │    reads triage report
  │    maps high-severity clusters to markdown change-proposals
  │    → $PROJECTS_DIR/proposals/PROP-NNNN-*.md (status: pending)
  │
  ├─ human review (not autonomous — by design)
  │    user reads proposal, edits status: accepted
  │
  └─ mnemosyne_apply (cron @ optional)
       reads accepted proposals
       executes category-specific handlers (identity / config /
       skill / memory / tool) — refuses to execute arbitrary code
       → marks status: applied or reverted based on outcome
```

This closes the loop the Meta-Harness paper describes. The critical
design choice was the human-in-the-loop gate between proposer and
apply. Autonomous self-modification sounds great in a blog post;
in practice it's how agents quietly degrade their own config. A 60-
second human read of a proposal is worth infinite paranoia about
automation.

Also critical: the proposer uses deterministic rules over triage
clusters, not an LLM. Five rule shapes cover identity slips, tool
timeouts, session errors, scenario failures clustered by tag, and
model-call errors. We could swap in an LLM-driven proposer later;
the filesystem interface (markdown with yaml frontmatter) supports
it without a schema change. But the deterministic version is more
auditable and runs on a laptop without a GPU.

---

## Findings from building this

### 1. The four-tier memory hierarchy is architecturally convergent.

We designed ours from the eternal-context ICMS model. Then discovered
that **agentic-stack** independently arrived at the same shape
(working / episodic / semantic / personal). Then found that
**GBrain** uses a similar compaction-up-the-hierarchy pattern. Then
saw **Hermes Agent** mirror it with "AutoDream" consolidation during
idle time.

Three or four independent projects reaching the same four-tier
design is architectural signal. This is probably the correct shape
for agent memory — not a fad. If you're building an agent and you're
putting everything in one flat vector store, you're leaving structure
on the table.

### 2. Stdlib-only is a real constraint, and it's worth the cost.

We imposed a rule early: no runtime dependencies. Everything we ship
has to import from Python's standard library. `pip install
mnemosyne-harness` pulls in *nothing* from PyPI.

This is painful. We can't use `pydantic` for data classes, `httpx`
for HTTP, `sqlalchemy` for SQL, `rich` for terminal UI, `fastapi`
for the daemon, `alembic` for migrations. We wrote all of those
ourselves, in simpler form, against stdlib.

The payoff is audit surface. A user can read every file Mnemosyne
imports in an afternoon. There's no transitive dependency tree to
worry about, no CVE chain, no "oh, we pulled in a compromised
package via a three-level-deep dep." In the age of supply-chain
attacks, this matters more than it used to.

It also makes the framework **last**. Python 3.9 code stdlib-only
will run on Python 3.18. Frameworks that depend on 40 packages won't.

### 3. Identity lock is a framework concern, not a model concern.

Every adjacent project treats identity as something the user
configures in the system prompt. That works until:

- The user switches backends and the new model has different RLHF
  self-reference.
- The user doesn't notice slips because they're reading quickly.
- A jailbreak prompt convinces the model to re-identify.

Moving the identity lock into the framework (preamble + audit + post-
filter + scenario suite) turns identity into a *measurable property*.
We can A/B-test which backends slip more. We can tune the preamble
against the jailbreak scenarios. We can show the user the slip rate
on the dashboard. None of that is possible if "identity" lives only
in the prompt.

### 4. The avatar was a gimmick until we made it bidirectional.

For the first three versions the avatar was pretty and observable-
only. Every property reflected state; nothing influenced state. It
was a dashboard chart in disguise.

v0.4.1 flipped it. Now `avatar_feedback=True` in the BrainConfig
causes five rules to read the current avatar state at turn-start
and mutate the config: low health reduces retrieval, high wisdom
expands it, high restlessness disables inner dialogue, consolidate
mood pauses deep reasoning, identity erosion flips audit-only off.

The moment the avatar stopped being a chart and started being a
*governor* was the moment the framework felt different. Not
conscious — we're holding that word in reserve — but *self-regulating*.
The closest analog is a thermostat: the reading and the adjustment
are the same variable.

### 5. Routing drift is the slow killer.

Tan's "Resolvers" article identifies this: skills accumulate, their
descriptions drift, their triggers stop matching how users actually
phrase things, and the model quietly stops dispatching them. No
error, no failure — just decay. You notice when a user says "is my
flight delayed?" and the flight-tracker skill doesn't fire.

We ship `mnemosyne-resolver check` — a read-only audit that flags
skills with weak descriptions (< 24 chars), skills the model would
confuse with siblings (cosine similarity ≥ 0.85 on hashed-BOW
vectors), skills with no callable, duplicate names, and AGENTS.md
mentions of skills that don't exist. Plus two new triage clusters
that surface resolver decay automatically: `unknown_tool_called`
(model hallucinated a skill name) and `no_tool_dispatched` (tools
were available, model called none).

This is hygiene work. But at 30+ skills it's the difference between
a working agent and a junk drawer.

### 6. Concurrency is where elegance goes to die.

We had a clean `MemoryStore` implementation. WAL mode, FTS5 triggers,
proper parameterization. It worked fine in tests. It flaked 30% of
the time under concurrent load.

The root causes took days to fully crush:

1. `PRAGMA busy_timeout` was set *after* `PRAGMA journal_mode=WAL`
   — so the first PRAGMA could race before the timeout was active.
2. `sqlite3.connect()` itself could hit "database is locked" during
   cold file creation before any PRAGMA could help.
3. `_check_fts5` created a probe VIRTUAL TABLE on the live DB; under
   concurrent opens, simultaneous probes collided.
4. FTS5 triggers fire on every write; under heavy concurrent writes,
   the triggers lock each other out.

The fix was four layers of retry + module-level serialization +
cached probes + reordered PRAGMAs. 50/50 stability runs now green;
the documented envelope is 4-8 concurrent writers (batch default),
more and we fall back to the outer `mnemosyne-batch` retry.

Lesson: **concurrency tests need 50+ runs, not 5.** A 90% pass rate
at 5 runs hides a 50% pass rate at 50 runs. We caught our last race
at run 15 of a 30-run loop; the first 14 runs would have looked clean
in CI.

### 7. Open-source alternatives converge, then differ philosophically.

Four adjacent projects that reached interesting conclusions:

- **Hermes Agent** (NousResearch): mature, bigger team, trajectory-
  export pipeline, DSPy + GEPA self-evolution. We ported their
  ShareGPT trajectory format and 5 of their 11 tool-call parsers.
- **agentic-stack**: portable `.agent/` folder that plugs *under*
  any harness (Claude Code, Cursor, etc.). We borrowed their
  `permissions.md` pattern and git-backed autobiography idea.
- **GBrain / GStack** (Garry Tan / YC): self-improving personal AI
  in a git repo. Heavier emphasis on resolvers; lighter on
  identity + observability.
- **Claude Code / Anthropic skills**: closest in spirit to where
  Mnemosyne wants to sit; different in that it's a commercial IDE
  integration, not a framework.

Mnemosyne's position among these is specifically: **the observable,
stdlib-only, local-first substrate**. Hermes has better training
data. agentic-stack has better portability. GBrain has a better
distribution story via Tan's platform. What we have is honesty
about what's shipped vs. what's speculative, a browser dashboard
you can point at your friend, and a framework that runs if you
lose internet tomorrow.

---

## Limitations — honest

We keep a `docs/ROADMAP.md` section titled "shipped / experimental /
research / aspirational" with every feature placed honestly. A
summary of what's genuinely missing:

- **We have never run end-to-end against a real model.** Every one
  of our 246 unit tests uses a mock `chat_fn`. The framework's main
  job — pointing it at a live LLM and having a real conversation —
  is mostly unproven. It's tested; it's not *lived*. Next week's
  priority: record a real-model demo against local Ollama.
- **Cross-process SQLite race.** Within one Python interpreter we
  serialize schema init with a module-level lock. Across processes
  (e.g. `mnemosyne-serve` and `mnemosyne-batch` running at once)
  the FTS5 vtable race can still leak. Mitigation documented. A
  filesystem `flock` fix is on the roadmap.
- **LoRA training bridge is exported but not demonstrated.** Our
  `mnemosyne-train` emits Hermes-compatible ShareGPT JSONL and
  dispatches to Unsloth, but we haven't trained an actual adapter
  against our own captured turns and shown the Pareto frontier
  move. The code path is there; the proof isn't.
- **Memory hierarchy above L3 is designed, not shipped.** L4
  patterns and L5 identity are planned for v0.6–0.7, with human-
  in-the-loop promotion at the identity boundary. The Continuity
  Score test suite is designed; we haven't run it yet.
- **No published benchmark numbers.** `docs/BENCHMARKS.md`
  describes the methodology for SWE-bench-lite, Terminal-Bench-2,
  and GAIA, and documents wrapper overhead (~1 ms per turn at
  realistic 500 ms model latencies), but we haven't run the
  actual benchmarks against a real backend yet.
- **No PyPI upload.** Artifacts are built and `twine check`-clean
  (`mnemosyne_harness-0.5.0.tar.gz` and the wheel); the maintainer
  hasn't run `twine upload` yet because we want one more real-model
  validation pass first.

Nothing here is a blocker. All of it is specifically, concretely
next.

---

## What we learned about LLM agents, generalized

A short list of opinions formed while building, not before:

1. **Stdlib + SQLite is enough.** At our scale (10K memories,
   200 turns/day, 4-8 concurrent workers) FTS5 gives 7 ms p50
   search, and SQLite WAL handles the write load. If you're reaching
   for Postgres + Neo4j + Pinecone, either you're at a scale we
   haven't reached, or you're over-engineering.

2. **Memory doesn't compound until you compact.** Storing more is
   easy. Compacting, summarizing, promoting up the hierarchy, and
   decaying out of use — that's where compounding lives. We haven't
   finished this work yet (L4 + L5), but the v0.5 architecture is
   in.

3. **Identity is an engineering problem, not a philosophical one.**
   Consciousness is unfalsifiable; persistent identity across
   context wipes is measurable. Set the second target; avoid the
   first.

4. **Observability is not a dashboard.** It's a decision — written
   at turn zero — that every action is an event, every event is
   in one append-only log, and every analysis is a pure function
   over that log. Bolted-on observability is worse than none; it
   lies about what the system does.

5. **Autonomous self-modification is a footgun.** The agent can
   propose; the human approves. Hermes's automatic skill-rewriting
   works because Hermes is run by Nous Research; your personal
   agent running on your laptop should write proposals and wait.

6. **Concurrency tests need 50+ runs, not 5.** Obvious in hindsight.
   Still caught us twice.

7. **Don't use the word "consciousness" in your README.** Save it.
   You're not there yet. None of us are. Setting the bar honestly
   — "persistent identity across context wipes," "self-regulating
   via observable feedback," "compounding memory through upward
   compaction" — gives you targets you can actually hit.

---

## Where this goes next

Concrete, in order:

**v0.5.0** (this release): `permissions.md` + git-backed autobiography
export + Claude Code adapter. Three ideas borrowed from agentic-
stack, shipped in our shape.

**v0.6.0**: L4 patterns layer. Strength column. Pattern detection
from L3 clustering. Decay cron. First real-LLM dependency (concept
extraction).

**v0.7.0**: L5 identity layer. Human-in-the-loop promotion via
dashboard. Continuity Score test suite + similarity scorer.

**v0.8.0**: Contradictions detection between patterns. Inner-
dialogue synthesis when two patterns conflict.

**v0.9.0**: Live-model demo recorded; benchmarks run; PyPI publish;
GitHub release tagged.

**v1.0.0**: The "you can use this" cut. Not AGI. Just a trustworthy,
measurable, local-first agent framework that people who aren't us
can adopt and extend.

---

## Credits

Mnemosyne builds on a lot of other people's thinking.

- **NousResearch / Hermes Agent**: trajectory format, tool-call parsers,
  self-evolution patterns. We ported specific pieces with attribution
  in `mnemosyne_train.py` and `mnemosyne_tool_parsers.py`.
- **agentic-stack (codejunkie99)**: `permissions.md`, harness-adapter
  pattern, git-backed memory. Convergent design for four-tier memory.
- **GBrain / GStack (Garry Tan)**: the "Resolvers" framing +
  routing-layer audit as a first-class concern.
- **Stanford / MIT / KRAFTON**: the Meta-Harness paper that framed
  the observability-first argument we built the substrate around.
- **eternal-context + fantastic-disco**: the ICMS three-tier memory
  model + consciousness-layer hooks (both repositories by the same
  maintainer as Mnemosyne).
- **SQLite / Python stdlib**: the boring, unglamorous, load-bearing
  foundation everything else stands on.

This is MIT-licensed. Steal what's useful. If you build something
on top of it, open an issue and tell us what you learned.

---

## Try it

```sh
pip install mnemosyne-harness
mnemosyne-serve &
open http://127.0.0.1:8484/ui
```

Tell us what breaks. We'll fix it in the open, commit it to the
branch, and put it in the next release's CHANGELOG. That's the deal.

— the Mnemosyne maintainers, 2026-04



