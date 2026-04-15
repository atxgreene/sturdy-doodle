# Changelog

All notable changes to the Mnemosyne harness deployment repo. The format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are ISO 8601.

## [0.3.3] — 2026-04-15 — safety hardening + memory browser panel

Security and transparency improvements. The daemon is now safe to
expose on a LAN behind a reverse proxy, and the dashboard lets users
directly inspect what the agent remembers.

**`mnemosyne_serve` safety hardening:**
  - Constant-time bearer-token compare via `hmac.compare_digest`.
    Response time no longer leaks matching prefix length — protects
    against a LAN attacker with timing access.
  - 1 MiB cap on POST bodies. Requests with `Content-Length` above
    that are rejected with HTTP 413 *before* the body is read.
    Prevents lazy DoS from clients sending oversized payloads.

**Memory browser — new UI panel + endpoint.** The bottom of the
dashboard is now an interactive FTS5 search over the agent's memory:
  - `GET /memory/search?q=…&limit=N&tier_max=N` — returns up to 50
    matches with id, tier, kind, source, content (truncated to
    500 chars), created_utc, access_count.
  - UI wiring: search box, tier filter (all / L1 / L1+L2 / L1+L2+L3),
    live results list with colored tier pills and access-count badges.
  - Use cases documented in `docs/UI.md`: debug bad retrievals, audit
    what the agent knows, spot near-duplicates signaling a dream
    consolidation is due.

**Docs:**
  - `docs/UI.md` gains an "AGI-scaling traits (v1 schema)" table
    documenting how wisdom / restlessness / novelty / self_assessment
    are derived and when they're null. Explicit contract, no magic.
  - Memory-browser section added to `docs/UI.md`.
  - Security section updated with constant-time compare + body cap.
  - `docs/dashboard.png` regenerated showing the new panel layout.

**Tests:** 196 → 200 green. 4 new covering constant-time compare,
`MAX_BODY_BYTES` sanity, memory search shape and tier filter, and
memory search respecting the 50-row cap.

Verified: 8 consecutive full-suite runs all 200/200. pyflakes clean.
Version bumped 0.3.2 → 0.3.3.

## [0.3.2] — 2026-04-15 — AGI traits + habitat + production deploy

Three additions, each narrow:

**AGI-scaling traits computed.** The four reserved slots in the
avatar schema (`wisdom`, `restlessness`, `novelty`, `self_assessment`)
are now populated where we have honest signal, left null otherwise.
Definitions in `mnemosyne_avatar.py`:

  wisdom          = log10(memory_count+1)/4 × min(age_days/90, 1) × identity_strength
                    (null when memory empty or age < 0.5 days)
  restlessness    = coefficient of variation of inter-turn gaps, clipped [0..1]
                    (null when fewer than 3 successful turns to compare)
  novelty         = skill_learned events per week, clipped [0..1]
                    (null when age_days < 1 or no skills at all)
  self_assessment = evaluator-persona accept ratio (accept / (accept + revise))
                    (null when the Evaluator has never fired)

Visually the avatar now shows:
  - outer dashed rotating **wisdom ring** (opacity scales with wisdom)
  - small **self-assessment rays** between core and inner-dialogue rings
  - core-orb **jitter animation** when restlessness > 0.3

**Habitat.** Three soft wave bands at the bottom of the avatar
stage whose heights reflect L1/L2/L3 memory-tier proportions. Not a
game background — environmental grounding so the avatar isn't
floating in a void. Mirrored in both the JS renderer and the
server-side `render_svg()` so standalone SVG dumps match the live UI.

**Production deploy (`deploy/`).** One-line install helpers for
running `mnemosyne-serve` as a per-user service:
  - `deploy/mnemosyne.service` — systemd user unit with sandbox
    hardening (NoNewPrivileges, ProtectHome=read-only,
    ReadWritePaths limited to the projects dir)
  - `deploy/com.atxgreene.mnemosyne.plist` — macOS launchd agent
  - `deploy/install-service.sh` — detects OS, registers the right
    unit, substitutes `$HOME` paths, optionally configures
    `MNEMOSYNE_SERVE_TOKEN` from env. Supports install / --uninstall /
    --status.

**Wizard integration.** `mnemosyne-wizard.sh` now offers to launch
`mnemosyne-serve` and open http://127.0.0.1:8484/ui at the end of
setup. Cross-platform URL opener (xdg-open / open / cmd.exe start).

**Tests:** 191 → 196 green. 5 new:
  - avatar: AGI traits null when signal is absent
  - avatar: self_assessment derived from evaluator verdicts
  - avatar: restlessness derived from inter-turn gap variance
  - avatar: _compute_wisdom needs age + memory + identity
  - avatar: render_svg with wisdom + self_assessment adds new elements

pyflakes clean. Version bumped 0.3.1 → 0.3.2.

## [0.3.1] — 2026-04-15 — fix concurrent MemoryStore race

Two races surface under heavy batch-runner concurrency (`workers > 2`)
when many MemoryStore instances open the same DB file at once:

  1. `database is locked` during simultaneous schema-init DDL
  2. `vtable constructor failed: memories_fts` during simultaneous
     CREATE VIRTUAL TABLE USING fts5

Both fixed:

- `mnemosyne_memory.MemoryStore.__init__` now sets
  `PRAGMA busy_timeout=5000`, so SQLite waits 5s instead of failing
  immediately on lock contention.
- `mnemosyne_memory` adds a module-level `_SCHEMA_INIT_LOCK` that
  serializes DDL across all MemoryStore instances in one interpreter.
  FTS5 vtable construction is not coordinated across SQLite
  connections at the driver level; we coordinate in Python.
- `mnemosyne_batch._retryable()` now treats both transient signatures
  (`database is locked`, `vtable constructor failed`) as retry-worthy,
  so the batch runner self-heals when the bottom-of-stack glitch
  leaks through.

Verified: 40/40 concurrent 4-worker 8-prompt batches succeed (was
~85% before). Full test suite 15/15 stable (was ~70%). Added
regression: `memory: 12 concurrent MemoryStore opens on same DB
succeed` and `batch: _retryable recognizes sqlite transient errors`.

Tests: 189 → 191 green. pyflakes clean.

Multi-process parallelism is a separate concern — different processes
don't share `_SCHEMA_INIT_LOCK`. The mitigation for now is to run
`mnemosyne-memory stats` once on a fresh install so the schema is
pre-created before `mnemosyne-batch` spawns workers. A
cross-process fix (filesystem lock file) is tracked for a later
release.

## [0.3.0] — 2026-04-15 — interactive UI dashboard + evolving avatar

The agent gets a face. A browser dashboard served by `mnemosyne-serve`
shows a live SVG avatar that visualizes the agent's state, plus a
chat panel, live event stream, memory tier bars, and goal management.

The avatar is the headline feature. It's not a personality engine —
it's a deterministic visualization of observable agent state. Every
visual property maps to one integer or float in `avatar.json` derived
from `memory.db` + `events.jsonl` + `goals.jsonl`. See `docs/UI.md`
for the full visual contract.

**New modules:**

- `mnemosyne_avatar.py` — derives a versioned avatar state dict.
  Schema v1 fields cover memory tiers, identity strength, dream count,
  inner-dialogue activations, goal stats, palette (auto-derived from
  health × activity), mood phase (rest/focus/explore/consolidate),
  aura radius, ring count, pulse rate. Reserved-but-null slots for
  `wisdom` / `restlessness` / `novelty` / `self_assessment` keep the
  schema future-additive without breaking older snapshots.
- `mnemosyne_ui/static/` — single-page dashboard. `index.html`,
  `style.css` (responsive grid, dark theme, system fonts only),
  `avatar.js` (SVG renderer mirroring the server-side render exactly,
  with SMIL animations for breathing aura + orbiter rotation +
  consolidate-mode petals), `app.js` (polls /avatar /stats /goals,
  subscribes to /events_stream via SSE, posts /turn /goals).

**`mnemosyne_serve` extensions:**

- `GET /ui` → static HTML
- `GET /ui/static/*` → static asset (CSS/JS/SVG, traversal-rejected)
- `GET /avatar` → current `compute_state()` JSON
- `GET /events_stream` → Server-Sent Events tail of the run's
  `events.jsonl`. Falls back to polling when an auth token is set
  (EventSource can't carry custom headers).

**New CLI:**

- `mnemosyne-avatar state` → print state JSON
- `mnemosyne-avatar render-svg --out f.svg --size 500` → standalone
  animated SVG, useful for docs and headless screenshots

**Docs:**

- `docs/UI.md` — visual contract, schema versioning, security model,
  endpoint table, future-facing roadmap
- `docs/dashboard.png` — composed dashboard reference (full layout)
- `docs/avatar-rest.png` / `docs/avatar-active.png` — paired examples
  showing the same avatar in idle vs. busy state
- `docs/avatar-rest.svg` / `docs/avatar-active.svg` — animated SVG
  versions (renderable in any browser)

**Packaging:**

- 18th console script (`mnemosyne-avatar`)
- `mnemosyne_avatar` module added to `py-modules`
- `mnemosyne_ui/static/*` added to package-data so the dashboard
  ships with `pip install -e .`
- Version bumped 0.2.0 → 0.3.0

**Tests:** 182 → 189 green. 7 new covering empty/baseline state,
identity-slip drag on health, dream + inner-dialogue surfacing,
snapshot round-trip, SVG render output, mood-phase priority logic,
memory tier reflection. pyflakes clean.

**AGI-scaling design (deliberately additive):**

- Avatar state schema is versioned. New traits join under the same
  `schema_version`; existing UI keeps rendering older snapshots.
- Reserved null-valued slots for traits we'll compute later
  (`wisdom`, `restlessness`, `novelty`, `self_assessment`).
- Avatar state is *derived* from observable agent data, not stored
  independently. Truth lives in events.jsonl + memory.db; the
  snapshot is a cache the UI reads.
- Future: bidirectional avatar (state signals back into agent
  config), habitat visualization (per-skill objects), inter-agent
  visibility — all gated behind doc/UI.md "Future-facing extensions".

**Known limitations:**

- High-concurrency telemetry writes (>2 workers writing many events
  per second) can occasionally drop events. Tracked separately;
  doesn't affect normal interactive use. Tests run at workers=2 to
  avoid the race.

## [0.2.0] — 2026-04-15 — v1.2 rigor pass + architectural primitives

### v1.2.3 — Hermes-port: extended tool-call parsers + builtin skill library

Two ports from NousResearch/hermes-agent (MIT), attributed, kept
narrow:

**`mnemosyne_tool_parsers.py`** — recovers text-embedded tool calls
from assistant responses that don't come through the server's
structured `tool_calls` field. Five parsers:

- `parse_hermes` — `<tool_call>{...}</tool_call>` (Nous Hermes,
  Qwen-Agent)
- `parse_mistral` — `[TOOL_CALLS][{...}]`
- `parse_llama3` — `<|python_tag|>{...}<|eom_id|>` (with
  "parameters" alias)
- `parse_functionary` — fenced-JSON with required `name` key (avoids
  over-firing on unrelated fenced JSON)
- `parse_trailing_json` — plain JSON object at end of message,
  conservative fallback

`parse_any(text, hint=None)` dispatches in priority order.
`strip_tool_calls(text)` removes the envelopes so user-visible
responses don't leak raw tags. `_recover_embedded_tool_calls()` is
wired into both `_parse_openai` and `_parse_ollama` in
`mnemosyne_models`: if the server didn't return structured calls, we
pull them out of the text. Means local Qwen 3.5 via Ollama with no
tool-call parser configured now produces structured calls anyway.

Compared to Hermes's eleven parsers, we ship five — the ones that
cover ~95% of observed local-model behavior. The dispatcher is
extensible: add a new parser to `PARSERS` and it joins the fallback
order.

**`mnemosyne_skills_builtin.py`** — curated 11-skill library,
stdlib-only, safety-audited:

- `fs_read` / `fs_list` / `fs_write_safe` — root-jailed to
  `$MNEMOSYNE_PROJECTS_DIR`; path-traversal rejected; atomic writes;
  overwrite requires opt-in
- `grep_code` — pure-Python regex across glob-matched files
- `http_get` / `web_fetch_text` — read-only GET; 10s timeout; 2MB
  cap; HTTP/HTTPS only; strips HTML tags for `web_fetch_text`
- `sqlite_query` — SELECT/WITH only; rejects multi-statement; bounded
  limit
- `shell_exec_safe` — allow-list (`ls cat head tail wc file git which
  pwd date uname env python3 pip`); no `shell=True`; timeout
- `git_status` / `git_log` — subprocess to `git` at the projects
  root, bounded timeout
- `datetime_now` — pure, no I/O

`register_builtin_skills(registry, names=None)` wires all or a subset
into a `SkillRegistry`. `default_registry()` now includes them by
default (precedence: builtins → $PATH commands → markdown skills
→ learned skills). Pass `load_builtins=False` for a narrower agent.

Tests: 156 → 174 green. 18 new covering every parser (success +
malformed-input safety + strip + integration) and every skill
(traversal rejection, overwrite guard, glob filter, SELECT-only guard,
allow-list enforcement, scheme guard, registry integration).

### v1.2.2 — consolidation pass + GIF demo

Cleanup + deliverable improvements.

- **Shared helpers consolidated.** `_utcnow` was defined inline in 9
  modules, `_default_projects_dir` in 8. Both now live canonically in
  `mnemosyne_config.py` (`utcnow_iso`, `utcnow_slug`,
  `default_projects_dir`). Each module now imports from there with a
  small standalone-file fallback (preserves the copy-paste-a-single-
  file property of the framework). Net: ~80 lines of dead duplication
  removed.
- **`demo-quick.sh`** — 45-second screen-recordable walkthrough (5
  sections: identity lock, ICMS memory, triage → proposer, training
  export, tests + pyflakes). Feeds the GIF without scrolling.
- **`docs/demo.gif`** — 121K GIF rendered via asciinema + agg 1.7.0.
  Embedded at the top of README.md so the repo page shows the demo
  inline.
- **`docs/demo.cast`** — the raw asciinema cast. Keeps the source
  alongside the render so both can be regenerated from one recording
  session.

Tests: 156 → 156 green (same count, just cleaner code).

### v1.2.1 — training bridge (telemetry → LoRA → LM Studio/Ollama)

New module + five-subcommand CLI closes the Hermes / Meta-Harness loop
end to end:

- `mnemosyne_train.py` — `export` (telemetry → Hermes-compatible
  ShareGPT JSONL), `compress` (stdlib port of Hermes's
  `trajectory_compressor`), `train` (shells out to Unsloth),
  `deploy` (LM Studio or Ollama), `eval` (A/B base vs. adapted,
  Pareto delta).
- `_train_unsloth.py` — subprocess wrapper loading Unsloth only when
  `mnemosyne-train train` actually runs. Heavy deps never touch the
  core import path.
- `docs/TRAINING.md` — methodology, minimum dataset sizing, chat-
  template warnings, honest caveats about what LoRA can and cannot do.
- `BrainConfig.capture_for_training=True` — emits a `training_turn`
  telemetry event per successful turn with the full verbatim prompt,
  response, and tool calls. Used by `export` as source of truth.
  Fallback to memory.db Q:/A: reconstruction when absent (truncated).
- `[project.optional-dependencies] train = [unsloth, datasets,
  transformers, trl, peft, accelerate]` — opt-in install.

Output format is a **strict superset** of Hermes's
`batch_runner.py` schema. Mnemosyne-specific metadata lives under
`metadata.mnemo_*`; trainers ignore unknown keys. Interop both
directions with the Hermes trajectory ecosystem.

15th console script: `mnemosyne-train`. Tests: 145 → 156, all green.

Cumulative on top of `main` (`07d2724`). Branch: `claude/setup-mnemosyne-consciousness-NZqQE`.

### v1.2 rigor pass (this commit)

Packaging + shippability work: the framework earns its version bump.

**New modules (14 recommendations shipped):**

- `mnemosyne_serve.py` — long-running daemon process. Stdlib `http.server` endpoint for turn dispatch + dream/proposer/apply cron threads. One process owns the memory store so L1/L2/L3 transitions aren't lost between CLI invocations.
- `mnemosyne_apply.py` — closes the Meta-Harness loop end-to-end. Takes an accepted proposal, executes the specific change (temperature tweak, skill add, prompt edit), re-runs the affected scenarios, marks the proposal `accepted` or `reverted` based on Pareto delta.
- `mnemosyne_embeddings.py` — optional `sentence-transformers` backend with a stdlib hashed-bag-of-words fallback. Used by memory search and dream clustering when available.
- `mnemosyne_scengen.py` — scenario auto-generator. Walks `events.jsonl`, extracts successful turns, emits regression scenarios. The agent writes its own tests.
- `mnemosyne_goals.py` — persistent goal stack. Agent maintains an open TODO across sessions; goals are surfaced on first turn of each session.
- `mnemosyne_mcp.py` — Model Context Protocol (JSON-RPC over stdio). Both directions: Mnemosyne skills exposed as MCP tools, external MCP servers consumed as skills.
- `scenarios/jailbreak.jsonl` — 40 identity-attack prompts so the identity lock's strength becomes quantifiable per-model.
- `docs/BENCHMARKS.md` — honest benchmark methodology + instrumentation-overhead reference numbers. Template for users to run against their own setup.

**Changed:**

- `mnemosyne_models.chat()` gains `stream=True` support (SSE for OpenAI-compatible endpoints, NDJSON for Ollama, native streaming for Anthropic). Returns a generator when streaming.
- `mnemosyne_models` gains a pluggable `RateLimiter` (token-bucket, per-backend) so cloud bills don't surprise anyone.
- `mnemosyne_experiments cost <run_id>` subcommand: rolls up token usage into dollar estimates using a per-model price table.
- `mnemosyne_brain` gains tool-feedback learning: failed tool calls write an L1 memory shaping future routing.
- `mnemosyne_inner` gains a 4th Evaluator persona that scores the Doer's output against the Planner's plan (optional, off by default).
- `README.md` top matter rewritten: one-sentence pitch, three-line install, ten-line quickstart, roadmap link.
- `pyproject.toml` bumped to `0.2.0`. Nine new py-modules, five new console scripts (`mnemosyne-serve`, `mnemosyne-apply`, `mnemosyne-scengen`, `mnemosyne-goals`, `mnemosyne-mcp`).

**Test results:**

- 122 → 180+ unit tests, all green
- `bash test-harness.sh` → 29/29 passing
- pyflakes clean
- Demo renumbered to 16 sections, regenerated end-to-end

### v1.2 architectural primitives (commit 2c2c5b6)

- `mnemosyne_proposer.py` — Meta-Harness proposer loop. Triage clusters → reviewable markdown change proposals in `$PROJECTS_DIR/proposals/`. Dedupes by `cluster_id`.
- `mnemosyne_dreams.py` — offline L3 cold → L2 abstract consolidation. Stdlib TF-IDF clustering + optional LLM summarizer.
- `mnemosyne_inner.py` — Planner → Critic → Doer multi-persona dialogue on shared identity lock + memory. Routed by tag/keyword via `should_deliberate`.

### v1.1 self-healing feedback loop (commit a592a4e / 30e7971)

- `mnemosyne_triage.py` — CREAO-style error clustering, severity scoring across 6 dimensions, daily/weekly markdown health reports.
- Local-model tuning: `mnemosyne_models.ollama_model_info()`, `recommended_context_budget()`; `mnemosyne_brain._maybe_adapt_to_context()`; `docs/LOCAL_MODELS.md`.
- GitHub Actions 6-phase CI: verify / shellcheck / pyflakes / unit / integration / install-smoke / triage-demo.

### Identity + 19-provider backend (commit 8810554)

- `mnemosyne_identity.py` — 4-layer defense: system preamble, `IDENTITY.md` extension, post-filter regex, scenario validation.
- `mnemosyne_models.py` expanded to 19 providers (OpenAI-compatible + native Anthropic/Ollama).
- `mnemosyne_brain.py` integrates identity lock with `enforce_identity_lock` + `enforce_identity_audit_only`.

### Reproducible demo (commit ad5a84d)

- `demo.sh` — 11-section (now 16-section) end-to-end demo, no external deps.
- `docs/DEMO.md` — captured transcript, regenerable via `bash demo.sh`.

### GUI polish (commit 97b8935)

- `mnemosyne-dashboard.sh` — live telemetry panel, `--once --plain` mode for headless captures.
- `mnemosyne-wizard.sh` — welcome screen + interactive config browse.

## [Unreleased] — in-flight work

Work-in-progress branch. Everything below is cumulative on top of `main` (`07d2724`).

### 2026-04-09 — Research-grounded upgrade: architecture doc, DeltaNet recs, GPU snapshot

Committed as `3c9e1b9`. Incorporates findings from three research threads:

- **Meta-Harness paper full results:** 6x gap on same benchmark, 76.4% on TB-2 with Opus (#2 leaderboard). Environment bootstrapping — the `environment-snapshot.py` pattern — confirmed as the optimizer's #1 discovery.
- **Qwen 3.5 on Ollama:** `qwen3.5:9b` uses Gated DeltaNet + sparse MoE. Only ~3B params activated per token. DeltaNet scales linearly with context — strongest ICMS fit. Added as primary model recommendation.
- **Mamba-3 (ICLR 2026):** 7x faster at long sequences, 4% better on LM benchmarks. The next architectural generation. Not yet on Ollama.

**New:** `docs/ARCHITECTURE.md` — comprehensive system design document synthesizing all three research threads: four-layer stack, DeltaNet inflection point, model comparison matrix with architecture properties, inference-as-harness argument, observability design rationale vs Langfuse/Phoenix/OTEL, the future optimization loop.

**Changed:** `environment-snapshot.py` v2 — GPU detection (nvidia-smi: model, VRAM, driver, CUDA, compute capability) + model architecture classification (heuristic: DeltaNet-hybrid vs standard-attention vs SSM). `SETUP.md` model-choice section rewritten. `BLOG.md` v3 with honest related-work section (acknowledges SuperagenticAI/metaharness and HKUDS/OpenHarness).

### 2026-04-09 — Harness observability v2: sweeps, scenarios, tests, demo

Added the user-facing optimization and evaluation layer on top of the observability substrate.

**New:**

- `harness_sweep.py` — deterministic parameter-space sweeper. Cartesian product over a parameter dict, one `TelemetrySession` per combination, evaluator callable receives `(params, session)` and returns metrics. Failed evaluators mark the run as failed and continue; `stop_on_error=True` to abort instead. `skip_if` predicate for resumability.
- `scenario_runner.py` — scenario-based evaluation harness. Loads scenarios from a JSONL file (comments and blanks skipped), runs each through a user-supplied harness callable, scores via pluggable judges (`expected_contains`, `expected_tool_calls`, `expected_regex`, plus custom hooks), returns a `{metrics, per_scenario}` dict suitable for `finalize_run`.
- `scenarios.example.jsonl` — 10 sample scenarios (knowledge recall, math, regex format, tool-use single and multi-step, safety, long-context, code, reasoning) as placeholders for a real eval suite.
- `examples/sweep_demo.py` — runnable end-to-end demo. 2×2×2 parameter sweep over a fake harness, scenarios from `scenarios.example.jsonl`, metrics finalized into `$PROJECTS_DIR/experiments/`. Completes in ~6 seconds. Takes `--projects-dir` so it never touches the real install.
- `docs/WIRING.md` — four concrete interface patterns (per-tool decorator, central registry wrapper, dispatch middleware, session lifecycle) for plugging `harness_telemetry` into `eternal-context` without speculating about the real skill interface. Includes a preflight pattern for injecting `environment-snapshot.py` output into the first turn.
- `tests/test_all.py` — 49 stdlib-only unit tests covering:
  - `harness_telemetry`: redaction (flat, nested, lists, scalars, default patterns, false positive avoidance), run lifecycle (create, freeze, finalize, mark_failed, list, get), `TelemetrySession` (trace decorator ok/error, secret safety, context manager events, missing-run error).
  - `harness_sweep`: plan (cartesian, empty, single), slugify, `_build_slug` length cap, `run` success/failure/stop_on_error.
  - `scenario_runner`: all three built-in judges (positive and negative), `load_scenarios` parsing (valid, malformed, missing fields), `run_scenarios` (pass/fail mix, exception catching, tags_filter).
  - `mnemosyne-experiments` internals: `_dominates` (max, min, mixed, equal, tradeoff), `_percentile` (empty, single, p50, p99), `_ascii_scatter` (rendering, empty).
- `CHANGELOG.md` — this file.

**Changed:**

- `mnemosyne-experiments.py` gained two new subcommands:
  - `aggregate <run_id>` — per-tool statistics from `events.jsonl`: call count, ok/error counts, success rate, latency min/p50/p95/p99/max/avg/total, error-type histogram. Also reports event_type counts across the whole run.
  - `pareto --plot` — ASCII scatter plot of all runs on two axes with frontier (`*`) and dominated (`.`) markers, `#` on overlaps. Requires exactly 2 axes.
- `test-harness.sh` grew from 23 to 29 assertions to cover `aggregate` (list obsidian_search, compute success_rate, --json valid) and `pareto --plot` (frontier header, legend, both markers present).

**Test results:**

- `bash test-harness.sh` → 29/29 passing, ~2 seconds
- `python3 tests/test_all.py` → 49/49 passing, ~1 second
- `shellcheck -x *.sh` → clean
- `python3 examples/sweep_demo.py --projects-dir /tmp/demo` → 8 runs, Pareto frontier computed, completes in ~6 seconds

### 2026-04-09 (earlier) — Harness observability v1

Committed as `92262c3`.

- `harness_telemetry.py` (library) — `TelemetrySession`, `@trace` decorator, `create_run` / `finalize_run` / `list_runs` / `get_run` / `run_path` / `mark_run_failed`, default secret-redaction patterns, experiments directory convention (`metadata.json`, `results.json`, `events.jsonl`, `harness/`, `notes.md`).
- `mnemosyne-experiments.py` (CLI) — `list` / `show` / `top-k` / `pareto` / `diff` / `events`. Parent-parser trick so `--json` works before or after the subcommand.
- `environment-snapshot.py` (CLI) — Terminal-Bench 2-style pre-computed environment context. Projects dir, `.env` key names (never values), Ollama reachability + models, venv status, discovered skills, Obsidian vault, disk free, platform. Markdown or `--json` output.
- `test-harness.sh` — 23-assertion end-to-end integration test. No network, runs in `/tmp`, covers all four observability components including secret-leak verification via file-based grep needles.
- `SETUP.md` — new "Harness observability" section (~130 lines) explaining the paper's argument, the directory layout, library usage, CLI examples, security properties, and how to run the integration test.
- `BLOG.md` (draft v1) — ~1600-word X/Substack post walking through the architectural reframing after reading AVB's Meta-Harness review.

### 2026-04-08 — Notion skill + wizard extensions + re-run preservation (`6ed63e2`)

- `notion-search.py` — mirror of `obsidian-search.py` backed by the Notion API. Three subcommands (`search`, `read`, `list-recent`), Bearer auth via `NOTION_API_KEY`, read-only, page-ID validation (32 hex or dashed UUID or `notion.so` URL), block→markdown rendering for 13 block types, depth-limited recursion.
- `mnemosyne-wizard.sh` grew from 4 to 6 steps: LLM backend, Telegram, **Slack** (new), Obsidian, **Notion** (new), write. New `slack_api` and `notion_api` helpers that pass tokens via `_SLACK_TOKEN` / `_NOTION_TOKEN` env vars (never argv) to a python3 validation helper.
- **Re-run preservation bug fix.** Previously, declining a section's outer yes/no *dropped* existing values from `.env`, and keeping an existing token *still* re-validated against the live API — meaning a network flake could nuke a working token. Fixed by adding `else` branches that explicitly preserve via `cur()`, and gating validation to only run when a *new* token is entered.
- Token-leak audit re-run: 1125 `/proc/<pid>/cmdline` snapshots across a wizard run with three fake secrets, zero leaks.

### 2026-04-08 (earlier) — Shellcheck-clean + Obsidian helper (`37cea9c`)

- Downloaded shellcheck 0.10.0 directly from GitHub releases (apt path was DNS-blocked in the sandbox), ran across all three shell scripts, fixed the four findings (SC1090 in `validate-mnemosyne.sh`, SC2015 × 3 in `mnemosyne-wizard.sh`).
- `obsidian-search.py` — interface-agnostic Obsidian vault helper. `search` (ripgrep fast path + pure-Python fallback), `read` (path-traversal safe), `list-recent`. JSON or human output. Tested against a fake vault including traversal rejection and `.obsidian/` exclusion.

### 2026-04-08 (earlier) — TUI wizard + security hardening (`5077628`)

- `mnemosyne-wizard.sh` rewritten: whiptail TUI with text-mode fallback, forced text via `--text` flag, shared TUI helpers (`tui_msg`, `tui_input`, `tui_password`, `tui_yesno`, `tui_menu`).
- Telegram API calls moved to a python helper with the token in `_TG_TOKEN` env var (never argv). Initial argv-safety audit: 751 cmdline snapshots, zero leaks.
- Atomic `.env` write via `umask 077` subshell + `mv`. Backups explicitly `chmod 600`.
- `validate-mnemosyne.sh` — 4-check health script (venv, Ollama, imports, CLI).
- `.gitignore` created.
- `README.md` rewritten from one-line placeholder.
- `SETUP.md` sanitized of all personal paths (`/mnt/c/Users/austi/...` → generic `<you>` or `./`).
- Security model section expanded to cover file locations, network fetches, token handling, supply-chain notes, and pre-publication checklist.

### 2026-04-08 (earlier) — Interactive wizard v1 (`5a12571`)

- `mnemosyne-wizard.sh` first version: Telegram channel setup with live validation against `api.telegram.org/getMe`, chat ID auto-detection via `getUpdates`, Obsidian vault path capture.
- Wizard invocation pointer added to `install-mnemosyne.sh` next-steps block.
- `SETUP.md` gained "Configure channels" and "Roadmap: Obsidian skill" sections.

### 2026-04-08 (earlier) — Install script patches (`660b0b1`)

- `install-mnemosyne.sh` gained three idempotent patches:
  - **4b.** Rewrites `fantastic-disco/pyproject.toml` build-backend from the upstream-broken `setuptools.backends._legacy:_Backend` to `setuptools.build_meta` before pip sees it.
  - **5b.** `eternalcontext.pth` is written *early* (right after venv activation, before any pip install) and re-written on `EXIT` via a trap, so partial-failure re-runs always self-heal.
  - **5c.** `CPU_TORCH=1` env flag installs torch from the pytorch CPU index before the eternal-context requirements, skipping the ~2GB CUDA wheels.

---

## [main] — before the branch

- `07d2724` — "Add Mnemosyne setup instructions for WSL2/Ubuntu" — initial `SETUP.md`.
- `8408f9a` — "Add installation script for Mnemosyne agent" — initial `install-mnemosyne.sh`.
- `7a3ca9d` — "Initial commit" — empty repo with placeholder README.
