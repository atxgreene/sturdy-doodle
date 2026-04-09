# Mnemosyne Setup

Reproducible bootstrap for the Mnemosyne local-agent stack on WSL2 / Ubuntu / Linux.

This repo holds nine files that together form a complete harness-deployment + observability stack for a local-first LLM agent:

**Deployment layer:**

| Script | Role |
|---|---|
| `install-mnemosyne.sh` | Unattended bootstrap. Installs Ollama, pulls a model, clones both upstream repos, builds a Python venv, smoke-tests the imports. Idempotent. |
| `mnemosyne-wizard.sh` | Interactive post-install wizard (whiptail TUI with text fallback). Six steps: LLM backend, Telegram, Slack, Obsidian, Notion, write `~/projects/mnemosyne/.env`. |
| `validate-mnemosyne.sh` | Health-check. Runs four checks (Ollama daemon, model present, Python imports, agent CLI loads) and exits non-zero on failure. Useful for `make check` / CI. |

**Skill helpers** (interface-agnostic, Python stdlib only):

| Script | Role |
|---|---|
| `obsidian-search.py` | Obsidian vault helper. `search` / `read` / `list-recent` subcommands, read-only, path-traversal safe, JSON or human output. Uses ripgrep if available, pure-Python fallback. See [`SETUP.md`](./SETUP.md#obsidian-skill). |
| `notion-search.py` | Same shape as `obsidian-search.py`, backed by the Notion API. Read-only, Bearer-auth via `NOTION_API_KEY` env var. See [`SETUP.md`](./SETUP.md#notion-skill). |

**Harness observability** (see [`SETUP.md`](./SETUP.md#harness-observability) for the architecture — inspired directly by the Stanford Meta-Harness paper, 2026):

| Script | Role |
|---|---|
| `harness_telemetry.py` | Observability library + experiments-directory convention. `create_run` / `finalize_run` / `TelemetrySession` / `@trace` decorator. Secrets are redacted by key name at write time. Events are written raw (no summarization — deliberately; see the paper's "compressed feedback is the failure mode" argument). |
| `mnemosyne-experiments.py` | CLI over the experiments tree. `list` / `show` / `top-k` / `pareto` / `diff` / `events` subcommands. Implements the six operations the Meta-Harness paper recommends for navigating a run history. |
| `environment-snapshot.py` | Pre-computes `$PROJECTS_DIR` / `.env` keys / Ollama models / venv / skills / vault / disk into a single markdown preamble or JSON dict. Mirrors the Meta-Harness "Terminal-Bench 2" optimization: inject the environment into the first LLM call instead of letting the agent discover it across 2–4 exploratory turns. **Never emits `.env` values.** |
| `test-harness.sh` | End-to-end integration test. 23 assertions covering all four observability components — run creation, event logging, secret redaction, CLI semantics (list, top-k with direction, Pareto frontier with multi-axis dominance, diff, events filtering), environment snapshot output + secret safety. Safe to run repeatedly, no network. |

The two Python packages live in their own repos and are cloned by the bootstrap:

- [`atxgreene/eternal-context`](https://github.com/atxgreene/eternal-context) — base agent (ICMS 3-tier memory, SDI selection, tool registry, channel adapters)
- [`atxgreene/fantastic-disco`](https://github.com/atxgreene/fantastic-disco) — `mnemosyne-consciousness` extensions (TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling)

Override either repo URL via `ETERNAL_REPO=` / `FANTASTIC_REPO=` / `FANTASTIC_BRANCH=` env vars when running `install-mnemosyne.sh` to track a fork.

## Quick start

```bash
git clone https://github.com/atxgreene/sturdy-doodle.git ~/mnemosyne-setup
cd ~/mnemosyne-setup
bash install-mnemosyne.sh        # ~10 min on a fresh box, mostly the model pull
bash mnemosyne-wizard.sh         # configure Telegram + Obsidian path
bash validate-mnemosyne.sh       # confirm everything's healthy
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
bash test-harness.sh     # 23 assertions across all four components
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
