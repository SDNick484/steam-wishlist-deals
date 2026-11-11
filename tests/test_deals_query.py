"""deals_query.py: the read layer under the Ask search, checked against the watcher-built fixture.

Each expectation is written here from the page's rule (deals.html), not by calling the code under test.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deals_query as dq  # noqa: E402
from tests.browser import _support  # noqa: E402

FX = _support.build_fixture()
NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


def int_targets(t):
    return {int(k): v for k, v in t.items()}


def snap(targets=FX.targets, run=None, **doc_over):
    doc = copy.deepcopy(FX.snapshot)
    doc.update(doc_over)
    return dq.build_snapshot(doc, int_targets(targets), run)


def all_rows(s, **kw):
    """Every row of a find, paged 25 at a time."""
    rows, offset = [], 0
    while True:
        out = dq.find(s, relax=False, limit=25, offset=offset, **kw)
        rows += out["games"]
        if not out["truncated"]:
            return rows
        offset += 25


def extra_game(title, appid, **over):
    g = {"appid": appid, "title": title, "rank": 99, "steamUrl": "https://store.steampowered.com/app/%d/" % appid,
         "price": None, "regular": None, "cut": 0, "shop": "", "url": None, "expiry": None, "low": None,
         "onSale": False, "isLow": False, "inBundle": False, "bundle": None, "target": None, "atTarget": False,
         "new": False, "reviews": {}, "tags": [], "released": "", "earlyAccess": False, "developer": "X",
         "deck": None, "protondb": {}}
    g.update(over)
    return g


def money(x):
    return {"amount": x, "amountInt": int(round(x * 100)), "currency": "USD"}


# the page's rules, written out again
def p_at_target(g, t=FX.targets):
    x = t.get(str(g["appid"]))
    return x is not None and g["price"] is not None and g["price"]["amount"] <= x


def p_bundle_only(g):
    return g["inBundle"] and not g["cut"] > 0


def p_score(g):
    s = (g.get("reviews") or {}).get("steam")
    return s["score"] if s else None


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(dq, "now", lambda: NOW)
        p.start()
        self.addCleanup(p.stop)


class Counts(Base):
    def test_scope_counts_match_fixture(self):
        for targets in (FX.targets, {}):
            s = snap(targets)
            self.assertEqual(dq.overview_out(s)["scopes"], FX.counts(targets, 0))
            for m in dq.CUTS:
                expect = FX.counts(targets, m)
                for scope in dq.SCOPES:
                    with self.subTest(targets=bool(targets), min_cut=m, scope=scope):
                        self.assertEqual(dq.find(s, relax=False, scope=scope, min_cut=m)["matches"], expect[scope])

    def test_bundle_only_note(self):
        s = snap()
        n = dq.find_out(s, scope="sale", min_cut=50)["note"]
        self.assertIn(f"{FX.bundle_only_count()} bundle-only", n)
        out = dq.find_out(s, scope="all", min_cut=50)
        self.assertIn("min_cut", out["note"])
        self.assertIn("ignored", out["note"])
        self.assertEqual(out["matches"], len(FX.games))

    def test_target_hidden_by_min_cut(self):
        n = FX.hidden_at_target(50)
        self.assertGreater(n, 0)
        note = dq.find_out(snap(), scope="target", min_cut=50)["note"]
        self.assertIn(f"{n} games", note)


class Filters(Base):
    def test_filters_each_alone(self):
        s = snap()

        def price_for(g):
            if p_bundle_only(g):
                return ((g.get("bundle") or {}).get("price") or {}).get("amount")
            return g["price"]["amount"] if g["price"] else None

        cases = [
            ({"shops": ["GOG"]}, lambda g: g["shop"] == "GOG"),
            ({"tags": ["roguelike"]}, lambda g: "Roguelike" in g["tags"]),
            ({"deck": "verified"}, lambda g: g["deck"] == 3),
            ({"deck": "works"}, lambda g: g["deck"] in (3, 2) or
             (g["protondb"] or {}).get("tier") in ("native", "platinum", "gold")),
            ({"min_rating": 85}, lambda g: (p_score(g) if p_score(g) is not None else -1) >= 85),
            ({"max_price": 10}, lambda g: price_for(g) is not None and price_for(g) <= 10),
            ({"at_target": True}, p_at_target),
            ({"at_target": False}, lambda g: not p_at_target(g)),
            ({"new_only": True}, lambda g: g["new"]),
            ({"query": "full price"}, lambda g: "full price" in g["title"].lower()),
            ({"query": "metroid"}, lambda g: any("metroid" in t.lower() for t in g["tags"])),
        ]
        for kw, rule in cases:
            with self.subTest(**kw):
                expect = {g["title"] for g in FX.games if rule(g)}
                self.assertTrue(expect, "the fixture has nothing for this filter")
                rows = all_rows(s, scope="all", **kw)
                self.assertEqual({r["title"] for r in rows}, expect)
                self.assertEqual(len(rows), len(expect))

    def test_tag_key_is_forgiving(self):
        s = snap(games=FX.games + [extra_game("Dark One", 1, tags=["Souls-like"])])
        rows = all_rows(s, scope="all", tags=["souls like"])
        self.assertIn("Dark One", [r["title"] for r in rows])
        self.assertEqual(len(rows), 1 + sum(1 for g in FX.games if "Souls-like" in g["tags"]))

    def test_sorts(self):
        s = snap()
        INF = float("inf")

        def ends(g):
            e = g["bundle"]["expiry"] if p_bundle_only(g) else (g["expiry"] if g["cut"] > 0 else None)
            return datetime.fromisoformat(e.replace("Z", "+00:00")).timestamp() if e else INF

        def count(g):
            return ((g.get("reviews") or {}).get("steam") or {}).get("count") or 0

        keys = {
            "rank": lambda g: g["rank"] or 1e9,
            "cut": lambda g: -g["cut"],
            "price": lambda g: g["price"]["amount"] if g["price"] else 1e9,
            "ending": ends,
            "rating": lambda g: (-(p_score(g) if p_score(g) is not None else -1), -count(g)),
        }
        for sort in dq.SORTS:
            with self.subTest(sort=sort):
                expect = sorted(FX.games, key=lambda g: (keys[sort](g), g["title"].casefold()))
                got = dq.find_out(s, scope="all", sort=sort, limit=25)
                self.assertEqual([r["title"] for r in got["games"]], [g["title"] for g in expect[:25]])

    def test_ending_sort_uses_expiry(self):
        s = snap(games=[
            extra_game("A late", 1, cut=10, price=money(5), onSale=True, expiry="2026-12-01T00:00:00Z"),
            extra_game("B soon", 2, cut=10, price=money(5), onSale=True, expiry="2026-11-12T00:00:00Z"),
            extra_game("C none", 3, cut=10, price=money(5), onSale=True),
            extra_game("D bundle", 4, inBundle=True, onSale=True,
                       bundle={"title": "B", "price": money(3), "expiry": "2026-11-20T00:00:00Z"}),
        ])
        got = [r["title"] for r in dq.find_out(s, scope="all", sort="ending")["games"]]
        self.assertEqual(got, ["B soon", "D bundle", "A late", "C none"])


class Relaxation(Base):
    def test_relaxation_order(self):
        s = snap()
        out = dq.find_out(s, scope="sale", tags=["Nonexistent"], deck="verified")
        self.assertIs(out["exact"], False)
        self.assertEqual(out["relaxed"], ["tags"])
        self.assertGreater(out["matches"], 0)
        self.assertTrue(out["note"])

        out = dq.find_out(s, scope="sale", query="zzzzqqqq", min_rating=100, tags=["Nonexistent"], deck="verified",
                          shops=["Nope"], max_price=0, min_cut=90, new_only=True, at_target=True)
        self.assertIs(out["exact"], False)
        self.assertEqual(out["relaxed"], list(dq.RELAX_ORDER[:len(out["relaxed"])]))
        self.assertGreaterEqual(len(out["relaxed"]), 6)
        self.assertNotIn("scope", out["relaxed"])
        self.assertGreater(out["matches"], 0)

    def test_relaxed_never_names_a_filter_with_no_effect(self):
        doc = copy.deepcopy(FX.snapshot)
        for g in doc["games"]:
            g["new"] = False
        s = dq.build_snapshot(doc, int_targets(FX.targets), None)
        out = dq.find_out(s, scope="all", min_cut=50, new_only=True)
        self.assertEqual(out["relaxed"], ["new_only"])
        self.assertIn("ignored: new_only.", out["note"])

    def test_exact_when_something_matches(self):
        out = dq.find_out(snap(), scope="sale", min_cut=50)
        self.assertIs(out["exact"], True)
        self.assertEqual(out["relaxed"], [])

    def test_query_tries_fuzzy_title_first(self):
        out = dq.find_out(snap(), scope="all", query="full prce 06")
        self.assertEqual(out["relaxed"], ["query"])
        self.assertIn("Full price 06", [r["title"] for r in out["games"]])

    def test_strict_has_no_relaxation(self):
        out = dq.find(snap(), relax=False, scope="sale", tags=["Nonexistent"])
        self.assertEqual((out["matches"], out["exact"], out["relaxed"]), (0, True, []))

    def test_empty_scope_stays_empty(self):
        s = snap(games=[extra_game("Only", 1)])
        out = dq.find_out(s, scope="sale", tags=["x"])
        self.assertEqual((out["matches"], out["exact"], out["relaxed"]), (0, True, []))


class Arguments(Base):
    def test_paging_and_limits(self):
        s = snap()
        one = dq.find(s, relax=False, scope="all", limit=1)
        self.assertEqual((len(one["games"]), one["truncated"], one["offset"], one["matches"]), (1, True, 0, 60))
        big = dq.find(s, relax=False, scope="all", limit=25)
        self.assertEqual(len(big["games"]), 25)
        nxt = dq.find(s, relax=False, scope="all", limit=25, offset=25)
        self.assertEqual(len(nxt["games"]), 25)
        self.assertEqual(set(r["appid"] for r in nxt["games"]) & set(r["appid"] for r in big["games"]), set())
        last = dq.find(s, relax=False, scope="all", limit=25, offset=50)
        self.assertEqual((len(last["games"]), last["truncated"]), (10, False))
        self.assertEqual(dq.find(s, relax=False, scope="all", offset=500)["games"], [])
        for bad in (0, 26):
            with self.assertRaises(dq.QueryError):
                dq.find_out(s, limit=bad)

    def test_out_of_range_arguments(self):
        s = snap()
        cases = [("min_cut", 40, "10"), ("scope", "cheap", "sale"), ("deck", "yes", "verified"),
                 ("sort", "best", "rating"), ("min_rating", 140, None), ("max_price", -1, None),
                 ("shops", "GOG", None), ("tags", "x", None), ("limit", True, None), ("offset", -1, None),
                 ("at_target", "yes", None), ("new_only", 1, None), ("query", 5, None)]
        for name, value, allowed in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaises(dq.QueryError) as cm:
                    dq.find_out(s, **{name: value})
                self.assertIn(name, str(cm.exception))
                if allowed:
                    self.assertIn(allowed, str(cm.exception))
        with self.assertRaises(dq.QueryError):
            dq.find(s, relax=False, bogus=1)

    def test_no_price_sorts_last_and_never_under_max(self):
        s = snap(games=FX.games + [
            extra_game("Zz no price", 5001, onSale=True),
            extra_game("Zz bundle no price", 5002, onSale=True, inBundle=True,
                       bundle={"title": "B", "price": None, "expiry": None}),
        ])
        rows = all_rows(s, scope="all", sort="price")
        self.assertEqual([r["title"] for r in rows][-2:], ["Zz bundle no price", "Zz no price"])
        under = {r["title"] for r in all_rows(s, scope="all", max_price=1000)}
        self.assertNotIn("Zz no price", under)
        self.assertNotIn("Zz bundle no price", under)

    def test_past_expiry_has_no_ends_in(self):
        s = snap(games=[
            extra_game("Past", 1, cut=10, price=money(5), onSale=True, expiry="2026-11-09T00:00:00Z"),
            extra_game("Soon", 2, cut=10, price=money(5), onSale=True, expiry="2026-11-13T06:00:00Z"),
            extra_game("Never", 3, cut=10, price=money(5), onSale=True),
        ])
        got = {r["title"]: r["ends_in_days"] for r in dq.find_out(s, scope="all")["games"]}
        self.assertEqual(got, {"Past": None, "Soon": 2, "Never": None})
        self.assertEqual(NOW + timedelta(days=2.5), datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc))


class Tools(Base):
    def test_row_shape(self):
        row = dq.find_out(snap(), scope="all", query="Sale 01")["games"][0]
        self.assertEqual(set(row), {
            "appid", "title", "rank", "price", "regular", "cut", "shop", "all_time_low", "is_low", "new_low",
            "on_sale", "in_bundle", "bundle", "target", "at_target", "steam_rating", "steam_reviews",
            "metacritic", "deck", "protondb", "tags", "year", "early_access", "ends_in_days", "new"})
        g = FX.game("Sale 01")
        self.assertEqual((row["price"], row["regular"], row["cut"], row["target"], row["at_target"]),
                         (36.0, 40.0, 10, 41.0, True))
        self.assertEqual((row["deck"], row["year"], row["steam_rating"], row["steam_reviews"]), ("Verified", 2012, 97, 48000))
        self.assertIsInstance(row["price"], float)
        self.assertEqual(g["deck"], 3)
        b = dq.find_out(snap(), scope="bundle", query="Bundle only 01")["games"][0]
        self.assertEqual(b["bundle"], {"title": "Fixture Bundle One", "price": 9.0})
        self.assertIsNone(b["price"])
        nodeck = dq.find_out(snap(), scope="all", query="No price 01")["games"][0]
        self.assertEqual((nodeck["deck"], nodeck["protondb"]), (None, "borked"))

    def test_get_game_non_ascii_digits_are_a_name(self):
        s = snap()
        for text in ("\u00b2", "\u2460", "\u0663"):
            with self.subTest(text=text):
                out = dq.game_out(s, game=text)
                self.assertIs(out["found"], False)

    def test_get_game(self):
        s = snap()
        appid = FX.game("Sale 01")["appid"]
        for ref in (appid, str(appid), "Sale 01", "sale 01", "full prce 06"):
            with self.subTest(ref=ref):
                out = dq.game_out(s, game=ref)
                self.assertIs(out["found"], True)
        out = dq.game_out(s, game=appid)
        self.assertEqual(out["game"]["title"], "Sale 01")
        self.assertEqual(out["game"]["developer"], "Studio A")
        self.assertEqual(out["game"]["opencritic"], 65)
        self.assertEqual(out["game"]["target_gap"], -5.0)
        self.assertEqual(out["game"]["expiry"], "2099-01-01T00:00:00Z")
        self.assertEqual(dq.game_out(s, game="full prce 06")["game"]["title"], "Full price 06")
        b = dq.game_out(s, game="Bundle only 01")["game"]
        self.assertEqual(b["bundle"]["expiry"], "2099-01-01T00:00:00Z")
        amb = dq.game_out(s, game="Full price")
        self.assertIs(amb["found"], False)
        self.assertGreaterEqual(len(amb["candidates"]), 2)
        for c in amb["candidates"]:
            self.assertEqual(set(c), {"appid", "title"})
        miss = dq.game_out(s, game=123)
        self.assertIs(miss["found"], False)
        self.assertEqual(miss["candidates"], [])
        self.assertIs(dq.game_out(s, game="qqqqzzzz")["found"], False)
        with self.assertRaises(dq.QueryError):
            dq.game_out(s, game="  ")

    def test_targets_out(self):
        s = snap(targets={**FX.targets, "999999": 12.5})
        rows = dq.targets_out(s)["targets"]
        self.assertEqual(len(rows), len(FX.targets) + 1)
        flags = [r["at_target"] for r in rows]
        self.assertEqual(flags, sorted(flags, reverse=True))
        under = [r["gap"] for r in rows if r["at_target"]]
        self.assertEqual(under, sorted(under))
        over = [r["gap"] for r in rows if not r["at_target"] and r["gap"] is not None]
        self.assertEqual(over, sorted(over))
        gone = next(r for r in rows if r["appid"] == 999999)
        self.assertEqual((gone["title"], gone["price"], gone["target"]), (None, None, 12.5))
        row = next(r for r in rows if r["title"] == "Sale 04")
        self.assertFalse(row["at_target"])
        self.assertGreater(row["gap"], 0)

    def test_unreadable_targets_degrade(self):
        s = dq.build_snapshot(FX.snapshot, None, None)
        self.assertIs(s.targets_ok, False)
        o = dq.overview_out(s)
        self.assertIsNone(o["targets"])
        self.assertIs(o["targets_unavailable"], True)
        with self.assertRaises(dq.QueryError) as cm:
            dq.targets_out(s)
        self.assertIn("couldn't be read", str(cm.exception))
        own = [g for g in FX.games if g.get("target") is not None and g["price"] is not None
               and g["price"]["amount"] <= g["target"]]
        self.assertTrue(own)
        self.assertEqual(o["scopes"]["target"], len(own))
        self.assertEqual(dq.find(s, relax=False, scope="target")["matches"], len(own))
        row = dq.find_out(s, scope="all", query="Sale 01")["games"][0]
        self.assertEqual((row["target"], row["at_target"]), (FX.game("Sale 01")["target"], True))
        self.assertEqual(dq.game_out(s, game="Sale 01")["game"]["target_gap"], -5.0)

    def test_legacy_and_missing(self):
        self.assertIsNone(dq.build_snapshot(None, {}, None))
        legacy = {k: v for k, v in FX.snapshot.items() if k != "games"}
        s = dq.build_snapshot(legacy, {}, None)
        self.assertTrue(s.legacy)
        calls = (lambda: dq.overview_out(s), lambda: dq.find_out(s), lambda: dq.find(s, relax=False),
                 lambda: dq.game_out(s, game="x"), lambda: dq.targets_out(s))
        for call in calls:
            with self.assertRaises(dq.QueryError) as cm:
                call()
            self.assertEqual(str(cm.exception), dq.LEGACY)

    def test_build_snapshot_skips_bad_games(self):
        doc = {"games": [extra_game("Ok", 1), {"appid": True, "title": "Bool"}, {"appid": "2", "title": "Str"},
                         {"appid": 3, "title": 7}, "junk", {"appid": 4}]}
        s = dq.build_snapshot(doc, {}, None)
        self.assertEqual([g["title"] for g in s.games], ["Ok"])
        self.assertFalse(s.legacy)

    def test_newlow_ids(self):
        s = snap()
        self.assertEqual(s.newlow_ids, frozenset(d["appid"] for d in FX.snapshot["deals"] if d.get("newLow")))
        self.assertTrue(s.newlow_ids)

    def test_no_urls_or_extra_keys(self):
        s = snap(steamid="7656119XXXXXXXXXX", profile="someone")
        outs = [dq.overview_out(s), dq.find_out(s, scope="all", limit=25), dq.find_out(s, scope="bundle", limit=25),
                dq.game_out(s, game="Bundle only 01"), dq.game_out(s, game="Sale 01"), dq.targets_out(s)]

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    yield from walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    yield from walk(x)
            else:
                yield v

        for out in outs:
            text = json.dumps(out)
            self.assertNotIn("7656119XXXXXXXXXX", text)
            self.assertNotIn("someone", text)
            for v in walk(out):
                self.assertFalse(isinstance(v, str) and v.startswith("http"), v)
        self.assertFalse(hasattr(s, "steamid"))

    def test_overview_fields(self):
        s = snap(run={"consecutive_failures": 0, "error_class": None, "last_ok": "x"})
        s = dq.build_snapshot({**FX.snapshot, "generatedAt": "2026-11-10T12:00:00+00:00"}, int_targets(FX.targets),
                              {"consecutive_failures": 0, "error_class": None, "last_ok": "x"})
        o = dq.overview_out(s)
        self.assertEqual(list(sorted(o)), sorted([
            "wishlist_games", "tracked", "untracked", "currency", "checked_at", "age_hours", "last_check",
            "scopes", "new", "no_price", "targets", "targets_unavailable"]))
        self.assertEqual((o["wishlist_games"], o["tracked"], o["untracked"], o["currency"]), (62, 60, 2, "USD"))
        self.assertEqual(o["age_hours"], 6.0)
        self.assertEqual(o["checked_at"], "2026-11-10T12:00:00+00:00")
        self.assertEqual(o["last_check"], {"ok": True, "consecutive_failures": 0, "error_class": None})
        self.assertEqual(o["new"], sum(1 for g in FX.games if g["new"]))
        self.assertEqual(o["no_price"], sum(1 for g in FX.games if g["price"] is None and not g["inBundle"]))
        self.assertEqual(o["targets"], len(FX.targets))
        self.assertIs(o["targets_unavailable"], False)
        bad = dq.build_snapshot(FX.snapshot, {}, {"consecutive_failures": 3, "error_class": "itad"})
        self.assertEqual(dq.overview_out(bad)["last_check"], {"ok": False, "consecutive_failures": 3, "error_class": "itad"})
        self.assertIsNone(dq.overview_out(snap())["last_check"])


if __name__ == "__main__":
    unittest.main()
