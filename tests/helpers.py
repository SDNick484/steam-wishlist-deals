"""Shared test helpers: load the watcher module and build canned ITAD data."""

import configparser
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Point WD_MODULE at another copy of wishlist_deals.py to run the same tests against it.
MODULE_PATH = os.environ.get("WD_MODULE") or os.path.join(ROOT, "wishlist_deals.py")


def load_watcher():
    spec = importlib.util.spec_from_file_location("wishlist_deals_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def money(amount, currency="USD"):
    return {"amount": amount, "amountInt": int(round(amount * 100)), "currency": currency}


def deal(shop, price, regular, url=None, voucher=None, expiry="2099-01-01T00:00:00Z"):
    cut = round((1 - price / regular) * 100) if regular else 0
    d = {"shop": {"id": 1, "name": shop}, "price": money(price), "regular": money(regular),
         "cut": cut, "url": url or f"https://example.com/{shop.lower().replace(' ', '-')}", "expiry": expiry}
    if voucher:
        d["voucher"] = voucher
    return d


def tier(price, *gids):
    return {"price": money(price) if price is not None else None, "games": [{"id": g} for g in gids]}


def bundle(bid, title, tiers, expiry="2099-01-01T00:00:00Z"):
    return {"id": bid, "title": title, "page": {"name": "Humble Bundle"}, "url": f"https://example.com/bundle/{bid}",
            "expiry": expiry, "tiers": tiers}


def make_cfg(rules=None, itad=None, targets=None):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"steam": {}, "itad": itad or {}, "rules": rules or {}})
    if targets is not None:
        cfg.add_section("targets")
        for k, v in targets.items():
            cfg["targets"][k] = v
    return cfg


def sample_games():
    """Wishlist games (the shape main() builds): one per case the rules care about."""
    names = [
        (1, "At Low"), (2, "Near Low"), (3, "Above Low"), (4, "Small Cut Low"), (5, "Regular Price Low"),
        (6, "Target Hit"), (7, "Target Miss"), (8, "Deep Cut"), (9, "Voucher Only"), (10, "Shop Filter"),
        (11, "No Deals"), (12, "No Price Entry"), (13, "Bundle Only"), (14, "Expired Bundle"),
        (15, "Deal And Bundle"), (16, "New Low"), (17, "Bad Target"), (18, "Not On Sale"),
    ]
    return [{"appid": 1000 + n, "gid": f"g{n}", "title": t, "rank": n} for n, t in names]


def sample_prices():
    p = {}

    def add(n, deals, low):
        p[f"g{n}"] = {"id": f"g{n}", "deals": deals, "historyLow": {"all": money(low) if low is not None else None}}

    add(1, [deal("Steam", 10.0, 20.0)], 10.0)                              # exactly at the all-time low
    add(2, [deal("Steam", 10.5, 20.0)], 10.0)                              # 5% above the low
    add(3, [deal("Steam", 14.0, 20.0)], 10.0)                              # well above the low
    add(4, [deal("Steam", 18.0, 20.0)], 18.0)                              # at the low, but only 10% off
    add(5, [deal("Steam", 20.0, 20.0)], 20.0)                              # regular price is the low; no sale
    add(6, [deal("Steam", 9.0, 30.0), deal("GOG", 12.0, 30.0)], 9.0)       # target hit, also a low
    add(7, [deal("Steam", 25.0, 30.0)], 5.0)                               # target not reached
    add(8, [deal("Steam", 12.0, 40.0)], 6.0)                               # 70% off, nowhere near the low
    add(9, [deal("Steam", 5.0, 20.0, voucher="CODE")], 5.0)                # only deal needs a voucher
    add(10, [deal("Steam", 20.0, 40.0), deal("Fanatical", 8.0, 40.0)], 8.0)  # cheapest is at a filtered shop
    add(11, [], 10.0)                                                      # ITAD lists no store price
    # g12: absent from the prices response entirely
    add(14, [deal("Steam", 30.0, 30.0)], 12.0)
    add(15, [deal("Steam", 6.0, 20.0)], 6.0)                               # a low, and also in a bundle
    add(16, [deal("Steam", 4.0, 20.0)], 6.0)                               # below the previous low
    add(17, [deal("Steam", 7.0, 10.0)], 7.0)
    add(18, [deal("Steam", 15.0, 15.0)], 7.5)                              # not discounted anywhere
    return p


def sample_bundles():
    return [
        bundle("b1", "Space Bundle", [tier(9.0, "g13"), tier(15.0, "g13", "g15")]),
        bundle("b2", "Old Bundle", [tier(3.0, "g14")], expiry="2000-01-01T00:00:00Z"),
        bundle("b3", "No Price Bundle", [tier(None, "g11")]),
        bundle("b4", "Not Ours", [tier(1.0, "g999")]),
    ]


# Rule/config variants the golden file covers.
VARIANTS = {
    "defaults": dict(),
    "tolerance10": dict(rules={"low_tolerance_pct": "10"}),
    "min_cut40": dict(rules={"low_min_discount_pct": "40"}),
    "big50": dict(rules={"big_discount_pct": "50"}),
    "no_bundles": dict(rules={"bundles": "false"}),
    "only_shops": dict(itad={"only_shops": "Steam, GOG"}),
    "exclude_shops": dict(itad={"exclude_shops": "fanatical"}),
    "targets": dict(targets={"1006": "10.00", "1007": "20", "1005": "25", "1017": "not-a-number", "oops": "5"}),
    "combo": dict(rules={"low_tolerance_pct": "10", "low_min_discount_pct": "40", "big_discount_pct": "50"},
                  targets={"1006": "10.00"}),
}
