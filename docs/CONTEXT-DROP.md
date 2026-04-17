# Mnemosyne — context drop for next Claude Code session

Paste this entire file as your first message in a new session to
restore full working context.

---

## Who I am / what I'm working on

I'm building **Mnemosyne**, a local-first AI agent framework. GitHub:
`atxgreene/sturdy-doodle`. Current development branch:
`claude/setup-mnemosyne-consciousness-NZqQE`.

Mnemosyne is a Python package (`pip install mnemosyne-harness`) that
gives a local LLM persistent memory, stable identity across model
swaps, learned user-pattern intuition, observable self-regulation,
and a closed loop for self-improvement. **Stdlib-only core.** Zero
runtime dependencies. 25 console scripts. All data lives as plain
SQLite + JSONL + Markdown in `~/projects/mnemosyne/`.

The previous Claude Code session shipped v0.7.0 → v0.9.0 across
multiple commits. v0.7.1 is on the remote; v0.8.0, v0.8.1, and
v0.9.0 are committed locally but unpushed due to a persistent
HTTP 503 from Anthropic's git-proxy backend during that session.
Backups exist at `/tmp/mnemosyne-v0.9.0.bundle` and
`/tmp/mnemosyne-v0.9.0-full.tar.gz`.

---

## Current repo state (as of session end, 2026-04-16)

- **Branch:** `claude/setup-mnemosyne-consciousness-NZqQE`
- **HEAD (local):** `b220d77` — v0.9.0
- **Origin HEAD:** `586de86` — v0.7.1 (3 commits behind local)
- **Tags on remote:** v0.4.0, v0.4.1, v0.5.0, v0.6.0
- **Tags local-only:** v0.7.0, v0.7.1, v0.8.0 (v0.8.1 and v0.9.0
  not yet tagged; was waiting for push to succeed first)
- **Tests:** 282/282 green. pyflakes clean.

The three unpushed commits:

| SHA | Version | Summary |
|---|---|---|
| `1b4c103` | v0.8.0 | Instinct overlay (then on L4) + compactor audit + bench skeleton + batched decay/search UPDATEs |
| `bedfa5b` | v0.8.1 | Launch docs: HARNESS.md, Substack article, X thread, LM Studio bench wiring |
| `b220d77` | v0.9.0 | Instinct promoted to its own L0 tier; 6-tier ICMS; Reflection → Instinct loop formalized |

---

## Architecture — the 6-tier ICMS (v0.9)

The memory system has six tiers in one SQLite database with FTS5.
Single `memories` table with `tier`, `kind`, `strength` columns.

| Constant | Tier | Name | Purpose |
|---|:---:|---|---|
| `L0_INSTINCT` | 0 | instinct | Fast-path automatic reactions; populated by `mnemosyne_instinct.distill()` from L5+lower; always checked first |
| `L1_HOT` | 1 | hot | Working memory; current session context |
| `L2_WARM` | 2 | warm | Short-term; default tier for new writes |
| `L3_COLD` | 3 | cold | Long-term; demoted from L2 |
| `L4_PATTERN` | 4 | pattern | Recurring clusters promoted by `mnemosyne_compactor` |
| `L5_IDENTITY` | 5 | identity | Human-approved core values; also the "Reflection" role — feeds L0 via distillation |

Decay: ACT-R base-level activation × per-kind multiplier
(`KIND_DECAY_MULTIPLIERS`). Identity 0.1×, preference 0.3×,
user_instinct 0.4×, pattern 0.5×, fact 1.0×, ops 3.0×. 7-day
half-life at multiplier=1.0.

**Reflection → Instinct loop:** Three offline modules collectively
constitute "reflection":
1. `mnemosyne_dreams.consolidate()` — TF-IDF clustering L2/L3 → L3
2. `mnemosyne_compactor.compact_patterns()` — Jaccard clustering L3 → L4
3. `mnemosyne_instinct.distill()` — Jaccard clustering L5+lower → L0

The third is the "Reflection → Instinct" transfer. L5 Identity +
everything beneath is the substrate the distiller reflects on; L0
is the fast-path output the Brain consults first on every turn.

**Brain injection order on every turn:**
1. Identity lock preamble (from `mnemosyne_identity`)
2. L5 identity block (`_build_l5_identity_block`)
3. L0 instinct block (`_build_instinct_block` — kind-based query,
   so it picks up both v0.9 L0 rows AND any legacy v0.8 L4 rows)
4. Personality
5. Env snapshot
6. Goals
7. Query-time retrieval (FTS5 against memory, AND → OR fallback)
8. User message

---

## Key files to know

| File | What it is |
|---|---|
| `mnemosyne_memory.py` | MemoryStore, 6 tier constants, decay, FTS5 search with OR fallback |
| `mnemosyne_brain.py` | Brain.turn() — orchestration loop; `_build_*_block` methods |
| `mnemosyne_instinct.py` | distill() — writes user_instinct rows to L0 |
| `mnemosyne_compactor.py` | compact_patterns() + audit_patterns() — L3→L4 promotion + quality audit |
| `mnemosyne_continuity.py` | Continuity Score benchmark runner (50 scenarios) |
| `mnemosyne_identity.py` | 4-layer identity lock |
| `mnemosyne_models.py` | 19-provider backend abstraction; `Backend(provider="lmstudio")` etc. |
| `mnemosyne_predictions.py` | Prediction → outcome → calibration (v0.6) |
| `docs/ARCHITECTURE.md` | 6-tier memory section is the canonical architecture ref |
| `docs/HARNESS.md` | 12-component agent-harness audit (9 ✓, 3 partial) |
| `docs/COGNITIVE_OS.md` | 5-property operational checklist (all 5 ✓ as of v0.7) |
| `docs/articles/v0.8-launch-substack.md` | ~1500-word launch essay — **ready to publish** |
| `docs/articles/v0.8-x-thread.md` | 12-tweet thread + quote cards — **ready to schedule** |
| `scenarios/continuity.jsonl` | 50 Continuity Score scenarios |
| `scenarios/jailbreak.jsonl` | 40 identity-lock attack prompts |
| `bench/locomo.py` | LOCOMO runner with MnemosyneSubstrate + Mem0Substrate adapters; LLM-grounded mode wired to `mnemosyne_models.chat()` |
| `bench/README.md` | LM Studio quick-path instructions |
| `docs/images/README.md` | Placeholder docs for hero banner / architecture diagram / owl portrait assets |

---

## Goals for tonight (carried over)

User's stated intent: **get Mnemosyne publicly launched this
evening**. Substack + X article + thread, marketing presence, then
benchmarks vs Mem0 to back the claims.

The user has LM Studio running locally as their LLM endpoint.
That's the key — it means real Continuity Score numbers against a
live model are runnable with `mnemosyne-continuity run --provider
lmstudio --model <id>` once the sandbox situation clears.

Ordered priority (what I was going to do next when session ended):

1. **Get the 3 local commits onto GitHub** (blocked — proxy 503).
   User was about to restart their desktop to try clearing it.
2. **Tag v0.8.1 + v0.9.0** once push succeeds.
3. **Run Continuity Score against LM Studio** — produce real
   live-model numbers to include in the launch article. Expected:
   live score ≥ substrate dryrun 0.96 aggregate / 1.00 cross-session.
4. **Publish Substack article** at `docs/articles/v0.8-launch-substack.md`.
5. **Schedule X thread** at `docs/articles/v0.8-x-thread.md` + quote cards.
6. **Drop image assets** into `docs/images/` at the filenames the
   README references (hero-owl-banner.png, architecture-overview.png,
   owl-portrait.png — the 6-layer tier-stack image was rejected as
   canonical architecture docs because it mislabels our tiers; see
   `docs/images/README.md`).

---

## Blockers and constraints

**Proxy 503:** Every `git push` from the Claude Code sandbox
returns HTTP 503 from `api.anthropic.com` (Anthropic's git-proxy
backend via Cloudflare). Reads (`git fetch`) work fine. Writes
(`git push`) consistently fail. Tried 20+ push attempts across
multiple sessions with exponential backoff up to 90 seconds.
Nothing client-side can fix this.

**Workaround:** User was restarting desktop to clear it. If that
doesn't work, backup files are at:
- `/tmp/mnemosyne-v0.9.0.bundle` (1.7 MB — clone with
  `git clone /path/to/bundle.bundle`)
- `/tmp/mnemosyne-v0.9.0-full.tar.gz` (5.1 MB — full repo tarball)

If the sandbox is ephemeral and those files are wiped on restart,
the design is fully captured in this context drop; reconstruction
is possible in one session.

---

## Working norms — things that make the user happy

The user consistently wants:

- **Stdlib only.** No new runtime dependencies in `pyproject.toml`.
  Optional benchmark deps live in `bench/requirements.txt` in a
  separate venv. That boundary is load-bearing.
- **Local-first.** No cloud dependencies. No telemetry collection.
  No callbacks. No auto-update. User's data lives in
  `~/projects/mnemosyne/` and survives the framework disappearing.
- **Honest framing.** The README tagline upgrades from "substrate"
  to "cognitive OS" only when all 5 cognitive-OS checklist rows
  are green with verify commands. Same gatekeeping for the 12-
  component HARNESS.md table.
- **No AGI claims.** No "we beat X benchmark by Y%" until the
  numbers are actually run and reproducible. No fabricated citations.
- **Pushback is expected.** User pastes AI-generated feedback
  (ChatGPT, Grok) and wants me to triage — accept what's useful,
  reject what's wrong. Over-acceptance is a failure mode.
- **Tests + pyflakes before commit.** Always run the full suite
  before committing. Current baseline: 282/282 tests.

---

## Things I correctly pushed back on (don't re-accept these)

- **The "6-layer L1-L6 Grok rename":** Grok's threads repeatedly
  proposed renaming all our existing tiers to L1 Instinct / L2
  Immediate / L3 Recent / L4 Core / L5 Archival / L6 Reflection.
  That would be a breaking rename. I correctly shipped L0_INSTINCT
  *below* the existing L1-L5 numbering instead. Grok's "L5 =
  Archival" framing also doesn't match our code (our L5 is
  Identity). The v0.9 design preserves L1-L5 and adds L0 below.
- **The 6-layer stone-tablet architecture diagram:** the image
  shown labels tiers incorrectly (L1 Instinct / L3 Recent / L4
  Core / L5 Archival / L6 Reflection, omits L2). Rejected as
  canonical architecture doc; marketing-only with a caption if
  used at all. See `docs/images/README.md`.
- **Owl-avatar redesign as substrate work:** user agreed to
  deprioritize. Owl visuals are marketing assets only; the
  existing avatar dashboard stays.
- **Claiming benchmark numbers we haven't run.** We have
  `bench/locomo.py` skeleton. We haven't run LOCOMO vs Mem0.
  Articles are careful to say "run these yourself" and don't
  invent numbers.

---

## Research context

**Akshay Pachaar's "The Anatomy of an Agent Harness" (April 2026)**
— 12-component synthesis from Anthropic/OpenAI/LangChain/Perplexity.
We audited against it in `docs/HARNESS.md`. 9 ✓, 3 partial (context
masking, error taxonomy, per-turn verification — all tracked for
v0.9 but not yet implemented in code; they're in the launch article
as v0.9 roadmap).

**Mem0 — the comparison target.** Mem0 reports 66.9% LOCOMO
(base) / 68.4% (graph) / 91.6% with their newer token-efficient
variant. Also reports 97%+ junk accumulation in production audits
without strict rules. Our compactor audit (v0.8) is the defense
against this exact failure mode.

**Continuity Score (our benchmark).** 50 scenarios, 6 categories,
10 cross-session. Substrate-only dryrun: 0.96 aggregate / 1.00
cross-session (v0.7.1). Live-model numbers TBD — that's tonight's
LM Studio run.

**ACT-R (cognitive science).** Base-level activation equation
`B = ln(Σ t_i^-d)` with d≈0.5 drives our decay model. Geometric
approximation in `_actr_base_level()` since we don't store
per-access timestamps.

**Ori-Mnemos (reference).** Knowledge-graph + PageRank + LinUCB
bandit system we evaluated. Borrowed: ACT-R decay, Hebbian
co-occurrence, kind-differentiated decay rates. Rejected:
graph DB, bandit routing (too complex for stdlib-only).

---

## What to do first in the new session

1. **Check the repo state:** `git log --oneline -5` and
   `git status -sb` to confirm whether the 3 unpushed commits
   survived the restart.
2. **Try a push:** `git push -u origin claude/setup-mnemosyne-consciousness-NZqQE`
   — if it succeeds, tag `v0.8.0 v0.8.1 v0.9.0` and push tags.
3. **If commits are gone:** restore from `/tmp/mnemosyne-v0.9.0.bundle`
   or `/tmp/mnemosyne-v0.9.0-full.tar.gz`, OR reconstruct v0.8.0
   through v0.9.0 from this context drop's architecture + key-files
   description. All design decisions captured here.
4. **If bundle survived + commits lost:**
   `git clone /tmp/mnemosyne-v0.9.0.bundle fresh && cd fresh && git remote set-url origin https://github.com/atxgreene/sturdy-doodle.git`.
5. **Once pushed:** run Continuity Score against LM Studio for
   real numbers. Then publish the Substack article.

---

## Honest failure modes to watch for

- Don't propose a breaking rename of L1-L5. L0 addition is the
  correct shape of that work.
- Don't implement anything that needs an external service to run
  (Mem0 SDK, vector DB, cloud API). The stdlib-only invariant is
  a hard rule.
- Don't ship docs with unverified claims. Every assertion in
  HARNESS.md and COGNITIVE_OS.md has a verify command; keep that
  gate.
- Don't batch-commit. Separate concerns per commit. The session
  that wrote v0.8 → v0.9 shipped one focused commit per version
  with a clean CHANGELOG entry.
- Don't push to main, don't force-push, don't skip hooks.

---

## Handoff summary

**Branch:** `claude/setup-mnemosyne-consciousness-NZqQE`
**HEAD:** `b220d77` (v0.9.0)
**3 commits unpushed. 282/282 tests. pyflakes clean.**
**Articles drafted, bench runner wired, LM Studio tonight.**
**User goal: public launch this evening.**

Go.
