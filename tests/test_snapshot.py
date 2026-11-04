"""The snapshot's games[]: one entry per tracked wishlist game, with the flags the page filters on."""

import json
import os
import unittest

from tests import helpers, stub_services

G = {g["appid"]: g for g in helpers.sample_games()}


class BuildGames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = helpers.load_watcher()

    def build(self, rules=None, itad=None, targets=None, new=(), meta=None):
        cfg = helpers.make_cfg(rules=rules, itad=itad)
        out = self.wd.build_games(helpers.sample_games(), helpers.sample_prices(), helpers.sample_bundles(),
                                  cfg, targets or {}, meta or {}, set(new))
        return {g["appid"]: g for g in out}

    def test_every_tracked_game_appears_once_in_rank_order(self):
        cfg = helpers.make_cfg()
        out = self.wd.build_games(helpers.sample_games(), helpers.sample_prices(), helpers.sample_bundles(), cfg, {}, {}, set())
        self.assertEqual([g["appid"] for g in out], [1000 + n for n in range(1, 19)])

    def test_discounted_and_all_time_low(self):
        g = self.build()[1001]
        self.assertEqual((g["price"]["amount"], g["cut"], g["shop"]), (10.0, 50, "Steam"))
        self.assertTrue(g["onSale"] and g["isLow"])
        self.assertFalse(g["inBundle"])
        self.assertEqual(g["low"]["amount"], 10.0)

    def test_discounted_but_not_a_low_until_tolerance_allows(self):
        self.assertFalse(self.build()[1002]["isLow"])
        g = self.build(rules={"low_tolerance_pct": "10"})[1002]
        self.assertTrue(g["isLow"] and g["onSale"])

    def test_low_uses_the_same_minimum_discount_as_alerts(self):
        self.assertTrue(self.build()[1004]["isLow"])
        g = self.build(rules={"low_min_discount_pct": "40"})[1004]
        self.assertFalse(g["isLow"])
        self.assertTrue(g["onSale"])  # still on sale, just not a "low" worth flagging

    def test_not_on_sale_still_has_its_regular_price(self):
        for appid in (1005, 1018):
            g = self.build()[appid]
            self.assertFalse(g["onSale"] or g["isLow"] or g["inBundle"])
            self.assertEqual(g["cut"], 0)
            self.assertIsNotNone(g["price"])
        self.assertEqual(self.build()[1018]["price"]["amount"], 15.0)

    def test_bundle_only_game_has_no_price_but_a_tier(self):
        g = self.build()[1013]
        self.assertIsNone(g["price"])
        self.assertEqual((g["cut"], g["shop"], g["url"]), (0, "", None))
        self.assertTrue(g["inBundle"] and g["onSale"])
        self.assertEqual(g["bundle"]["title"], "Space Bundle")
        self.assertEqual(g["bundle"]["price"]["amount"], 9.0)  # cheapest tier that has it

    def test_discounted_and_bundled(self):
        g = self.build()[1015]
        self.assertEqual(g["price"]["amount"], 6.0)
        self.assertTrue(g["onSale"] and g["isLow"] and g["inBundle"])
        self.assertEqual(g["bundle"]["price"]["amount"], 15.0)
        self.assertEqual(g["bundle"]["url"], "https://example.com/bundle/b1")

    def test_bundle_at_full_price_counts_as_on_sale(self):
        # A game with no store discount that's in a live bundle is "on sale" through the bundle.
        prices = helpers.sample_prices()
        cfg = helpers.make_cfg()
        bundles = [helpers.bundle("bx", "Full Price Bundle", [helpers.tier(5.0, "g18")])]
        g = {x["appid"]: x for x in self.wd.build_games(helpers.sample_games(), prices, bundles, cfg, {}, {}, set())}[1018]
        self.assertTrue(g["onSale"] and g["inBundle"])
        self.assertEqual(g["cut"], 0)

    def test_expired_bundle_is_ignored(self):
        g = self.build()[1014]
        self.assertFalse(g["inBundle"] or g["onSale"])
        self.assertIsNone(g["bundle"])

    def test_bundle_without_a_price(self):
        g = self.build()[1011]
        self.assertTrue(g["inBundle"] and g["onSale"])
        self.assertIsNone(g["price"])           # ITAD lists no store price for it
        self.assertIsNone(g["bundle"]["price"])  # and the bundle doesn't say what a tier costs

    def test_no_itad_price_at_all(self):
        g = self.build()[1012]                   # absent from the prices response
        self.assertIsNone(g["price"])
        self.assertIsNone(g["low"])
        self.assertFalse(g["onSale"] or g["isLow"] or g["inBundle"] or g["atTarget"])
        self.assertIsNone(g["expiry"])

    def test_bundles_off_means_no_bundle_flags(self):
        out = self.build(rules={"bundles": "false"})
        self.assertFalse(any(g["inBundle"] for g in out.values()))
        self.assertFalse(out[1013]["onSale"])

    def test_shop_filters_pick_the_best_allowed_deal(self):
        self.assertEqual(self.build()[1010]["shop"], "Fanatical")
        self.assertEqual(self.build(itad={"only_shops": "steam"})[1010]["shop"], "Steam")
        self.assertEqual(self.build(itad={"exclude_shops": "Fanatical"})[1010]["price"]["amount"], 20.0)

    def test_voucher_only_deal_is_not_a_price(self):
        g = self.build()[1009]
        self.assertIsNone(g["price"])
        self.assertFalse(g["onSale"])

    def test_targets(self):
        out = self.build(targets={1006: 10.0, 1007: 20.0, 1005: 25.0, 1012: 1.0})
        self.assertEqual((out[1006]["target"], out[1006]["atTarget"]), (10.0, True))
        self.assertEqual((out[1007]["target"], out[1007]["atTarget"]), (20.0, False))   # $25 > $20
        self.assertEqual((out[1005]["target"], out[1005]["atTarget"]), (25.0, True))    # full price, under target
        self.assertEqual((out[1012]["target"], out[1012]["atTarget"]), (1.0, False))    # no price to compare
        self.assertIsNone(out[1001]["target"])

    def test_target_equal_to_price_counts(self):
        self.assertTrue(self.build(targets={1001: 10.0})[1001]["atTarget"])
        self.assertFalse(self.build(targets={1001: 9.99})[1001]["atTarget"])

    def test_new_flag_and_metadata(self):
        meta = {"g1": {"r": {"steam": {"score": 95}}, "tags": ["RPG"], "released": "2020-01-01", "ea": True,
                       "dev": "Studio", "deck": 3, "proton": {"tier": "gold"}}}
        g = self.build(new={1001}, meta=meta)
        self.assertTrue(g[1001]["new"])
        self.assertFalse(g[1002]["new"])
        a = g[1001]
        self.assertEqual((a["reviews"]["steam"]["score"], a["tags"], a["released"], a["earlyAccess"], a["developer"],
                          a["deck"], a["protondb"]["tier"]), (95, ["RPG"], "2020-01-01", True, "Studio", 3, "gold"))
        b = g[1002]  # nothing cached yet: empty, not missing
        self.assertEqual((b["reviews"], b["tags"], b["deck"], b["protondb"], b["earlyAccess"]), ({}, [], None, {}, False))


class Currency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = helpers.load_watcher()

    def test_first_price_wins(self):
        games = [{"price": None}, {"price": {"amount": 1, "currency": "EUR"}}, {"price": {"amount": 1, "currency": "USD"}}]
        self.assertEqual(self.wd.snapshot_currency(games, helpers.make_cfg()), "EUR")

    def test_falls_back_to_the_configured_country(self):
        self.assertEqual(self.wd.snapshot_currency([], helpers.make_cfg(itad={"country": "GB"})), "GBP")
        self.assertEqual(self.wd.snapshot_currency([{"price": None}], helpers.make_cfg()), "USD")
        self.assertIsNone(self.wd.snapshot_currency([], helpers.make_cfg(itad={"country": "ZZ"})))


class WholeRun(unittest.TestCase):
    """The watcher end to end against the stub services."""

    def run_it(self, args, **kw):
        stub = stub_services.Stub()
        self.addCleanup(stub.close)
        work = stub_services.tempdir()
        self.addCleanup(work.cleanup)
        res = stub_services.run_watcher(stub, work.name, args, **kw)
        self.assertEqual(res[0], 0, res[2])
        return res

    def test_games_cover_tracked_wishlist_only(self):
        _, _, _, snap, _ = self.run_it(["--snapshot-only"])
        self.assertEqual(snap["wishlistCount"], 20)
        self.assertEqual(snap["untracked"], 2)
        self.assertEqual(sorted(g["appid"] for g in snap["games"]), sorted(G))
        self.assertEqual(snap["currency"], "USD")
        self.assertTrue(snap["deals"])  # the alert list is still there
        self.assertTrue(all(isinstance(g["title"], str) and g["title"] for g in snap["games"]))
        by = {g["appid"]: g for g in snap["games"]}
        self.assertEqual(by[1013]["bundle"]["title"], "Space Bundle")
        self.assertEqual(by[1018]["price"]["amount"], 15.0)
        self.assertTrue(by[1001]["new"])     # nothing has been notified yet

    def test_games_without_deals_get_details_gradually(self):
        env = {"WD_META_CAP": "5"}
        with stub_services.tempdir() as work:
            stub = stub_services.Stub()
            self.addCleanup(stub.close)
            _, _, _, snap, _ = stub_services.run_watcher(stub, work, ["--snapshot-only"], extra_env=env)
            have = lambda s: {g["appid"] for g in s["games"] if g["deck"] is not None}
            deal_games = {d["appid"] for d in snap["deals"]}
            self.assertEqual(len(deal_games), 9)
            self.assertEqual(have(snap), deal_games | {1002, 1003, 1005, 1007, 1008})   # best-ranked 5 of the rest
            _, _, _, snap2, _ = stub_services.run_watcher(stub, work, ["--snapshot-only"], extra_env=env)
            self.assertEqual(have(snap2), set(G))                                         # the last 4 on the next run

    def test_old_pages_can_still_read_deals(self):
        _, _, _, snap, _ = self.run_it(["--snapshot-only"])
        for d in snap["deals"]:
            for key in ("key", "kind", "why", "appid", "title", "rank", "shop", "price", "regular", "cut", "low",
                        "isLow", "newLow", "expiry", "new", "steamUrl", "reviews", "tags", "deck", "protondb"):
                self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
