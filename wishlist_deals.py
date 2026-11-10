#!/usr/bin/env python3
"""
wishlist_deals.py - watch your Steam wishlist for deals worth acting on.

Each run:
  1. reads your public Steam wishlist
  2. asks IsThereAnyDeal (ITAD) for every official store's current price, each game's
     all-time low, and any live bundles that include it
  3. alerts only on games that are NEW to one of these lists since the last run:
       * at (or within a tolerance of) the all-time low, and actually on sale
       * in a live bundle (Humble, Fanatical, Indie Gala, ...)
       * at or under a target price you set for that game
       * discounted by at least N% (optional)
     A game that keeps qualifying isn't repeated, unless its price drops further. Once it stops
     qualifying (the sale ends), it can alert again the next time it qualifies.
  4. sends one digest to Home Assistant (phone push) and/or email, and prints it.

Standard library only. Needs a free ITAD API key: https://isthereanydeal.com/apps/my/
(create an app; the key is on its page). Your Steam profile and wishlist must be public.

Usage:
  python3 wishlist_deals.py --init               write a starter config and exit
  python3 wishlist_deals.py --dry-run            show what would be sent; change nothing
  python3 wishlist_deals.py --all --dry-run      show every qualifying deal, even ones already sent
  python3 wishlist_deals.py                      normal run (for cron)

Config: ~/.config/wishlist-deals/config.ini     State: ~/.local/state/wishlist-deals/state.json
"""

from __future__ import annotations

import argparse
import configparser
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

STEAM_API = os.environ.get("WD_STEAM_API", "https://api.steampowered.com")
ITAD_API = os.environ.get("WD_ITAD_API", "https://api.isthereanydeal.com")
STORE_API = os.environ.get("WD_STORE_API", "https://store.steampowered.com")
PROTONDB_API = os.environ.get("WD_PROTONDB_API", "https://www.protondb.com")
STEAM_SHOP_ID = 61
BATCH = 200
UA = "steam-wishlist-deals/1.0"
# Games that have no deal yet get their Deck/ProtonDB/rating details filled in gradually: at most
# this many per run (games with deals are always refreshed), so no single run balloons.
META_CAP = int(os.environ.get("WD_META_CAP", "60"))
# Used only when no price has been seen to read a currency from.
COUNTRY_CURRENCY = {"US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "DE": "EUR", "FR": "EUR", "ES": "EUR",
                    "IT": "EUR", "NL": "EUR", "IE": "EUR", "AT": "EUR", "BE": "EUR", "FI": "EUR", "PT": "EUR"}

CONFIG_PATH = os.path.expanduser(os.environ.get("WD_CONFIG", "~/.config/wishlist-deals/config.ini"))
STATE_PATH = os.path.expanduser(os.environ.get("WD_STATE", "~/.local/state/wishlist-deals/state.json"))

STARTER_CONFIG = """\
[steam]
# Your profile URL, custom URL name, or 17-digit SteamID64.
profile = https://steamcommunity.com/id/yourname/
# Only needed if you give a custom URL name instead of a SteamID64.
api_key =

[itad]
key = YOUR_ITAD_API_KEY
country = US
# Leave both blank to consider every store ITAD tracks. Names as ITAD shows them.
only_shops =
exclude_shops =

[rules]
# Alert when the best current price is within this % of the all-time low (0 = at or below it).
low_tolerance_pct = 0
# Ignore all-time lows with a smaller discount than this % (new games' first small sale). 0 = off.
low_min_discount_pct = 0
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
# Leave url blank to skip. Token: HA profile > Security > Long-lived access tokens.
url =
token =
service = notify.mobile_app_your_phone
# Where tapping the notification goes. Blank = the first deal's store page.
deals_page_url =

[output]
# JSON the deals web page reads (backlog_server.py serves it at /deals).
deals_json = ~/exports/deals.json

[email]
# Leave host blank to skip. For Gmail, use an app password and port 587.
host =
port = 587
starttls = true
username =
password =
from =
to =
"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# =========================================================================== HTTP

def http(method: str, url: str, body=None, headers=None, retries: int = 3):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    delay = 5.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                wait = float(e.headers.get("Retry-After") or delay)
                log(f"  HTTP {e.code} from {urllib.parse.urlparse(url).netloc}; retrying in {wait:.0f}s")
                time.sleep(wait)
                delay *= 2
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"{method} {url.split('?')[0]} failed: HTTP {e.code} {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise SystemExit(f"{method} {url.split('?')[0]} failed: {e}")


# =========================================================================== Steam

def resolve_steamid(profile: str, api_key: str | None) -> str:
    profile = profile.strip()
    if re.fullmatch(r"\d{17}", profile):
        return profile
    m = re.search(r"/profiles/(\d{17})", profile)
    if m:
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)", profile)
    vanity = m.group(1) if m else profile
    if not api_key:
        raise SystemExit("To use a custom URL name, set [steam] api_key, or use your SteamID64 instead.")
    q = urllib.parse.urlencode({"key": api_key, "vanityurl": vanity})
    r = (http("GET", f"{STEAM_API}/ISteamUser/ResolveVanityURL/v1/?{q}") or {}).get("response", {})
    if r.get("success") != 1:
        raise SystemExit(f"Steam couldn't resolve '{vanity}'.")
    return r["steamid"]


def get_wishlist(steamid: str) -> list[dict]:
    q = urllib.parse.urlencode({"steamid": steamid})
    r = (http("GET", f"{STEAM_API}/IWishlistService/GetWishlist/v1/?{q}") or {}).get("response", {})
    items = r.get("items")
    if items is None:
        raise SystemExit("Steam returned no wishlist. Make sure your profile and game details are public.")
    return items


# =========================================================================== ITAD

ITAD_PACE = float(os.environ.get("WD_ITAD_PACE", "0.4"))  # seconds between requests (~750 per 5 min)


class ITAD:
    def __init__(self, key: str, country: str):
        self.key, self.country = key, country
        self._next = 0.0

    def url(self, path: str, **params) -> str:
        # Every ITAD call builds its URL here first, so this is where requests get paced.
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = time.monotonic() + ITAD_PACE
        params = {"key": self.key, **{k: v for k, v in params.items() if v is not None}}
        return f"{ITAD_API}{path}?{urllib.parse.urlencode(params)}"

    def lookup_steam(self, appids: list[int]) -> dict[int, str | None]:
        out: dict[int, str | None] = {}
        for i in range(0, len(appids), BATCH):
            chunk = appids[i:i + BATCH]
            r = http("POST", self.url(f"/lookup/id/shop/{STEAM_SHOP_ID}/v1"), [f"app/{a}" for a in chunk]) or {}
            for a in chunk:
                out[a] = r.get(f"app/{a}")
        return out

    def info(self, gid: str) -> dict:
        return http("GET", self.url("/games/info/v2", id=gid)) or {}

    def prices(self, gids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(gids), BATCH):
            for g in http("POST", self.url("/games/prices/v3", country=self.country), gids[i:i + BATCH]) or []:
                out[g["id"]] = g
        return out

    def bundles(self, gids: list[str]) -> list[dict]:
        out, seen = [], set()
        for i in range(0, len(gids), BATCH):
            r = http("POST", self.url("/games/overview/v2", country=self.country), gids[i:i + BATCH]) or {}
            for b in r.get("bundles") or []:
                if b.get("id") not in seen:
                    seen.add(b.get("id"))
                    out.append(b)
        return out


# =========================================================================== Rules

def money(p: dict | None) -> str:
    if not p:
        return "?"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}.get(p.get("currency", ""), "")
    amt = f"{p['amount']:.2f}"
    return f"{sym}{amt}" if sym else f"{amt} {p.get('currency', '')}".strip()


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def best_deal(deals: list[dict], only: set[str], exclude: set[str]) -> dict | None:
    ok = [d for d in deals or []
          if (not only or d["shop"]["name"].lower() in only)
          and d["shop"]["name"].lower() not in exclude
          and not d.get("voucher")]
    return min(ok, key=lambda d: d["price"]["amountInt"]) if ok else None


def is_low(deal: dict, low: dict | None, tolerance_pct: float = 0.0, min_cut_pct: float = 0.0) -> bool:
    """Is this deal at (or within tolerance_pct of) the all-time low, and really on sale?

    The one test behind both the "all-time low" alert and the deals page's low flag. A deal
    needs a discount (cut > 0) of at least min_cut_pct; a game at full price is never "a low",
    even when full price is its lowest price ever.
    """
    cut = deal.get("cut", 0)
    return bool(cut > 0 and cut >= min_cut_pct and low
                and deal["price"]["amountInt"] <= low["amountInt"] * (1 + tolerance_pct / 100))


def parse_rules(cfg) -> dict:
    """The [rules] and [itad] settings that decide what counts as a deal, parsed once."""
    rules = cfg["rules"]
    split = lambda s: {x.strip().lower() for x in (s or "").split(",") if x.strip()}
    return {
        "tol": float(rules.get("low_tolerance_pct", "0") or 0),
        "low_min_cut": float(rules.get("low_min_discount_pct", "0") or 0),
        "big": float(rules.get("big_discount_pct", "0") or 0),
        "want_bundles": rules.getboolean("bundles", fallback=True),
        "only": split(cfg["itad"].get("only_shops")),
        "exclude": split(cfg["itad"].get("exclude_shops")),
    }


def ini_targets(cfg) -> dict[int, float]:
    """Target prices from the legacy [targets] section of config.ini: {steam appid: price}."""
    targets = {}
    if cfg.has_section("targets"):
        for k, v in cfg["targets"].items():
            try:
                targets[int(k)] = float(v)
            except ValueError:
                log(f"Ignoring target '{k} = {v}' (use: steam_app_id = price)")
    return targets


def evaluate(games: list[dict], prices: dict[str, dict], bundles: list[dict], cfg,
             targets: dict[int, float] | None = None) -> dict[str, dict]:
    """Return {alert_key: alert} for everything that qualifies right now.

    targets is {steam appid: price}; None reads the legacy [targets] section of the config.
    """
    r = parse_rules(cfg)
    tol, low_min_cut, big, want_bundles = r["tol"], r["low_min_cut"], r["big"], r["want_bundles"]
    only, exclude = r["only"], r["exclude"]
    if targets is None:
        targets = ini_targets(cfg)

    now = datetime.now(timezone.utc)
    by_gid = {g["gid"]: g for g in games}
    alerts: dict[str, dict] = {}

    for g in games:
        p = prices.get(g["gid"])
        if not p:
            continue
        d = best_deal(p.get("deals"), only, exclude)
        if not d:
            continue
        price = d["price"]
        low = ((p.get("historyLow") or {}).get("all")) or None
        base = {"appid": g["appid"], "title": g["title"], "rank": g["rank"], "price": price,
                "regular": d.get("regular"), "cut": d.get("cut", 0), "shop": d["shop"]["name"],
                "url": d.get("url"), "low": low, "expiry": d.get("expiry")}
        # One price alert per game, filed under its strongest reason: target > all-time low > deep cut.
        t = targets.get(g["appid"])
        hit_target = t is not None and price["amount"] <= t
        hit_low = is_low(d, low, tol, low_min_cut)
        hit_cut = bool(big and d.get("cut", 0) >= big)
        if hit_target or hit_low or hit_cut:
            kind = "target" if hit_target else "low" if hit_low else "cut"
            new_low = hit_low and price["amountInt"] < low["amountInt"]
            alerts[f"deal:{g['appid']}"] = {**base, "kind": kind, "is_low": hit_low, "new_low": new_low,
                                            "why": {"target": f"Under your {money({'amount': t, 'currency': price.get('currency', 'USD')})} target" if t is not None else "",
                                                    "low": "All-time low", "cut": f"{d.get('cut')}% off"}[kind]}

    if want_bundles:
        for b in bundles:
            exp = parse_ts(b.get("expiry"))
            if exp and exp < now:
                continue
            for tier in b.get("tiers") or []:
                for bg in tier.get("games") or []:
                    g = by_gid.get(bg.get("id"))
                    if not g:
                        continue
                    key = f"bundle:{b['id']}:{g['appid']}"
                    tier_price = tier.get("price")
                    if key in alerts and tier_price and alerts[key]["price"] and \
                            alerts[key]["price"]["amountInt"] <= tier_price["amountInt"]:
                        continue
                    alerts[key] = {"appid": g["appid"], "title": g["title"], "rank": g["rank"], "kind": "bundle",
                                   "why": f"In {b.get('title', 'a bundle')}", "shop": (b.get("page") or {}).get("name", ""),
                                   "price": tier_price, "url": b.get("url") or b.get("details"),
                                   "expiry": b.get("expiry"), "cut": None, "regular": None, "low": None}
    return alerts


def new_alerts(current: dict[str, dict], sent: dict[str, dict]) -> list[dict]:
    """Alerts not sent before, or whose price dropped since they were sent."""
    out = []
    for key, a in current.items():
        prev = sent.get(key)
        amt = (a.get("price") or {}).get("amountInt")
        if prev is None or (amt is not None and prev.get("amountInt") is not None and amt < prev["amountInt"]):
            out.append(a)
    return out


# =========================================================================== Targets file

TARGETS_VERSION = 1


def data_dir(cfg) -> str:
    """Directory of the deals page data: where deals.json (and targets.json) live."""
    out = cfg["output"] if cfg.has_section("output") else {}
    return os.path.dirname(os.path.expanduser(out.get("deals_json") or "~/exports/deals.json"))


def targets_path(cfg) -> str:
    return os.path.join(data_dir(cfg), "targets.json")


def country_currency(cfg, default: str | None = None) -> str | None:
    country = ((cfg["itad"].get("country") if cfg.has_section("itad") else "") or "US").strip().upper()
    return COUNTRY_CURRENCY.get(country, default)


def read_targets_file(path: str) -> dict[int, float] | None:
    """{appid: price} from targets.json; None when the file doesn't exist.

    A damaged file reads as empty (and says so) rather than stopping the run.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        raw = doc["targets"]
        if not isinstance(raw, dict):
            raise ValueError("'targets' is not an object")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        log(f"Couldn't read {path} ({e}); treating it as having no targets.")
        return {}
    out = {}
    for k, v in raw.items():
        try:
            price = float(v)
            if price > 0:
                out[int(k)] = price
        except (TypeError, ValueError):
            log(f"Ignoring target '{k}: {v}' in {path}")
    return out


def create_targets_file(path: str, targets: dict[int, float], currency: str) -> bool:
    """Create targets.json only if it doesn't exist yet. True if this call created it.

    The content is written to a temp file first and then hard-linked into place, so the file
    appears complete, and can never replace one the deals server wrote a moment earlier.
    """
    doc = {"version": TARGETS_VERSION, "currency": currency,
           "targets": {str(k): round(v, 2) for k, v in sorted(targets.items())}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_targets(cfg, dry_run: bool = False) -> dict[int, float]:
    """Target prices for this run: {steam appid: price}.

    They live in targets.json, which the deals page edits. If it's missing and config.ini still
    has a [targets] section, those entries move into a new targets.json (once). After that the
    section is ignored. A dry run reads config.ini but writes nothing.
    """
    path = targets_path(cfg)
    legacy = ini_targets(cfg)
    current = read_targets_file(path)
    if current is None and legacy:
        if dry_run:
            return legacy
        try:
            created = create_targets_file(path, legacy, country_currency(cfg, "USD"))
        except OSError as e:
            log(f"Couldn't create {path} ({e}); using the targets in config.ini this run.")
            return legacy
        if created:
            log(f"Moved {len(legacy)} target{'s' if len(legacy) != 1 else ''} from config.ini to targets.json")
            return legacy
        current = read_targets_file(path) or {}  # the deals server wrote it first; its file wins
    if current is None:
        return {}
    if legacy:
        log("Ignoring [targets] in config.ini: targets are managed on the deals page (targets.json).")
    return current


# =========================================================================== Output

KIND_ORDER = {"target": 0, "low": 1, "bundle": 2, "cut": 3}
KIND_HEAD = {"target": "Under your target price", "low": "At an all-time low",
             "bundle": "In a live bundle", "cut": "Deep discounts"}


def ends(a: dict) -> str:
    exp = parse_ts(a.get("expiry"))
    if not exp:
        return ""
    days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
    if days < 1:
        return "ends today"
    return f"ends in {int(days)} day{'s' if int(days) != 1 else ''}"


def line(a: dict) -> str:
    bits = [money(a.get("price"))]
    if a["kind"] == "bundle":
        bits = [a["why"][0].lower() + a["why"][1:]]
        if a.get("price"):
            bits.append(f"({money(a['price'])} tier)")
    else:
        if a.get("cut"):
            bits.append(f"-{a['cut']}%")
        bits.append(f"at {a['shop']}")
        if a.get("new_low"):
            bits.append(f"(new all-time low; was {money(a['low'])})")
        elif a.get("is_low"):
            if a["kind"] != "low":
                bits.append("(all-time low)")
        elif a.get("low"):
            bits.append(f"(low {money(a['low'])})")
    rv = (a.get("reviews") or {}).get("steam")
    if rv:
        bits.append(f"· Steam {rv['score']}%")
    e = ends(a)
    if e:
        bits.append(f"· {e}")
    return f"{a['title']}: " + " ".join(b for b in bits if b)


def sort_alerts(alerts: list[dict]) -> list[dict]:
    return sorted(alerts, key=lambda a: (KIND_ORDER[a["kind"]], a["rank"] or 10**6, a["title"].lower()))


def digest_text(alerts: list[dict]) -> str:
    out, kind = [], None
    for a in sort_alerts(alerts):
        if a["kind"] != kind:
            kind = a["kind"]
            out.append(("\n" if out else "") + KIND_HEAD[kind])
        out.append(f"  {line(a)}\n    {a.get('url') or ''}".rstrip())
    return "\n".join(out)


def digest_html(alerts: list[dict]) -> str:
    rows, kind = [], None
    for a in sort_alerts(alerts):
        if a["kind"] != kind:
            kind = a["kind"]
            rows.append(f'<tr><td colspan="2" style="padding:14px 0 4px;font-weight:600;color:#555">{KIND_HEAD[kind]}</td></tr>')
        title = html.escape(a["title"])
        link = f'<a href="{html.escape(a["url"])}">{title}</a>' if a.get("url") else title
        rows.append(f'<tr><td style="padding:3px 16px 3px 0">{link}</td>'
                    f'<td style="padding:3px 0;color:#333">{html.escape(line(a)[len(a["title"]) + 2:])}</td></tr>')
    return ('<div style="font-family:system-ui,sans-serif;font-size:14px">'
            f'<table style="border-collapse:collapse">{"".join(rows)}</table>'
            '<p style="color:#888;font-size:12px;margin-top:16px">Prices from IsThereAnyDeal. '
            'Links go through ITAD to the store.</p></div>')


def notify_ha(cfg, alerts: list[dict]) -> None:
    ha = cfg["home_assistant"] if cfg.has_section("home_assistant") else {}
    if not ha.get("url"):
        return
    service = ha.get("service", "").strip()
    if not re.fullmatch(r"notify\.[a-z0-9_]+", service):
        raise SystemExit("[home_assistant] service should look like notify.mobile_app_your_phone")
    ordered = sort_alerts(alerts)
    shown = ordered[:6]
    msg = "\n".join(line(a) for a in shown)
    if len(ordered) > len(shown):
        msg += f"\n+{len(ordered) - len(shown)} more"
    title = f"{len(alerts)} wishlist deal{'s' if len(alerts) != 1 else ''}"
    body = {"title": title, "message": msg}
    tap_url = ha.get("deals_page_url") or next((a["url"] for a in ordered if a.get("url")), None)
    if tap_url:
        body["data"] = {"url": tap_url, "clickAction": tap_url}
    url = ha["url"].rstrip("/") + "/api/services/" + service.replace(".", "/", 1)
    http("POST", url, body, headers={"Authorization": f"Bearer {ha.get('token', '')}"}, retries=1)
    log(f"Sent Home Assistant notification via {service}")


def notify_email(cfg, alerts: list[dict]) -> None:
    em = cfg["email"] if cfg.has_section("email") else {}
    if not em.get("host"):
        return
    msg = EmailMessage()
    n = len(alerts)
    lows = sum(1 for a in alerts if a.get("is_low"))
    msg["Subject"] = f"{n} Steam wishlist deal{'s' if n != 1 else ''}" + (f" ({lows} at all-time low)" if lows else "")
    msg["From"] = em.get("from") or em.get("username")
    msg["To"] = em.get("to") or em.get("username")
    msg.set_content(digest_text(alerts) + "\n\nPrices from IsThereAnyDeal.\n")
    msg.add_alternative(digest_html(alerts), subtype="html")
    port = int(em.get("port", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(em["host"], port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(em["host"], port, timeout=30)
        if em.get("starttls", "true").lower() in ("1", "true", "yes"):
            s.starttls(context=ctx)
    with s:
        if em.get("username"):
            s.login(em["username"], em.get("password", ""))
        s.send_message(msg)
    log(f"Emailed {msg['To']}")


# =========================================================================== Deals page snapshot

def live_bundles(bundles: list[dict], now: datetime | None = None) -> dict[str, dict]:
    """{itad game id: its cheapest live bundle tier}, for the deals page's "In a bundle" view."""
    now = now or datetime.now(timezone.utc)
    best: dict[str, dict] = {}
    rank = lambda c: (c["price"] is None, (c["price"] or {}).get("amountInt", 0))  # priced tiers first, cheapest first
    for b in bundles:
        exp = parse_ts(b.get("expiry"))
        if exp and exp < now:
            continue
        for t in b.get("tiers") or []:
            for bg in t.get("games") or []:
                gid = bg.get("id")
                cand = {"title": b.get("title") or "a bundle", "price": t.get("price"),
                        "url": b.get("url") or b.get("details"), "expiry": b.get("expiry")}
                if gid and (gid not in best or rank(cand) < rank(best[gid])):
                    best[gid] = cand
    return best


def meta_fields(meta: dict) -> dict:
    """The cached ratings/tags/Deck details the page shows for a game."""
    return {"reviews": meta.get("r") or {}, "tags": meta.get("tags") or [], "released": meta.get("released"),
            "earlyAccess": bool(meta.get("ea")), "developer": meta.get("dev"),
            "deck": meta.get("deck"), "protondb": meta.get("proton") or {}}


def build_games(games: list[dict], prices: dict[str, dict], bundles: list[dict], cfg, targets: dict[int, float],
                meta_by_gid: dict[str, dict], new_appids: set[int], now: datetime | None = None) -> list[dict]:
    """One entry per tracked wishlist game, with the flags the deals page's views filter on.

    Unlike the alerts, which are one per qualifying deal, every game appears here: on sale or
    not, priced or not. ITAD returns a game's regular price too when no store discounts it, so
    price is set whenever any allowed store sells it. A game ITAD lists no store for has price None.
    """
    r = parse_rules(cfg)
    in_bundle = live_bundles(bundles, now) if r["want_bundles"] else {}
    out = []
    for g in games:
        p = prices.get(g["gid"]) or {}
        d = best_deal(p.get("deals"), r["only"], r["exclude"])
        low = ((p.get("historyLow") or {}).get("all")) or None
        price = d["price"] if d else None
        cut = (d.get("cut") or 0) if d else 0
        bundle = in_bundle.get(g["gid"])
        target = targets.get(g["appid"])
        out.append({
            "appid": g["appid"], "title": g["title"], "rank": g["rank"],
            "steamUrl": f"https://store.steampowered.com/app/{g['appid']}/",
            "price": price, "regular": d.get("regular") if d else None, "cut": cut,
            "shop": d["shop"]["name"] if d else "", "url": d.get("url") if d else None,
            "expiry": d.get("expiry") if d else None, "low": low,
            "onSale": bool(cut > 0 or bundle), "isLow": bool(d and is_low(d, low, r["tol"], r["low_min_cut"])),
            "inBundle": bundle is not None, "bundle": bundle,
            "target": target, "atTarget": bool(target is not None and price and price["amount"] <= target),
            "new": g["appid"] in new_appids,
            **meta_fields(meta_by_gid.get(g["gid"]) or {}),
        })
    return out


def snapshot_currency(games: list[dict], cfg) -> str | None:
    """Currency of the prices on the page: the first price seen, else the configured country's."""
    for g in games:
        if g.get("price") and g["price"].get("currency"):
            return g["price"]["currency"]
    return country_currency(cfg)


def write_snapshot(cfg, current: dict[str, dict], unsent: list[dict], wishlist_count: int, untracked: int,
                   games: list[dict] | None = None, currency: str | None = None) -> None:
    """Save every qualifying deal (and, with games, every tracked wishlist game) as JSON for the page.

    'new' = not notified yet. "deals" is the list of alert-qualifying deals; "games" has one
    entry per tracked game for the page's views.
    """
    out_cfg = cfg["output"] if cfg.has_section("output") else {}
    path = os.path.expanduser(out_cfg.get("deals_json") or "~/exports/deals.json")
    unsent_ids = {id(a) for a in unsent}
    deals = []
    for key, a in current.items():
        deals.append({
            "key": key, "kind": a["kind"], "why": a["why"], "appid": a["appid"], "title": a["title"],
            "rank": a["rank"], "shop": a.get("shop") or "", "url": a.get("url"),
            "price": a.get("price"), "regular": a.get("regular"), "cut": a.get("cut"), "low": a.get("low"),
            "isLow": bool(a.get("is_low")), "newLow": bool(a.get("new_low")), "expiry": a.get("expiry"),
            "new": id(a) in unsent_ids,
            "steamUrl": f"https://store.steampowered.com/app/{a['appid']}/",
            "reviews": a.get("reviews") or {},
            "tags": a.get("tags") or [], "released": a.get("released"),
            "earlyAccess": bool(a.get("earlyAccess")), "developer": a.get("developer"),
            "deck": a.get("deck"), "protondb": a.get("protondb") or {},
        })
    snap = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "wishlistCount": wishlist_count, "untracked": untracked, "deals": deals}
    if games is not None:
        snap["games"] = games
        snap["currency"] = currency
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        log(f"Couldn't write the deals page data to {path}: {e}")


# =========================================================================== Ratings

REVIEW_TTL = 7 * 86400
REVIEW_SOURCES = {"steam": "steam", "metascore": "metacritic", "opencritic": "opencritic"}


def parse_reviews(info: dict) -> dict:
    """ITAD info 'reviews' -> {steam|metacritic|opencritic: {score, count, url}} (scores 0-100)."""
    out = {}
    for r in info.get("reviews") or []:
        key = REVIEW_SOURCES.get(str(r.get("source", "")).strip().lower())
        if key and isinstance(r.get("score"), (int, float)):
            out[key] = {"score": int(r["score"]), "count": r.get("count"), "url": r.get("url")}
    return out


def game_meta(info: dict) -> dict:
    """Ratings plus the descriptive bits the deals page shows (tags, release, early access)."""
    devs = [d.get("name") for d in info.get("developers") or [] if isinstance(d, dict) and d.get("name")]
    return {
        "r": parse_reviews(info),
        "tags": [t for t in info.get("tags") or [] if isinstance(t, str)][:6],
        "released": info.get("releaseDate") if isinstance(info.get("releaseDate"), str) else None,
        "ea": bool(info.get("earlyAccess")),
        "dev": devs[0] if devs else None,
    }


def soft_json(url: str):
    """GET JSON; None on any failure (these extras must never break a run)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"null")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def fetch_deck(appid: int) -> int | None:
    """Valve's Steam Deck rating: 3 Verified, 2 Playable, 1 Unsupported, 0 Unknown."""
    q = urllib.parse.urlencode({"nAppID": appid, "l": "english"})
    data = soft_json(f"{STORE_API}/saleaction/ajaxgetdeckappcompatibilityreport?{q}")
    if data is None:
        return None
    cat = (data.get("results") or {}).get("resolved_category")
    return cat if cat in (0, 1, 2, 3) else 0


def fetch_protondb(appid: int) -> dict:
    """ProtonDB community summary: tier (native/platinum/gold/silver/bronze/borked/pending) and report count."""
    data = soft_json(f"{PROTONDB_API}/api/v1/reports/summaries/{appid}.json")
    if not isinstance(data, dict) or not data.get("tier"):
        return {}
    return {"tier": str(data["tier"]).lower(), "total": data.get("total"),
            "trending": str(data.get("trendingTier") or "").lower() or None}


def refresh_reviews(itad, st: dict, pairs: list[tuple[str, int]], extra: list[tuple[str, int]] | None = None,
                    extra_cap: int | None = None) -> None:
    """pairs = [(itad_id, steam_appid)] for games that currently have deals: all stale ones are refreshed.

    extra = the other wishlist games (best-ranked first). They're refreshed too, but at most
    extra_cap per run, so the first run after an upgrade isn't a few hundred lookups at once.
    """
    now = time.time()

    def is_stale(gid: str) -> bool:
        entry = st["reviews"].get(gid) or {}
        # Entries cached before tags or Deck status were added lack those keys; refresh them too.
        return now - entry.get("t", 0) > REVIEW_TTL or "tags" not in entry or entry.get("deck") is None  # None: Steam was unreachable

    stale = [(g, a) for g, a in dict.fromkeys(pairs) if is_stale(g)]
    if extra:
        seen = {g for g, _ in stale} | {g for g, _ in pairs}
        cap = META_CAP if extra_cap is None else extra_cap
        more = [(g, a) for g, a in dict.fromkeys(extra) if g not in seen and is_stale(g)]
        stale += more[:cap]
        if len(more) > cap:
            log(f"{len(more) - cap} more wishlist games will get ratings and Deck status on later runs.")
    if not stale:
        return
    log(f"Updating ratings, tags and Deck status for {len(stale)} games...")
    for i, (gid, appid) in enumerate(stale, 1):
        info = itad.info(gid)
        deck = fetch_deck(appid)
        proton = fetch_protondb(appid)
        time.sleep(0.3)  # be polite to Steam's store and ProtonDB
        st["reviews"][gid] = {"t": int(now), **game_meta(info), "deck": deck, "proton": proton}
        if info.get("title"):
            st["titles"][gid] = info["title"]
        if i % 50 == 0:
            save_state(st)


# =========================================================================== State

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = {}
    st.setdefault("ids", {})      # steam appid -> itad id (or null)
    st.setdefault("titles", {})   # itad id -> title
    st.setdefault("reviews", {})  # itad id -> {"t": epoch, "r": {steam|metacritic|opencritic: {score,count,url}}}
    st.setdefault("sent", {})     # alert key -> {amountInt, at}
    return st


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


# =========================================================================== Run status

RUN_STATUS_FILE = "run_status.json"
RUN_STATUS_VERSION = 1
ERROR_CLASSES = ("steam", "itad", "notify", "config", "other")
FAIL_NOTICE_AFTER = 3   # consecutive failed runs before the one "checks are failing" notice
FAIL_NOTICE = "Wishlist checks are failing: {}"
RECOVERED_NOTICE = "Wishlist checks are working again"


class Stage:
    """Where a run is, so a failure can be classed by the stage it happened in. Never holds error text."""

    def __init__(self):
        self.name = "config"
        self.have_config = False   # the config file was read; only then is there a data directory to write to

    def error_class(self) -> str:
        return self.name if self.name in ERROR_CLASSES else "other"

    def recording(self, args) -> bool:
        """Dry runs don't touch run_status.json. The page's quiet refresh (--snapshot-only) does, without notices."""
        return self.have_config and not args.dry_run


def utc_stamp(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_status_path(cfg) -> str:
    return os.path.join(data_dir(cfg), RUN_STATUS_FILE)


def read_run_status(path: str) -> dict | None:
    """The previous run's status, or None when there is no usable file."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        failures = doc["consecutive_failures"]
        if not isinstance(doc, dict) or isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            return None
        last_ok = doc.get("last_ok")
        return {"last_ok": last_ok if isinstance(last_ok, str) else None, "consecutive_failures": failures}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def next_run_status(prev: dict | None, error_class: str | None, now: str) -> dict:
    """The run_status.json content after a run. error_class None = the run succeeded."""
    if error_class is None:
        return {"version": RUN_STATUS_VERSION, "last_attempt": now, "last_ok": now,
                "consecutive_failures": 0, "error_class": None}
    return {"version": RUN_STATUS_VERSION, "last_attempt": now, "last_ok": (prev or {}).get("last_ok"),
            "consecutive_failures": (prev or {}).get("consecutive_failures", 0) + 1,
            "error_class": error_class if error_class in ERROR_CLASSES else "other"}


def write_run_status(path: str, doc: dict) -> None:
    """Atomic (temp file, then rename), mode 0640 like targets.json."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def send_notice(cfg, text: str) -> int:
    """Send one short text through every configured channel. Returns how many delivered it; never raises."""
    delivered = 0
    ha = cfg["home_assistant"] if cfg.has_section("home_assistant") else {}
    if ha.get("url"):
        try:
            service = ha.get("service", "").strip()
            if not re.fullmatch(r"notify\.[a-z0-9_]+", service):
                raise SystemExit("[home_assistant] service should look like notify.mobile_app_your_phone")
            body = {"title": "Wishlist Deals", "message": text}
            if ha.get("deals_page_url"):
                body["data"] = {"url": ha["deals_page_url"], "clickAction": ha["deals_page_url"]}
            url = ha["url"].rstrip("/") + "/api/services/" + service.replace(".", "/", 1)
            http("POST", url, body, headers={"Authorization": f"Bearer {ha.get('token', '')}"}, retries=1)
            delivered += 1
        except (Exception, SystemExit) as e:
            log(f"Home Assistant notice failed: {e}")
    em = cfg["email"] if cfg.has_section("email") else {}
    if em.get("host"):
        try:
            msg = EmailMessage()
            msg["Subject"] = text
            msg["From"] = em.get("from") or em.get("username")
            msg["To"] = em.get("to") or em.get("username")
            msg.set_content(text + "\n")
            port = int(em.get("port", "587"))
            ctx = ssl.create_default_context()
            if port == 465:
                s = smtplib.SMTP_SSL(em["host"], port, context=ctx, timeout=30)
            else:
                s = smtplib.SMTP(em["host"], port, timeout=30)
                if em.get("starttls", "true").lower() in ("1", "true", "yes"):
                    s.starttls(context=ctx)
            with s:
                if em.get("username"):
                    s.login(em["username"], em.get("password", ""))
                s.send_message(msg)
            delivered += 1
        except (Exception, SystemExit) as e:
            log(f"Email notice failed: {e}")
    return delivered


def record_run(cfg, error_class: str | None, quiet: bool = False) -> None:
    """Write run_status.json after a run that isn't a dry run, and send the failure / recovery notice when due.

    A full run sends one notice when the failure counter reaches FAIL_NOTICE_AFTER or more while the previous
    file was below it, and one when it succeeds after a streak that long. A quiet run (--snapshot-only) is
    recorded the same way but never notifies. The alert rules, the digest and state.json are not involved.
    """
    path = run_status_path(cfg)
    prev = read_run_status(path)
    doc = next_run_status(prev, error_class, utc_stamp())
    try:
        write_run_status(path, doc)
    except OSError as e:
        log(f"Couldn't write {path}: {e}")
        return
    if error_class is not None:
        log(f"Run failed ({doc['error_class']}); {doc['consecutive_failures']} in a row.")
        before = (prev or {}).get("consecutive_failures", 0)
        if not quiet and doc["consecutive_failures"] >= FAIL_NOTICE_AFTER and before < FAIL_NOTICE_AFTER:
            send_notice(cfg, FAIL_NOTICE.format(doc["error_class"]))
    elif not quiet and prev and prev["consecutive_failures"] >= FAIL_NOTICE_AFTER:
        send_notice(cfg, RECOVERED_NOTICE)


# =========================================================================== Main

def main() -> None:
    ap = argparse.ArgumentParser(description="Alert on Steam wishlist deals (all-time lows, bundles, targets).")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--init", action="store_true", help="Write a starter config and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print the digest; don't notify or save state")
    ap.add_argument("--all", action="store_true", help="Include deals that were already sent")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="Just refresh the deals page data; print nothing, notify nothing")
    args = ap.parse_args()

    if args.init:
        if os.path.exists(args.config):
            raise SystemExit(f"{args.config} already exists.")
        os.makedirs(os.path.dirname(args.config), exist_ok=True)
        with open(os.open(args.config, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(STARTER_CONFIG)
        print(f"Wrote {args.config}. Fill in [steam] profile and [itad] key, then run with --dry-run.")
        return

    cfg = configparser.ConfigParser(interpolation=None)
    stage = Stage()
    try:
        error = run(args, cfg, stage)
    except (Exception, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code in (None, 0):
            error = None
        else:
            if stage.recording(args):
                record_run(cfg, stage.error_class(), quiet=args.snapshot_only)
            raise
    if stage.recording(args):
        record_run(cfg, error, quiet=args.snapshot_only)


def run(args, cfg, stage: "Stage") -> str | None:
    """One watcher run. Returns None on success, or an error class when the run failed without raising."""
    if not cfg.read(args.config):
        raise SystemExit(f"No config at {args.config}. Run with --init to create one.")
    stage.have_config = True
    for sec in ("steam", "itad", "rules"):
        if not cfg.has_section(sec):
            cfg.add_section(sec)
    key = cfg["itad"].get("key", "").strip()
    if not key or key == "YOUR_ITAD_API_KEY":
        raise SystemExit("Set [itad] key (free at https://isthereanydeal.com/apps/my/).")

    stage.name = "steam"
    steamid = resolve_steamid(cfg["steam"].get("profile", ""), cfg["steam"].get("api_key") or None)
    wl = get_wishlist(steamid)
    # Steam's rank order. If priorities are a clean 0..N-1 sequence, 0 is the top pick;
    # otherwise treat 0 as "unranked" and put those after the ranked games.
    prios = [w.get("priority") for w in wl]
    zero_based = 0 in prios and len(set(prios)) == len(prios)
    wl.sort(key=lambda w: ((w.get("priority") if zero_based or w.get("priority") else 10**6),
                           w.get("date_added") or 0))
    stage.name = "config"
    top_n = int(cfg["rules"].get("top_n", "0") or 0)
    if top_n:
        wl = wl[:top_n]
    appids = [int(w["appid"]) for w in wl]
    rank = {int(w["appid"]): i + 1 for i, w in enumerate(wl)}

    st = load_state()
    stage.name = "itad"
    itad = ITAD(key, cfg["itad"].get("country", "US") or "US")

    missing = [a for a in appids if str(a) not in st["ids"]]
    if missing:
        for a, gid in itad.lookup_steam(missing).items():
            st["ids"][str(a)] = gid
    # Titles: one ITAD request per game, but only the first time a game is seen. Paced to stay
    # under ITAD's rate limit, and saved as we go so an interrupted first run isn't wasted.
    need = [st["ids"][str(a)] for a in appids if st["ids"].get(str(a)) and st["ids"][str(a)] not in st["titles"]]
    if need:
        log(f"Looking up {len(need)} new game titles on ITAD (about {len(need) * ITAD_PACE / 60:.0f} min; cached after this)...")
    for i, gid in enumerate(need, 1):
        info = itad.info(gid)
        st["titles"][gid] = info.get("title") or "Unknown title"
        st["reviews"][gid] = {"t": int(time.time()), **game_meta(info)}
        if i % 50 == 0:
            save_state(st)
            log(f"  {i}/{len(need)}")
    if missing or need:
        save_state(st)
    games = []
    for a in appids:
        gid = st["ids"].get(str(a))
        if not gid:
            continue
        games.append({"appid": a, "gid": gid, "title": st["titles"].get(gid, f"App {a}"), "rank": rank[a]})
    untracked = len(appids) - len(games)

    gids = [g["gid"] for g in games]
    prices = itad.prices(gids)
    bundles = itad.bundles(gids) if cfg["rules"].getboolean("bundles", fallback=True) else []
    stage.name = "other"
    targets = load_targets(cfg, dry_run=args.dry_run)
    current = evaluate(games, prices, bundles, cfg, targets)
    unsent = new_alerts(current, st["sent"])
    fresh = list(current.values()) if args.all else unsent
    gid_of = {g["appid"]: g["gid"] for g in games}
    stage.name = "itad"
    refresh_reviews(itad, st, [(gid_of[a["appid"]], a["appid"]) for a in current.values() if a["appid"] in gid_of],
                    extra=[(g["gid"], g["appid"]) for g in games])
    save_state(st)
    stage.name = "other"
    for a in current.values():
        a.update(meta_fields(st["reviews"].get(gid_of.get(a["appid"])) or {}))
    page_games = build_games(games, prices, bundles, cfg, targets, st["reviews"], {a["appid"] for a in unsent})
    write_snapshot(cfg, current, unsent, len(appids), untracked, page_games, snapshot_currency(page_games, cfg))
    if args.snapshot_only:
        return None

    log(f"{len(appids)} wishlist games ({untracked} not on ITAD); {len(current)} qualifying deals; {len(fresh)} new.")
    if fresh:
        print(digest_text(fresh))
    if args.dry_run:
        return None

    if fresh:
        # Try every configured channel. If all of them fail, don't mark anything as sent,
        # so the next run tries again instead of silently dropping the deals.
        tried = ok = 0
        for name, fn, sec, field in (("Home Assistant", notify_ha, "home_assistant", "url"),
                                     ("Email", notify_email, "email", "host")):
            if not (cfg.has_section(sec) and cfg[sec].get(field)):
                continue
            tried += 1
            try:
                fn(cfg, fresh)
                ok += 1
            except (Exception, SystemExit) as e:
                log(f"{name} notification failed: {e}")
        if tried and not ok:
            log("No notification was delivered; these deals will be retried next run.")
            return "notify"

    # Remember what's been sent. Forget alerts that no longer qualify, so the next sale alerts again.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sent = {}
    for k, a in current.items():
        amt = (a.get("price") or {}).get("amountInt")
        prev = st["sent"].get(k)
        if prev and not any(f is a for f in fresh):
            sent[k] = prev
        else:
            sent[k] = {"amountInt": amt, "at": now}
    st["sent"] = sent
    save_state(st)
    return None


if __name__ == "__main__":
    main()
