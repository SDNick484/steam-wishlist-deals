"""The read layer under the Ask search: pure functions over deals.json and targets.json.

No file or network access here and no import of wishlist_deals.py. The server parses the files and hands the
result to build_snapshot(); every tool function takes that Snapshot and returns JSON-ready dicts.

What it mirrors: the rules in deals.html (steamScore, deckOk, atTarget, bundleOnly, cutOf, endsAt, inScope,
cutOk, and render()'s base filter and sort). deals.html wins when they disagree. The scope flags (onSale, isLow,
inBundle) are the ones the watcher wrote into deals.json; only "at or under target" is worked out here, from the
live targets, as the page does.

norm() and suggest() are copied from steam-backlog-triage's backlog_query.py (commit 7468859); tag_key() is its
tag_key() without the alias table.

Bad arguments raise QueryError with a short message that names the argument and, for choices, the allowed
values. The message goes to the model.
"""
from __future__ import annotations

import difflib
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

SCOPES = ("all", "sale", "low", "bundle", "target")
CUT_SCOPES = ("sale", "low", "target")     # the minimum-% filter applies to these scopes only
CUTS = (0, 10, 25, 33, 50, 66, 75, 90)
DECKS = ("verified", "playable", "works")
SORTS = ("rank", "cut", "price", "ending", "rating")
PROTON_GOOD = ("native", "platinum", "gold")
DECK_LABEL = {3: "Verified", 2: "Playable", 1: "Unsupported", 0: "Unknown"}
# Filters are dropped in this order when nothing matches (weakest first). The scope is never dropped.
RELAX_ORDER = ("query", "min_rating", "tags", "deck", "shops", "max_price", "min_cut", "new_only", "at_target")
NO_LIST = "No deals list on the server yet."
LEGACY = "The deals list is from an older watcher; run Check now on the list page."
MAX_LIMIT = 25

_DEFAULTS: dict[str, Any] = {
    "scope": "sale", "min_cut": 0, "max_price": None, "shops": None, "tags": None, "deck": None,
    "min_rating": None, "at_target": None, "new_only": False, "query": None, "sort": "rank",
    "limit": 10, "offset": 0,
}


class QueryError(ValueError):
    """A bad argument. The message goes to the model, so it names the argument and the allowed values."""


# --------------------------------------------------------------------------- lookup helpers
# Copied from steam-backlog-triage backlog_query.py (7468859).
def norm(name: str) -> str:
    """Matching key: case-, space-, dash- and underscore-insensitive ('Volume Up' -> 'volumeup')."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def suggest(wanted: str, candidates: Iterable[str], *, limit: int = 5) -> list[str]:
    """Up to ``limit`` close matches: substring matches on the normalized key first, then difflib."""
    if limit <= 0:
        return []
    key = norm(wanted)
    if not key:
        return []
    by_norm: dict[str, str] = {}
    for c in candidates:
        n = norm(c)
        if n and n not in by_norm:
            by_norm[n] = c
    substr = [c for n, c in by_norm.items() if key in n or n in key]
    fuzzy = [by_norm[k] for k in difflib.get_close_matches(key, list(by_norm.keys()), n=limit, cutoff=0.5)]
    return list(dict.fromkeys([*substr, *fuzzy]))[:limit]


def tag_key(raw: Any) -> str:
    """NFKC, lower case, letters and digits only: "Souls-like", "souls like" and "Soulslike" share a key."""
    text = unicodedata.normalize("NFKC", "" if raw is None else str(raw)).lower()
    return "".join(c for c in text if unicodedata.category(c)[0] in "LN")


def now() -> datetime:
    """The clock. Tests patch this."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- the snapshot
@dataclass(frozen=True)
class Snapshot:
    games: tuple[dict, ...]
    targets: dict[int, float]
    targets_ok: bool
    generated_at: str | None
    currency: str | None
    wishlist_count: int | None
    untracked: int | None
    newlow_ids: frozenset[int]
    run: dict | None
    legacy: bool


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _opt_int(v: Any) -> int | None:
    return v if _is_int(v) else None


def _opt_str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def build_snapshot(doc: dict | None, targets: dict[int, float] | None, run: dict | None) -> Snapshot | None:
    """doc is the parsed deals.json (None -> None). Targets None means targets.json could not be read.
    Only the fields below are copied, so nothing else in the file can reach the model."""
    if not isinstance(doc, dict):
        return None
    raw = doc.get("games")
    legacy = not isinstance(raw, list)
    games = () if legacy else tuple(
        g for g in raw if isinstance(g, dict) and _is_int(g.get("appid")) and isinstance(g.get("title"), str))
    clean: dict[int, float] = {}
    for k, v in (targets or {}).items():
        if _is_int(k) and _is_num(v):
            clean[k] = float(v)
    newlow = frozenset(d["appid"] for d in (doc.get("deals") or []) if isinstance(d, dict) and d.get("newLow")
                       and _is_int(d.get("appid")))
    return Snapshot(
        games=games, targets=clean, targets_ok=targets is not None,
        generated_at=_opt_str(doc.get("generatedAt")),
        currency=_opt_str(doc.get("currency")),
        wishlist_count=_opt_int(doc.get("wishlistCount")), untracked=_opt_int(doc.get("untracked")),
        newlow_ids=newlow, run=dict(run) if isinstance(run, dict) else None, legacy=legacy)


# --------------------------------------------------------------------------- the page's rules
def _amount(p: Any) -> float | None:
    a = p.get("amount") if isinstance(p, dict) else None
    return float(a) if _is_num(a) else None


def at_target(g: dict, targets: dict[int, float]) -> bool:
    t = targets.get(g["appid"])
    a = _amount(g.get("price"))
    return t is not None and a is not None and a <= t


def bundle_only(g: dict) -> bool:
    """On sale only because a bundle has it."""
    return bool(g.get("inBundle")) and not (g.get("cut") or 0) > 0


def _cut(g: dict) -> int:
    return g.get("cut") or 0


def in_scope(g: dict, scope: str, targets: dict[int, float]) -> bool:
    if scope == "all":
        return True
    if scope == "sale":
        return bool(g.get("onSale"))
    if scope == "low":
        return bool(g.get("isLow"))
    if scope == "bundle":
        return bool(g.get("inBundle"))
    return at_target(g, targets)


def cut_ok(g: dict, scope: str, min_cut: int) -> bool:
    return not min_cut or scope not in CUT_SCOPES or _cut(g) >= min_cut


def _proton(g: dict) -> str | None:
    t = (g.get("protondb") or {}).get("tier")
    return t if isinstance(t, str) and t else None


def deck_ok(g: dict, deck: str | None) -> bool:
    if not deck:
        return True
    d = g.get("deck")
    if deck == "verified":
        return d == 3
    if deck == "playable":
        return d in (3, 2)
    return d in (3, 2) or _proton(g) in PROTON_GOOD


def query_ok(g: dict, q: str | None) -> bool:
    q = (q or "").strip().lower()
    return not q or q in g["title"].lower() or any(q in str(t).lower() for t in (g.get("tags") or []))


def _steam(g: dict) -> dict:
    return (g.get("reviews") or {}).get("steam") or {}


def _score(g: dict) -> int | float | None:
    s = _steam(g).get("score")
    return s if _is_num(s) else None


def rating_ok(g: dict, n: int | float | None) -> bool:
    if not n:
        return True
    s = _score(g)
    return (s if s is not None else -1) >= n


def _parse_time(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def ends_at(g: dict, at: datetime) -> datetime | None:
    """When the deal that makes the game 'on sale' ends: the bundle's expiry if bundle-only, else the store
    deal's when there is a % off, else None. `at` is the current time (kept for the callers' symmetry; the
    expiry itself does not depend on it)."""
    if bundle_only(g):
        return _parse_time((g.get("bundle") or {}).get("expiry"))
    return _parse_time(g.get("expiry")) if _cut(g) > 0 else None


def _ends_in_days(g: dict, at: datetime) -> int | None:
    e = ends_at(g, at)
    if e is None:
        return None
    secs = (e - at).total_seconds()
    return None if secs < 0 else int(secs // 86400)


def _price_for_max(g: dict) -> float | None:
    if bundle_only(g):
        return _amount((g.get("bundle") or {}).get("price"))
    return _amount(g.get("price"))


def _is_new_low(s: Snapshot, g: dict) -> bool:
    return bool(g.get("newLow")) or g["appid"] in s.newlow_ids


# --------------------------------------------------------------------------- sorting
def _sort_key(sort: str, at: datetime):
    inf = float("inf")

    def ends(g):
        e = ends_at(g, at)
        return e.timestamp() if e else inf

    def count(g):
        c = _steam(g).get("count")
        return c if _is_num(c) else 0

    def price(g):
        a = _amount(g.get("price"))
        return a if a is not None else 1e9

    def score(g):
        s = _score(g)
        return s if s is not None else -1

    base = {
        "rank": lambda g: g.get("rank") or 1e9,
        "cut": lambda g: -_cut(g),
        "price": price,
        "ending": ends,
        "rating": lambda g: (-score(g), -count(g)),
    }[sort]
    return lambda g: (base(g), g["title"].casefold())


# --------------------------------------------------------------------------- argument checking
def _choice(name: str, v: Any, allowed: tuple) -> Any:
    if v not in allowed or isinstance(v, bool):
        raise QueryError(f"{name} must be one of: {', '.join(map(str, allowed))}.")
    return v


def _str_list(name: str, v: Any) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        raise QueryError(f"{name} must be a list of strings.")
    v = [x for x in v if x.strip()]
    return v or None


def _check(args: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(args) - set(_DEFAULTS))
    if unknown:
        raise QueryError(f"Unknown argument: {', '.join(unknown)}.")
    a = {**_DEFAULTS, **args}
    _choice("scope", a["scope"], SCOPES)
    _choice("sort", a["sort"], SORTS)
    if not _is_int(a["min_cut"]) or a["min_cut"] not in CUTS:
        raise QueryError(f"min_cut must be one of: {', '.join(map(str, CUTS))}.")
    if a["deck"] is not None:
        _choice("deck", a["deck"], DECKS)
    if a["max_price"] is not None and (not _is_num(a["max_price"]) or a["max_price"] < 0):
        raise QueryError("max_price must be a number of 0 or more.")
    if a["min_rating"] is not None and (not _is_num(a["min_rating"]) or not 0 <= a["min_rating"] <= 100):
        raise QueryError("min_rating must be a number from 0 to 100.")
    a["shops"] = _str_list("shops", a["shops"])
    a["tags"] = _str_list("tags", a["tags"])
    if a["at_target"] is not None and not isinstance(a["at_target"], bool):
        raise QueryError("at_target must be true or false.")
    if not isinstance(a["new_only"], bool):
        raise QueryError("new_only must be true or false.")
    if a["query"] is not None and not isinstance(a["query"], str):
        raise QueryError("query must be a string.")
    if not (a["query"] or "").strip():
        a["query"] = None
    if not _is_int(a["limit"]) or not 1 <= a["limit"] <= MAX_LIMIT:
        raise QueryError(f"limit must be a whole number from 1 to {MAX_LIMIT}.")
    if not _is_int(a["offset"]) or a["offset"] < 0:
        raise QueryError("offset must be a whole number of 0 or more.")
    return a


def _live(snap: Snapshot | None) -> Snapshot:
    if snap is None:
        raise QueryError(NO_LIST)
    if snap.legacy:
        raise QueryError(LEGACY)
    return snap


# --------------------------------------------------------------------------- rows
def _year(g: dict) -> int | None:
    r = g.get("released")
    head = r[:4] if isinstance(r, str) else ""
    return int(head) if len(head) == 4 and head.isdigit() else None


def _row(s: Snapshot, g: dict, at: datetime) -> dict:
    target = s.targets.get(g["appid"]) if s.targets_ok else None
    b = g.get("bundle") if isinstance(g.get("bundle"), dict) else None
    d = g.get("deck")
    low = _amount(g.get("low"))
    return {
        "appid": g["appid"], "title": g["title"], "rank": g.get("rank"),
        "price": _amount(g.get("price")), "regular": _amount(g.get("regular")), "cut": _cut(g),
        "shop": g.get("shop") or "", "all_time_low": low,
        "is_low": bool(g.get("isLow")), "new_low": _is_new_low(s, g),
        "on_sale": bool(g.get("onSale")), "in_bundle": bool(g.get("inBundle")),
        "bundle": {"title": b.get("title"), "price": _amount(b.get("price"))} if b else None,
        "target": target, "at_target": at_target(g, s.targets),
        "steam_rating": _score(g), "steam_reviews": _steam(g).get("count"),
        "metacritic": ((g.get("reviews") or {}).get("metacritic") or {}).get("score"),
        "deck": None if d is None else DECK_LABEL.get(d, DECK_LABEL[0]),
        "protondb": _proton(g), "tags": [t for t in (g.get("tags") or []) if isinstance(t, str)],
        "year": _year(g), "early_access": bool(g.get("earlyAccess")),
        "ends_in_days": _ends_in_days(g, at), "new": bool(g.get("new")),
    }


def _detail(s: Snapshot, g: dict, at: datetime) -> dict:
    row = _row(s, g, at)
    row["developer"] = g.get("developer") if isinstance(g.get("developer"), str) else None
    row["opencritic"] = ((g.get("reviews") or {}).get("opencritic") or {}).get("score")
    if row["bundle"]:
        row["bundle"]["expiry"] = _opt_str((g.get("bundle") or {}).get("expiry"))
    row["expiry"] = _opt_str(g.get("expiry"))
    p, t = row["price"], row["target"]
    row["target_gap"] = round(p - t, 2) if p is not None and t is not None else None
    return row


# --------------------------------------------------------------------------- find
def _unset(a: dict, name: str) -> bool:
    return {"query": a["query"] is None, "min_rating": not a["min_rating"], "tags": a["tags"] is None,
            "deck": a["deck"] is None, "shops": a["shops"] is None, "max_price": a["max_price"] is None,
            "min_cut": not a["min_cut"], "new_only": not a["new_only"], "at_target": a["at_target"] is None}[name]


_NEUTRAL = {"query": None, "min_rating": None, "tags": None, "deck": None, "shops": None, "max_price": None,
            "min_cut": 0, "new_only": False, "at_target": None}


def _select(s: Snapshot, a: dict, titles: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(base, listing): base is everything but the scope and the % filter, as on the page."""
    shops = {x.casefold() for x in a["shops"]} if a["shops"] else None
    tags = {tag_key(x) for x in a["tags"]} - {""} if a["tags"] else None
    base = []
    for g in s.games:
        if shops and (g.get("shop") or "").casefold() not in shops:
            continue
        if a["new_only"] and not g.get("new"):
            continue
        if titles is not None:
            if g["title"] not in titles:
                continue
        elif not query_ok(g, a["query"]):
            continue
        if not rating_ok(g, a["min_rating"]) or not deck_ok(g, a["deck"]):
            continue
        if tags and not tags & {tag_key(t) for t in (g.get("tags") or [])}:
            continue
        if a["max_price"] is not None:
            p = _price_for_max(g)
            if p is None or p > a["max_price"]:
                continue
        if a["at_target"] is not None and at_target(g, s.targets) != a["at_target"]:
            continue
        base.append(g)
    listing = [g for g in base if in_scope(g, a["scope"], s.targets) and cut_ok(g, a["scope"], a["min_cut"])]
    return base, listing


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def find(snap: Snapshot | None, *, relax: bool, **args: Any) -> dict:
    s = _live(snap)
    a = _check(args)
    at = now()
    base, listing = _select(s, a)
    strict_base, strict_cut = base, a["min_cut"]
    relaxed: list[str] = []
    notes: list[str] = []
    titles: set[str] | None = None
    if not listing and relax:
        work = dict(a)
        dropped: list[str] = []
        for name in RELAX_ORDER:
            if _unset(work, name):
                continue
            if name == "query":
                close = set(suggest(work["query"], (g["title"] for g in s.games)))
                if close:
                    fb, fl = _select(s, work, close)
                    if fl:
                        a, base, listing, titles = work, fb, fl, close
                        relaxed = ["query"]
                        notes.append(f'No exact match for query "{work["query"]}"; showing close title matches.')
                        break
            work = {**work, name: _NEUTRAL[name]}
            dropped.append(name)
            fb, fl = _select(s, work)
            if fl:
                a, base, listing = work, fb, fl
                relaxed = dropped
                notes.append("Nothing matched every filter; ignored: " + ", ".join(dropped) + ".")
                break
    matches = len(listing)
    scope = a["scope"]
    # What the % filter hid, worked out for the filters as asked, so the note survives a relaxed search.
    min_cut, dropped_cut = strict_cut, strict_cut and not a["min_cut"]
    if min_cut and scope not in CUT_SCOPES:
        notes.append(f"min_cut was ignored: it doesn't apply to scope {scope}.")
    elif min_cut and scope in ("sale", "target"):
        hid = [g for g in strict_base if in_scope(g, scope, s.targets) and not cut_ok(g, scope, min_cut)]
        tail = "(min_cut was ignored)" if dropped_cut else "by min_cut"
        if scope == "sale":
            n = sum(1 for g in hid if bundle_only(g))
            if n:
                notes.append(f"{_plural(n, 'bundle-only deal', 'bundle-only deals')} hidden {tail}.")
        elif hid:
            notes.append(f"{_plural(len(hid), 'game', 'games')} at or under target hidden {tail} (no discount that deep).")
    listing = sorted(listing, key=_sort_key(a["sort"], at))
    page = listing[a["offset"]:a["offset"] + a["limit"]]
    return {
        "scope": scope, "matches": matches, "offset": a["offset"],
        "games": [_row(s, g, at) for g in page],
        "truncated": a["offset"] + len(page) < matches,
        "exact": not relaxed, "relaxed": relaxed, "note": " ".join(notes) or None,
    }


# --------------------------------------------------------------------------- tool-facing functions
def overview_out(snap: Snapshot | None) -> dict:
    s = _live(snap)
    at = now()
    gen = _parse_time(s.generated_at)
    run = s.run
    scopes = {sc: sum(1 for g in s.games if in_scope(g, sc, s.targets)) for sc in SCOPES}
    return {
        "wishlist_games": s.wishlist_count if s.wishlist_count is not None else len(s.games),
        "tracked": len(s.games), "untracked": s.untracked, "currency": s.currency,
        "checked_at": s.generated_at,
        "age_hours": round((at - gen).total_seconds() / 3600, 1) if gen else None,
        "last_check": None if run is None else {
            "ok": run.get("consecutive_failures") == 0,
            "consecutive_failures": run.get("consecutive_failures"), "error_class": run.get("error_class")},
        "scopes": scopes,
        "new": sum(1 for g in s.games if g.get("new")),
        "no_price": sum(1 for g in s.games if _amount(g.get("price")) is None and not g.get("inBundle")),
        "targets": len(s.targets) if s.targets_ok else None,
        "targets_unavailable": not s.targets_ok,
    }


def find_out(snap: Snapshot | None, *, scope="sale", min_cut=0, max_price=None, shops=None, tags=None,
             deck=None, min_rating=None, at_target=None, new_only=False, query=None, sort="rank",
             limit=10, offset=0) -> dict:
    return find(snap, relax=True, scope=scope, min_cut=min_cut, max_price=max_price, shops=shops, tags=tags,
                deck=deck, min_rating=min_rating, at_target=at_target, new_only=new_only, query=query,
                sort=sort, limit=limit, offset=offset)


def game_out(snap: Snapshot | None, *, game) -> dict:
    s = _live(snap)
    at = now()
    if isinstance(game, bool) or not isinstance(game, (int, str)):
        raise QueryError("game must be an app id or a game name.")
    text = str(game).strip()
    if not text:
        raise QueryError("game must be an app id or a game name.")
    if text.isdigit():
        hit = next((g for g in s.games if g["appid"] == int(text)), None)
        if hit:
            return {"found": True, "game": _detail(s, hit, at)}
        return {"found": False, "query": text, "candidates": []}
    key = norm(text)
    exact = [g for g in s.games if norm(g["title"]) == key] if key else []
    if len(exact) == 1:
        return {"found": True, "game": _detail(s, exact[0], at)}
    cands: list[dict] = []
    seen: set[int] = set()
    for t in suggest(text, (g["title"] for g in s.games)):
        for g in s.games:
            if g["title"] == t and g["appid"] not in seen:
                seen.add(g["appid"])
                cands.append(g)
                break
    if len(cands) > 1:
        # One clearly closest title wins ("full prce 06"); several equally close ones stay ambiguous.
        r = sorted(((difflib.SequenceMatcher(None, key, norm(g["title"])).ratio(), i) for i, g in enumerate(cands)),
                   reverse=True)
        if r[0][0] >= 0.8 and r[0][0] - r[1][0] >= 0.05:
            cands = [cands[r[0][1]]]
    if len(cands) == 1:
        return {"found": True, "game": _detail(s, cands[0], at)}
    return {"found": False, "query": text, "candidates": [{"appid": g["appid"], "title": g["title"]} for g in cands]}


def targets_out(snap: Snapshot | None) -> dict:
    s = _live(snap)
    if not s.targets_ok:
        raise QueryError("Target prices couldn't be read right now.")
    by_id = {g["appid"]: g for g in s.games}
    rows = []
    for appid, t in s.targets.items():
        g = by_id.get(appid)
        p = _amount(g.get("price")) if g else None
        rows.append({
            "appid": appid, "title": g["title"] if g else None, "target": t, "price": p,
            "cut": _cut(g) if g else None, "at_target": bool(g) and at_target(g, s.targets),
            "gap": round(p - t, 2) if p is not None else None})
    rows.sort(key=lambda r: (not r["at_target"], r["gap"] is None, r["gap"] or 0, r["appid"]))
    return {"count": len(rows), "targets": rows}
