#!/usr/bin/env bash
# ==============================================================================
#  install-mnemosyne.sh
#  Reproducible bootstrap for the Mnemosyne agent (eternal-context + fantastic-disco)
#  on WSL2 Ubuntu / Linux / macOS.
#
#  What this script does (idempotent — safe to re-run):
#    1. Verifies prerequisites: git, python>=3.11, pip, curl
#    2. Installs Ollama if missing, starts the daemon
#    3. Pulls the local model (default: qwen3:8b)
#    4. Clones eternal-context (base agent) and fantastic-disco (extensions)
#       into ~/projects/mnemosyne/
#    4b. Patches fantastic-disco/pyproject.toml build-backend (upstream bug)
#    5. Creates a Python venv at ~/projects/mnemosyne/.venv
#    5b. Writes eternalcontext.pth EARLY + on EXIT (self-heals partial runs)
#    5c. Optionally installs CPU-only torch (CPU_TORCH=1)
#    6. Installs eternal-context requirements + fantastic-disco as editable
#    7. Smoke-tests both imports, prints next steps
#
#  This script does NOT touch OpenClaw or any existing workspace.
#
#  Usage:
#    bash install-mnemosyne.sh                       # default: qwen3:8b model
#    MODEL=llama3.1:8b bash install-mnemosyne.sh     # override model
#    PROJECTS_DIR=$HOME/code bash install-mnemosyne.sh   # override location
#    CPU_TORCH=1 bash install-mnemosyne.sh           # force CPU-only torch wheels
#                                                    # (~200MB vs ~2GB CUDA)
#
#  All config is via env vars (no CLI flags):
#    PROJECTS_DIR        install root (default: $HOME/projects/mnemosyne)
#    MODEL               Ollama model (default: qwen3:8b)
#    CPU_TORCH=1         install CPU-only torch wheels
#    ETERNAL_REPO        eternal-context git URL (override for forks)
#    FANTASTIC_REPO      fantastic-disco git URL (override for forks)
#    FANTASTIC_BRANCH    fantastic-disco branch to track
# ==============================================================================

set -euo pipefail

# Resolve where this script lives so we can point at sibling files (wizard).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- Config (override via env vars) ------------------------------------------
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects/mnemosyne}"
MODEL="${MODEL:-qwen3:8b}"
PY_MIN_MAJOR=3
PY_MIN_MINOR=11
ETERNAL_REPO="${ETERNAL_REPO:-https://github.com/atxgreene/eternal-context.git}"
FANTASTIC_REPO="${FANTASTIC_REPO:-https://github.com/atxgreene/fantastic-disco.git}"
FANTASTIC_BRANCH="${FANTASTIC_BRANCH:-claude/review-mnemosyne-agent-5bb7m}"
CPU_TORCH="${CPU_TORCH:-0}"

# ---- Pretty output ------------------------------------------------------------
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf "%s==>%s %s\n" "$c_blue"  "$c_off" "$*"; }
ok()   { printf "%s✓%s   %s\n" "$c_green" "$c_off" "$*"; }
warn() { printf "%s!%s   %s\n" "$c_yellow" "$c_off" "$*"; }
err()  { printf "%s✗%s   %s\n" "$c_red"   "$c_off" "$*" 1>&2; }
die()  { err "$*"; exit 1; }

# ---- Step 1: prerequisites ---------------------------------------------------
log "Checking prerequisites"

command -v git    >/dev/null || die "git not found. Install: sudo apt install -y git"
command -v curl   >/dev/null || die "curl not found. Install: sudo apt install -y curl"
command -v python3 >/dev/null || die "python3 not found. Install: sudo apt install -y python3 python3-venv python3-pip"

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=${PY_VER%.*}; PY_MINOR=${PY_VER#*.}
if [ "$PY_MAJOR" -lt "$PY_MIN_MAJOR" ] || { [ "$PY_MAJOR" -eq "$PY_MIN_MAJOR" ] && [ "$PY_MINOR" -lt "$PY_MIN_MINOR" ]; }; then
  die "Python $PY_MIN_MAJOR.$PY_MIN_MINOR+ required (found $PY_VER). On Ubuntu 22.04: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install -y python3.11 python3.11-venv"
fi
ok "Python $PY_VER"

python3 -c 'import venv' 2>/dev/null || die "python3-venv not installed. Run: sudo apt install -y python3-venv"
ok "python3-venv available"

# ---- Step 2: Ollama ----------------------------------------------------------
log "Ensuring Ollama is installed"
if ! command -v ollama >/dev/null; then
  warn "Ollama not found — installing via official script"
  curl -fsSL https://ollama.com/install.sh | sh
else
  ok "Ollama already installed: $(ollama --version 2>/dev/null || echo present)"
fi

# Start ollama daemon if not running (WSL doesn't auto-start systemd services on older setups)
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  warn "Ollama daemon not responding — starting in background"
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 3
  curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || die "Ollama failed to start. Check /tmp/ollama.log"
fi
ok "Ollama daemon responding on :11434"

# ---- Step 3: pull local model ------------------------------------------------
log "Ensuring local model: $MODEL"
if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
  ok "Model $MODEL already pulled"
else
  warn "Pulling $MODEL (this can take several minutes / GBs)"
  ollama pull "$MODEL"
fi

# ---- Step 4: clone repos -----------------------------------------------------
log "Setting up project directory: $PROJECTS_DIR"
mkdir -p "$PROJECTS_DIR"

clone_or_pull() {
  local url="$1" dest="$2" branch="${3:-}"
  if [ -d "$dest/.git" ]; then
    ok "$(basename "$dest") already cloned — pulling latest"
    git -C "$dest" fetch --all --quiet
    if [ -n "$branch" ]; then
      git -C "$dest" checkout "$branch" --quiet
    fi
    git -C "$dest" pull --ff-only --quiet || warn "Could not fast-forward $(basename "$dest") — leaving as-is"
  else
    log "Cloning $(basename "$dest")"
    if [ -n "$branch" ]; then
      git clone --branch "$branch" "$url" "$dest"
    else
      git clone "$url" "$dest"
    fi
  fi
}

clone_or_pull "$ETERNAL_REPO"   "$PROJECTS_DIR/eternal-context"
clone_or_pull "$FANTASTIC_REPO" "$PROJECTS_DIR/fantastic-disco" "$FANTASTIC_BRANCH"

# ---- Step 4b: patch fantastic-disco pyproject.toml ---------------------------
# Upstream bug: build-backend points at setuptools.backends._legacy:_Backend,
# which doesn't exist and makes `pip install -e .` fail at the build-system
# resolution step. Rewrite to the real entrypoint BEFORE pip ever sees it.
# Idempotent: re-running after the fix is a no-op.
DISCO_PYPROJECT="$PROJECTS_DIR/fantastic-disco/pyproject.toml"
if [ -f "$DISCO_PYPROJECT" ] && grep -q 'setuptools\.backends\._legacy:_Backend' "$DISCO_PYPROJECT"; then
  warn "Patching fantastic-disco/pyproject.toml build-backend (upstream bug)"
  cp "$DISCO_PYPROJECT" "$DISCO_PYPROJECT.bak"
  sed -i 's|setuptools\.backends\._legacy:_Backend|setuptools.build_meta|' "$DISCO_PYPROJECT"
  ok "build-backend patched -> setuptools.build_meta"
fi

# ---- Step 5: Python venv -----------------------------------------------------
VENV="$PROJECTS_DIR/.venv"
log "Creating Python venv at $VENV"
if [ -d "$VENV" ] && [ ! -f "$VENV/bin/activate" ]; then
  warn "Found broken venv at $VENV (no bin/activate) — recreating"
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
[ -f "$VENV/bin/activate" ] || die "venv creation failed. Try: sudo apt install -y python3-venv python3.12-venv"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
ok "venv active: $(python --version)"

# ---- Step 5b: link eternalcontext into venv (EARLY + idempotent) -------------
# The eternalcontext package lives at eternal-context/skills/eternal-context,
# not at the repo root, so `import eternalcontext` only works if a .pth file
# in site-packages points there. Write it BEFORE any pip install so a mid-run
# pip crash still leaves a working import path on retry, and re-write on EXIT
# so partial-failure re-runs always self-heal.
write_eternal_pth() {
  local site_packages pth
  site_packages=$(python -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null) || return 0
  [ -n "$site_packages" ] && [ -d "$site_packages" ] || return 0
  pth="$site_packages/eternalcontext.pth"
  printf '%s\n' "$PROJECTS_DIR/eternal-context/skills/eternal-context" > "$pth"
}
write_eternal_pth
ok "Linked eternalcontext into venv (will re-link on exit)"
trap 'write_eternal_pth || true' EXIT

python -m pip install --upgrade pip wheel setuptools

# ---- Step 5c: optional CPU-only torch ----------------------------------------
# CUDA torch wheels are ~2GB; for CPU-only hosts (or if you don't want the
# CUDA download) set CPU_TORCH=1. We install torch from the cpu index BEFORE
# the eternal-context requirements so pip sees it as already-satisfied and
# won't re-resolve to the default index.
if [ "$CPU_TORCH" = "1" ]; then
  warn "CPU_TORCH=1 — installing CPU-only torch wheels (skipping CUDA download)"
  pip install --index-url https://download.pytorch.org/whl/cpu torch
  ok "CPU-only torch installed"
fi

# ---- Step 6: install both packages -------------------------------------------
log "Installing eternal-context dependencies"
pip install -r "$PROJECTS_DIR/eternal-context/skills/eternal-context/requirements.txt"

log "Installing fantastic-disco (consciousness extensions) in editable mode"
pip install -e "$PROJECTS_DIR/fantastic-disco[dev]"

# ---- Step 6b: install this harness repo (Mnemosyne) ----------------------
# If this script is running FROM a clone of Mnemosyne (the canonical case),
# install it into the same venv so that the harness CLI commands
# (mnemosyne-experiments, mnemosyne-pipeline, environment-snapshot, obsidian-search,
#  notion-search, harness-telemetry) are available on $PATH and the library
# modules import cleanly without sys.path shims. Zero runtime dependencies —
# installing this is strictly adding declared metadata + entry points.
if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
  log "Installing mnemosyne-harness (this repo) in editable mode"
  pip install -e "$SCRIPT_DIR" || warn "harness install failed; CLIs will only work as python3 scripts"
else
  warn "pyproject.toml not found next to this script — skipping harness install"
fi

# ---- Step 7: smoke test ------------------------------------------------------
log "Smoke test: importing both packages"
python - <<'PY'
import importlib, sys
try:
    importlib.import_module("eternalcontext")
    print("  eternalcontext: OK")
except Exception as e:
    print("  eternalcontext: FAIL ->", e); sys.exit(1)
try:
    importlib.import_module("mnemosyne")
    print("  mnemosyne (fantastic-disco): OK")
except Exception as e:
    print("  mnemosyne: FAIL ->", e); sys.exit(1)
PY
ok "Both packages importable"

# ---- Done --------------------------------------------------------------------
cat <<EOF

${c_green}✓ Mnemosyne installed.${c_off}

Project root: $PROJECTS_DIR
venv:         $VENV
Local model:  $MODEL  (Ollama on http://127.0.0.1:11434)

Next steps:
  source $VENV/bin/activate

  # 1. Configure Telegram (and Obsidian path) interactively:
  bash $SCRIPT_DIR/mnemosyne-wizard.sh

  # 2. Load the .env the wizard wrote and boot the base agent:
  set -a; . $PROJECTS_DIR/.env; set +a
  cd $PROJECTS_DIR/eternal-context/skills/eternal-context
  python -m eternalcontext

  # Multi-channel server (Telegram/Discord/Slack/REST) — needs the env loaded above
  python -m eternalcontext.server

  # Tests for the consciousness extensions
  cd $PROJECTS_DIR/fantastic-disco && pytest mnemosyne/tests/ -v

OpenClaw was not touched. To uninstall this later, just delete:
  $PROJECTS_DIR

EOF
