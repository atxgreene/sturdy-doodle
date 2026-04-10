# Changelog

All notable changes to the Mnemosyne harness deployment repo. The format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are ISO 8601.

## [Unreleased] — `claude/setup-mnemosyne-consciousness-NZqQE`

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
