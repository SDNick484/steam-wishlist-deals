# CLAUDE.md

## What this is

`steam-wishlist-deals`: a watcher that reads a Steam wishlist, finds deals through IsThereAnyDeal, sends
notifications and writes `deals.json`, plus a small server and a single-page deals view with per-game target
prices. Python standard library only; the page is one HTML file with no build step. Alpine with OpenRC and
BusyBox crond is the primary target, systemd units are secondary.

## Layout

- `wishlist_deals.py`: the watcher. `evaluate()` decides what is an alert, `new_alerts()` what is new since the
  last notification, `digest_text()`/`digest_html()` and `notify_ha()`/`notify_email()` send them,
  `build_games()` and `write_snapshot()` write `deals.json` for the page, `load_state()`/`save_state()` keep
  `state.json`. `load_targets()` reads `targets.json`
- `deals_server.py`: serves the page and its JSON, saves target prices, re-runs the watcher quietly on
  `POST /api/deals/refresh`. `App` holds the paths, `make_handler(app)` builds the request handler
- `run_status.json` (data dir, next to `deals.json`): written by the watcher after every real run (not
  `--dry-run`; `--snapshot-only` is recorded too, without notices); `Stage`, `record_run()`, `next_run_status()` and `send_notice()` are in
  `wishlist_deals.py`. The server reads it for `GET /api/deals/status` and `GET /healthz?deep=1`
- `deals_query.py`: the read-only query layer over `deals.json` and `targets.json` that Ask's tools use
- `ask.py`: Ask (AI search): settings from `ask.env`, the tools, the question loop, the daily cap and `ask_log.jsonl`
- `access_jwt.py`: Cloudflare Access token verification (RS256, audience, issuer, expiry; needs `openssl`)
- `tests/ask_shim.py`: runs `deals_server.py` with the model call replaced by a scripted fake; test entry point only
- `deals.html`: the whole page (HTML, CSS and JS in one file). It fetches relative `api/deals` and
  `api/deals/targets`
- `install/alpine.sh`: the installer. It keeps config and data, remembers its options in
  `/etc/steam-wishlist-deals.conf`, replaces only its own `# BEGIN/END steam-wishlist-deals` crontab block, writes
  only its own `/etc/logrotate.d/steam-wishlist-deals`, and stamps the build id into the installed server
- `contrib/systemd/`: service and timer units. `examples/config.example.ini`: every setting
- `tests/`: unit tests (`test_evaluate`, `test_snapshot`, `test_targets_file`, `test_server`,
  `test_e2e_unchanged`, `test_security`, `test_run_status`, `test_privacy_scan`, `test_deals_query`, `test_ask`,
  `test_access_jwt`, with `tests/ask_shim.py` for the Ask server; the Ask page test is `tests/browser/test_ask_page.py`) with local stand-ins for Steam, ITAD and ProtonDB
  (`stub_services.py`, `helpers.py`) and golden files in `tests/fixtures/`.
  `tests/browser/` is the page test (Playwright, test-only), `tests/installer/check.sh` the installer check
- `.github/`: `workflows/ci.yml`, `dependabot.yml`, and `scripts/` (`lint.sh`, `systemd_verify.sh`,
  `privacy_scan.py`)

## Rules for changes

- Standard library only in the product. The one non-stdlib thing in the repo is Playwright, pinned in
  `tests/browser/requirements.txt` and used by tests only
- One HTML file for the page. No build step, no bundler, no external script
- **Every route is under `/api/deals/...` and has a `/deals/api/deals/...` twin**, because a path-routing
  reverse proxy sends `/deals` and `/api/deals` to this server. Add both or neither. The page must work at
  `/` and at `/deals` (no trailing slash)
- **Notifications, the digest and `state.json` behavior are pinned by the golden tests**
  (`tests/fixtures/`). A change there is a behavior change: say so in the PR, and update the golden file in
  the same commit with the reason
- **`targets.json` is edited only through `deals_server.py`** (atomic write, mode 0640). The watcher only
  creates it, once, from legacy `[targets]` entries in `config.ini` when it is missing, and never replaces
  an existing one
- A new top-level file that has to ship (a `.py`, `.html` or `.sh` the app needs at run time) is added to
  `install/alpine.sh`, and the PR calls it out
- **Every mutating route goes through `cross_site_request()`** (called first in `do_PUT`, `do_POST` and
  `do_DELETE`), and every response gets the headers in `SECURITY_HEADERS` (via `end_headers`). A new route
  inherits both; don't bypass them. No `script-src` policy: the page's script is inline
- **`run_status.json` has a fixed shape** (`version`, `last_attempt`, `last_ok`, `consecutive_failures`,
  `error_class`) shared with steam-backlog-triage. `error_class` is one of steam, itad, notify, config, other
  and never error text. The failure notice is sent when a full run takes the counter to 3 or more from below 3, the recovery
  notice when a full run succeeds after a streak of 3 or more; quiet runs never notify
- `BUILD` in `deals_server.py` is an `export-subst` placeholder (`.gitattributes`); the installer stamps the
  installed copy only. Never commit a real hash into it
- **`access_jwt.py` is a copy of steam-backlog-triage's; change it there first**
- **`ask.py` follows triage's; keep behavior in step**
- **The server never reads `config.ini` or `state.json`**: Ask sees only `deals.json`, `targets.json` and `run_status.json` from the data directory
- **`deals_query.py` mirrors `deals.html`; the page wins** when they disagree
- `ask.env` is created once by the installer (mode 600) and never overwritten
- Installers stay safe to re-run. Behavior or option changes update the README in the same PR

## Commands

```sh
REQUIRE_OPENSSL=1 python3 -m unittest -v                 # unit tests, no skips allowed
pip install -r tests/browser/requirements.txt && playwright install chromium
REQUIRE_BROWSER=1 python3 -m unittest -v tests.browser.test_page tests.browser.test_ask_page   # the pages in Chromium
docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh
sh .github/scripts/lint.sh                               # shellcheck, py_compile, systemd-analyze verify
python3 .github/scripts/privacy_scan.py                  # files and history
```

Set `BROWSER_EXECUTABLE` to use an installed Chrome or Chromium instead of Playwright's download.
`tests/installer/check.sh` changes `/etc`, `/opt` and `/home`: run it in a container, or in a throwaway
machine with `INSTALLER_TEST_OK=1 FAKE_USERS=1`.

STANDING RULES
- Read the current code on main before changing anything. If it contradicts a decision above, stop and
  tell me rather than improvising.
- steam-backlog-triage, steam-wishlist-deals, steam-deck-ha-bridge: Python standard library only; each page
  is one HTML file with no build step; Alpine with OpenRC and BusyBox crond is the primary target.
  mcp-server-steam-backlog: follow its CLAUDE.md; shared files change only in mcp-server-shared.
- Everything committed AND everything in a PR title, body or comment is public. Never write IPs, hostnames,
  my domain, container IDs, my time zone, my SteamID, phone or notify-service names, my real name or
  personal email, my Cloudflare team name or AUD, or any key. Use placeholders (7656119XXXXXXXXXX,
  notify.mobile_app_your_phone, steam.example.com, your-team.cloudflareaccess.com). Grep every diff.
  Author: SDNick484 <SDNick484@users.noreply.github.com>.
- Never ask me to paste keys or tokens into chat. If a step needs one, give me a vim command for the file
  on the server.
- Installers stay safe to re-run: keep config and data, remember options, replace only their own crontab
  block. Update the README in the same PR when behavior or options change.

SHIP PROCEDURE
1. Work on a feature branch. Small, focused commits, each with its tests.
2. Before pushing, run everything CI runs, locally, with nothing skipped (REQUIRE_BROWSER=1 for the page
   test; the installer check in a python:3.12-alpine container if an installer changed; lint; the privacy
   scan).
3. git push -u origin <branch>, then gh pr create. PR body: what changed, the exact commands you ran with
   pass counts, and anything I must do by hand. No deploy commands and no server details in the PR.
4. gh pr checks <number> --watch --interval 30. If a check fails: gh run view <run-id> --log-failed, fix
   the cause, push, watch again. Never get to green by skipping, loosening or deleting a test, lowering a
   threshold, or changing a pin this prompt didn't ask you to change. After three failed rounds, stop and
   tell me what is failing and why.
5. When every check is green, notify me: use your push-notification tool if you have one; otherwise, if
   STEAM_NOTIFY_CMD is set, run it with one argument, "<repo>#<number> CI green: <title>". Do not merge.
6. Finish with one message, in chat only: the PR URL, the check summary, and a DEPLOY VERDICT, from the diff:
     A  Dashboard deploy is enough: only already-shipped files changed.
     B  Installer by hand: name the paths that changed under install/ or deploy/, or the dependency.
     C  Registry first: list every NEW file that must ship (any top-level .py, .html or .sh the app needs
        at run time), then A or B.
