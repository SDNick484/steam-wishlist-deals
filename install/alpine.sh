#!/bin/sh
# install/alpine.sh - install Steam Wishlist Deals on Alpine Linux (OpenRC + BusyBox crond).
#
# Run as root from a checkout of this repository:
#   git clone https://github.com/SDNick484/steam-wishlist-deals.git
#   cd steam-wishlist-deals && sh install/alpine.sh
#
# Settings used to fill in the config on first install (environment variables; anything left
# out gets a placeholder you can edit later in ~steam/.config/wishlist-deals/config.ini):
#   STEAM_PROFILE    SteamID64 or profile URL
#   ITAD_KEY         IsThereAnyDeal API key (https://isthereanydeal.com/apps/my/)
#   STEAM_API_KEY    only needed if STEAM_PROFILE is a custom URL name
#   HA_URL           Home Assistant base URL, e.g. http://homeassistant.local:8123
#   HA_TOKEN         Home Assistant long-lived access token
#   HA_SERVICE       notify service, e.g. notify.mobile_app_my_phone
#   DEALS_PAGE_URL   where tapping a notification goes, e.g. https://deals.example.com/
# Install options:
#   CHECK_TIMES      daily check times, local time (default "10:40,19:40"; Steam sales start 10:00 PT)
#   PORT             deals page port (default 8485)
#   NAV              header link to another page, LABEL=URL with no spaces
#   TZ_NAME          set the system time zone, e.g. America/New_York (default: leave as is)
#
# Ask (AI search) is configured in ~/.config/wishlist-deals/ask.env, created empty on first install; it stays
# off until you set ANTHROPIC_SEARCH_KEY there. See the README's "AI search (Ask)" section.
#
# Safe to re-run: code is updated, your config, ask.env and state are kept, and the install options are
# remembered (in /etc/steam-wishlist-deals.conf). Set NAV= (empty) to remove a link.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/steam-wishlist-deals
APP_USER=steam
HOME_DIR=/home/$APP_USER
# Options from a previous install are reused unless given again on the command line.
SAVED=/etc/steam-wishlist-deals.conf
SAVED_PORT=""; SAVED_CHECK_TIMES=""; SAVED_NAV=""
[ -f "$SAVED" ] && . "$SAVED"
PORT="${PORT:-${SAVED_PORT:-8485}}"
CHECK_TIMES="${CHECK_TIMES:-${SAVED_CHECK_TIMES:-10:40,19:40}}"
NAV="${NAV-${SAVED_NAV:-}}"
TZ_NAME="${TZ_NAME:-}"

say() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -f /etc/alpine-release ] || { echo "This installer is for Alpine Linux. See the README for other systems."; exit 1; }
case "$NAV" in *[[:space:]\'\"]*) echo "NAV must not contain spaces or quotes."; exit 1;; esac
CRON_LINES=""
for t in $(echo "$CHECK_TIMES" | tr ',' ' '); do
  h=${t%%:*}; m=${t##*:}
  case "$h:$m" in [0-9]:[0-5][0-9]|[01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;; *) echo "Bad time in CHECK_TIMES: $t"; exit 1;; esac
  CRON_LINES="$CRON_LINES$(printf '%d %d * * *  /usr/bin/python3 %s/wishlist_deals.py >> %s/logs/wishlist.log 2>&1' "${m#0}" "${h#0}" "$PREFIX" "$HOME_DIR")
"
done

say "Installing packages"
apk add --no-cache python3 tzdata ca-certificates logrotate >/dev/null </dev/null

if [ -n "$TZ_NAME" ]; then
  say "Setting time zone to $TZ_NAME"
  cp "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
  echo "$TZ_NAME" > /etc/timezone
fi

say "Creating the unprivileged '$APP_USER' user"
id "$APP_USER" >/dev/null 2>&1 || adduser -D -h "$HOME_DIR" -s /bin/sh "$APP_USER"
mkdir -p "$PREFIX" "$HOME_DIR/.config/wishlist-deals" "$HOME_DIR/exports" "$HOME_DIR/logs"

say "Installing to $PREFIX"
install -m 0755 "$SRC/wishlist_deals.py" "$SRC/deals_server.py" "$PREFIX/"
install -m 0644 "$SRC/deals.html" "$SRC/deals_query.py" "$SRC/access_jwt.py" "$SRC/ask.py" "$PREFIX/"
# Stamp the build id into the INSTALLED server only (never the checkout): the short commit this checkout is at.
# Without git, or outside a checkout, the placeholder stays and the server reports "unknown".
BUILD_ID=$(git -c safe.directory="$SRC" -C "$SRC" rev-parse --short HEAD 2>/dev/null) || BUILD_ID=""
case "$BUILD_ID" in
  ""|*[!0-9a-f]*) ;;
  *) if grep -q '^BUILD = "[$]Format' "$PREFIX/deals_server.py"; then
       sed -i "s|^BUILD = .*|BUILD = \"$BUILD_ID\"|" "$PREFIX/deals_server.py"
       chmod 0755 "$PREFIX/deals_server.py"
       echo "Build $BUILD_ID"
     fi;;
esac

CFG="$HOME_DIR/.config/wishlist-deals/config.ini"
if [ ! -f "$CFG" ]; then
  say "Writing $CFG"
  umask 077
  cat > "$CFG" <<EOF
[steam]
# SteamID64 (17 digits), profile URL, or custom URL name.
profile = ${STEAM_PROFILE:-https://steamcommunity.com/id/yourname/}
# Only needed if profile is a custom URL name instead of a SteamID64.
api_key = ${STEAM_API_KEY:-}

[itad]
key = ${ITAD_KEY:-YOUR_ITAD_API_KEY}
country = US
# Leave both blank to consider every store ITAD tracks. Names as ITAD shows them.
only_shops =
exclude_shops =

[rules]
# Alert when the best price is within this % of the all-time low (0 = at or below it).
low_tolerance_pct = 0
# Ignore all-time lows with a smaller discount than this % (0 = off).
low_min_discount_pct = 40
# Alert on any discount at least this deep, low or not (0 = off).
big_discount_pct = 0
# Alert when a wishlist game shows up in a live bundle.
bundles = true
# Only alert for games in your top N wishlist ranks (0 = all).
top_n = 0

[targets]
# Legacy: target prices are now set on the deals page and stored in targets.json next to
# deals.json. Entries here are copied into targets.json once, the first time it's missing,
# and ignored after that. Format: steam app id = price
# 1245620 = 20.00

[home_assistant]
# Leave url blank to skip.
url = ${HA_URL:-}
token = ${HA_TOKEN:-}
service = ${HA_SERVICE:-notify.mobile_app_your_phone}
# Where tapping the notification goes. Blank = the first deal's store page.
deals_page_url = ${DEALS_PAGE_URL:-}

[output]
deals_json = $HOME_DIR/exports/deals.json

[email]
# Leave host blank to skip. For Gmail, use an app password and port 587.
host =
port = 587
starttls = true
username =
password =
from =
to =
EOF
  umask 022
else
  say "Keeping existing $CFG"
  # Settings added in later versions: insert with defaults if missing.
  if ! grep -q '^low_min_discount_pct' "$CFG"; then
    sed -i '/^low_tolerance_pct/a low_min_discount_pct = 40' "$CFG"
    echo "Added low_min_discount_pct = 40 to [rules]"
  fi
  if ! grep -q '^deals_page_url' "$CFG"; then
    sed -i '/^service = notify/a deals_page_url =' "$CFG"
    echo "Added deals_page_url to [home_assistant]"
  fi
  if ! grep -q '^\[output\]' "$CFG"; then
    printf '\n[output]\ndeals_json = %s/exports/deals.json\n' "$HOME_DIR" >> "$CFG"
    echo "Added [output] deals_json"
  fi
fi

# Ask settings. Created once (empty, so Ask stays off) and never touched again.
ASK_DIR="$HOME_DIR/.config/wishlist-deals"
ASKF="$ASK_DIR/ask.env"
chmod 700 "$ASK_DIR"
if [ ! -f "$ASKF" ]; then
  umask 077
  cat > "$ASKF" <<'EOF'
# Settings for Ask (AI search), read by deals_server.py at start-up. KEY=VALUE, one per line.
# Ask is off until ANTHROPIC_SEARCH_KEY is set. Edit this file (it must stay mode 600), then run:
#   rc-service wishlist-deals restart
# See the README section "AI search (Ask)" for where to find each value.

# Anthropic API key, ideally from its own Console workspace with a spend limit.
ANTHROPIC_SEARCH_KEY=
# Model id; empty means claude-haiku-5-5.
ANTHROPIC_SEARCH_MODEL=
# Questions per day across everyone; empty means 200.
ASK_DAILY_LIMIT=
# Cloudflare Access team domain, e.g. your-team.cloudflareaccess.com
ASK_ACCESS_TEAM=
# The deals page's Access application Audience (AUD) tag.
ASK_ACCESS_AUD=
# Comma-separated emails allowed to ask; empty means any user Access lets in.
ASK_EMAILS=
# 1 = also allow direct requests from your LAN, opened by IP address (see the README); empty or 0 = off.
ASK_LAN=
EOF
  umask 022
  echo "Wrote $ASKF"
fi
chown -R "$APP_USER:$APP_USER" "$HOME_DIR"
chown -R root:root "$PREFIX"

say "Scheduling checks at $CHECK_TIMES"
# Replace only this project's block, so other jobs in the same crontab survive.
mkdir -p /etc/crontabs
TAB=/etc/crontabs/$APP_USER
touch "$TAB"
sed -i '/^# BEGIN steam-wishlist-deals$/,/^# END steam-wishlist-deals$/d' "$TAB"
{ echo "# BEGIN steam-wishlist-deals"; printf '%s' "$CRON_LINES"; echo "# END steam-wishlist-deals"; } >> "$TAB"
chmod 0600 "$TAB"
rc-update add crond default >/dev/null 2>&1 || true
rc-service crond restart >/dev/null 2>&1 || rc-service crond start

say "Rotating this app's logs"
# Only this app's own two logs; one file, rewritten on every run. copytruncate: the daemons keep their open files.
mkdir -p /etc/logrotate.d
cat > /etc/logrotate.d/steam-wishlist-deals <<EOF
$HOME_DIR/logs/deals-server.log $HOME_DIR/logs/wishlist.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
chmod 0644 /etc/logrotate.d/steam-wishlist-deals

say "Installing the deals page service (port $PORT)"
NAV_ARG=""
[ -n "$NAV" ] && NAV_ARG="--nav $NAV"
cat > /etc/init.d/wishlist-deals <<EOF
#!/sbin/openrc-run
name="wishlist-deals"
description="Steam Wishlist Deals page"
command="/usr/bin/python3"
command_args="$PREFIX/deals_server.py --port $PORT --data $HOME_DIR/exports --check-times $CHECK_TIMES --ask-env $ASKF $NAV_ARG"
command_user="$APP_USER:$APP_USER"
supervisor="supervise-daemon"
respawn_delay=5
output_log="$HOME_DIR/logs/deals-server.log"
error_log="$HOME_DIR/logs/deals-server.log"

depend() {
  after net
}
EOF
chmod 0755 /etc/init.d/wishlist-deals
touch "$HOME_DIR/logs/deals-server.log" && chown "$APP_USER:$APP_USER" "$HOME_DIR/logs/deals-server.log"
rc-update add wishlist-deals default >/dev/null 2>&1 || true
rc-service wishlist-deals restart >/dev/null 2>&1 || rc-service wishlist-deals start

IP=$(ip -4 -o addr show 2>/dev/null | awk '$2 != "lo" {print $4; exit}' | cut -d/ -f1)
sleep 1
if wget -q -O /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then
  echo "Deals page running: http://${IP:-<this-host>}:$PORT/"
else
  echo "WARNING: the deals page didn't answer; check $HOME_DIR/logs/deals-server.log"
fi

cat > "$SAVED" <<EOF
# Written by install/alpine.sh; reused on re-runs. Pass a variable again to change it.
SAVED_PORT='$PORT'
SAVED_CHECK_TIMES='$CHECK_TIMES'
SAVED_NAV='$NAV'
EOF

say "Checking the watcher"
if grep -q "YOUR_ITAD_API_KEY" "$CFG" || grep -q "id/yourname" "$CFG"; then
  echo "Config still has placeholders. Fill in $CFG, then test with:"
  echo "  su $APP_USER -c '/usr/bin/python3 $PREFIX/wishlist_deals.py --all --dry-run'"
else
  su "$APP_USER" -c "/usr/bin/python3 $PREFIX/wishlist_deals.py --all --dry-run" </dev/null || \
    echo "Dry run failed; see the message above."
fi

say "Done"
echo "Config: $CFG"
echo "Ask settings: $ASKF"
echo "Logs:   $HOME_DIR/logs/"
