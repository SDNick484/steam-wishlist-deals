# Steam Wishlist Deals

Watches your Steam wishlist for deals worth acting on, sends one digest to your phone through Home Assistant and/or by email, and serves a web page listing every current deal.

**What triggers an alert:**
- **All-time low:** the best price is at or below the lowest price ever. An optional tolerance and a minimum discount filter out small first sales.
- **Live bundle:** the game is in a bundle at Humble, Fanatical, IndieGala and other stores, with the cheapest tier that includes it.
- **Your target price:** the price is at or under a number you set for that game, on the deals page.
- **Deep discount:** optional; anything at least N% off.

Each game alerts once, under its strongest reason. It alerts again only if the price drops further, or after the sale ends and comes back.

**The deals page** lists your whole wishlist, not only the games that alerted. Pick a view:
- **All wishlisted:** every tracked game, with its current price or "No current price on ITAD".
- **On sale** (the default): any game with a discount or a live bundle.
- **All-time lows**, **In a bundle** and **At or under target.**

Each view button shows its count under your other filters. **Minimum % off** (10 to 90) narrows On sale, All-time lows and At or under target. It's greyed out in All wishlisted and In a bundle, and bundle-only games are hidden while it's set, with a note saying how many. Your view and minimum are remembered in the browser.

Each row shows price, discount, store, previous all-time low and sale end date. It also shows:
- Steam, Metacritic and OpenCritic ratings
- the game's top Steam-style tags
- Steam Deck compatibility (Valve's rating plus ProtonDB's community tier)

You can also filter by store, tag, Steam rating and Deck compatibility, and sort by wishlist rank, discount, price, end date or rating. A **Check now** button refreshes prices without sending a notification.

**Target prices** are set on the page. Every row has **Set target** (or **Edit**): type a price, press Enter to save or Esc to cancel, and **Clear** removes it. The row shows how far the price is under or above the target. Targets are saved on the server in `targets.json`, next to `deals.json`, and the watcher alerts on them at the next scheduled run. Changes show immediately, and the page puts the old value back with a message if a save fails.

The project uses only Python's standard library, and the page is a single HTML file.

## Files

| File | What it does |
|---|---|
| `wishlist_deals.py` | The watcher: reads your wishlist, finds deals, notifies, writes `deals.json` for the page |
| `deals_server.py` | Serves the deals page and its JSON, and saves target prices |
| `deals.html` | The deals page |
| `deals_query.py`, `access_jwt.py`, `ask.py` | Ask (AI search): the read-only wishlist queries, Cloudflare Access token checks, and the question loop. Imported by the server at start-up, so keep them next to `deals_server.py` |
| `ask_log.jsonl` | Not in the repo: the server writes it into the data directory (see [AI search (Ask)](#ai-search-ask)) |
| `examples/config.example.ini` | Every setting, documented |
| `install/alpine.sh` | Installer for Alpine Linux (OpenRC); works well in a small Proxmox LXC |
| `contrib/systemd/` | Service and timer units for systemd hosts |
| `tests/`, `.github/` | Tests and CI (see [Tests and CI](#tests-and-ci)) |

## What you need

- **An IsThereAnyDeal API key** (free): sign in at <https://isthereanydeal.com/apps/my/>, register an app, and copy its key.
- **Your SteamID64**, the 17-digit number starting with `7656119`. Find it in the Steam client under your name → **Account details**. A profile URL also works, though a custom URL name also needs a Steam Web API key.
- **A public wishlist:** your Steam profile and game details must be public.
- **For phone alerts (optional):** a Home Assistant long-lived access token and your phone's `notify.mobile_app_…` service. Email works through any SMTP server instead.

## Quick start (any Linux)

```sh
python3 wishlist_deals.py --init                 # writes ~/.config/wishlist-deals/config.ini
$EDITOR ~/.config/wishlist-deals/config.ini      # fill in [steam] profile and [itad] key
python3 wishlist_deals.py --all --dry-run        # show every current deal; sends nothing
python3 deals_server.py --check-times 10:40,19:40   # then open http://localhost:8485/
```

Schedule the watcher from cron or a systemd timer. A normal run (no flags) notifies only about deals that are new since the last run. Steam sales start at 10:00 Pacific, so a check shortly after that catches them on day one.

The first run looks up every wishlist game once on IsThereAnyDeal and caches the results. That's about 2 minutes for 300 games, paced to stay under their rate limit.

## Install on Alpine (OpenRC)

```sh
git clone https://github.com/SDNick484/steam-wishlist-deals.git
cd steam-wishlist-deals
STEAM_PROFILE=7656119XXXXXXXXXX ITAD_KEY=your_key \
HA_URL=http://homeassistant.local:8123 HA_TOKEN=your_token HA_SERVICE=notify.mobile_app_my_phone \
sh install/alpine.sh
```

The installer sets up:
- an unprivileged `steam` user and the code in `/opt/steam-wishlist-deals`
- the config at `/home/steam/.config/wishlist-deals/config.ini` (mode 600)
- an empty settings file for [Ask](#ai-search-ask) at `/home/steam/.config/wishlist-deals/ask.env` (mode 600), created once and never overwritten
- checks at 10:40 and 19:40, as their own marked block in the `steam` user's crontab
- the deals page as an OpenRC service on port 8485
- log rotation for this app's own two logs (see [Log rotation](#log-rotation))

It finishes with a dry run against your real wishlist. Re-running it updates the code, keeps your config, adds any newer settings with defaults, and remembers the options below.

Other options:
- `CHECK_TIMES`: comma-separated daily times. The page shows the next one.
- `PORT`: the deals page's port.
- `DEALS_PAGE_URL`: where tapping a notification goes.
- `NAV=Label=URL`: adds a header link, for example to [Steam Backlog Triage](https://github.com/SDNick484/steam-backlog-triage).
- `TZ_NAME`: sets the system time zone.

### Log rotation

The installer runs `apk add logrotate` and writes one file, `/etc/logrotate.d/steam-wishlist-deals`, covering only this app's own logs: the deals page's `deals-server.log` and the watcher's `wishlist.log`, both in the `steam` user's `logs` directory. They rotate weekly, keep 8 rotations, compress, skip a missing or empty log, and use `copytruncate` so the running server and the cron job keep their open files. Re-running the installer rewrites that file and touches no other logrotate file.

## Failed and stale runs

After every run except `--dry-run` (the page's quiet **Check now** included) the watcher writes `run_status.json` next to `deals.json`, atomically and with mode 0640:

```json
{"version": 1, "last_attempt": "2026-11-10T19:40:03Z", "last_ok": "2026-11-10T19:40:03Z",
 "consecutive_failures": 0, "error_class": null}
```

- `last_ok` stays at the last good run when a run fails. `consecutive_failures` goes back to 0 on success.
- `error_class` is `null` after a success. After a failure it names the stage that failed and never holds error text: `steam` (the wishlist fetch), `itad` (IsThereAnyDeal), `notify` (no channel delivered the digest), `config` (a missing or bad setting) or `other`.
- A run that finds nothing to alert on is still a success.

**The deals page** shows one dismissible banner when the last run failed ("Deals data last refreshed … The last check failed (itad); see the watcher log on the server.") or when the data is over 36 hours old ("Deals data was last refreshed 40 hours ago; …"). Dismissing it lasts for that page session. Without a `run_status.json` (an older watcher, or before the first run) the page judges staleness from the time in `deals.json` and never shows the failure text.

**A failure notice** goes out through your configured channels (Home Assistant and/or email) when a scheduled run takes the failure counter to 3 or more and the run before it was below 3: "Wishlist checks are failing: itad". It is sent once, not on every later failure. When a run succeeds after such a streak you get one more: "Wishlist checks are working again". **Check now** is recorded too (a success clears the banner, a failure counts) but never notifies, so if it is the run that crosses 3, no failure notice is sent for that streak. If sending the notice fails, it is logged and the run carries on. The alert rules, the digest and `state.json` behave exactly as before.

**Routes** (each also answers under `/deals/...`):
- `GET /api/deals/status` returns the file's content, or `{"version":1,"last_attempt":null,"last_ok":null,"consecutive_failures":0,"error_class":null}` when there is none.
- `GET /healthz` still answers `ok`.
- `GET /healthz?deep=1` answers 200 or 503 with `{"ok": true, "build": "abc1234", "last_ok": "2026-11-10T19:40:03Z", "age_seconds": 3600, "consecutive_failures": 0}`. `ok` means the last run succeeded and `last_ok` is at most 36 hours old. Without `run_status.json`, `last_ok` falls back to the modification time of `deals.json`; with neither, `ok` is false.
- `build` is the short commit the running code came from. The installer stamps it into the installed `deals_server.py`; a `git archive` or GitHub tarball gets it through `export-subst`. A plain checkout that is run directly reports `unknown`.

An example Home Assistant sensor that turns on when the deals watcher is failing or stale:

```yaml
binary_sensor:
  - platform: rest
    name: "Wishlist deals problem"
    resource: http://deals.example.com:8485/healthz?deep=1
    value_template: "{{ not value_json.ok }}"
    device_class: problem
    scan_interval: 900
    timeout: 10
```

## Settings worth knowing

See `examples/config.example.ini` for the full list.

| Setting | Effect |
|---|---|
| `[itad] only_shops` / `exclude_shops` | Limit the stores considered, e.g. `only_shops = Steam, GOG, Humble Store, Fanatical` |
| `[rules] low_min_discount_pct` | Ignore all-time lows smaller than this discount (new releases' first 10–20% sales) |
| `[rules] big_discount_pct` | Also alert on any discount this deep, low or not |
| `[targets]` | Legacy. Targets are set on the page; see below for how existing ones move |
| `[home_assistant] deals_page_url` | Open the deals page when a notification is tapped |

## Target prices and `targets.json`

`targets.json` lives in the folder of `[output] deals_json` and looks like this:

```json
{"version": 1, "currency": "USD", "targets": {"1245620": 20.0}}
```

- **Moving from config.ini:** the first time the watcher runs and finds no `targets.json`, it copies any `[targets]` lines from `config.ini` into it and logs `Moved N targets from config.ini to targets.json`. After that it ignores `[targets]` and says so in the log. A `--dry-run` reads them without writing.
- **After an update**, run the watcher once before opening the page, so the migration happens first: `su steam -c '/usr/bin/python3 /opt/steam-wishlist-deals/wishlist_deals.py --snapshot-only'`.
- **Editing the file by hand** is fine; the page reads it on load. The server owns writes while it runs. A file it can't parse is reported as an error and never overwritten.
- **Currency:** a target is in the currency the snapshot reports (`currency` in `deals.json`).

The server's API, also reachable under `/deals/api/deals/...` for setups that only forward `/deals`:

| Request | Result |
|---|---|
| `GET /api/deals/targets` | `{"version": 1, "currency": "USD", "targets": {...}}` |
| `PUT /api/deals/targets/{appid}` with `{"price": 12.5}` | `{"appid", "price", "previous"}` |
| `DELETE /api/deals/targets/{appid}` | `{"appid", "previous"}` |
| `GET /api/deals/ask/status` | `{"ask": true, "turns": 5}` when this request may use [Ask](#ai-search-ask), else `{"ask": false}` |
| `POST /api/deals/ask` with `{"thread": [{"q": "..."}]}` | `{"answer", "items", "turns_left"}` |
| `GET /ask` (also `/deals/ask`) | The deals page with Ask showing |

Writes need `Content-Type: application/json` and a body of at most 1 KB. The price must be above 0 and at most 1000 (rounded to cents), and there's a limit of 5,000 targets. Anything else gets a 400 with a short error.

## Tests and CI

The product needs only the Python standard library. The tests use `unittest`, and CI adds test-only tools (Playwright, shellcheck) that are never installed on a server.

```sh
# Evaluation, notifications and digest (pinned by golden files), the snapshot, targets.json, the server, Ask, the privacy scanner
REQUIRE_OPENSSL=1 python3 -m unittest -v

# The Ask tests need openssl (they sign test tokens); REQUIRE_OPENSSL=1 makes a missing one an error, not a skip
REQUIRE_OPENSSL=1 python3 -m unittest -v tests.test_ask tests.test_access_jwt tests.test_deals_query

# The pages in a real browser (pip install -r tests/browser/requirements.txt && playwright install chromium)
REQUIRE_BROWSER=1 python3 -m unittest -v tests.browser.test_page tests.browser.test_ask_page

# The Alpine installer, stubbed (changes /etc and /opt: run it in a container)
docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh

# shellcheck, py_compile, systemd-analyze verify
sh .github/scripts/lint.sh
```

What each part checks:

- **Unit tests** run the watcher against local stand-ins for Steam, ITAD and ProtonDB and check that alerts, the digest and `state.json` haven't changed, that `deals.json` has the shape the page expects, how `targets.json` is read and written, every route of the server, the same-origin guard on every changing route, the response headers, `run_status.json` (shape, classes, counting, the failure and recovery notices), the deep health check and the build id. They also include the privacy scanner's own tests.
- **Page test** starts `deals_server.py` over a generated 60-game `deals.json` and a `targets.json`, then opens `deals.html` in Chromium at 390x844, in light and dark, at `/` and at `/deals` (no trailing slash). It checks that each scope's number matches the rows listed, the minimum % filter and its "bundle-only deals hidden" line, that a target can be set, edited and cleared with the target view updating without a reload and the value surviving one, that an older `deals.json` without `games` shows the fallback message, that the failed-run and stale-data banner shows, dismisses and stays hidden when healthy, that there are no console errors, and that the page never scrolls sideways.
- **Ask tests** cover the read layer over `deals.json` and `targets.json`, the Access token checks, the question loop with a scripted fake model (no network), the daily cap, the log, and the server's Ask routes under both prefixes. `tests/browser/test_ask_page.py` checks the Ask pill and an answer in Chromium with the API stubbed.
- **Installer test** runs `install/alpine.sh` in `python:3.12-alpine` twice (and a third time with one option changed, a fourth without git): the Ask modules are installed and `ask.env` is created empty (mode 600) and kept byte for byte on re-runs, the config and the saved options are kept, the logrotate file is written for this app's logs only and rewritten on a re-run, the build id is stamped into the installed server and never into the checkout, there is exactly one `BEGIN`/`END` cron block, unrelated cron lines survive, and nothing but the local health check touches the network.
- **Lint** runs shellcheck on every shell script, `py_compile` on every Python file and `systemd-analyze verify` on `contrib/systemd`.

CI runs on pushes to `main`, pull requests, weekly, and on demand. The unit tests run on Python 3.11 to 3.14 and fail if any test is skipped; the other jobs use one version. Every third-party action is pinned to a full commit SHA with its version in a comment, and Dependabot updates them (and the test-only Playwright pin) weekly.

### Privacy scan

This repository is public, so a CI job (`.github/scripts/privacy_scan.py`) fails if a tracked file or a commit contains a SteamID, an IPv4 address that isn't loopback, documentation (RFC 5737) or private (RFC 1918), an email that isn't a `noreply` or `example` address, or any term from a private denylist. CI logs are public too, so the scanner prints only the file, line and rule, never the matched text. Files under `tests/fixtures/` may contain IP addresses, but never SteamIDs.

The denylist holds things no pattern can know (your hostnames, domain, name, time zone, device names). It lives in a repository secret, so it is never committed. To add it:

1. On GitHub, open this repository and choose **Settings**.
2. In the left sidebar, choose **Secrets and variables**, then **Actions**.
3. Under **Repository secrets**, choose **New repository secret**.
4. Name it exactly `PRIVACY_DENYLIST`.
5. In **Secret**, enter one term per line. Matching ignores case and looks at file contents, file names and commit messages. Lines starting with `#` are ignored.
6. Choose **Add secret**.

Until it exists, the job runs the pattern checks only and shows a warning. Pull requests from forks, and Dependabot's, can't read the secret and get the same warning. To check locally: `PRIVACY_DENYLIST="$(cat my-terms.txt)" python3 .github/scripts/privacy_scan.py`. A line can opt out with the text `privacy-scan: allow` (say why beside it).

## AI search (Ask)

**Ask** lets you question your wishlist in plain language ("what's on sale under $10 that runs on the Deck?", "which games are at or below my target?") at `https://steam.example.com/deals/ask`. It answers with the matching games as the same rows the list shows. A small **Ask** pill appears in the page header when it is on. Ask is off until an API key is set.

**Setup**

1. In the Anthropic Console, create an API key, ideally in its own workspace with a monthly spend limit.
2. Edit the settings file the installer created and fill in the lines below: `vim /home/steam/.config/wishlist-deals/ask.env`. Type the key into the file yourself; don't paste it into a chat, a ticket or a shell command line.
3. Run `rc-service wishlist-deals restart`.

The file is `KEY=VALUE` lines; quotes are optional and `#` starts a comment. The server refuses to read it if group or others can read or write it (`chmod 600`). The installer creates it empty and never touches an existing one, so re-running the installer keeps your settings. If you don't use the installer, pass `--ask-env PATH` to `deals_server.py` (default `~/.config/wishlist-deals/ask.env`).

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_SEARCH_KEY` | empty (Ask is off) | API key |
| `ANTHROPIC_SEARCH_MODEL` | `claude-haiku-5-5` | Model used to answer |
| `ASK_DAILY_LIMIT` | `200` | Questions per day across everyone, for this app (the Backlog app counts its own) |
| `ASK_ACCESS_TEAM` | empty | Your Cloudflare Access team domain, e.g. `your-team.cloudflareaccess.com` |
| `ASK_ACCESS_AUD` | empty | The Audience (AUD) tag of the Access application that covers the page's hostname (the same value as the Backlog app's `ask.env`) |
| `ASK_EMAILS` | empty (any user Access lets in) | Comma-separated emails allowed to ask, e.g. `you@example.com` |
| `ASK_LAN` | `0` | `1` lets direct requests from your LAN ask (see below) |

**Who can ask.** Ask needs to know who is asking, because it spends money. There are two ways in:

- **Through Cloudflare Access** (Tunnel + Access in front of the page). The server checks the signed Access token on each request (RS256 signature, audience, issuer, expiry), so the `Cf-Access-Authenticated-User-Email` header alone is never trusted. Set `ASK_ACCESS_TEAM` and `ASK_ACCESS_AUD`, and optionally `ASK_EMAILS` to limit it to certain people. Find the team domain in Cloudflare Zero Trust under Settings, Custom pages. Find the AUD under Access, Applications, the application that covers this page, Overview, Application Audience (AUD) Tag. The deals page shares a hostname with the Backlog dashboard (`/deals...` is a path inside the dashboard's Access application), so `ASK_ACCESS_TEAM` and `ASK_ACCESS_AUD` are that application's values, the same ones the Backlog app's `ask.env` uses. The application must cover the page's hostname, e.g. `steam.example.com`.
- **From your LAN**, with `ASK_LAN=1`. A request is accepted only if all of these hold: it comes straight from a private or loopback address, it carries no Cloudflare headers (`Cf-Ray`, `Cf-Connecting-Ip`), and you opened the page by IP address (`http://<server-ip>:8485/ask`), not by a hostname. The hostname rule stops a DNS-rebinding page from reaching Ask. If you put a reverse proxy on the same machine, leave `ASK_LAN` off: its requests arrive from a private address.

With neither set up, Ask answers 401. Leave `ASK_ACCESS_*` empty and `ASK_LAN` at `0` if you want it off even with a key.

**Limits.** A thread holds at most 5 questions (start a new question to clear it). Each question runs at most 5 rounds of lookups and 60 seconds, and at most 2 questions run at once (others get a "busy" answer you can retry). The daily cap counts questions since local midnight in the server's time zone; when it is reached Ask answers "daily limit reached" until the next day. `ASK_DAILY_LIMIT=0` turns every question away.

**Cost** (Claude Haiku 5.5, model id `claude-haiku-5-5`, at $0.10 / $0.50 per MTok input / output for prompts up to 100k tokens, and $0.50 / $2.50 above that, priced per request; checked 2026-11-10 at platform.claude.com/docs/en/about-claude/pricing and platform.claude.com/docs/en/models/haiku-5-5/overview). Haiku 5.5's tokenizer counts about 30% more tokens than Haiku 4.5 for the same text, so the figures below assume 3 characters per token:

- **Typical question** (2 to 3 rounds, about 10k input and 600 output tokens in total; the instructions and tools are about 4.3k characters, roughly 1.5k tokens, resent each round, and a typical lookup result is about 5k characters, roughly 1.7k tokens): about $0.0013.
- **Heavy case (illustrative, not a ceiling):** a question of 5 rounds, each resending the 1.5k-token instructions and tools plus 3 lookup results of 12,000 characters (about 4k tokens each) from every earlier round, comes to roughly 130k input tokens and about 10k output tokens: about $0.018. The 40k-token forced-finish rule is checked between rounds; cumulative input passes 40k after round 3 here, so round 4 would be forced to finish and a real question would stop sooner than this. Tool calls per round are not capped, so a single round can be larger than assumed. The real bound on spend is the daily cap plus a spend limit on the Console workspace.
- **At the default cap of 200 a day:** about $0.26 a day typically, and about $3.60 a day if every question were the heavy case above ($0.018 x 200), which is not a hard maximum.

Prices change; check the pricing page, and the daily cap and a spend limit on the Console workspace are what bound spend.

**Data sent to Anthropic** (api.anthropic.com only): each question and the results of the lookups the model makes, which can include game names, prices, discounts, stores, tags, bundle titles and prices, all-time lows, developer and release year, your target prices, Deck and ProtonDB ratings and review scores. Never your SteamID, profile name, file paths or keys. Your API key stays on the server: it is read into memory, never logged and never sent to the browser.

**Log.** `ask_log.jsonl` in the data directory (mode 600) gets one JSON line per request: who asked, rounds, token counts, how many answer items were dropped and the status. The question text is never stored. Entries older than 90 days are removed.

## Security

The server has no login.
- **Same-origin only for changes.** Every `PUT`, `POST` and `DELETE` is refused with a 403 (`{"error": "cross-site request refused"}`) when the browser's `Origin` header names a different host than the request's `Host` (including `Origin: null`), or when `Sec-Fetch-Site` is `cross-site`. That stops another web page from saving a target price or starting a check through your browser. Requests with neither header, such as `curl` or cron, are allowed. A reverse proxy has to pass the original `Host` header on, or the page's own saves are refused.
- **Response headers.** Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `Content-Security-Policy: frame-ancestors 'none'` (the page can't be framed). There is no `script-src` policy because the page's script is inline.
- **Keep it private:** bind it to your LAN or a VPN, or put it behind an authenticating reverse proxy such as Cloudflare Tunnel + Access.
- **"Check now" runs the watcher on the server,** so don't expose the page publicly.
- **Anyone who can open the page can change target prices**, so the same rule applies.
- **Keys stay on the server.** API keys and tokens live only in the config file and are never sent to the page.

## Data sources

- Steam Web API: your wishlist.
- [IsThereAnyDeal](https://isthereanydeal.com): prices, all-time lows, bundles, ratings and tags.
- Steam store: Valve's Steam Deck rating.
- [ProtonDB](https://www.protondb.com): community compatibility tiers, from its unofficial public summary feed. If that feed changes, only the ProtonDB badge disappears.

Ratings, tags and Deck data are fetched only for games that currently have deals, and cached for a week.

This project isn't affiliated with Valve, IsThereAnyDeal, ProtonDB or any store.

## License

MIT
