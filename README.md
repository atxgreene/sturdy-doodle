# Mnemosyne Setup

Reproducible bootstrap for the Mnemosyne local-agent stack on WSL2 / Ubuntu / Linux.

**Mnemosyne is a local-first, consciousness-aware agent framework** with a built-in Meta-Harness-aligned observability substrate. This repo is the pip-installable Python package that ties everything together:

- **Agent core:** routing brain, SQLite+FTS5 memory with ICMS 3-tier (L1/L2/L3), agentskills.io-compatible skill registry, model-agnostic backend (Ollama + any OpenAI-compatible HTTP endpoint — OpenRouter, Anthropic, Nous Portal, Together, Fireworks, vLLM, LM Studio)
- **Consciousness layer:** integrates with fantastic-disco's TurboQuant / metacognition / dream consolidation / autobiography / behavioral coupling (graceful no-op when not installed)
- **Meta-Harness observability:** raw-trace telemetry, experiment runs, parameter sweeps, scenario evaluation, Pareto frontier analysis — designed for optimization, not dashboards
- **Deployment:** installer, interactive wizard (whiptail TUI), validate/health check, skill helpers for Obsidian + Notion

Zero runtime dependencies. Stdlib only. Honest comparison with Hermes Agent, OpenClaw, and the observability tool landscape in [`docs/POSITIONING.md`](./docs/POSITIONING.md).

### Agent framework (v1)

| Module | Role |
|---|---|
| `mnemosyne_brain.py` | Routing orchestrator. One turn = retrieve memory → build prompt (+env snapshot +AGENTS.md/TOOLS.md) → call model → dispatch tools → feed back → respond → persist. Integrates with eternal-context ICMS + fantastic-disco ConsciousnessLoop when installed. |
| `mnemosyne_memory.py` | SQLite+FTS5 memory with ICMS 3-tier (L1 hot / L2 warm / L3 cold). Promote/demote/evict operations. Every op logs to telemetry. |
| `mnemosyne_models.py` | Model-agnostic backend. One `chat()` API for Ollama + any OpenAI-compatible HTTP endpoint: OpenRouter, OpenAI, Anthropic, Together, Fireworks, Nous Portal, vLLM, LM Studio. Stdlib only. |
| `mnemosyne_skills.py` | agentskills.io-compatible skill registry. Loads markdown skill files, in-process `@register_python` decorators, and installed $PATH commands. Self-improvement: `record_learned_skill()` writes a new skill file. |
| `mnemosyne_config.py` | Shared config single-source-of-truth (PROJECTS_DIR, .env parsing, Ollama host). |

### Observability substrate (Meta-Harness aligned)

| Module | Role |
|---|---|
| `harness_telemetry.py` | Observability library + experiments-directory convention. `create_run` / `finalize_run` / `TelemetrySession` / `@trace` decorator. Secrets redacted by key name. **Events written raw — no summarization**, per the paper's core argument. |
| `mnemosyne_experiments.py` | CLI over the experiments tree. `list` / `show` / `top-k` / `pareto` (with `--plot`) / `diff` / `events` / `aggregate`. Entry point: `mnemosyne-experiments`. |
| `environment_snapshot.py` | First-turn context injection. Pre-computes projects dir, `.env` key names (never values), Ollama models, **GPU info**, model architecture classification (DeltaNet-hybrid vs standard-attention), venv, skills, vault, disk. Entry point: `environment-snapshot`. |
| `harness_sweep.py` | Deterministic parameter-space grid search using the telemetry substrate. |
| `scenario_runner.py` | JSONL-driven evaluation harness with pluggable judges. |
| `mnemosyne_pipeline.py` | OBSERVE→EVALUATE→SWEEP→COMPARE→INSPECT in one call. Entry point: `mnemosyne-pipeline`. |

### Skill helpers (stdlib-only, read-only)

| Module | Role |
|---|---|
| `obsidian_search.py` | Obsidian vault helper. `search` / `read` / `list-recent`. Ripgrep fast-path, pure-Python fallback. Path-traversal safe. Entry point: `obsidian-search`. |
| `notion_search.py` | Notion workspace helper. Same shape as obsidian-search, Bearer-auth via `NOTION_API_KEY`. Entry point: `notion-search`. |

### Deployment

| Script | Role |
|---|---|
| `install-mnemosyne.sh` | Unattended bootstrap. Installs Ollama, pulls a model, clones both upstream repos, builds a Python venv, `pip install -e .` of this harness repo, smoke-tests imports. Idempotent. |
| `mnemosyne-wizard.sh` | Interactive post-install wizard (whiptail TUI with text fallback). Six steps: LLM backend, Telegram, Slack, Obsidian, Notion, write `~/projects/mnemosyne/.env`. |
| `validate-mnemosyne.sh` | Health-check. Four checks (Ollama daemon, model present, Python imports, agent CLI loads). Non-zero exit on failure. |
| `test-harness.sh` | End-to-end integration test. 29 assertions. No network. |

The two Python packages live in their own repos and are cloned by the bootstrap:

- [`atxgreene/eternal-context`](https://github.com/atxgreene/eternal-context) — base agent (ICMS 3-tier memory, SDI selection, tool registry, channel adapters)
- [`atxgreene/fantastic-disco`](https://github.com/atxgreene/fantastic-disco) — `mnemosyne-consciousness` extensions (TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling)

Override either repo URL via `ETERNAL_REPO=` / `FANTASTIC_REPO=` / `FANTASTIC_BRANCH=` env vars when running `install-mnemosyne.sh` to track a fork.

## Quick start

```bash
git clone https://github.com/atxgreene/sturdy-doodle.git ~/mnemosyne-setup
cd ~/mnemosyne-setup
bash install-mnemosyne.sh        # clones + builds venv + pip install -e . of this repo
bash mnemosyne-wizard.sh         # configure Telegram / Slack / Obsidian / Notion
bash validate-mnemosyne.sh       # confirm everything's healthy
```

After the install, these commands are on `$PATH` inside the venv:

```
mnemosyne-experiments   # list / show / top-k / pareto / diff / events / aggregate
mnemosyne-pipeline      # observe → evaluate → sweep → compare → inspect in one shot
environment-snapshot    # first-turn environment preamble (markdown or --json)
obsidian-search         # search / read / list-recent against your vault
notion-search           # same shape, backed by the Notion API
harness-telemetry       # library smoke test
```

Then boot the agent:

```bash
source ~/projects/mnemosyne/.venv/bin/activate
set -a; . ~/projects/mnemosyne/.env; set +a
cd ~/projects/mnemosyne/eternal-context/skills/eternal-context
python -m eternalcontext
```

Verify the observability stack is healthy (no network required, runs in a sandbox `/tmp` dir):

```bash
bash test-harness.sh           # 29 integration assertions
python3 tests/test_all.py      # 49 unit tests (1 second)
```

Full walkthrough — channel setup, the Obsidian/Notion skills, harness observability architecture, security model — in [`SETUP.md`](./SETUP.md).

## Security TL;DR

- **`.env` lives outside both upstream repos** (`~/projects/mnemosyne/.env`) and is `.gitignore`d here. Mode `600`. The wizard creates it via `umask 077` so there's no TOCTOU window where the file is briefly world-readable.
- **Channel tokens never appear in `argv`.** Telegram, Slack, and Notion API calls all go through `python3 urllib.request` with the token passed as an env var (`_TG_TOKEN`, `_SLACK_TOKEN`, `_NOTION_TOKEN`) — not via curl URLs or command-line arguments. Verified: 1125 `/proc/<pid>/cmdline` snapshots across a wizard run with three fake secrets, zero leaks.
- **No third-party shell installers** beyond the official Ollama installer (`https://ollama.com/install.sh`). All Python deps come from PyPI.
- **No telemetry, no callbacks, no auto-updates.**
- **No LICENSE shipped by default** — pick one before publishing your fork. MIT is a reasonable default for tooling.

Full security model in [`SETUP.md`](./SETUP.md#security-model).

## Requirements

- WSL2 Ubuntu 24.04 (or any Debian-ish Linux with `python3 >= 3.11`, `python3-venv`, `git`, `curl`)
- ~10 GB free disk for the model + venv
- Optional: `whiptail` for the TUI wizard (pre-installed on most Ubuntu); `--text` mode works without it
- Optional: GPU passthrough for faster inference (CPU works, just slower; use `CPU_TORCH=1` to skip the ~2GB CUDA wheels)
