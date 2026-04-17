# Mnemosyne ↔ the 12 components of an agent harness

In April 2026, Akshay Pachaar published *"The Anatomy of an Agent
Harness,"* synthesizing 12 production-harness components from
Anthropic, OpenAI, LangChain, and Perplexity. We had been shipping
all 12 for months without that vocabulary. This document is the
self-audit: row-by-row, what's in the repo, where it lives, and
**a verify command you can run locally to confirm.**

The point of this format isn't to claim parity with Claude Code or
the Codex harness. The point is honesty: marketing pages get to be
aspirational; this page does not. If a row is partial, it says so.
If a verify command fails, file an issue.

---

## The 12-component table

| # | Component | Status | Implementation | Verify command |
|---|-----------|:------:|----------------|----------------|
| 1 | Orchestration loop (TAO / ReAct) | ✓ | `mnemosyne_brain.Brain.turn()` — single-agent loop, max_tool_iterations bounded | `python3 -c "from mnemosyne_brain import Brain, BrainConfig; print(Brain.turn.__doc__)"` |
| 2 | Tools (registration, schema, dispatch) | ✓ | `mnemosyne_skills.SkillRegistry` + `mnemosyne_skills_builtin` (11 builtin skills: fs/http/git/sqlite/shell) | `mnemosyne-pipeline list-skills` |
| 3 | Memory (multi-timescale) | ✓ | `mnemosyne_memory.MemoryStore` — 6-tier ICMS (L0 instinct / L1 hot / L2 warm / L3 cold / L4 pattern / L5 identity) with Reflection → Instinct loop | `mnemosyne-memory stats` |
| 4 | Context management | ⚠ partial | System-prompt assembly in `Brain.turn()` orders identity → L5 → instinct → personality → env → goals → memory hits → user. **Missing:** observation masking on long tool-use loops; intra-turn compaction. v0.8.1 target. | n/a — read `mnemosyne_brain.py:340-400` |
| 5 | Prompt construction | ✓ | `_build_l5_identity_block`, `_build_instinct_block`, `_build_env_snapshot`, `_read_user_docs` (AGENTS.md / TOOLS.md) | inspect via `python3 -c "import mnemosyne_brain as b; help(b.Brain)"` |
| 6 | Output parsing (native + text-embedded) | ✓ | `mnemosyne_tool_parsers` — 5 parsers (Hermes, Qwen, Mistral, Llama-3, plain-JSON) for unquantized models that emit `<tool_call>` text instead of structured fields | `python3 -m pytest tests/test_all.py -k tool_parser` (see `tests/test_all.py`) |
| 7 | State management | ✓ | `MemoryStore` (SQLite + WAL) + `harness_telemetry` JSONL events + git-backed autobiography (`mnemosyne-memory export --to-git`) | `mnemosyne-experiments list` |
| 8 | Error handling | ⚠ partial | 5-attempt exponential-backoff retry on SQLite locks; tool-call exceptions caught in Brain loop; identity-slip detection. **Missing:** explicit 4-category taxonomy (transient / LLM-recoverable / user-fixable / unexpected). Tracked for v0.9. | grep for `last_err` / `try:` in `mnemosyne_memory.py` |
| 9 | Guardrails / safety | ✓ | `mnemosyne_permissions` (allowed / denied skills, forbidden paths, rate limits) + 4-layer identity lock + 40-prompt jailbreak suite at `scenarios/jailbreak.jsonl` | `mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl` |
| 10 | Verification loops | ⚠ partial | Post-hoc: triage → proposer → apply (`mnemosyne_triage`, `mnemosyne_proposer`, `mnemosyne_apply`). Per-turn LLM-as-judge / rules-based / visual verification: not yet wired. v0.9 target. | `mnemosyne-triage scan --window-days 30` |
| 11 | Subagent orchestration | ✓ | Inner-dialogue (`mnemosyne_inner`) — Planner / Critic / Doer / Evaluator phases; opt-in via `BrainConfig.inner_dialogue_enabled` | `python3 -c "from mnemosyne_inner import deliberate, DEFAULT_TRIGGER_TAGS; print(DEFAULT_TRIGGER_TAGS)"` |
| 12 | Self-improvement / closed loop | ✓ | `triage → proposer → apply → measure` runs autonomously inside the daemon. Predictions → outcomes → calibration trait (v0.6, `mnemosyne_predictions`). Compactor audit (v0.8) reports L4 pattern hit-rate. | `mnemosyne-resolver check && mnemosyne-triage scan && mnemosyne-proposer --min-severity 20` |

**Score: 9 ✓ + 3 partial.** The three partials (context-management
masking, error taxonomy, per-turn verification) are tracked for
v0.9 with concrete designs in `docs/ROADMAP.md`.

---

## Why this matters

Akshay's article frames the modern agent as: *"the agent is the
emergent behavior; the harness is the machinery producing that
behavior. When someone says 'I built an agent,' they mean they
built a harness and pointed it at a model."*

That's been our position from `7a3ca9d` (initial commit, April 7
2026) — without the vocabulary. The model is interchangeable. We
support 19 LLM backends through one `Backend(provider="...")` call
specifically because **the model isn't the agent.** What persists
across model swaps is identity, memory, learned skills, and the
self-improvement loop — all of which live in the harness, not the
weights.

---

## Where Mnemosyne is genuinely ahead

These aren't in Akshay's 12-component baseline because most production
harnesses don't have them yet:

- **Cognitive-OS checklist with verify commands.** `docs/COGNITIVE_OS.md`
  defines 5 properties (persistent identity, layered memory + upward
  compaction, observable self-regulation, self-calibration, self-
  auditing). Each row has a verify command. As of v0.7, all 5 are
  green. This format is unusual: most projects state capabilities;
  we publish a runnable checklist that gates the marketing language
  ("substrate" until all 5 green; "cognitive OS" once they're green).
- **6-tier ICMS with ACT-R decay + Hebbian reinforcement.** Not a
  vector store, not a graph. SQLite + FTS5 with `tier`, `kind`, and
  `strength` columns; offline ACT-R decay multiplied by per-kind rate
  (identity 0.1×, ops 3.0×). Reinforces on read. Demotes below 0.3.
- **L0 Instinct tier + Reflection → Instinct loop (v0.9).**
  User-pattern signals distilled from L5+lower into L0 rows with
  `kind="user_instinct"`. Brain consults them every turn before
  query-relevance retrieval — a fast path of learned automatic
  reactions, populated offline, idempotent across re-runs. L5 Identity
  (human-approved core values) feeds L0 via the distillation pass;
  slow deliberate reflection gradually shapes fast automatic reaction.
- **Continuity Score benchmark.** 50 scenarios, six categories,
  10-scenario cross-session subset. Memory-layer-only dryrun: 0.96
  aggregate, 1.0 cross-session (v0.7.1 substrate fix). Reproducible:
  `mnemosyne-continuity dryrun --scenarios scenarios/continuity.jsonl`.
- **Compactor audit (v0.8).** Defends against the Mem0-reported
  97%-junk failure mode by measuring L4 pattern hit-rate and
  dead-fraction. Triage can cluster on `dead_fraction > 0.5` as a
  drift signal.
- **Stdlib-only core.** Zero runtime dependencies. 25 console scripts
  installed by `pip install mnemosyne-harness`. The whole substrate is
  auditable in an afternoon.

---

## Deeper read: Rohit Yadav's Claude Code teardown (April 7 2026)

After Akshay's 12-component framing, Rohit Yadav published a much
deeper reverse-engineering of Claude Code's source (55 directories,
331 modules). Mapping our position against the specific patterns he
surfaces:

**Things Claude Code does that Mnemosyne matches:**

- Multi-level memory hierarchy with offline consolidation — our
  6-tier ICMS + dreams + compactor.
- Native tool dispatch with schema validation — `mnemosyne_skills`.
- Sub-agent orchestration for delegated sub-tasks — our inner
  dialogue (Planner / Critic / Doer / Evaluator personas in one
  Brain).
- Hook-shaped extensibility — `mnemosyne_triage` post-hoc clustering
  + `mnemosyne_proposer` + `mnemosyne_apply` as the self-improvement
  pipeline; MCP tools via `mnemosyne_mcp`.
- Permission pipeline — `mnemosyne_permissions`, simpler (our
  deployments are single-user local; Claude Code runs a 7-stage
  enterprise pipeline we don't need).
- Retry with state-machine semantics per error class — partial.
  `mnemosyne_memory` has 5-attempt exponential backoff on SQLite
  locks; `mnemosyne_models` has provider-specific retry. Not the
  823-line state-class-per-error-type architecture Claude Code
  runs — that's tracked for v0.9 / v0.10 under "error taxonomy."

**Things Claude Code does that Mnemosyne doesn't (yet):**

- **Async-generator agent loop with streaming + cancellation +
  backpressure.** Our `Brain.turn()` is a synchronous function.
  Refactoring to a generator-based loop is substantial work;
  high-leverage for long-running sessions but not blocking the
  current feature set. Provisional v0.10+ target.
- **Streaming tool executor** that starts tools mid-stream as soon
  as each tool-call's input JSON is complete. We wait for the
  full model response. Meaningful latency win on multi-tool turns;
  depends on the generator refactor above.
- **Concurrency classification for tools.** Claude Code marks each
  tool read-only vs state-mutating and runs read-only ones in
  parallel batches (up to 10), mutating ones serially. Our tool
  executor runs serially. Concrete, shippable improvement —
  candidate for v0.9.2 or v0.10.
- ~~**Tool result budgeting.**~~ Shipped in v0.9.2.
  `Skill.max_result_size` + `BrainConfig.tool_result_max_chars`.
  Oversized results persist to
  `$PROJECTS_DIR/tool-outputs/<date>/...` and the model sees a
  preview + file reference. `tool_result_budget_hit` telemetry event
  emits on every cap hit so triage can cluster chronically-oversized
  tools.
- **Four-strategy context compaction hierarchy** (microcompact /
  snip / auto summarization / context collapse). Our context
  management is the identity-lock + tier-based-injection block
  pattern plus `mnemosyne_dreams` for long-term consolidation.
  We don't run intra-turn compaction. Useful for long tool-use
  loops; candidate for v0.10.
- **System-prompt cache-boundary marker.** Claude Code explicitly
  splits the prompt into "static above the boundary, volatile
  below" so the API's prompt cache hits for the static portion
  across every call. For local-first operation we don't pay the
  same cache-miss cost the cloud API does; lower priority for us.
  Documenting the split anyway for readers running Mnemosyne
  against cloud providers.
- **CLAUDE.md four-level composable-instruction hierarchy** (enterprise
  / project / user / local, with `@include` directives). We have
  `AGENTS.md` + `TOOLS.md` workspace docs, single level, no include.
  Extending to a Claude-Code-style hierarchy is a medium lift;
  candidate for v0.10.
- **Git worktree isolation for parallel sub-agents.** Not applicable
  unless we add multi-process sub-agent fan-out (we currently
  don't — inner dialogue runs in one process). Deferred.

**The "layer 4" infrastructure framing.** Rohit makes the case that
production agent systems have four layers, not three: model weights
/ context / harness / **infrastructure** (multi-tenancy, RBAC,
state persistence, distributed coordination). Mnemosyne deliberately
operates in the single-user, local-first, single-machine quadrant —
we don't need multi-tenancy or RBAC because there's one user and
one machine. State persistence and session coordination are already
covered (SQLite + JSONL + git-backed autobiography). No v0.x-era
changes needed here; if we ever ship a team-facing deployment mode,
this is where that work would live.

---

## How to read this audit

If you've built an agent harness, you can use this table to compare
notes. If you're evaluating Mnemosyne, you can use it as a feature
checklist with proof. If you're a maintainer of a harness mentioned in
Akshay's article and want to suggest a row we should add or
re-classify, open an issue with the verify command you'd run.

The table is dated by the version that ships each row (see
`CHANGELOG.md`). When a partial flips to ✓, the change requires:

1. The code that provides the capability.
2. The test that verifies the capability.
3. The verify command above passes.

Same gatekeeping rules as `docs/COGNITIVE_OS.md`. No quiet upgrades.

---

## What's deliberately missing

Some things that are in some "harness" frameworks aren't in ours, and
won't be:

- **No multi-agent orchestrator.** We have inner-dialogue (Planner /
  Critic / Doer / Evaluator inside one Brain). Multi-agent fan-out
  is interesting but cuts against the "single observable agent"
  premise. If you need it, wrap multiple Mnemosyne instances —
  each one is small.
- **No vector DB requirement.** Embeddings are optional
  (`mnemosyne_embeddings`). The default substrate is FTS5 + ACT-R.
  We tested this against 50K rows and the search-hit path stays
  under 3 ms.
- **No managed cloud.** Local-first is load-bearing. Your data
  doesn't leave the machine unless you explicitly send it somewhere.
- **No telemetry collection.** Telemetry is local JSONL files in
  `$PROJECTS_DIR`. We don't have analytics. We don't know who's
  using it. We can't disable your install. That's the point.

---

## Run the audit yourself

Every verify command in the table above runs locally. If any fail,
that's a real bug — open an issue with the failing command and the
output, and we'll either fix the code or fix the doc.

```sh
pip install mnemosyne-harness
# Then walk the table — each verify command is one line.
```
