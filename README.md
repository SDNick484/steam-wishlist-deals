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
| `examples/config.example.ini` | Every setting, documented |
| `install/alpine.sh` | Installer for Alpine Linux (OpenRC); works well in a small Proxmox LXC |
| `contrib/systemd/` | Service and timer units for systemd hosts |

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
- checks at 10:40 and 19:40, as their own marked block in the `steam` user's crontab
- the deals page as an OpenRC service on port 8485

It finishes with a dry run against your real wishlist. Re-running it updates the code, keeps your config, adds any newer settings with defaults, and remembers the options below.

Other options:
- `CHECK_TIMES`: comma-separated daily times. The page shows the next one.
- `PORT`: the deals page's port.
- `DEALS_PAGE_URL`: where tapping a notification goes.
- `NAV=Label=URL`: adds a header link, for example to [Steam Backlog Triage](https://github.com/SDNick484/steam-backlog-triage).
- `TZ_NAME`: sets the system time zone.

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

Writes need `Content-Type: application/json` and a body of at most 1 KB. The price must be above 0 and at most 1000 (rounded to cents), and there's a limit of 5,000 targets. Anything else gets a 400 with a short error.

## Tests

```sh
python3 -m unittest
```

Standard library only. The tests run the watcher against local stand-ins for Steam, ITAD and ProtonDB, and check that alerts and the digest haven't changed.

## Security

The server has no login.
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
