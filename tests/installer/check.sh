#!/bin/sh
# tests/installer/check.sh - run install/alpine.sh twice (three times, really) with the system commands
# stubbed, and check what a re-run must keep and must not duplicate.
#
# It changes /etc, /opt and /home, so it only runs where that is safe: inside the python:3.12-alpine CI
# container, or anywhere else when INSTALLER_TEST_OK=1 (a throwaway VM or container). On a machine that
# isn't Alpine, set FAKE_USERS=1 as well: adduser and chown are stubbed, since there is no BusyBox adduser.
#
#   docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh
#
# The installer finishes by running the watcher once as the app user (a dry run that calls the network)
# when the config has no placeholders left. su is stubbed, so that never happens here, and the stubs
# for wget and curl record every call so the test can assert nothing but the local health check ran.
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${INSTALLER_TEST_OK:-}" != 1 ] && [ ! -f /.dockerenv ] && [ ! -f /etc/alpine-release ]; then
  echo "Refusing to run: this changes /etc, /opt and /home. Use a container, or set INSTALLER_TEST_OK=1." >&2
  exit 2
fi

STUBS=$(mktemp -d)
CALLS=$STUBS/calls.log
: > "$CALLS"
made_alpine_release=0
fail=0

# shellcheck disable=SC2317,SC2329  # runs from the EXIT trap (SC2317 on shellcheck < 0.10, SC2329 after)
cleanup() {
  [ "$made_alpine_release" = 1 ] && rm -f /etc/alpine-release
  rm -rf "$STUBS"
}
trap cleanup EXIT

ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }
check() { # check "description" command...
  desc=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
count() { grep -c "$1" "$2" || true; }

# ---- stubs: record the call, do nothing. curl and su are here so that a network call, or the watcher's
# dry run, cannot happen by accident; the test asserts they were never reached for anything but su's dry run.
for cmd in apk rc-service rc-update wget curl ip su; do
  cat > "$STUBS/$cmd" <<EOF
#!/bin/sh
echo "$cmd \$*" >> "$CALLS"
exit 0
EOF
  chmod +x "$STUBS/$cmd"
done
if [ "${FAKE_USERS:-}" = 1 ]; then
  for cmd in adduser chown; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 0\n' "$cmd" "$CALLS" > "$STUBS/$cmd"
    chmod +x "$STUBS/$cmd"
  done
  # shellcheck disable=SC2016  # the stub's own $1 and $@ must stay literal
  printf '#!/bin/sh\n[ "${1:-}" = steam ] && exit 1\nexec /usr/bin/id "$@"\n' > "$STUBS/id"
  chmod +x "$STUBS/id"
fi
PATH="$STUBS:$PATH"
export PATH

[ -f /etc/alpine-release ] || { echo "3.20.0" > /etc/alpine-release; made_alpine_release=1; }

PREFIX=/opt/steam-wishlist-deals
HOME_DIR=/home/steam
TAB=/etc/crontabs/steam
SAVED=/etc/steam-wishlist-deals.conf
INIT=/etc/init.d/wishlist-deals
CFG=$HOME_DIR/.config/wishlist-deals/config.ini
# Start from a machine that has never had the installer (matters when this is re-run in the same VM).
rm -f "$SAVED" "$INIT" "$CFG"
rm -rf "$PREFIX"
mkdir -p /etc/crontabs /etc/init.d "$HOME_DIR"

# An unrelated job, and a block from an older install that must be replaced rather than duplicated.
cat > "$TAB" <<'EOF'
0 3 * * * /usr/local/bin/other-job.sh
# BEGIN steam-wishlist-deals
1 1 * * 1  /usr/bin/python3 /old/path/wishlist_deals.py >> /old.log 2>&1
# END steam-wishlist-deals
30 6 * * * /usr/local/bin/another-job.sh
EOF

run() { # run label [VAR=value ...]
  label=$1; shift
  printf '\n--- %s\n' "$label"
  env "$@" sh "$SRC/install/alpine.sh" > "$STUBS/out.$label" 2>&1 || { cat "$STUBS/out.$label" >&2; bad "$label: installer exited non-zero"; return 1; }
  ok "$label: installer exited 0"
}

# ================================================================== first install
run first STEAM_PROFILE=7656119XXXXXXXXXX ITAD_KEY=FAKEITADKEY0123456789 HA_URL=http://ha.example.com:8123 \
  HA_TOKEN=FAKE-NOT-A-TOKEN DEALS_PAGE_URL=https://deals.example.com/ \
  PORT=8585 CHECK_TIMES=06:15,18:45 NAV=Backlog=http://backlog.example.com:8686/

check "writes the config from the given settings" sh -c "grep -q '^profile = 7656119XXXXXXXXXX\$' '$CFG' && grep -q '^key = FAKEITADKEY0123456789\$' '$CFG'"
check "the config has the default notify service placeholder" grep -q '^service = notify.mobile_app_your_phone$' "$CFG"
check "the config is mode 600" test "$(stat -c %a "$CFG")" = 600
check "saves the options" sh -c "grep -q \"SAVED_PORT='8585'\" '$SAVED' && grep -q \"SAVED_CHECK_TIMES='06:15,18:45'\" '$SAVED' && grep -q \"SAVED_NAV='Backlog=http://backlog.example.com:8686/'\" '$SAVED'"
check "init script uses the port, check times and nav link" \
  grep -q -- "--port 8585 .*--check-times 06:15,18:45 --nav Backlog=http://backlog.example.com:8686/" "$INIT"
check "init script is executable" test -x "$INIT"
check "unrelated cron line before the block is kept" grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' "$TAB"
check "unrelated cron line after the block is kept" grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' "$TAB"
check "the old block's job is gone" sh -c "! grep -q '/old/path' '$TAB'"
check "the first check time is scheduled" grep -q '^15 6 \* \* \*  /usr/bin/python3 /opt/steam-wishlist-deals/wishlist_deals.py ' "$TAB"
check "the second check time is scheduled" grep -q '^45 18 \* \* \*  /usr/bin/python3 /opt/steam-wishlist-deals/wishlist_deals.py ' "$TAB"
check "exactly one BEGIN" test "$(count '^# BEGIN steam-wishlist-deals$' "$TAB")" = 1
check "exactly one END" test "$(count '^# END steam-wishlist-deals$' "$TAB")" = 1
check "installed the files with the right modes" sh -c "[ \"\$(stat -c %a '$PREFIX/wishlist_deals.py')\" = 755 ] && [ \"\$(stat -c %a '$PREFIX/deals_server.py')\" = 755 ] && [ \"\$(stat -c %a '$PREFIX/deals.html')\" = 644 ]"
check "installed every top-level file the server and watcher need" sh -c "cd '$SRC' && for f in wishlist_deals.py deals_server.py deals.html; do cmp -s \"\$f\" '$PREFIX'/\$f || exit 1; done"
check "the installed server and watcher compile" sh -c "cd '$PREFIX' && python3 -m py_compile deals_server.py wishlist_deals.py"
check "enabled and restarted the service" grep -q '^rc-service wishlist-deals restart' "$CALLS"
check "enabled and restarted crond" sh -c "grep -q '^rc-update add crond' '$CALLS' && grep -q '^rc-service crond restart' '$CALLS'"
check "ran the watcher's dry run as the app user, through the stub" grep -q '^su steam -c .*wishlist_deals.py --all --dry-run' "$CALLS"

cp "$TAB" "$STUBS/tab.first"

# ================================================================== re-run: nothing given
# The user edits the config between runs; it must survive untouched.
sed -i 's/^low_tolerance_pct = 0$/low_tolerance_pct = 7/' "$CFG"
echo "# edited by the user" >> "$CFG"
cp "$CFG" "$STUBS/cfg.before"
run second

check "keeps the config byte for byte" cmp -s "$CFG" "$STUBS/cfg.before"
check "keeps the config mode 600" test "$(stat -c %a "$CFG")" = 600
check "remembers PORT" grep -q -- "--port 8585 " "$INIT"
check "remembers CHECK_TIMES" grep -q -- "--check-times 06:15,18:45" "$INIT"
check "remembers NAV" grep -q -- "--nav Backlog=http://backlog.example.com:8686/" "$INIT"
check "saved options are unchanged in content" sh -c "grep -q \"SAVED_PORT='8585'\" '$SAVED' && grep -q \"SAVED_CHECK_TIMES='06:15,18:45'\" '$SAVED'"
check "still exactly one BEGIN" test "$(count '^# BEGIN steam-wishlist-deals$' "$TAB")" = 1
check "still exactly one END" test "$(count '^# END steam-wishlist-deals$' "$TAB")" = 1
check "crontab is identical to after the first run" cmp -s "$TAB" "$STUBS/tab.first"
check "unrelated cron lines still there" sh -c "grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' '$TAB' && grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' '$TAB'"

# ================================================================== re-run: change one option, clear another
run third CHECK_TIMES=08:00 NAV=

check "a given option replaces the saved one" sh -c "grep -q '^0 8 \* \* \*  ' '$TAB' && ! grep -q '^15 6 ' '$TAB' && ! grep -q '^45 18 ' '$TAB'"
check "the new check time reaches the init script" grep -q -- "--check-times 08:00" "$INIT"
check "an empty NAV removes the link" sh -c "! grep -q -- '--nav' '$INIT'"
check "the other saved option is kept" grep -q -- "--port 8585 " "$INIT"
check "the saved options file follows" sh -c "grep -q \"SAVED_CHECK_TIMES='08:00'\" '$SAVED' && grep -q \"SAVED_NAV=''\" '$SAVED' && grep -q \"SAVED_PORT='8585'\" '$SAVED'"
check "still exactly one block after the third run" test "$(count '^# BEGIN steam-wishlist-deals$' "$TAB")" = 1
check "the block holds one job" test "$(grep -c 'wishlist_deals.py' "$TAB")" = 1
check "unrelated cron lines still there after the third run" sh -c "grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' '$TAB' && grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' '$TAB'"
check "the config is still untouched" cmp -s "$CFG" "$STUBS/cfg.before"

# ================================================================== no network
printf '\n--- network\n'
check "wget was only used for the local health check" sh -c "! grep '^wget ' '$CALLS' | grep -qv '127.0.0.1'"
check "wget did check the local health endpoint" grep -q '^wget .*http://127.0.0.1:8585/healthz' "$CALLS"
check "curl was never called" sh -c "! grep -q '^curl ' '$CALLS'"
check "the only apk call is the package install" sh -c "[ \"\$(grep -c '^apk ' '$CALLS')\" = 3 ] && ! grep '^apk ' '$CALLS' | grep -qv 'apk add --no-cache python3 tzdata ca-certificates'"

# ================================================================== refusals
printf '\n--- refusals\n'
if env 'NAV=bad nav' sh "$SRC/install/alpine.sh" >/dev/null 2>&1; then bad "NAV with a space is refused"; else ok "NAV with a space is refused"; fi
if env 'CHECK_TIMES=25:00' sh "$SRC/install/alpine.sh" >/dev/null 2>&1; then bad "a bad check time is refused"; else ok "a bad check time is refused"; fi
check "a refused run changed nothing" sh -c "cmp -s '$CFG' '$STUBS/cfg.before' && [ \"\$(grep -c '^# BEGIN' '$TAB')\" = 1 ]"

echo
if [ "$fail" = 0 ]; then echo "installer checks passed"; else echo "installer checks FAILED" >&2; fi
exit "$fail"
