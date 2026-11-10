"""Page smoke test: deals.html in headless Chromium against a real deals_server.py.

Run from the repo root:  REQUIRE_BROWSER=1 python -m unittest -v tests.browser.test_page

Every check runs at 390x844 (a phone), in light and dark, at "/" and at "/deals" (no trailing slash, where the
page's relative fetches resolve to /api/deals...). Without Playwright or a browser the tests skip, unless
REQUIRE_BROWSER=1, which turns that into a failure so CI cannot pass by skipping. BROWSER_EXECUTABLE points at a
Chrome or Chromium to use instead of Playwright's own download.

Requests to anything but the local server are answered from here (the page loads Google Fonts), so the run needs
no network and a failed font request cannot count as a console error.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.browser._support import (CUT_SCOPES, SCOPES, TARGET_GAME, TARGET_GAME_PRICE, DealsServer,
                                    build_fixture)

try:
    from playwright.sync_api import sync_playwright
except ImportError:      # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
VIEW = {"width": 390, "height": 844}
PATHS = ("/", "/deals")
SCHEMES = ("light", "dark")
LEGACY_MESSAGE = "This list only has games with alerts. Run Check now to load all wishlist games."
FIXTURE = None


def fixture():
    global FIXTURE
    if FIXTURE is None:
        FIXTURE = build_fixture()
    return FIXTURE


class PageCase(unittest.TestCase):
    pw = None
    browser = None

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            msg = "playwright is not installed (pip install -r tests/browser/requirements.txt)"
            if REQUIRE:
                raise AssertionError(msg + ", but REQUIRE_BROWSER=1")
            raise unittest.SkipTest(msg)
        cls.pw = sync_playwright().start()
        try:
            exe = os.environ.get("BROWSER_EXECUTABLE") or None
            cls.browser = cls.pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        except Exception as e:
            cls.pw.stop()
            if REQUIRE:
                raise AssertionError(f"no browser could be launched, but REQUIRE_BROWSER=1: {e}")
            raise unittest.SkipTest(f"no browser could be launched: {e}")

    @classmethod
    def tearDownClass(cls):
        if cls.browser:
            cls.browser.close()
        if cls.pw:
            cls.pw.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = fixture()
        self.dir = Path(self._tmp.name)
        self.server = None

    def serve(self, **install) -> DealsServer:
        """(Re)start the server over freshly installed data, so each view begins from the same state."""
        if self.server:
            self.server.stop()
        for f in self.dir.glob("*.json"):
            f.unlink()
        self.fx.install(self.dir, **install)
        self.server = DealsServer(self.dir).start()
        self.addCleanup(self.server.stop)
        return self.server

    def open(self, path: str, scheme: str, ready: str = "#kinds button.kind"):
        """-> page. Its console errors, page errors and failed requests are collected in page.problems."""
        ctx = self.browser.new_context(viewport=VIEW, color_scheme=scheme)
        self.addCleanup(ctx.close)
        base = self.server.base

        def route(r):
            if r.request.url == base + "/favicon.ico":      # the page names no icon; Chrome asks anyway
                r.fulfill(status=204)
            elif r.request.url.startswith(base):
                r.continue_()
            else:
                r.fulfill(status=200, content_type="text/css", body="")
        ctx.route("**/*", route)
        page = ctx.new_page()
        page.problems = []
        def on_console(m):
            # Chrome asks for /favicon.ico by itself (outside the routing above); the page names no icon.
            if m.type == "error" and m.location.get("url", "") != base + "/favicon.ico":
                page.problems.append(f"console: {m.text} @ {m.location.get('url', '')}")
        page.on("console", on_console)
        page.on("pageerror", lambda e: page.problems.append("pageerror: " + str(e)))
        page.on("requestfailed", lambda r: page.problems.append(f"requestfailed: {r.url}"))
        page.on("response", lambda r: r.status >= 400 and page.problems.append(f"HTTP {r.status}: {r.url}"))
        page.goto(self.server.base + path)
        page.wait_for_selector(ready)
        return page

    def for_views(self, check):
        """Run check(path, scheme) at every path and colour scheme, each from freshly installed data."""
        for path in PATHS:
            for scheme in SCHEMES:
                with self.subTest(path=path, scheme=scheme):
                    check(path, scheme)

    # --- page helpers -----------------------------------------------------------------------------------------

    @staticmethod
    def scope(page, name: str) -> None:
        page.click(f'#kinds button[data-scope="{name}"]')
        page.wait_for_selector(f'#kinds button[data-scope="{name}"][aria-checked="true"]')

    @staticmethod
    def counts(page) -> dict[str, int]:
        return {s: int(page.inner_text(f'#kinds button[data-scope="{s}"] .num')) for s in SCOPES}

    @staticmethod
    def rows(page) -> int:
        return page.locator("#groups .deal").count()

    @staticmethod
    def row(page, title: str):
        return page.locator("#groups .deal", has=page.locator(".name .t", has_text=title)).first

    def assert_clean(self, page):
        self.assertEqual(page.problems, [], "the page logged errors")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), VIEW["width"],
                             "the page scrolls sideways at 390 px")


class ScopesTest(PageCase):
    def test_each_scope_count_equals_its_list(self):
        want = self.fx.counts()
        self.assertEqual(want, {"all": 60, "sale": 40, "low": 12, "bundle": 10, "target": 5})

        def check(path, scheme):
            self.serve()
            page = self.open(path, scheme)
            self.assertEqual(self.counts(page), want)
            for s in SCOPES:
                self.scope(page, s)
                self.assertEqual(self.rows(page), want[s], f"scope {s}: rows vs count")
                if s != "all":
                    self.assertIn(f"{want[s]} of 60 wishlist games", page.inner_text("#headline"))
            self.assert_clean(page)
        self.for_views(check)

    def test_default_view_is_the_sale_scope(self):
        self.serve()
        page = self.open("/", "light")
        self.assertEqual(page.get_attribute('#kinds button[data-scope="sale"]', "aria-checked"), "true")
        self.assertEqual(self.rows(page), self.fx.counts()["sale"])


class MinimumCutTest(PageCase):
    def test_minimum_percent_filter_and_bundle_only_line(self):
        bundle_only = self.fx.bundle_only_count()
        self.assertGreater(bundle_only, 0)

        def check(path, scheme):
            self.serve()
            page = self.open(path, scheme)
            self.scope(page, "sale")
            self.assertTrue(page.is_hidden("#notice"), "no notice before filtering")
            for cut in (25, 50, 90):
                page.select_option("#minCut", str(cut))
                want = self.fx.counts(min_cut=cut)
                self.assertEqual(self.counts(page), want, f"counts at {cut}%")
                self.assertEqual(self.rows(page), want["sale"], f"sale rows at {cut}%")
                self.assertIn(f"{bundle_only} bundle-only deals hidden by the % filter.", page.inner_text("#notice"))
                self.assertEqual(want["bundle"], self.fx.counts()["bundle"], "the bundle scope ignores the minimum")
            self.scope(page, "low")
            self.assertEqual(self.rows(page), self.fx.counts(min_cut=90)["low"])
            self.scope(page, "target")
            self.assertEqual(self.rows(page), self.fx.counts(min_cut=90)["target"])
            if self.fx.hidden_at_target(90):
                self.assertIn("hidden by the % filter", page.inner_text("#notice"))
            self.scope(page, "all")
            self.assertTrue(page.is_disabled("#minCut"), "the filter is off where it does not apply")
            self.assertEqual(self.rows(page), 60)
            self.assert_clean(page)
        self.for_views(check)

    def test_cut_scopes_are_the_ones_the_filter_applies_to(self):
        self.assertEqual(set(CUT_SCOPES), {"sale", "low", "target"})


class TargetsTest(PageCase):
    def test_set_edit_clear_a_target(self):
        high = round(TARGET_GAME_PRICE + 5, 2)

        def check(path, scheme):
            srv = self.serve()
            page = self.open(path, scheme)
            self.scope(page, "all")
            appid = str(self.fx.game(TARGET_GAME)["appid"])
            self.assertNotIn(appid, srv.targets_on_disk())
            before = self.counts(page)["target"]

            # set a target above the price: the game is now "at or under target", with no reload
            self.row(page, TARGET_GAME).locator("button[data-tgt-open]").click()
            page.fill(f'input[data-tgt-input="{appid}"]', str(high))
            page.click(f'button[data-tgt-save="{appid}"]')
            page.wait_for_selector("#toast:not([hidden])")
            self.assertIn("Target saved", page.inner_text("#toast"))
            self.assertEqual(self.counts(page)["target"], before + 1, "target view updated without a reload")
            self.assertIn("under", self.row(page, TARGET_GAME).inner_text())
            self.assertEqual(srv.targets_on_disk()[appid], high)
            self.scope(page, "target")
            self.assertEqual(self.rows(page), before + 1)
            self.assertEqual(self.row(page, TARGET_GAME).locator(".name .t").count(), 1)

            # it survives a reload
            page.reload()
            page.wait_for_selector("#kinds button.kind")
            self.assertEqual(self.counts(page)["target"], before + 1)
            self.scope(page, "all")
            self.assertIn(f"{high:.2f}", self.row(page, TARGET_GAME).inner_text())

            # edit it down below the price: no longer at target, so it leaves the target view
            self.row(page, TARGET_GAME).locator("button[data-tgt-open]").click()
            page.fill(f'input[data-tgt-input="{appid}"]', "5")
            page.press(f'input[data-tgt-input="{appid}"]', "Enter")
            page.wait_for_function("document.querySelector('#toast').textContent.includes('saved')")
            self.assertEqual(self.counts(page)["target"], before)
            self.assertEqual(srv.targets_on_disk()[appid], 5.0)
            self.assertIn("above", self.row(page, TARGET_GAME).inner_text())

            # clear it
            self.row(page, TARGET_GAME).locator("button[data-tgt-clear]").click()
            page.wait_for_function("document.querySelector('#toast').textContent.includes('cleared')")
            self.assertEqual(self.row(page, TARGET_GAME).locator("button[data-tgt-open]").inner_text(), "Set target")
            self.assertNotIn(appid, srv.targets_on_disk())
            page.reload()
            page.wait_for_selector("#kinds button.kind")
            self.scope(page, "all")
            self.assertEqual(self.row(page, TARGET_GAME).locator("button[data-tgt-open]").inner_text(), "Set target")
            self.assertEqual(self.counts(page)["target"], before)
            self.assert_clean(page)
        self.for_views(check)

    def test_a_bad_value_is_refused_and_nothing_is_saved(self):
        srv = self.serve()
        page = self.open("/", "light")
        self.scope(page, "all")
        appid = str(self.fx.game(TARGET_GAME)["appid"])
        self.row(page, TARGET_GAME).locator("button[data-tgt-open]").click()
        page.fill(f'input[data-tgt-input="{appid}"]', "abc")
        page.press(f'input[data-tgt-input="{appid}"]', "Enter")
        page.wait_for_selector("[role=alert]")
        self.assertNotIn(appid, srv.targets_on_disk())
        self.assert_clean(page)


class LegacySnapshotTest(PageCase):
    def test_old_deals_json_without_games_shows_the_fallback(self):
        self.assertNotIn('"games"', self.fx.legacy_text)

        def check(path, scheme):
            self.serve(legacy=True)
            page = self.open(path, scheme, ready="#groups .deal")
            self.assertEqual(page.inner_text("#notice"), LEGACY_MESSAGE)
            self.assert_clean(page)
        self.for_views(check)


class NoDataTest(PageCase):
    def test_no_snapshot_yet_still_renders(self):
        self.serve()
        (self.dir / "deals.json").unlink()
        page = self.open("/", "light", ready="#groups .empty")
        self.assertIn("appear here after the first check", page.inner_text("#headline"))


class BannerTest(PageCase):
    """The one banner for a failed last check or data older than 36 hours."""

    FAILED_TAIL = "The last check failed ({}); see the watcher log on the server."

    @staticmethod
    def stamp(hours_ago=0.0, seconds_ago=0):
        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago, seconds=seconds_ago)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    def status(self, last_ok, failures=0, cls=None, attempt=None):
        doc = {"version": 1, "last_attempt": attempt or self.stamp(seconds_ago=30), "last_ok": last_ok,
               "consecutive_failures": failures, "error_class": cls}
        (self.dir / "run_status.json").write_text(json.dumps(doc), encoding="utf-8")

    def age_snapshot(self, hours_ago):
        path = self.dir / "deals.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["generatedAt"] = self.stamp(hours_ago)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def banner(self, page):
        return page.is_visible("#runBanner"), page.inner_text("#runBannerText")

    @staticmethod
    def local(page, iso):
        return page.evaluate("(iso) => new Date(iso).toLocaleString(undefined, "
                             "{ month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })", iso)

    def test_hidden_when_everything_is_healthy(self):
        def check(path, scheme):
            self.serve()
            self.status(self.stamp(seconds_ago=60))
            page = self.open(path, scheme)
            self.assertFalse(page.is_visible("#runBanner"))
            self.assert_clean(page)
        self.for_views(check)

    def test_hidden_with_no_status_file_and_fresh_data(self):
        def check(path, scheme):
            self.serve()
            page = self.open(path, scheme)
            self.assertFalse(page.is_visible("#runBanner"))
            self.assert_clean(page)
        self.for_views(check)

    def test_a_failed_last_check_shows_when_and_why(self):
        def check(path, scheme):
            self.serve()
            ok = self.stamp(2)
            self.status(ok, failures=2, cls="itad")
            self.age_snapshot(2)
            page = self.open(path, scheme)
            shown, text = self.banner(page)
            self.assertTrue(shown)
            self.assertEqual(text, f"Deals data last refreshed {self.local(page, ok)}. " + self.FAILED_TAIL.format("itad"))
            self.assertNotIn("/", text)
            self.assert_clean(page)
        self.for_views(check)

    def test_a_failed_check_with_no_success_on_record_still_shows(self):
        self.serve()
        self.status(None, failures=1, cls="steam")
        page = self.open("/", "light")
        shown, text = self.banner(page)
        self.assertTrue(shown)
        self.assertTrue(text.endswith(self.FAILED_TAIL.format("steam")), text)
        self.assertTrue(text.startswith("Deals data last refreshed "), text)   # the snapshot's own time

    def test_old_data_shows_how_many_hours(self):
        def check(path, scheme):
            self.serve()
            self.status(self.stamp(40.5))
            self.age_snapshot(40.5)
            page = self.open(path, scheme)
            self.assertEqual(self.banner(page), (True, "Deals data was last refreshed 40 hours ago; see the watcher log on the server."))
            self.assert_clean(page)
        self.for_views(check)

    def test_old_data_with_no_status_file(self):
        self.serve()
        self.age_snapshot(50.2)
        page = self.open("/deals", "dark")
        self.assertEqual(self.banner(page), (True, "Deals data was last refreshed 50 hours ago; see the watcher log on the server."))

    def test_just_under_36_hours_is_not_stale(self):
        self.serve()
        self.status(self.stamp(35.5))
        self.age_snapshot(35.5)
        page = self.open("/", "light")
        self.assertFalse(page.is_visible("#runBanner"))

    def test_dismissing_hides_it_for_the_page_session(self):
        self.serve()
        self.status(self.stamp(2), failures=3, cls="notify")
        page = self.open("/", "light")
        self.assertTrue(page.is_visible("#runBanner"))
        page.click("#runBannerClose")
        self.assertFalse(page.is_visible("#runBanner"))
        # the page reloads its data when the tab becomes visible again; the banner must stay away
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(300)
        self.assertFalse(page.is_visible("#runBanner"))
        self.assert_clean(page)

    def test_the_status_route_answers_at_both_paths(self):
        self.serve()
        self.status(self.stamp(2), failures=1, cls="steam")
        for path in PATHS:
            page = self.open(path, "light")
            self.assertEqual(page.evaluate("fetch('api/deals/status').then(r => r.json()).then(j => j.error_class)"), "steam")


class FixtureSanityTest(unittest.TestCase):
    """No browser needed: the fixture really covers what the page tests lean on."""

    def test_fixture_shape(self):
        fx = fixture()
        self.assertEqual(len(fx.games), 60)
        self.assertTrue(all(c > 0 for c in fx.counts().values()))
        self.assertGreater(fx.bundle_only_count(), 0)
        g = fx.game(TARGET_GAME)
        self.assertFalse(g["onSale"] or g["inBundle"])
        self.assertEqual(g["price"]["amount"], TARGET_GAME_PRICE)
        self.assertNotIn(str(g["appid"]), fx.targets)


if __name__ == "__main__":
    unittest.main()
