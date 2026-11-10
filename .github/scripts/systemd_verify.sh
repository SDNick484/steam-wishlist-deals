#!/bin/sh
# Check contrib/systemd/*.service and *.timer with `systemd-analyze verify`.
#
# Two things make this less simple than it looks:
#  - verify exits 0 even when it prints parse warnings, so any output at all is treated as a failure here.
#  - it checks that ExecStart= programs exist and are executable, so the paths the units name are created
#    as empty executable stubs first (the unit files are not changed). Needs root, or sudo without a password.
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)
UNITS="$SRC/contrib/systemd"
command -v systemd-analyze >/dev/null || { echo "systemd-analyze not found" >&2; exit 2; }

SUDO=""
[ "$(id -u)" = 0 ] || SUDO="sudo"

made=""
# shellcheck disable=SC2329  # runs from the EXIT trap
cleanup() {
  for f in $made; do $SUDO rm -f "$f"; done
  rm -rf "${WORK:-}"
}
trap cleanup EXIT
WORK=$(mktemp -d)

stub() { # stub PATH: an executable that does nothing, created only if it isn't there already
  if [ ! -e "$1" ]; then
    $SUDO mkdir -p "$(dirname "$1")"
    printf '#!/bin/sh\nexit 0\n' | $SUDO tee "$1" >/dev/null
    $SUDO chmod 0755 "$1"
    made="$made $1"
  fi
}

# Every absolute program named by an ExecStart= line, and the /opt script arguments the units pass.
{
  sed -n 's/^ExecStart=\(\/[^ ]*\).*/\1/p' "$UNITS"/*.service
  sed -n 's/^ExecStart=.* \(\/opt\/[^ ]*\).*/\1/p' "$UNITS"/*.service
} | sort -u > "$WORK/paths"
while read -r p; do stub "$p"; done < "$WORK/paths"

# Verify copies, so nothing depends on where the checkout is.
cp "$UNITS"/* "$WORK"/
status=0
out=$(cd "$WORK" && systemd-analyze verify ./*.service ./*.timer 2>&1) || status=$?
# verify also loads the units ours depend on and reports warnings about the host's own packaged units (CI runners
# have a few). Those are not ours: drop lines about files under the system unit directories.
out=$(printf '%s\n' "$out" | grep -Ev '^(/usr/lib|/lib|/etc|/run)/systemd/' || true)
out=$(printf '%s' "$out" | sed '/^$/d')
if [ -n "$out" ]; then
  echo "systemd-analyze verify printed:" >&2
  echo "$out" >&2
  status=1
fi
[ "$status" = 0 ] && echo "systemd units verified: $(cd "$UNITS" && echo *)"
exit "$status"
