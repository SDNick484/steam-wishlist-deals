#!/bin/sh
# Lint: shellcheck on every shell script, py_compile on every Python file, systemd-analyze on the units.
set -eu
cd "$(dirname "$0")/../.."

status=0
echo "== shellcheck"
# SC1090/SC1091: the scripts source files whose names are only known at run time (the env file).
# shellcheck disable=SC2046
shellcheck --exclude=SC1090,SC1091 $(git ls-files '*.sh') || status=1

echo "== py_compile"
for f in $(git ls-files '*.py'); do
  python3 -m py_compile "$f" || status=1
done
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo "== systemd-analyze verify"
sh .github/scripts/systemd_verify.sh || status=1

[ "$status" = 0 ] && echo "lint passed"
exit "$status"
