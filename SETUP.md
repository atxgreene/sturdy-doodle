# Mnemosyne Setup (WSL2 / Ubuntu)

A reproducible install of the Mnemosyne local-agent stack: `eternal-context` (base agent, ICMS memory, SDI selection, channels) + `mnemosyne-consciousness` (TurboQuant, metacognition, dream consolidation, autobiography) on top of a local Ollama runtime.

This repo holds **only** the bootstrap script and wizard. The two Python packages live in their own repos and are cloned by `install-mnemosyne.sh`.

## What gets installed

| Component | Repo | Role |
|---|---|---|
| `eternal-context` | atxgreene/eternal-context | Base agent: 3-tier ICMS memory, SDI selection, model routing, channel adapters, tool registry |
| `fantastic-disco` | atxgreene/fantastic-disco @ `claude/review-mnemosyne-agent-5bb7m` | Consciousness extensions: TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling |
| Ollama | ollama.com | Local LLM runtime |
| `qwen3:8b` | via Ollama | Default local model (override with `MODEL=...`) |

`fantastic-disco` is **not** standalone — it imports and wraps `eternalcontext`. Both must be installed.

### Model choice

The default is `qwen3:8b`. Alternatives worth testing on your host:

| Model | Size | Context | Why |
|---|---|---|---|
| `qwen3:8b` | ~5 GB | 32K | Current default. Solid tool use, stable. |
| `gemma4:e4b` | ~5 GB | **128K** | Released April 2026, day-one Ollama support. 4× the context window of qwen3:8b — directly benefits ICMS retrieval. Multimodal (image input). Good candidate to A/B against qwen3:8b on your workload. |
| `gemma4:26b` | ~18 GB | 256K | MoE with ~4B activated params. More quality but needs more RAM. |

Override via env var:
```bash
MODEL=gemma4:e4b bash install-mnemosyne.sh
```

No recommendation to change the default until you've A/B'd on your actual workload — model quality for agent tool use is host-specific and hard to generalize.

## Prereqs

- WSL2 with Ubuntu (24.04 recommended; 22.04 works but you may need `python3.11` from deadsnakes)
- `git`, `curl`, `python3 >= 3.11`, `python3-venv`
- ~10 GB free disk for the model + venv
- Optional: GPU passthrough into WSL if you want faster inference (CPU works, just slower)

Install missing prereqs once:
```bash
sudo apt update
sudo apt install -y git curl python3 python3-venv python3-pip
```

## Run the bootstrap

Clone this repo (or copy the three scripts into a Mnemosyne-Setup folder of your choice) and run the bootstrap from inside it. From WSL:

```bash
git clone https://github.com/atxgreene/sturdy-doodle.git ~/mnemosyne-setup
cd ~/mnemosyne-setup
bash install-mnemosyne.sh
```

Optional overrides (all via env vars — no CLI flags):

```bash
# Pick a different model and install location
MODEL=llama3.1:8b PROJECTS_DIR=$HOME/code/mnemosyne bash install-mnemosyne.sh

# Skip the ~2GB CUDA torch download — install CPU-only wheels (~200MB) instead.
# Useful on hosts without GPU passthrough or when you don't care about
# embedding-model speed.
CPU_TORCH=1 bash install-mnemosyne.sh

# Fork override: track your own fork of either upstream repo
ETERNAL_REPO=https://github.com/you/eternal-context.git \
FANTASTIC_REPO=https://github.com/you/fantastic-disco.git \
FANTASTIC_BRANCH=main \
  bash install-mnemosyne.sh
```

The script is **idempotent** — re-running it pulls latest from both repos, re-syncs deps, and skips anything already done. Partial-failure re-runs always re-write the `eternalcontext.pth` link via an `EXIT` trap, so a crashed run never leaves the venv in a half-linked state.

## What it does, in order

1. Verifies `git`, `curl`, `python3 >= 3.11`, `python3-venv`.
2. Installs Ollama (official script) if missing; starts `ollama serve` if the daemon isn't responding on `:11434`.
3. Pulls `qwen3:8b` (or your override) if not already present.
4. Creates `~/projects/mnemosyne/`, clones both repos.
4b. **Patches** `fantastic-disco/pyproject.toml` — upstream ships `build-backend = "setuptools.backends._legacy:_Backend"` which doesn't exist; rewritten to `setuptools.build_meta` before pip ever sees it.
5. Creates venv at `~/projects/mnemosyne/.venv`.
5b. **Writes `eternalcontext.pth` early** (before any `pip install`) and re-writes on `EXIT` so partial-failure re-runs always self-heal.
5c. If `CPU_TORCH=1`, installs CPU-only torch wheels from the pytorch CPU index *before* the eternal-context requirements, so pip sees torch as already-satisfied and skips the ~2GB CUDA download.
6. `pip install -r eternal-context/skills/eternal-context/requirements.txt`
7. `pip install -e fantastic-disco[dev]`
8. Smoke-tests both imports (`import eternalcontext, mnemosyne`).

## Configure channels (wizard)

After the bootstrap finishes, run the interactive wizard to set up channel credentials:

```bash
bash mnemosyne-wizard.sh
# or, to force plain-text mode (no whiptail TUI):
bash mnemosyne-wizard.sh --text
```

The wizard auto-detects `whiptail` and uses it for a full-screen TUI when available; otherwise it falls back to plain prompts. Both paths produce the same `~/projects/mnemosyne/.env`.

**Six steps:**

1. **LLM backend** — Ollama host + model name, validated against a running daemon (optional; config still writes if Ollama is offline).
2. **Telegram channel** — bot token validated via `api.telegram.org/getMe`, chat ID auto-detected via `getUpdates` and selectable from a TUI menu.
3. **Slack channel** — bot token validated via `slack.com/api/auth.test`, optional Socket Mode app-level token + signing secret.
4. **Obsidian skill** — vault path (consumed by `obsidian-search.py`).
5. **Notion integration** — API key validated via `api.notion.com/v1/users/me` (consumed by `notion-search.py`).
6. **Write** — atomic `.env` write with `umask 077`, timestamped backup of any prior version.

**Re-run semantics** (these are important — they affect whether you accidentally nuke working config):

- Answering **"no"** to a section's outer prompt **preserves** whatever was in `.env` for that section. It does NOT remove the existing config.
- Answering **"yes"** then **"keep existing token"** preserves the existing credentials **without re-validating** against the live API. This means a network flake during a re-run cannot invalidate a working token.
- Answering **"yes"** then entering a new token **does** re-validate against the live API and writes the new token only if validation succeeds.
- To explicitly **remove** a credential, edit `.env` by hand.

The wizard:

1. **LLM backend** — confirms `OLLAMA_HOST` + `OLLAMA_MODEL`, validates the daemon is responding and the model is pulled.
2. **Telegram channel** — prompts for a bot token, validates it against `https://api.telegram.org/bot<token>/getMe`, then auto-detects your chat ID by polling `getUpdates` after you message the bot. (Other channels — Discord/Slack/REST — are roadmap; the wizard preserves any keys you add by hand.)
3. **Obsidian skill (preview)** — captures `OBSIDIAN_VAULT_PATH` for the upcoming Obsidian skill. Only writes the env var; the skill module itself isn't wired up yet (see roadmap below).
4. **Writes `~/projects/mnemosyne/.env`** with mode `600`, backing up any previous version to `.env.bak.<timestamp>`.

The wizard is **safe to re-run** — it reads the existing `.env`, offers current values as defaults, and preserves any keys it doesn't manage (so Discord/Slack/REST credentials you add by hand survive a re-run). Nothing in `.env` is ever committed to either repo.

## Boot the agent

```bash
source ~/projects/mnemosyne/.venv/bin/activate
set -a; . ~/projects/mnemosyne/.env; set +a   # load wizard-written creds

# CLI REPL (base agent — proves the stack is healthy)
cd ~/projects/mnemosyne/eternal-context/skills/eternal-context
python -m eternalcontext

# Multi-channel server (uses Telegram if you configured it via the wizard)
python -m eternalcontext.server

# Run consciousness-extension tests
cd ~/projects/mnemosyne/fantastic-disco
pytest mnemosyne/tests/ -v
```

### Entrypoint choice: base agent vs. ConsciousnessLoop

Two valid boot paths once the venv is ready:

- **`python -m eternalcontext`** — base agent only. ICMS, SDI, tools, channels. Skips the consciousness layer. Use this **first** to verify Ollama, ICMS, the `.pth` link, and the channel adapter all work.
- **`python -m mnemosyne`** (or programmatic `from mnemosyne import ConsciousnessLoop`) — wraps `eternalcontext` with TurboQuant, metacognition, dream consolidation, autobiography, behavioral coupling. This is the actual product surface.

Recommendation: validate `python -m eternalcontext` first; switch the daily-driver entrypoint to `ConsciousnessLoop` only after you've confirmed the base agent is healthy. Don't make the switch as the *first* run — too many things can fail at once.

## Compatibility

- The bootstrap installs **only** to `$PROJECTS_DIR` (default `~/projects/mnemosyne/`). It does not modify any other npm/Python/Go projects on the host. Only Ollama is installed system-wide, and only if missing.
- Default ports: Ollama listens on `127.0.0.1:11434`; Mnemosyne's REST channel (if you enable it) listens on `127.0.0.1:8765` by default. If those collide with anything you're running, override `OLLAMA_HOST` and `MNEMOSYNE_REST_PORT` in `.env`.
- `install-mnemosyne.sh` is idempotent and additive — running it on a host with other agent stacks installed won't disturb them.

## Uninstall

```bash
# Remove the Mnemosyne install entirely (does not touch Ollama or models)
rm -rf ~/projects/mnemosyne

# Optionally remove Ollama models
ollama rm qwen3:8b

# Optionally remove Ollama itself (Linux)
sudo rm /usr/local/bin/ollama
sudo rm -rf /usr/share/ollama
```

## Troubleshooting

**`python3: command not found`** → `sudo apt install -y python3 python3-venv python3-pip`

**`Python 3.11+ required` but you're on Ubuntu 22.04** →
```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt install -y python3.11 python3.11-venv
PY=python3.11 bash install-mnemosyne.sh   # then edit the script's python3 calls, or just upgrade WSL to 24.04
```

**`Ollama failed to start`** → check `/tmp/ollama.log`. Most common cause: another process on :11434, or WSL2 systemd not enabled. Workaround: `nohup ollama serve &` manually.

**`sentence-transformers` install hangs** → it pulls torch (~2 GB CUDA wheels by default). Re-run with `CPU_TORCH=1` to use the CPU-only index instead (~200 MB), or `pip install --no-cache-dir` if you're disk-constrained.

**`ImportError: No module named 'eternalcontext'`** → the `.pth` link didn't write. The bootstrap now writes it both early (before pip install) and again on `EXIT` via a trap, so this should never happen on a fresh run. If it does, just re-run the bootstrap — it will rewrite the link without re-installing anything.

**`pip install -e fantastic-disco[dev]` fails with `Cannot import 'setuptools.backends._legacy'`** → upstream pyproject.toml bug. The bootstrap auto-patches this on clone (step 4b). If you cloned manually, run:
```bash
sed -i 's|setuptools\.backends\._legacy:_Backend|setuptools.build_meta|' \
  ~/projects/mnemosyne/fantastic-disco/pyproject.toml
```

## First-run validation checklist

Run this from `~/projects/mnemosyne/eternal-context/skills/eternal-context` after `source ~/projects/mnemosyne/.venv/bin/activate`:

```bash
ollama list                       # confirm qwen3:8b row exists
python -c "import eternalcontext, mnemosyne; print('imports ok')"
python -m eternalcontext --help   # confirm CLI loads (no traceback)
python -m eternalcontext          # boot the REPL — should hit Ollama on first prompt
```

A successful boot proves: venv intact, `.pth` link working, eternal-context requirements installed, fantastic-disco editable install resolving, Ollama daemon up, model present. If any step fails, re-run the bootstrap — it's idempotent.

## Obsidian skill

The search/read logic ships here as `obsidian-search.py` — an interface-agnostic, stdlib-only Python helper. It works as a CLI today, and can be wrapped in whatever shape `eternal-context/skills/*` expects once you paste a representative existing skill.

### What the helper does

```bash
# Full-text search (uses ripgrep if installed, falls back to pure Python)
./obsidian-search.py search "daily note"                 # human output
./obsidian-search.py search "project alpha" --limit 5 --json   # machine output

# Read a note (refuses paths outside the vault)
./obsidian-search.py read Projects/mnemosyne.md

# Notes modified in the last N days
./obsidian-search.py list-recent --days 7
./obsidian-search.py list-recent --days 1 --json
```

Reads `OBSIDIAN_VAULT_PATH` from the environment (the wizard writes it), or `--vault` on the CLI. Zero dependencies beyond the Python stdlib. Exit codes: `0` ok, `2` usage, `3` path safety violation, `4` IO error.

### Security properties

- **Read-only.** No subcommand writes to the vault.
- **Path-traversal safe.** `read` resolves paths and refuses anything outside the vault root (`..`, absolute paths escaping the vault, symlinks that resolve out → all rejected with exit `3`). Tested against `../../../etc/passwd` and `/etc/passwd`.
- **Hidden dirs skipped.** `.obsidian/`, `.git/`, `.trash/`, and anything else starting with `.` is excluded from search and list-recent so config files never surface as results.
- **No shell interpolation of user input.** The ripgrep fast path invokes `rg` via `subprocess.run([...])` with a list, not a string, so the query is never passed through a shell.

### Wiring into `eternal-context`

The skill wrapper is a few lines once you paste an existing skill file so I can mirror the registration pattern. Expected shape:

```python
# hypothetical — needs the real eternal-context skill interface
import json, subprocess
from pathlib import Path

HELPER = Path("~/mnemosyne-setup/obsidian-search.py").expanduser()

def obsidian_search(query: str, limit: int = 10) -> list[dict]:
    r = subprocess.run(
        [str(HELPER), "--json", "search", query, "--limit", str(limit)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)

def obsidian_read(path: str) -> str:
    r = subprocess.run(
        [str(HELPER), "read", path],
        capture_output=True, text=True, check=True,
    )
    return r.stdout

def obsidian_list_recent(days: int = 7, limit: int = 50) -> list[dict]:
    r = subprocess.run(
        [str(HELPER), "--json", "list-recent", "--days", str(days), "--limit", str(limit)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)
```

Each of these becomes a registered tool in whatever way eternal-context actually does registration (decorator, YAML manifest, registry class, etc.). Paste one existing skill from `~/projects/mnemosyne/eternal-context/skills/<something>/` and the wrapper is a 5-minute follow-up commit.

## Notion skill

The Notion counterpart to `obsidian-search.py`. Same three-subcommand shape, same interface-agnostic philosophy, same wrapper story — so a skill layer can treat Obsidian and Notion as interchangeable "notes surfaces."

### What the helper does

```bash
# Full-text search across the workspace (pages + databases visible to the integration)
./notion-search.py search "quarterly review"
./notion-search.py search "project alpha" --limit 5 --json --kind page

# Read a page — accepts hex ID, dashed UUID, or notion.so URL
./notion-search.py read abcdef1234567890abcdef1234567890
./notion-search.py read 'https://www.notion.so/My-Page-abcdef1234567890abcdef1234567890'

# Pages edited in the last N days
./notion-search.py list-recent --days 7 --json
```

Reads `NOTION_API_KEY` from the environment (the wizard writes it). Stdlib-only — uses `urllib.request` with Bearer auth. Bot tokens are **never** interpolated into URLs; all API calls go through the HTTP Authorization header.

**Output formats** mirror `obsidian-search.py`:
- `search` → `{id, object, title, url, last_edited_time}`
- `read` → `{id, title, url, last_edited_time, markdown}` (block tree rendered to markdown-ish text)
- `list-recent` → `{id, title, url, last_edited_time}`

**Exit codes:** `0` ok, `2` usage/missing-token, `3` invalid page ID / URL escape, `4` HTTP error (auth failure, not-found), `5` network error.

### Security properties (tested)

- **Read-only.** No subcommand creates, updates, comments on, or deletes anything. All endpoints used are `GET` or `POST /search`.
- **Page ID validation.** `read` requires a 32-hex-char ID (dashed or bare) or a `notion.so` URL. URLs from other hosts are rejected with exit `3`. Garbage input (`not-a-uuid`, 32 non-hex chars, empty string) is rejected before any network call. Tested against:
  - `https://evil.com/abcdef1234567890abcdef1234567890` → **rejected** (not notion.so)
  - `/etc/passwd`-style traversal → rejected (not a valid ID format)
  - Empty string → rejected
- **Bearer auth, not URL-embedded.** The token goes in the `Authorization: Bearer <token>` HTTP header, never in the URL path or query string. No `/proc/<pid>/cmdline` exposure possible because no curl process is ever spawned.
- **Block renderer recursion limit.** `read` follows `has_children` up to depth 4 before printing `[max depth reached]`, so a pathological nesting can't exhaust memory.
- **`--token` CLI flag exists but is discouraged.** The wizard never uses it; it's for ad-hoc testing. The env-var path is the normal flow.

### Wiring into `eternal-context`

Same shape as the Obsidian wrapper:

```python
import json, subprocess
from pathlib import Path

HELPER = Path("~/mnemosyne-setup/notion-search.py").expanduser()

def notion_search(query: str, limit: int = 10) -> list[dict]:
    r = subprocess.run(
        [str(HELPER), "--json", "search", query, "--limit", str(limit)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)

def notion_read(page: str) -> dict:
    r = subprocess.run(
        [str(HELPER), "--json", "read", page],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)

def notion_list_recent(days: int = 7, limit: int = 20) -> list[dict]:
    r = subprocess.run(
        [str(HELPER), "--json", "list-recent", "--days", str(days), "--limit", str(limit)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)
```

The subprocess inherits `NOTION_API_KEY` from the parent process's environment (which itself loaded `.env` via `set -a; . .env; set +a`), so the skill wrapper doesn't need to handle the token.

### Setup gotcha: "share with integration"

When you create a Notion integration at <https://www.notion.com/my-integrations>, it gets an API key but **cannot see any of your pages** until you explicitly share them. For each page or database you want the helper to reach, open it in Notion and:

- Click the `⋯` menu in the top-right
- **Add connections** → pick your integration

This applies to search too — the `/v1/search` endpoint only returns pages the integration has been shared into. The wizard reminds you of this after validation succeeds.

### Open design questions (v2, not blocking v1)

1. **Embeddings vs. ripgrep.** v1 is ripgrep-only — fast, deterministic, no model dependency. If recall becomes a problem, v2 can add a sentence-transformers index (torch is already in the venv from the eternal-context requirements).
2. **Frontmatter exposure.** v1 treats YAML frontmatter as flat text inside the file. v2 could expose tags/aliases as separate query surfaces (`obsidian_search_by_tag`, etc.).
3. **Write tools.** Deliberately out of scope for v1 — daily-note appending and link rewriting are useful but high-blast-radius. Land them as a separate `obsidian-write` skill once the read path is proven.
4. **WSL path translation.** If the vault lives on the Windows side (e.g. `/mnt/c/Users/<you>/Documents/Obsidian`), file-watch performance over `9p` is mediocre. Acceptable for v1 (queries are point-in-time). If it becomes a problem, mirror to a WSL-native path under `~/` or poll with a longer interval.

## Harness observability

The four files `harness_telemetry.py`, `mnemosyne-experiments.py`, `environment-snapshot.py`, and `test-harness.sh` together form the **observability substrate** for Mnemosyne's harness. They were built after reviewing the Stanford Meta-Harness paper (Khattab et al., 2026) and are designed to be the prerequisite layer any future optimization system would run against.

### Why this layer exists

The Meta-Harness paper's central technical claim is that **compressed feedback is the core failure mode of prior optimizers** like DSPy and text-gradient tools: they reduce each candidate harness to a single scalar ("accuracy: 0.82") and try to improve from there, but the scalar discards the causal information an optimizer needs. Meta-Harness argues for the opposite: log **everything** — source code + raw scores + raw execution traces — to a filesystem-as-database, and let the proposer agent navigate the history with `grep` and `cat`.

`harness_telemetry.py` implements that principle for Mnemosyne. It never summarizes. Each tool call is written verbatim (modulo secret redaction) to an append-only JSONL event log, keyed by run id. The `mnemosyne-experiments` CLI gives you (and any future optimizer agent) the six operations the paper recommends for navigating that history: list, show, top-k, Pareto, diff, events.

### Directory layout

```
$PROJECTS_DIR/experiments/
  latest -> run_<id>/                    symlink to most recent (best effort)
  run_<YYYYMMDD-HHMMSS>-<slug>/
    metadata.json                        run_id, model, status, tags, notes, git sha
    results.json                         final metrics (written by finalize_run)
    events.jsonl                         append-only event log, one JSON object per line
    harness/                             optional: frozen snapshot of harness scripts
    notes.md                             optional: free-form notes
```

Every file is plain text, grep-friendly, and can be committed to its own git repo if you want a formal history of your harness evolution.

### Using the library from Python

```python
import harness_telemetry as ht

run_id = ht.create_run(
    model="gemma4:e4b",
    tags=["baseline", "telegram-enabled"],
    notes="first run after wizard setup",
    freeze_files=["install-mnemosyne.sh", "mnemosyne-wizard.sh"],
)

with ht.TelemetrySession(run_id) as sess:
    @sess.trace
    def obsidian_search(query, limit=10):
        # ... real implementation (or subprocess to obsidian-search.py) ...
        return {"matches": [...]}

    obsidian_search("project alpha")
    obsidian_search("quarterly review", limit=5)

    # Or log events manually for prompts, responses, state changes
    sess.log("prompt", args={"prompt": "What did I work on yesterday?"})
    sess.log("response", result={"text": "Based on Obsidian notes, ..."})

ht.finalize_run(run_id, metrics={
    "accuracy": 0.82,
    "latency_ms_avg": 1250.5,
    "turns_successful": 34,
    "turns_failed": 2,
})
```

### Navigating run history with the CLI

```bash
# All runs, most recent first
mnemosyne-experiments.py list

# Filter by tag or status
mnemosyne-experiments.py list --tag baseline --status completed

# Inspect one run
mnemosyne-experiments.py show run_20260409-053012-baseline

# Top 5 runs by any numeric metric
mnemosyne-experiments.py top-k 5 --metric accuracy --direction max
mnemosyne-experiments.py top-k 5 --metric latency_ms_avg --direction min

# Pareto frontier on multiple axes at once
#   (which runs are not strictly dominated by any other?)
mnemosyne-experiments.py pareto \
  --axes accuracy,latency_ms_avg \
  --directions max,min

# Diff two runs: metadata, metrics, harness code (if frozen)
mnemosyne-experiments.py diff run_20260409-053012-baseline run_20260409-053200-gemma4ab

# Read a run's event stream, filterable by type/tool/status
mnemosyne-experiments.py events run_20260409-053012-baseline --tool obsidian_search
mnemosyne-experiments.py events run_20260409-053012-baseline --event-type tool_call

# Machine-readable JSON mode works on every subcommand
mnemosyne-experiments.py list --json | jq '.[].run_id'
mnemosyne-experiments.py pareto --json --axes accuracy,latency_ms_avg --directions max,min
```

### Environment snapshot (the Terminal-Bench 2 pattern)

The Meta-Harness paper's most striking concrete result was on Terminal-Bench 2: the proposer agent discovered that instead of letting the LLM spend 2–4 turns exploring its environment via tool calls (`pwd`, `ls /app`, `which python`, etc.), you could **pre-compute a snapshot and inject it into the first LLM call**, eliminating the exploration phase entirely.

`environment-snapshot.py` implements that pattern for Mnemosyne:

```bash
./environment-snapshot.py              # human-readable markdown preamble
./environment-snapshot.py --json       # machine-readable dict
```

It snapshots the projects directory layout, the keys configured in `.env` (**names only — never values**), Ollama reachability and model list, venv health, available skills, Obsidian vault status (path + note count), disk free, and platform info. A skill wrapper can inject the markdown as a system prompt preamble, eliminating the agent's need to run discovery tool calls on every cold start.

### Security properties (all tested by `test-harness.sh`)

- **Secrets are redacted by key name** at event-write time. The default pattern list covers `token`, `secret`, `api_key`, `password`, `bearer`, `credential`, `signing_key` (case-insensitive substring). Verified in the integration test: a deliberately-planted secret is never present in any `events.jsonl` file, and the `<redacted>` marker IS present where expected.
- **Environment snapshot never emits `.env` values.** Only the key names cross the boundary. Verified in the integration test against a `.env` pre-seeded with fake token values that must not appear in either markdown or JSON output.
- **No summarization.** Every tool call is written raw. The paper's "compressed feedback loses information" claim is respected deliberately.
- **Read-only.** No subcommand modifies anything in `eternal-context` or `fantastic-disco`. The observability layer is purely additive — turn it off and the agent still works.
- **No network dependencies** for the integration test. `test-harness.sh` runs in a `/tmp` scratch dir, exercises all four components, and exits non-zero on any failure. Safe to wire into CI.

### What this does NOT do

- **No optimizer agent.** The paper runs Claude Code as the agentic proposer in a loop, reading and rewriting harness code. This repo ships the substrate the optimizer runs *against*, not the optimizer itself. That's its own project.
- **No auto-eval suite.** You'd need to pick ~50 realistic scenarios to score against. The `finalize_run` API accepts any metrics dict — bring your own evaluator.
- **No runtime modification of `eternal-context` or `fantastic-disco`.** The harness deployment layer is still the responsibility of `install-mnemosyne.sh` and `mnemosyne-wizard.sh`. Observability is a separate layer that sits alongside, not inside.

### Running the integration test

```bash
bash test-harness.sh          # 23 assertions, exits 0 on success
bash test-harness.sh --keep   # leave the fake PROJECTS_DIR in /tmp for inspection
```

The test creates three fake runs with deliberately diverse metrics (one baseline, one faster-but-less-accurate, one dominated), exercises every CLI subcommand, verifies secret redaction at the filesystem level, runs the environment snapshot twice (markdown and JSON), and asserts that no planted secret ever escapes into any output.

## Security model

**What gets stored where:**

| File | Location | Mode | Contains |
|---|---|---|---|
| `.env` | `$PROJECTS_DIR/.env` | `600` | Bot tokens, chat IDs, paths. **Never** committed. |
| `.env.bak.<timestamp>` | `$PROJECTS_DIR/.env.bak.*` | `600` | Backup of previous `.env`, written by the wizard before each rewrite. |
| `eternalcontext.pth` | venv `site-packages/` | default | Path string only. No secrets. |
| `ollama.log` | `$PROJECTS_DIR/ollama.log` | default | Ollama daemon stderr. Should not contain secrets but is not audited. |

**What `install-mnemosyne.sh` fetches over the network:**

1. `https://ollama.com/install.sh` → piped to `sh`. This is the official Ollama installer; review it at <https://github.com/ollama/ollama/blob/main/scripts/install.sh> before running if you don't trust curl-pipe-sh.
2. The two git repos pinned in `ETERNAL_REPO` and `FANTASTIC_REPO` (defaults: `atxgreene/eternal-context` and `atxgreene/fantastic-disco`). Override via env vars to use forks.
3. `pypi.org` for Python packages via pip. The CPU-torch path additionally hits `download.pytorch.org/whl/cpu`.
4. `registry.ollama.ai` for the model pull (one-time, ~5 GB for `qwen3:8b`).

No third-party shell installers, no telemetry, no callbacks.

**Token handling:**

- The wizard prompts for the Telegram bot token via `read -s` (text mode) or `whiptail --passwordbox` (TUI mode). The token is **never** echoed to the screen and **never** appears in shell history.
- API calls to `api.telegram.org` go through `python3 urllib.request` with the token passed as an env var (`_TG_TOKEN`), so the token does **not** appear in `argv` (`/proc/<pid>/cmdline`, world-readable). It only lives in `/proc/<pid>/environ` of the short-lived python process, which is mode `600`.
- The `.env` file is created with `umask 077` inside a subshell so the file is mode `600` from the moment it exists — there is no TOCTOU window where it briefly has wider perms.
- The wizard's preview screen masks tokens (`123456…(hidden)`) and counts but does not display preserved unknown keys (which may also be secrets).
- Backups (`.env.bak.<timestamp>`) are explicitly `chmod 600`'d after `cp`, since `cp` does not preserve permissions by default.

**Supply-chain notes:**

- Mnemosyne is pure Python + Ollama (Go). It does not pull from npm. The recurring `strapi-plugin-*` npm supply-chain attacks do not apply.
- The `pyproject.toml` patch (step 4b) is a build-system fix only; it does not change any runtime dependency.
- Pin and review the upstream commit hashes of `eternal-context` and `fantastic-disco` if you're deploying to a security-sensitive host. The bootstrap currently follows branches, not pinned commits — easy to change in `clone_or_pull`.

**Things to know if you fork or open-source this repo:**

- `.gitignore` excludes `.env`, `*.bak.*`, `*.log`, `__pycache__/`, `.venv/`, and a few other footguns. Re-check `git status` before any commit.
- The repo has **no LICENSE file by default** — pick one before publishing. If you're not sure, MIT is a safe default for tooling like this. Add it as `LICENSE` at the repo root.
- Run `git log -p -- .env*` before publishing. If `.env` has ever been committed, even briefly, the secret is in history forever — rotate the token, then [purge the history](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) or start fresh.
