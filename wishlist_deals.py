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
# Alert when a game's best price is at or under your number. Steam app id = price.
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


def evaluate(games: list[dict], prices: dict[str, dict], bundles: list[dict], cfg) -> dict[str, dict]:
    """Return {alert_key: alert} for everything that qualifies right now."""
    rules = cfg["rules"]
    tol = float(rules.get("low_tolerance_pct", "0") or 0)
    low_min_cut = float(rules.get("low_min_discount_pct", "0") or 0)
    big = float(rules.get("big_discount_pct", "0") or 0)
    want_bundles = rules.getboolean("bundles", fallback=True)
    split = lambda s: {x.strip().lower() for x in (s or "").split(",") if x.strip()}
    only, exclude = split(cfg["itad"].get("only_shops")), split(cfg["itad"].get("exclude_shops"))
    targets = {}
    if cfg.has_section("targets"):
        for k, v in cfg["targets"].items():
            try:
                targets[int(k)] = float(v)
            except ValueError:
                log(f"Ignoring target '{k} = {v}' (use: steam_app_id = price)")

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
        hit_low = bool(d.get("cut", 0) > 0 and d.get("cut", 0) >= low_min_cut
                       and low and price["amountInt"] <= low["amountInt"] * (1 + tol / 100))
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

def write_snapshot(cfg, current: dict[str, dict], unsent: list[dict], wishlist_count: int, untracked: int) -> None:
    """Save every qualifying deal as JSON for the deals web page. 'new' = not notified yet."""
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


def refresh_reviews(itad, st: dict, pairs: list[tuple[str, int]]) -> None:
    """pairs = [(itad_id, steam_appid)] for games that currently have deals."""
    now = time.time()
    # Entries cached before tags or Deck status were added lack those keys; refresh them too.
    stale = [(g, a) for g, a in dict.fromkeys(pairs)
             if now - (st["reviews"].get(g) or {}).get("t", 0) > REVIEW_TTL
             or "tags" not in (st["reviews"].get(g) or {})
             or (st["reviews"].get(g) or {}).get("deck") is None]  # missing, or Steam was unreachable
    if not stale:
        return
    log(f"Updating ratings, tags and Deck status for {len(stale)} games with deals...")
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
    if not cfg.read(args.config):
        raise SystemExit(f"No config at {args.config}. Run with --init to create one.")
    for sec in ("steam", "itad", "rules"):
        if not cfg.has_section(sec):
            cfg.add_section(sec)
    key = cfg["itad"].get("key", "").strip()
    if not key or key == "YOUR_ITAD_API_KEY":
        raise SystemExit("Set [itad] key (free at https://isthereanydeal.com/apps/my/).")

    steamid = resolve_steamid(cfg["steam"].get("profile", ""), cfg["steam"].get("api_key") or None)
    wl = get_wishlist(steamid)
    # Steam's rank order. If priorities are a clean 0..N-1 sequence, 0 is the top pick;
    # otherwise treat 0 as "unranked" and put those after the ranked games.
    prios = [w.get("priority") for w in wl]
    zero_based = 0 in prios and len(set(prios)) == len(prios)
    wl.sort(key=lambda w: ((w.get("priority") if zero_based or w.get("priority") else 10**6),
                           w.get("date_added") or 0))
    top_n = int(cfg["rules"].get("top_n", "0") or 0)
    if top_n:
        wl = wl[:top_n]
    appids = [int(w["appid"]) for w in wl]
    rank = {int(w["appid"]): i + 1 for i, w in enumerate(wl)}

    st = load_state()
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
    current = evaluate(games, prices, bundles, cfg)
    fresh = list(current.values()) if args.all else new_alerts(current, st["sent"])
    gid_of = {g["appid"]: g["gid"] for g in games}
    refresh_reviews(itad, st, [(gid_of[a["appid"]], a["appid"]) for a in current.values() if a["appid"] in gid_of])
    save_state(st)
    for a in current.values():
        meta = st["reviews"].get(gid_of.get(a["appid"])) or {}
        a["reviews"] = meta.get("r") or {}
        a["tags"], a["released"] = meta.get("tags") or [], meta.get("released")
        a["earlyAccess"], a["developer"] = bool(meta.get("ea")), meta.get("dev")
        a["deck"], a["protondb"] = meta.get("deck"), meta.get("proton") or {}
    write_snapshot(cfg, current, new_alerts(current, st["sent"]), len(appids), untracked)
    if args.snapshot_only:
        return

    log(f"{len(appids)} wishlist games ({untracked} not on ITAD); {len(current)} qualifying deals; {len(fresh)} new.")
    if fresh:
        print(digest_text(fresh))
    if args.dry_run:
        return

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
            return

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


if __name__ == "__main__":
    main()
