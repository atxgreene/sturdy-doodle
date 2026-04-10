#!/usr/bin/env bash
# ==============================================================================
#  validate-mnemosyne.sh
#  Health check for an installed Mnemosyne stack.
#
#  Runs four checks and reports each:
#    1. venv exists and python is callable
#    2. Ollama daemon is responding (and the configured model is pulled)
#    3. eternalcontext + mnemosyne both import cleanly
#    4. `python -m eternalcontext --help` exits 0 (CLI loads without traceback)
#
#  Exits 0 if everything passes, non-zero otherwise. Suitable for `make check`,
#  CI smoke tests, or post-update sanity checks.
#
#  Usage:
#    bash validate-mnemosyne.sh
#    bash validate-mnemosyne.sh --quiet     # only print failures
#    PROJECTS_DIR=$HOME/code/mnemosyne bash validate-mnemosyne.sh
# ==============================================================================

set -uo pipefail

PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects/mnemosyne}"
VENV="$PROJECTS_DIR/.venv"
ENV_FILE="$PROJECTS_DIR/.env"
ETERNAL_PKG="$PROJECTS_DIR/eternal-context/skills/eternal-context"

QUIET=0
for arg in "$@"; do
  case "$arg" in
    -q|--quiet) QUIET=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS+1)); [ "$QUIET" = 1 ] || printf '%s✓%s   %s\n' "$c_green" "$c_off" "$*"; }
warn() { WARN=$((WARN+1)); printf '%s!%s   %s\n' "$c_yellow" "$c_off" "$*"; }
fail() { FAIL=$((FAIL+1)); printf '%s✗%s   %s\n' "$c_red"   "$c_off" "$*"; }
note() { [ "$QUIET" = 1 ] || printf '%s    %s%s\n' "$c_dim" "$*" "$c_off"; }

# Load .env so OLLAMA_HOST/OLLAMA_MODEL etc. are available — but don't choke
# if it's missing (validate should still work for partial installs).
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"

# ---- check 1: venv ------------------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
  PY_VER=$("$VENV/bin/python" --version 2>&1)
  pass "venv: $VENV ($PY_VER)"
else
  fail "venv missing or broken at $VENV"
  note "Re-run install-mnemosyne.sh to recreate it."
fi

# ---- check 2: Ollama daemon + model ------------------------------------------
OLLAMA_STATUS=$(python3 - "$OLLAMA_HOST" "$OLLAMA_MODEL" <<'PY' 2>/dev/null
import sys, json, urllib.request
host, model = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
        data = json.load(r)
except Exception:
    print("unreachable")
    sys.exit(0)
names = [m.get("name", "") for m in data.get("models", [])]
print("present" if model in names else "missing", *names, sep="\t")
PY
)

case "$(printf '%s' "$OLLAMA_STATUS" | cut -f1)" in
  present)
    pass "ollama: $OLLAMA_HOST responding, model $OLLAMA_MODEL present"
    ;;
  missing)
    warn "ollama: $OLLAMA_HOST responding but $OLLAMA_MODEL not pulled"
    ALL_MODELS=$(printf '%s' "$OLLAMA_STATUS" | cut -f2- | tr '\t' ',')
    [ -n "$ALL_MODELS" ] && note "available: $ALL_MODELS"
    note "fix: ollama pull $OLLAMA_MODEL"
    ;;
  unreachable|*)
    fail "ollama: $OLLAMA_HOST not responding"
    note "fix: start the daemon — 'ollama serve' or 'sudo systemctl start ollama'"
    ;;
esac

# ---- check 3: imports ---------------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
  if IMPORT_OUT=$("$VENV/bin/python" - <<'PY' 2>&1
import sys
errs = []
for m in ("eternalcontext", "mnemosyne"):
    try:
        __import__(m)
    except Exception as e:
        errs.append(f"{m}: {type(e).__name__}: {e}")
if errs:
    print("\n".join(errs))
    sys.exit(1)
PY
  ); then
    pass "imports: eternalcontext + mnemosyne"
  else
    fail "imports: failed"
    printf '%s\n' "$IMPORT_OUT" | sed 's/^/    /'
    note "fix: re-run install-mnemosyne.sh"
  fi
else
  fail "imports: skipped (venv missing)"
fi

# ---- check 4: agent CLI loads ------------------------------------------------
if [ -x "$VENV/bin/python" ] && [ -d "$ETERNAL_PKG" ]; then
  if (cd "$ETERNAL_PKG" && "$VENV/bin/python" -m eternalcontext --help) >/dev/null 2>&1; then
    pass "cli: python -m eternalcontext --help loaded"
  else
    fail "cli: python -m eternalcontext --help failed"
    note "fix: cd $ETERNAL_PKG && python -m eternalcontext --help (read the traceback)"
  fi
else
  fail "cli: skipped (venv or package dir missing)"
fi

# ---- summary ------------------------------------------------------------------
echo
TOTAL=$((PASS + FAIL + WARN))
if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
  printf '%s%d/%d checks passed%s\n' "$c_green" "$PASS" "$TOTAL" "$c_off"
  exit 0
elif [ "$FAIL" -eq 0 ]; then
  printf '%s%d passed, %d warning(s), %d failed%s\n' "$c_yellow" "$PASS" "$WARN" "$FAIL" "$c_off"
  exit 0
else
  printf '%s%d passed, %d warning(s), %d failed%s\n' "$c_red" "$PASS" "$WARN" "$FAIL" "$c_off"
  exit 1
fi
