# Mnemosyne Setup

Reproducible bootstrap for the Mnemosyne local-agent stack on WSL2 / Ubuntu / Linux.

This repo holds three small scripts:

| Script | Role |
|---|---|
| `install-mnemosyne.sh` | Unattended bootstrap. Installs Ollama, pulls a model, clones both upstream repos, builds a Python venv, smoke-tests the imports. Idempotent. |
| `mnemosyne-wizard.sh` | Interactive post-install wizard (whiptail TUI with text fallback). Configures channel credentials and writes `~/projects/mnemosyne/.env`. |
| `validate-mnemosyne.sh` | Health-check. Runs four checks (Ollama daemon, model present, Python imports, agent CLI loads) and exits non-zero on failure. Useful for `make check` / CI. |

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

Full walkthrough — including channel setup, troubleshooting, and the Obsidian-skill roadmap — in [`SETUP.md`](./SETUP.md).

## Security TL;DR

- **`.env` lives outside both upstream repos** (`~/projects/mnemosyne/.env`) and is `.gitignore`d here. Mode `600`. The wizard creates it via `umask 077` so there's no TOCTOU window where the file is briefly world-readable.
- **Telegram tokens never appear in `argv`.** All API calls go through `python3 urllib.request` with the token passed as an env var, not through `curl <url>` (where the token would be visible in `/proc/<pid>/cmdline`).
- **No third-party shell installers** beyond the official Ollama installer (`https://ollama.com/install.sh`). All Python deps come from PyPI.
- **No telemetry, no callbacks, no auto-updates.**
- **No LICENSE shipped by default** — pick one before publishing your fork. MIT is a reasonable default for tooling.

Full security model in [`SETUP.md`](./SETUP.md#security-model).

## Requirements

- WSL2 Ubuntu 24.04 (or any Debian-ish Linux with `python3 >= 3.11`, `python3-venv`, `git`, `curl`)
- ~10 GB free disk for the model + venv
- Optional: `whiptail` for the TUI wizard (pre-installed on most Ubuntu); `--text` mode works without it
- Optional: GPU passthrough for faster inference (CPU works, just slower; use `CPU_TORCH=1` to skip the ~2GB CUDA wheels)
