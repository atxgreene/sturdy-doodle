#!/usr/bin/env bash
# ==============================================================================
#  mnemosyne-wizard.sh
#  Interactive post-install setup wizard for Mnemosyne.
#
#  Walks through:
#    1. LLM backend (Ollama host + model, validated against running daemon)
#    2. Telegram channel (bot token validated via api.telegram.org/getMe,
#       chat ID auto-detected from /getUpdates) — token never appears in argv
#    3. Obsidian vault path (env-var slot for the upcoming skill — preview only)
#    4. Writes ~/projects/mnemosyne/.env (mode 600) atomically
#
#  UI modes:
#    - whiptail TUI (auto-detected, default if installed)
#    - plain text prompts (fallback, or forced via --text)
#
#  Safe to re-run: reads existing .env, offers current values as defaults,
#  preserves any keys it doesn't manage (so Discord/Slack/REST creds you add
#  by hand survive a re-run).
#
#  Usage:
#    bash mnemosyne-wizard.sh
#    bash mnemosyne-wizard.sh --text          # force text mode (no whiptail)
#    bash mnemosyne-wizard.sh --help
#    PROJECTS_DIR=$HOME/code/mnemosyne bash mnemosyne-wizard.sh
# ==============================================================================

set -euo pipefail

# bash 4+ required for associative arrays
if (( BASH_VERSINFO[0] < 4 )); then
  echo "bash >= 4 required (you have $BASH_VERSION)" >&2
  exit 1
fi

PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects/mnemosyne}"
ENV_FILE="$PROJECTS_DIR/.env"
VENV="$PROJECTS_DIR/.venv"

# ---- args ---------------------------------------------------------------------
FORCE_TEXT=0
for arg in "$@"; do
  case "$arg" in
    --text) FORCE_TEXT=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# ---- pretty output (text mode) ------------------------------------------------
c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'
c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
log()  { printf "%s==>%s %s\n" "$c_blue"  "$c_off" "$*"; }
ok()   { printf "%s✓%s   %s\n" "$c_green" "$c_off" "$*"; }
warn() { printf "%s!%s   %s\n" "$c_yellow" "$c_off" "$*"; }
err()  { printf "%s✗%s   %s\n" "$c_red"   "$c_off" "$*" 1>&2; }
die()  { err "$*"; exit 1; }
hr()   { printf "%s──────────────────────────────────────────────────────────────%s\n" "$c_dim" "$c_off"; }

# ---- preflight ----------------------------------------------------------------
[ -d "$PROJECTS_DIR" ] || die "$PROJECTS_DIR not found. Run install-mnemosyne.sh first."
command -v python3 >/dev/null || die "python3 required"

# curl is optional — only needed for the LLM backend reachability check.
# All Telegram API calls go through python3 (urllib) so the token never
# leaks into argv via curl's URL.

# ---- TUI detection ------------------------------------------------------------
# Use whiptail iff: it's installed AND stdin is a tty AND --text wasn't passed.
TUI=0
if [ "$FORCE_TEXT" = 0 ] && [ -t 0 ] && command -v whiptail >/dev/null 2>&1; then
  TUI=1
fi

WIZ_TITLE="Mnemosyne setup"

# ---- TUI helpers --------------------------------------------------------------
# All helpers fall back to plain prompts when whiptail is unavailable. They
# echo the result to stdout. yesno returns 0/1 via exit code.

tui_msg() {
  # tui_msg "title" "message" — \n in message is interpreted as a newline
  local title="$1" msg="$2"
  if [ "$TUI" = 1 ]; then
    whiptail --title "$title" --msgbox "$msg" 14 70
  else
    hr; printf "%s%s%s\n" "$c_blue" "$title" "$c_off"; hr
    printf '%b\n' "$msg"; echo
  fi
}

tui_input() {
  # tui_input "title" "prompt" "default" -> stdout: value
  local title="$1" prompt="$2" default="${3:-}"
  if [ "$TUI" = 1 ]; then
    whiptail --title "$title" --inputbox "$prompt" 10 70 "$default" 3>&1 1>&2 2>&3
  else
    local reply
    if [ -n "$default" ]; then
      read -r -p "  $prompt [$default]: " reply
      printf '%s' "${reply:-$default}"
    else
      read -r -p "  $prompt: " reply
      printf '%s' "$reply"
    fi
  fi
}

tui_password() {
  # tui_password "title" "prompt" -> stdout: value (no default; whiptail
  # cannot pre-fill a password box safely)
  local title="$1" prompt="$2"
  if [ "$TUI" = 1 ]; then
    whiptail --title "$title" --passwordbox "$prompt" 10 70 3>&1 1>&2 2>&3
  else
    local reply
    read -r -s -p "  $prompt: " reply
    echo
    printf '%s' "$reply"
  fi
}

tui_yesno() {
  # tui_yesno "title" "prompt" "default(y|n)" -> exit 0 if yes, 1 if no
  local title="$1" prompt="$2" default="${3:-n}"
  if [ "$TUI" = 1 ]; then
    if [ "$default" = "y" ]; then
      whiptail --title "$title" --yesno "$prompt" 10 70
    else
      whiptail --title "$title" --defaultno --yesno "$prompt" 10 70
    fi
  else
    local reply
    read -r -p "  $prompt [$default]: " reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
  fi
}

tui_menu() {
  # tui_menu "title" "prompt" key1 label1 key2 label2 ... -> stdout: selected key
  local title="$1" prompt="$2"; shift 2
  if [ "$TUI" = 1 ]; then
    whiptail --title "$title" --menu "$prompt" 16 70 6 "$@" 3>&1 1>&2 2>&3
  else
    echo "  $prompt"
    local i=1 keys=()
    while [ $# -gt 0 ]; do
      keys+=("$1")
      printf "    %d) %s\n" "$i" "$2"
      i=$((i+1)); shift 2
    done
    local choice
    read -r -p "  selection: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#keys[@]} )); then
      printf '%s' "${keys[$((choice-1))]}"
    fi
    return 0
  fi
}

# ---- .env loader (preserves unknown keys) -------------------------------------
declare -A CFG
if [ -f "$ENV_FILE" ]; then
  while IFS=$'\t' read -r k v; do
    [ -z "$k" ] && continue
    CFG[$k]="$v"
  done < <(python3 - "$ENV_FILE" <<'PY'
import sys
path = sys.argv[1]
for line in open(path):
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    k = k.strip()
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    print(f"{k}\t{v}")
PY
)
fi

cur() { printf '%s' "${CFG[$1]:-${2:-}}"; }

# ---- Telegram API helper (token via env var, never argv) ----------------------
# Usage: tg_api <token> <action>  -> stdout per action, exit 0 ok, !=0 fail
#   getMe       -> prints bot username
#   getUpdates  -> prints "<chat_id>\t<label>" lines for unique chats
tg_api() {
  local token="$1" action="$2"
  _TG_TOKEN="$token" python3 - "$action" <<'PY'
import os, sys, json, urllib.request, urllib.error
token = os.environ.get("_TG_TOKEN", "")
if not token:
    sys.exit(2)
action = sys.argv[1]
url = f"https://api.telegram.org/bot{token}/{action}"
try:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as r:
        data = json.load(r)
except urllib.error.HTTPError:
    sys.exit(1)
except (urllib.error.URLError, TimeoutError):
    sys.exit(3)
except Exception:
    sys.exit(1)
if not data.get("ok"):
    sys.exit(1)
if action == "getMe":
    print(data["result"].get("username", ""))
elif action == "getUpdates":
    seen = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        label = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
        seen[cid] = label
    for cid, label in seen.items():
        print(f"{cid}\t{label}")
PY
}

# ---- Slack API helper (token via env var, Bearer auth, never argv) -----------
# Usage: slack_api <token> <method>  -> stdout, exit 0 ok, !=0 fail
#   auth.test   -> prints "<team> (@<user>)" on success
slack_api() {
  local token="$1" method="$2"
  _SLACK_TOKEN="$token" python3 - "$method" <<'PY'
import os, sys, json, urllib.request, urllib.error
token = os.environ.get("_SLACK_TOKEN", "")
if not token:
    sys.exit(2)
method = sys.argv[1]
url = f"https://slack.com/api/{method}"
req = urllib.request.Request(
    url,
    method="POST",
    data=b"",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
)
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
except urllib.error.HTTPError:
    sys.exit(1)
except (urllib.error.URLError, TimeoutError):
    sys.exit(3)
except Exception:
    sys.exit(1)
if not data.get("ok"):
    sys.exit(1)
if method == "auth.test":
    team = data.get("team", "")
    user = data.get("user", "")
    print(f"{team} (@{user})")
PY
}

# ---- Notion API helper (token via env var, Bearer auth, never argv) ----------
# Usage: notion_api <token> <action>  -> stdout, exit 0 ok, !=0 fail
#   me  -> prints the integration/bot name on success
notion_api() {
  local token="$1" action="$2"
  _NOTION_TOKEN="$token" python3 - "$action" <<'PY'
import os, sys, json, urllib.request, urllib.error
token = os.environ.get("_NOTION_TOKEN", "")
if not token:
    sys.exit(2)
action = sys.argv[1]
if action == "me":
    url = "https://api.notion.com/v1/users/me"
else:
    sys.exit(2)
req = urllib.request.Request(
    url,
    headers={
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    },
)
try:
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
except urllib.error.HTTPError:
    sys.exit(1)
except (urllib.error.URLError, TimeoutError):
    sys.exit(3)
except Exception:
    sys.exit(1)
if data.get("object") != "user":
    sys.exit(1)
if action == "me":
    name = data.get("name") or "bot"
    print(name)
PY
}

# ---- Ollama reachability check ------------------------------------------------
ollama_check() {
  local host="$1" model="$2"
  python3 - "$host" "$model" <<'PY'
import sys, json, urllib.request, urllib.error
host, model = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
        data = json.load(r)
except Exception:
    print("unreachable")
    sys.exit(0)
names = [m.get("name", "") for m in data.get("models", [])]
print("present" if model in names else "missing")
PY
}

# ---- header -------------------------------------------------------------------
if [ "$TUI" = 1 ]; then
  whiptail --title "$WIZ_TITLE" --msgbox \
"This wizard configures channel credentials for Mnemosyne and writes them to:

  $ENV_FILE

Existing values are reused unless you overwrite them. The .env file will
be created with mode 600 (owner-readable only). Press Esc/Cancel anytime
to abort." 16 70
else
  clear 2>/dev/null || true
  hr
  printf "%s  Mnemosyne setup wizard%s\n" "$c_green" "$c_off"
  hr
  echo
  echo "Configures channel credentials and writes them to:"
  echo "  $ENV_FILE"
  echo
  echo "Existing values are reused unless you overwrite them. ^C anytime to abort."
  echo "Mode: $([ "$TUI" = 1 ] && echo whiptail || echo text)"
  echo
fi

# ---- step 1: LLM backend ------------------------------------------------------
[ "$TUI" = 0 ] && log "Step 1/6: LLM backend"

OLLAMA_HOST=$(tui_input "$WIZ_TITLE — LLM backend (1/6)" \
  "Ollama API base URL" "$(cur OLLAMA_HOST http://localhost:11434)")
OLLAMA_MODEL=$(tui_input "$WIZ_TITLE — LLM backend (1/6)" \
  "Model name (try gemma4:e4b for 128K context)" "$(cur OLLAMA_MODEL qwen3:8b)")

OLLAMA_STATUS=$(ollama_check "$OLLAMA_HOST" "$OLLAMA_MODEL" 2>/dev/null)
OLLAMA_STATUS="${OLLAMA_STATUS:-unreachable}"
case "$OLLAMA_STATUS" in
  present)
    if [ "$TUI" = 1 ]; then
      tui_msg "Ollama" "Daemon at $OLLAMA_HOST is responding.\nModel '$OLLAMA_MODEL' is present."
    else
      ok "Ollama responding; model $OLLAMA_MODEL present"
    fi
    ;;
  missing)
    if [ "$TUI" = 1 ]; then
      tui_msg "Ollama" "Daemon is up but model '$OLLAMA_MODEL' is not pulled.\n\nPull it with:\n  ollama pull $OLLAMA_MODEL"
    else
      warn "Model $OLLAMA_MODEL not in ollama list — pull with: ollama pull $OLLAMA_MODEL"
    fi
    ;;
  *)
    if [ "$TUI" = 1 ]; then
      tui_msg "Ollama" "Daemon at $OLLAMA_HOST is not responding.\n\nThe wizard will still write your config — start Ollama later."
    else
      warn "Ollama at $OLLAMA_HOST not responding (config will still be written)"
    fi
    ;;
esac

# ---- step 2: Telegram channel -------------------------------------------------
[ "$TUI" = 0 ] && { echo; log "Step 2/6: Telegram channel"; }

TELEGRAM_BOT_TOKEN=""
TELEGRAM_ALLOWED_CHAT_IDS=""
BOT_NAME=""

# Default to "yes" iff there's already a token to keep
TG_DEFAULT="n"
[ -n "$(cur TELEGRAM_BOT_TOKEN)" ] && TG_DEFAULT="y"

if tui_yesno "$WIZ_TITLE — Telegram (2/6)" \
  "Configure Telegram channel?

Answering 'no' keeps any existing Telegram config in .env unchanged —
it does NOT remove it. To remove credentials, edit .env by hand." "$TG_DEFAULT"; then

  EXISTING_TOKEN="$(cur TELEGRAM_BOT_TOKEN)"
  KEEP=0
  if [ -n "$EXISTING_TOKEN" ]; then
    if tui_yesno "$WIZ_TITLE — Telegram" \
      "Keep existing bot token (${EXISTING_TOKEN:0:6}...)?" "y"; then
      # Preserve both token AND chat IDs, skip live validation
      # (network flakes shouldn't nuke a working config).
      TELEGRAM_BOT_TOKEN="$EXISTING_TOKEN"
      TELEGRAM_ALLOWED_CHAT_IDS="$(cur TELEGRAM_ALLOWED_CHAT_IDS)"
      KEEP=1
    fi
  fi

  if [ "$KEEP" = 0 ]; then
    if [ "$TUI" = 1 ]; then
      tui_msg "Telegram" "Get a token from @BotFather on Telegram (/newbot).\n\nThe next prompt is hidden — paste your bot token and press Enter."
    else
      printf "%sGet a token from @BotFather on Telegram (/newbot).%s\n" "$c_dim" "$c_off"
    fi
    TELEGRAM_BOT_TOKEN=$(tui_password "$WIZ_TITLE — Telegram" "Bot token (hidden):")

    if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
      tui_msg "Telegram" "No token entered — skipping Telegram setup."
    else
      BOT_NAME=$(tg_api "$TELEGRAM_BOT_TOKEN" getMe 2>/dev/null || true)

      if [ -z "$BOT_NAME" ]; then
        tui_msg "Telegram" "Token rejected by api.telegram.org, or network is unreachable.\n\nDouble-check the token and re-run the wizard."
        TELEGRAM_BOT_TOKEN=""
      else
        tui_msg "Telegram" "Bot validated: @$BOT_NAME"

        tui_msg "Telegram — chat IDs" \
"Mnemosyne only responds to chat IDs in TELEGRAM_ALLOWED_CHAT_IDS.

To find your chat ID:
  1. Open Telegram and message @$BOT_NAME (any text)
  2. Continue this wizard — it will scan recent updates

You can also enter chat IDs directly (comma-separated)."

        CHAT_INPUT=$(tui_input "$WIZ_TITLE — Telegram chat IDs" \
          "chat ID(s), or leave blank to scan getUpdates" "")

        if [[ "$CHAT_INPUT" =~ ^-?[0-9]+(,-?[0-9]+)*$ ]]; then
          TELEGRAM_ALLOWED_CHAT_IDS="$CHAT_INPUT"
        else
          DETECTED=$(tg_api "$TELEGRAM_BOT_TOKEN" getUpdates 2>/dev/null || true)
          if [ -n "$DETECTED" ]; then
            if [ "$TUI" = 1 ]; then
              menu_args=()
              while IFS=$'\t' read -r cid label; do
                [ -z "$cid" ] && continue
                menu_args+=("$cid" "$label")
              done <<< "$DETECTED"
              menu_args+=("MANUAL" "Type a different chat ID manually")
              PICK=$(tui_menu "$WIZ_TITLE — Telegram chat IDs" \
                "Recent chats that messaged @$BOT_NAME (pick one):" "${menu_args[@]}")
              if [ "$PICK" = "MANUAL" ] || [ -z "$PICK" ]; then
                TELEGRAM_ALLOWED_CHAT_IDS=$(tui_input "$WIZ_TITLE — Telegram" \
                  "Allowed chat IDs (comma-separated)" "$(cur TELEGRAM_ALLOWED_CHAT_IDS)")
              else
                TELEGRAM_ALLOWED_CHAT_IDS="$PICK"
              fi
            else
              echo
              echo "  Recent chats that messaged @$BOT_NAME:"
              printf '%s\n' "$DETECTED" | awk -F'\t' '{printf "    %s\t%s\n", $1, $2}'
              echo
              DEFAULT_ID=$(printf '%s' "$DETECTED" | head -1 | cut -f1)
              TELEGRAM_ALLOWED_CHAT_IDS=$(tui_input "$WIZ_TITLE — Telegram" \
                "Allowed chat IDs (comma-separated)" "$DEFAULT_ID")
            fi
          else
            tui_msg "Telegram — chat IDs" \
"No updates found.

Message @$BOT_NAME from your Telegram client first, then re-run the wizard.

You can also enter a chat ID manually on the next prompt."
            TELEGRAM_ALLOWED_CHAT_IDS=$(tui_input "$WIZ_TITLE — Telegram" \
              "chat ID(s)" "$(cur TELEGRAM_ALLOWED_CHAT_IDS)")
          fi
        fi
      fi
    fi
  fi
else
  # Outer decline — preserve any existing Telegram values unchanged
  TELEGRAM_BOT_TOKEN="$(cur TELEGRAM_BOT_TOKEN)"
  TELEGRAM_ALLOWED_CHAT_IDS="$(cur TELEGRAM_ALLOWED_CHAT_IDS)"
fi

# ---- step 3: Slack channel ----------------------------------------------------
[ "$TUI" = 0 ] && { echo; log "Step 3/6: Slack channel"; }

SLACK_BOT_TOKEN=""
SLACK_APP_TOKEN=""
SLACK_SIGNING_SECRET=""
SLACK_TEAM=""
SLACK_DEFAULT="n"
[ -n "$(cur SLACK_BOT_TOKEN)" ] && SLACK_DEFAULT="y"

if tui_yesno "$WIZ_TITLE — Slack (3/6)" \
  "Configure Slack channel?

You'll need a Slack app with a Bot User (xoxb- token). Socket Mode is
recommended in WSL — it avoids exposing an inbound webhook.

Answering 'no' keeps any existing Slack config in .env unchanged." "$SLACK_DEFAULT"; then

  EXISTING_SLACK_TOKEN="$(cur SLACK_BOT_TOKEN)"
  SLACK_KEEP=0
  if [ -n "$EXISTING_SLACK_TOKEN" ]; then
    if tui_yesno "$WIZ_TITLE — Slack" \
      "Keep existing bot token (${EXISTING_SLACK_TOKEN:0:6}...)?" "y"; then
      # Preserve token + app token + signing secret, skip live validation
      SLACK_BOT_TOKEN="$EXISTING_SLACK_TOKEN"
      SLACK_APP_TOKEN="$(cur SLACK_APP_TOKEN)"
      SLACK_SIGNING_SECRET="$(cur SLACK_SIGNING_SECRET)"
      SLACK_KEEP=1
    fi
  fi

  if [ "$SLACK_KEEP" = 0 ]; then
    if [ "$TUI" = 1 ]; then
      tui_msg "Slack" "Create a Slack app at https://api.slack.com/apps, add a Bot User, install to your workspace, and copy the Bot User OAuth Token (starts with xoxb-)."
    else
      printf "%sCreate a Slack app at https://api.slack.com/apps and copy the Bot User OAuth Token (xoxb-...).%s\n" "$c_dim" "$c_off"
    fi
    SLACK_BOT_TOKEN=$(tui_password "$WIZ_TITLE — Slack" "Bot token (xoxb-...):")

    if [ -z "$SLACK_BOT_TOKEN" ]; then
      tui_msg "Slack" "No token entered — skipping Slack setup."
    else
      SLACK_TEAM=$(slack_api "$SLACK_BOT_TOKEN" auth.test 2>/dev/null || true)
      if [ -z "$SLACK_TEAM" ]; then
        tui_msg "Slack" "Token rejected by slack.com/api/auth.test, or network is unreachable.\n\nDouble-check the token and re-run the wizard."
        SLACK_BOT_TOKEN=""
      else
        tui_msg "Slack" "Bot validated: $SLACK_TEAM"

        # Optional extras for Socket Mode / incoming webhook verification.
        if tui_yesno "$WIZ_TITLE — Slack" \
          "Also configure Socket Mode app-level token + signing secret?
(Skippable — only needed if eternal-context runs Slack in Socket Mode.)" "n"; then
          SLACK_APP_TOKEN=$(tui_password "$WIZ_TITLE — Slack" "App-level token (xapp-...):")
          SLACK_SIGNING_SECRET=$(tui_password "$WIZ_TITLE — Slack" "Signing secret:")
        fi
      fi
    fi
  fi
else
  # Outer decline — preserve any existing Slack values unchanged
  SLACK_BOT_TOKEN="$(cur SLACK_BOT_TOKEN)"
  SLACK_APP_TOKEN="$(cur SLACK_APP_TOKEN)"
  SLACK_SIGNING_SECRET="$(cur SLACK_SIGNING_SECRET)"
fi

# ---- step 4: Obsidian skill ---------------------------------------------------
[ "$TUI" = 0 ] && { echo; log "Step 4/6: Obsidian skill"; }

OBSIDIAN_VAULT_PATH=""
OBS_DEFAULT="n"
[ -n "$(cur OBSIDIAN_VAULT_PATH)" ] && OBS_DEFAULT="y"

if tui_yesno "$WIZ_TITLE — Obsidian (4/6)" \
  "Configure Obsidian vault path?

The search/read helper ships as obsidian-search.py in this repo. The
eternal-context skill wrapper is still a small follow-up — see
SETUP.md#obsidian-skill.

Answering 'no' keeps any existing vault path unchanged." "$OBS_DEFAULT"; then

  OBSIDIAN_VAULT_PATH=$(tui_input "$WIZ_TITLE — Obsidian" \
    "Vault path (absolute, accessible from this shell)" "$(cur OBSIDIAN_VAULT_PATH)")
  if [ -n "$OBSIDIAN_VAULT_PATH" ] && [ ! -d "$OBSIDIAN_VAULT_PATH" ]; then
    tui_msg "Obsidian" "Path does not exist or is not accessible:\n  $OBSIDIAN_VAULT_PATH\n\nSaving anyway — you can fix it later."
  fi
else
  # Outer decline — preserve any existing vault path
  OBSIDIAN_VAULT_PATH="$(cur OBSIDIAN_VAULT_PATH)"
fi

# ---- step 5: Notion integration -----------------------------------------------
[ "$TUI" = 0 ] && { echo; log "Step 5/6: Notion integration"; }

NOTION_API_KEY=""
NOTION_BOT_NAME=""
NOTION_DEFAULT="n"
[ -n "$(cur NOTION_API_KEY)" ] && NOTION_DEFAULT="y"

if tui_yesno "$WIZ_TITLE — Notion (5/6)" \
  "Configure Notion integration?

The notion-search.py helper in this repo will use this to search and
read pages from your workspace. You must separately share individual
pages or databases with the integration for it to access them.

Answering 'no' keeps any existing Notion key unchanged." "$NOTION_DEFAULT"; then

  EXISTING_NOTION="$(cur NOTION_API_KEY)"
  NOTION_KEEP=0
  if [ -n "$EXISTING_NOTION" ]; then
    if tui_yesno "$WIZ_TITLE — Notion" \
      "Keep existing API key (${EXISTING_NOTION:0:6}...)?" "y"; then
      # Preserve, skip live validation
      NOTION_API_KEY="$EXISTING_NOTION"
      NOTION_KEEP=1
    fi
  fi

  if [ "$NOTION_KEEP" = 0 ]; then
    if [ "$TUI" = 1 ]; then
      tui_msg "Notion" "Create an integration at https://www.notion.com/my-integrations and copy its Internal Integration Token (starts with ntn_ or secret_)."
    else
      printf "%sCreate an integration at https://www.notion.com/my-integrations and copy its Internal Integration Token.%s\n" "$c_dim" "$c_off"
    fi
    NOTION_API_KEY=$(tui_password "$WIZ_TITLE — Notion" "API key:")

    if [ -z "$NOTION_API_KEY" ]; then
      tui_msg "Notion" "No key entered — skipping Notion setup."
    else
      NOTION_BOT_NAME=$(notion_api "$NOTION_API_KEY" me 2>/dev/null || true)
      if [ -z "$NOTION_BOT_NAME" ]; then
        tui_msg "Notion" "Key rejected by api.notion.com/v1/users/me, or network is unreachable.\n\nCheck the key and re-run the wizard."
        NOTION_API_KEY=""
      else
        tui_msg "Notion" "Integration validated: $NOTION_BOT_NAME\n\nDon't forget to share pages/databases with the integration\n(the Notion UI: '...' menu → 'Add connections')."
      fi
    fi
  fi
else
  # Outer decline — preserve any existing Notion key
  NOTION_API_KEY="$(cur NOTION_API_KEY)"
fi

# ---- step 6: write .env -------------------------------------------------------
[ "$TUI" = 0 ] && { echo; log "Step 6/6: write $ENV_FILE"; }

# Merge new values into preserved CFG (unset = drop)
update() {
  local k="$1" v="$2"
  if [ -n "$v" ]; then
    CFG[$k]="$v"
  else
    unset 'CFG['"$k"']' 2>/dev/null || true
  fi
}
update OLLAMA_HOST "$OLLAMA_HOST"
update OLLAMA_MODEL "$OLLAMA_MODEL"
update TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
update TELEGRAM_ALLOWED_CHAT_IDS "$TELEGRAM_ALLOWED_CHAT_IDS"
update SLACK_BOT_TOKEN "$SLACK_BOT_TOKEN"
update SLACK_APP_TOKEN "$SLACK_APP_TOKEN"
update SLACK_SIGNING_SECRET "$SLACK_SIGNING_SECRET"
update OBSIDIAN_VAULT_PATH "$OBSIDIAN_VAULT_PATH"
update NOTION_API_KEY "$NOTION_API_KEY"

# Build preview (tokens masked, never raw)
build_preview() {
  echo "OLLAMA_HOST=${CFG[OLLAMA_HOST]:-}"
  echo "OLLAMA_MODEL=${CFG[OLLAMA_MODEL]:-}"
  if [ -n "${CFG[TELEGRAM_BOT_TOKEN]:-}" ]; then
    echo "TELEGRAM_BOT_TOKEN=${CFG[TELEGRAM_BOT_TOKEN]:0:6}…(hidden)"
    echo "TELEGRAM_ALLOWED_CHAT_IDS=${CFG[TELEGRAM_ALLOWED_CHAT_IDS]:-}"
  fi
  if [ -n "${CFG[SLACK_BOT_TOKEN]:-}" ]; then
    echo "SLACK_BOT_TOKEN=${CFG[SLACK_BOT_TOKEN]:0:6}…(hidden)"
    [ -n "${CFG[SLACK_APP_TOKEN]:-}" ] && echo "SLACK_APP_TOKEN=${CFG[SLACK_APP_TOKEN]:0:6}…(hidden)"
    [ -n "${CFG[SLACK_SIGNING_SECRET]:-}" ] && echo "SLACK_SIGNING_SECRET=(hidden)"
  fi
  if [ -n "${CFG[OBSIDIAN_VAULT_PATH]:-}" ]; then
    echo "OBSIDIAN_VAULT_PATH=${CFG[OBSIDIAN_VAULT_PATH]}"
  fi
  if [ -n "${CFG[NOTION_API_KEY]:-}" ]; then
    echo "NOTION_API_KEY=${CFG[NOTION_API_KEY]:0:6}…(hidden)"
  fi
  # Show preserved unknown keys (count only — don't echo their values, may be secrets)
  local extra_count=0
  for k in "${!CFG[@]}"; do
    case "$k" in
      OLLAMA_HOST|OLLAMA_MODEL|TELEGRAM_BOT_TOKEN|TELEGRAM_ALLOWED_CHAT_IDS) ;;
      SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_SIGNING_SECRET) ;;
      OBSIDIAN_VAULT_PATH|NOTION_API_KEY) ;;
      *) extra_count=$((extra_count+1)) ;;
    esac
  done
  if [ "$extra_count" -gt 0 ]; then
    echo "(${extra_count} preserved key(s) from existing .env)"
  fi
  return 0
}

PREVIEW="$(build_preview)"

if [ "$TUI" = 1 ]; then
  if ! whiptail --title "$WIZ_TITLE — confirm" --yesno \
"Write the following to $ENV_FILE?

$PREVIEW" 20 72; then
    tui_msg "Aborted" "No file written."
    exit 0
  fi
else
  echo
  echo "Preview:"
  hr
  printf '%s\n' "$PREVIEW"
  hr
  if ! tui_yesno "$WIZ_TITLE" "Write to $ENV_FILE?" "y"; then
    warn "Aborted. No file written."
    exit 0
  fi
fi

# Backup existing .env (mode 600 explicit — cp does NOT preserve mode by default)
if [ -f "$ENV_FILE" ]; then
  ts=$(date +%Y%m%d-%H%M%S)
  BACKUP="$ENV_FILE.bak.$ts"
  cp "$ENV_FILE" "$BACKUP"
  chmod 600 "$BACKUP"
fi

# Atomic write with restrictive umask so the file is mode 600 from creation
TMP_ENV="$ENV_FILE.tmp.$$"
(
  umask 077
  {
    echo "# Mnemosyne credentials — NEVER commit this file"
    echo "# Written by mnemosyne-wizard.sh on $(date -Iseconds)"
    echo
    echo "# --- LLM backend ---"
    echo "OLLAMA_HOST=${CFG[OLLAMA_HOST]:-http://localhost:11434}"
    echo "OLLAMA_MODEL=${CFG[OLLAMA_MODEL]:-qwen3:8b}"
    echo
    echo "# --- Telegram ---"
    if [ -n "${CFG[TELEGRAM_BOT_TOKEN]:-}" ]; then
      echo "TELEGRAM_BOT_TOKEN=${CFG[TELEGRAM_BOT_TOKEN]}"
      echo "TELEGRAM_ALLOWED_CHAT_IDS=${CFG[TELEGRAM_ALLOWED_CHAT_IDS]:-}"
    else
      echo "# TELEGRAM_BOT_TOKEN="
      echo "# TELEGRAM_ALLOWED_CHAT_IDS="
    fi
    echo
    echo "# --- Slack ---"
    if [ -n "${CFG[SLACK_BOT_TOKEN]:-}" ]; then
      echo "SLACK_BOT_TOKEN=${CFG[SLACK_BOT_TOKEN]}"
      [ -n "${CFG[SLACK_APP_TOKEN]:-}" ] && echo "SLACK_APP_TOKEN=${CFG[SLACK_APP_TOKEN]}"
      [ -n "${CFG[SLACK_SIGNING_SECRET]:-}" ] && echo "SLACK_SIGNING_SECRET=${CFG[SLACK_SIGNING_SECRET]}"
    else
      echo "# SLACK_BOT_TOKEN="
      echo "# SLACK_APP_TOKEN="
      echo "# SLACK_SIGNING_SECRET="
    fi
    echo
    echo "# --- Obsidian skill ---"
    if [ -n "${CFG[OBSIDIAN_VAULT_PATH]:-}" ]; then
      echo "OBSIDIAN_VAULT_PATH=${CFG[OBSIDIAN_VAULT_PATH]}"
    else
      echo "# OBSIDIAN_VAULT_PATH="
    fi
    echo
    echo "# --- Notion integration ---"
    if [ -n "${CFG[NOTION_API_KEY]:-}" ]; then
      echo "NOTION_API_KEY=${CFG[NOTION_API_KEY]}"
    else
      echo "# NOTION_API_KEY="
    fi
    echo
    # Preserve any other keys (Discord/REST/whatever the user added)
    printed_other=0
    for k in "${!CFG[@]}"; do
      case "$k" in
        OLLAMA_HOST|OLLAMA_MODEL|TELEGRAM_BOT_TOKEN|TELEGRAM_ALLOWED_CHAT_IDS) ;;
        SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_SIGNING_SECRET) ;;
        OBSIDIAN_VAULT_PATH|NOTION_API_KEY) ;;
        *)
          if [ "$printed_other" = 0 ]; then
            echo "# --- Other (preserved from previous .env) ---"
            printed_other=1
          fi
          printf '%s=%s\n' "$k" "${CFG[$k]}"
          ;;
      esac
    done
  } > "$TMP_ENV"
)
chmod 600 "$TMP_ENV"
mv "$TMP_ENV" "$ENV_FILE"

# ---- done ---------------------------------------------------------------------
DONE_MSG="Wrote $ENV_FILE (mode 600).

Next:
  source $VENV/bin/activate
  set -a; . $ENV_FILE; set +a
  cd $PROJECTS_DIR/eternal-context/skills/eternal-context
  python -m eternalcontext"

if [ -n "${CFG[TELEGRAM_BOT_TOKEN]:-}" ]; then
  DONE_MSG+="

Telegram: configured. The agent should start listening on @${BOT_NAME:-your-bot} after launch."
fi
if [ -n "${CFG[SLACK_BOT_TOKEN]:-}" ]; then
  DONE_MSG+="

Slack: configured${SLACK_TEAM:+ ($SLACK_TEAM)}."
fi
if [ -n "${CFG[NOTION_API_KEY]:-}" ]; then
  DONE_MSG+="

Notion: configured${NOTION_BOT_NAME:+ ($NOTION_BOT_NAME)}.
Remember to share specific pages/databases with the integration in the Notion UI
before the agent can read them."
fi
if [ -n "${CFG[OBSIDIAN_VAULT_PATH]:-}" ]; then
  DONE_MSG+="

Obsidian helper usage:
  ./obsidian-search.py search \"query\"
  ./obsidian-search.py read path/to/note.md
  ./obsidian-search.py list-recent --days 7"
fi

if [ "$TUI" = 1 ]; then
  whiptail --title "$WIZ_TITLE — done" --msgbox "$DONE_MSG" 20 72
else
  echo
  hr
  ok "Wizard complete"
  hr
  echo
  printf '%s\n' "$DONE_MSG"
  echo
fi
