"""The Ask view of deals.html in a real browser, served by a real deals_server.py.

Run from the repo root:  REQUIRE_BROWSER=1 python -m unittest -v tests.browser.test_ask_page

What it proves:
  ListUnchanged  the list's rows are byte-for-byte what main's page drew (a golden file recorded from the page
                 before the shared row renderer was refactored);
  Parity         the page's row order, for filter states set through its own controls, equals what
                 deals_query.find() returns for the same filters (what Ask's tools will say);
  AskPage        the pill appears only when api/deals/ask/status says so; an answer is shown as text and its
                 games as the list's own rows, target editing included; Retry, the turn limit, New question and
                 "Ask instead" behave as spec section 10 says; the thread is never written to browser storage;
                 no horizontal scroll at 390 and 360 px. api/deals/ask and api/deals/ask/status are stubbed with
                 page.route (both URL prefixes), so no request ever reaches Anthropic.

Same switches as test_page.py: BROWSER_EXECUTABLE, REQUIRE_BROWSER=1. RECORD_GOLDEN=1 rewrites
tests/fixtures/list_rows_golden.json (do that only from a page whose list rows are known to be right).
"""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

import deals_query
from tests.browser._support import TARGET_GAME
from tests.browser.test_page import PageCase

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "fixtures" / "list_rows_golden.json"
FROZEN = "2026-11-10T18:00:00Z"
SCHEMES = ("light", "dark")
WIDE = {"width": 1280, "height": 900}
READY_ASK = "#chatQ:not([disabled])"
LEGACY_ASK = "The deals list is from an older watcher; run Check now on the list page."


def pick(page, *, scope=None, sort=None, cut=None, deck=None, rating=None, q=None, tag=None):
    """Set the list's controls the way a person would."""
    if scope:
        PageCase.scope(page, scope)
    if cut is not None:
        page.select_option("#minCut", str(cut))
    if deck is not None:
        page.select_option("#deckF", deck)
    if rating is not None:
        page.select_option("#minRating", str(rating))
    if sort:
        page.select_option("#sort", sort)
    if tag:
        page.select_option("#tag", tag)
    if q is not None:
        page.fill("#q", q)
        page.wait_for_timeout(250)     # the search box is debounced


def titles(page) -> list[str]:
    return page.eval_on_selector_all("#groups .deal .name a.t", "els => els.map(e => e.textContent)")


class ListUnchanged(PageCase):
    """#groups' HTML for four filter states, recorded before row() was given its opts argument."""

    STATES = {
        "default": {},
        "all_by_price": {"scope": "all", "sort": "price"},
        "sale_50": {"scope": "sale", "cut": 50},
        "target_roguelike": {"scope": "target", "tag": "Roguelike"},
    }

    def capture(self) -> dict:
        self.serve()
        out = {}
        for name, controls in self.STATES.items():
            page = self.open("/deals", "light", view=WIDE, setup_page=lambda p: p.clock.set_fixed_time(FROZEN))
            pick(page, **controls)
            out[name] = page.inner_html("#groups")
            self.assertEqual(page.problems, [])
            page.close()
        return out

    def test_list_rows_match_golden(self):
        got = self.capture()
        if os.environ.get("RECORD_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(list(got), list(want))
        for name in self.STATES:
            with self.subTest(state=name):
                self.assertTrue(got[name].count('class="deal"') > 0, "the state shows rows")
                self.assertEqual(got[name], want[name])


class Parity(PageCase):
    """Page row order == deals_query.find() order, strict (no relaxation, which would hide an empty-state gap)."""

    def test_page_order_matches_find_deals(self):
        self.serve()
        snap = deals_query.build_snapshot(self.fx.snapshot, {int(k): v for k, v in self.fx.targets.items()}, None)
        states = [
            ("sort rank", {"sort": "rank"}, {"sort": "rank"}),
            ("sort cut", {"sort": "cut"}, {"sort": "cut"}),
            ("sort price", {"sort": "price"}, {"sort": "price"}),
            ("sort ending", {"sort": "ending"}, {"sort": "ending"}),
            ("sort rating", {"sort": "rating"}, {"sort": "rating"}),
            ("scope low", {"scope": "low"}, {"scope": "low"}),
            ("min % 33", {"cut": 33}, {"min_cut": 33}),
            ("deck works", {"deck": "works"}, {"deck": "works"}),
            ("rating 85", {"rating": 85}, {"min_rating": 85}),
            ("search full", {"scope": "all", "q": "full"}, {"scope": "all", "query": "full"}),
        ]
        for scheme in SCHEMES:
            for label, controls, args in states:
                with self.subTest(scheme=scheme, state=label):
                    page = self.open("/deals", scheme, view=WIDE)
                    pick(page, **controls)
                    got = titles(page)
                    want = [g["title"] for g in deals_query.find(snap, relax=False, limit=deals_query.MAX_LIMIT,
                                                                 **args)["games"]]
                    self.assertTrue(want, "the state matches something")
                    self.assertEqual(got[:deals_query.MAX_LIMIT], want)
                    page.close()


class AskPage(PageCase):
    """The Ask view. Each check runs in light and dark, from /ask and /deals/ask, at 390 px unless it says."""

    def setUp(self):
        super().setUp()
        self.status = {"ask": True, "turns": 5}
        self.replies = []        # (status, body, headers) answers to POST api/deals/ask, in order; the last repeats
        self.posts = []          # request bodies api/deals/ask received
        self.puts = []           # (method, url, body) of target saves
        self.serve()
        a, b = self.fx.game("Sale 01"), self.fx.game(TARGET_GAME)
        self.alpha = {"appid": a["appid"], "name": a["title"]}
        self.bravo = {"appid": b["appid"], "name": b["title"]}
        self.answer = {"answer": "Two <b>ideas</b>.", "items": [self.alpha, self.bravo], "turns_left": 4}
        self.replies = [(200, self.answer, None)]

    # --- plumbing -------------------------------------------------------------------------------------------
    def _status(self, route):
        route.fulfill(status=200, json=self.status)

    def _ask(self, route):
        if route.request.method != "POST":
            return route.fulfill(status=405, json={"error": "method not allowed"})
        self.posts.append(route.request.post_data_json)
        status, body, headers = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        route.fulfill(status=status, json=body, headers=headers or {})

    def stubs(self, ctx):
        ctx.route("**/api/deals/ask/status", self._status)
        ctx.route("**/api/deals/ask", self._ask)

    def go(self, path="/deals/ask", scheme="light", ready=None, view=None, setup_ctx=None):
        ready = ready or (READY_ASK if path.endswith("ask") else "#kinds button.kind")
        def both(ctx):
            self.stubs(ctx)
            if setup_ctx:
                setup_ctx(ctx)
        return self.open(path, scheme, ready, view=view, setup_ctx=both)

    def each(self, fn, paths=("/ask", "/deals/ask")):
        for path in paths:
            for scheme in SCHEMES:
                with self.subTest(path=path, scheme=scheme):
                    self.status = {"ask": True, "turns": 5}
                    self.replies = [(200, self.answer, None)]
                    self.posts = []
                    fn(path, scheme)

    def ask(self, page, q="something cheap for tonight", wait=".chat-a"):
        n = page.locator(wait).count()
        page.fill("#chatQ", q)
        page.press("#chatQ", "Enter")
        page.wait_for_function(f"document.querySelectorAll({json.dumps(wait)}).length > {n}", timeout=10000)
        page.wait_for_function("!document.querySelector('#chatThread .chat-status')", timeout=10000)

    def clean(self, page, allow_ask_errors=False):
        def expected(p):    # what a deliberate error answer from api/deals/ask leaves behind
            return "/api/deals/ask" in p or "Failed to load resource: the server responded with a status of" in p
        left = [p for p in page.problems if not (allow_ask_errors and expected(p))]
        self.assertEqual(left, [])

    # --- the pill ---------------------------------------------------------------------------------------------
    def test_pill_visible_and_current(self):
        def run(path, scheme):
            base = self.server.base
            page = self.go("/deals", scheme, ready="#chatTop")
            self.assertIsNone(page.get_attribute("#chatTop", "aria-current"))
            self.assertEqual(page.eval_on_selector("#chatTop", "a => a.href"), base + "/deals/ask")
            self.assertIn("Ask", page.inner_text("#chatTop"))
            self.assertEqual(page.locator("#chatTop svg").count(), 1)
            self.assertEqual(page.title(), "Wishlist Deals")
            root = self.go("/", scheme, ready="#chatTop")
            self.assertEqual(root.eval_on_selector("#chatTop", "a => a.href"), base + "/ask")
            ask = self.go(path, scheme, ready="#chatTop")
            self.assertEqual(ask.get_attribute("#chatTop", "aria-current"), "page")
            self.assertEqual(ask.title(), "Ask your wishlist")
            self.assertTrue(ask.is_visible("#chatSec"))
            self.assertEqual(ask.eval_on_selector("#chatTop", "a => a.href"), base + path)
            for p in (page, root, ask):
                self.clean(p)
        self.each(run, paths=("/ask", "/deals/ask"))

    def test_pill_hidden_when_off(self):
        def run(path, scheme):
            self.status = {"ask": False}
            page = self.go("/", scheme)
            page.wait_for_timeout(200)
            self.assertEqual(page.locator("#chatTop").count(), 0)
            pick(page, q="zzzz")
            page.wait_for_selector("#groups .empty")
            self.assertEqual(page.locator("#chatInstead").count(), 0, "no Ask instead without the pill")
            off = self.go(path, scheme, ready="#chatThread a")
            self.assertIn("Ask isn't available here.", off.inner_text("#chatThread"))
            self.assertEqual(off.locator("#chatTop").count(), 0)
            self.assertEqual(off.eval_on_selector("#chatThread a", "a => a.href"),
                             self.server.base + ("/deals/" if path.startswith("/deals") else "/"))
            self.assertTrue(off.is_disabled("#chatQ"))
            self.assertEqual(self.posts, [])
            self.clean(page)
            self.clean(off)
        self.each(run)

    # --- answers and rows ------------------------------------------------------------------------------------
    def test_answer_is_text_and_rows_render(self):
        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page, "two short ones")
            self.assertEqual(page.eval_on_selector(".chat-a", "e => e.textContent"), "Two <b>ideas</b>.")
            self.assertEqual(page.locator(".chat-a b").count(), 0)
            self.assertEqual(page.eval_on_selector(".chat-q", "e => e.textContent"), "two short ones")
            self.assertEqual(page.locator("#chatThread .deal").count(), 2)
            self.assertEqual(titles_in(page), [self.fx.game("Sale 01")["title"], TARGET_GAME])
            self.assertEqual(self.posts, [{"thread": [{"q": "two short ones"}]}])
            self.assertIn("4 left", page.inner_text("#chatMeta"))
            # The list view is hidden, not rebuilt.
            for sel in ("#kinds", ".filters", "#groups", "footer", "#checkBtn", "#notice"):
                self.assertFalse(page.is_visible(sel), sel)
            self.assertEqual(page.inner_html("#groups"), "")
            self.clean(page)
        self.each(run)

    def test_set_target_on_ask_row(self):
        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page)
            row = page.locator("#chatThread .deal", has=page.locator(".name .t", has_text=TARGET_GAME))
            row.get_by_role("button", name=re.compile("Set a target price")).click()
            box = page.locator("#chatThread input[data-tgt-input]")
            box.wait_for()
            box.fill("12")
            with page.expect_request(lambda r: r.method == "PUT" and "/api/deals/targets/" in r.url) as req:
                box.press("Enter")
            appid = self.bravo["appid"]
            self.assertTrue(req.value.url.endswith(f"api/deals/targets/{appid}"), req.value.url)
            self.assertEqual(req.value.post_data_json, {"price": 12})
            row.locator(".tgt .state").wait_for()
            self.assertIn("Target", row.locator(".tgt").inner_text())
            self.assertEqual(page.locator(".chat-a").count(), 1, "the thread survives the re-render")
            self.assertEqual(page.locator("#chatThread .deal").count(), 2)
            page.wait_for_function("!document.getElementById('toast').hidden")
            self.assertEqual(self.server.targets_on_disk()[str(appid)], 12.0)
            self.serve()      # a clean data directory for the next view
            self.clean(page)
        self.each(run)

    def test_tags_plain_in_ask_rows(self):
        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page)
            self.assertEqual(page.locator("#chatThread button[data-tag]").count(), 0)
            self.assertGreater(page.locator("#chatThread .tags .tag").count(), 0, "the tags are still shown")
            self.assertEqual(page.locator("#chatThread .tags button").count(), 0)
            self.clean(page)
        self.each(run)

    def test_unknown_item_plain_text(self):
        def run(path, scheme):
            self.replies = [(200, {"answer": "One.", "items": [{"appid": 999999, "name": "<i>Gone</i> & Co"}],
                                   "turns_left": 4}, None)]
            page = self.go(path, scheme)
            self.ask(page)
            self.assertEqual(page.eval_on_selector(".chat-missing", "e => e.textContent"), "<i>Gone</i> & Co")
            self.assertEqual(page.locator(".chat-missing i").count(), 0)
            self.assertEqual(page.locator("#chatThread .deal").count(), 0)
            self.clean(page)
        self.each(run)

    def test_no_horizontal_scroll(self):
        width = "document.documentElement.scrollWidth"

        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page)
            self.assertLessEqual(page.evaluate(width), 390)
            for p in ("/", path):
                small = self.go(p, scheme, ready="#chatTop", view={"width": 360, "height": 740})
                if p == path:
                    small.wait_for_selector(READY_ASK)
                    self.ask(small)
                self.assertLessEqual(small.evaluate(width), 360, p)
                self.clean(small)
            self.clean(page)
        self.each(run)

    def test_pill_fits_at_360(self):
        def run(path, scheme):
            for p in ("/", path):
                page = self.go(p, scheme, ready="#chatTop", view={"width": 360, "height": 740})
                pill, h1 = page.locator("#chatTop").bounding_box(), page.locator("h1").bounding_box()
                mid = pill["y"] + pill["height"] / 2
                self.assertTrue(h1["y"] <= mid <= h1["y"] + h1["height"], (p, pill, h1))
                self.assertLessEqual(pill["x"] + pill["width"], 360)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 360)
                self.clean(page)
        self.each(run)

    # --- states ----------------------------------------------------------------------------------------------
    def test_retry_after_502_resends_thread(self):
        def run(path, scheme):
            self.replies = [(502, {"error": "AI search is unavailable right now, try again"}, None),
                            (200, self.answer, None)]
            page = self.go(path, scheme)
            self.ask(page, "retry me", wait=".chat-error")
            self.assertIn("AI search is unavailable right now, try again", page.inner_text(".chat-error"))
            self.assertEqual(page.get_attribute(".chat-error", "role"), "alert")
            page.click("#chatRetry")
            page.wait_for_selector(".chat-a")
            self.assertEqual(self.posts, [{"thread": [{"q": "retry me"}]}] * 2)
            self.assertEqual(page.locator(".chat-error").count(), 0)
            self.assertEqual(page.locator(".chat-q").count(), 1)
            self.clean(page, allow_ask_errors=True)
        self.each(run)

    def test_no_retry_on_cap_429(self):
        def run(path, scheme):
            self.replies = [(429, {"error": "Daily AI search limit reached; try again tomorrow."}, {})]
            page = self.go(path, scheme)
            self.ask(page, wait=".chat-error")
            self.assertIn("Daily AI search limit reached", page.inner_text(".chat-error"))
            self.assertEqual(page.locator("#chatRetry").count(), 0)
            self.clean(page, allow_ask_errors=True)
        self.each(run)

    def test_no_retry_on_400_401_403(self):
        def run(path, scheme):
            for code in (400, 401, 403):
                self.replies = [(code, {"error": f"refused {code}"}, None)]
                page = self.go(path, scheme)
                self.ask(page, wait=".chat-error")
                self.assertIn(f"refused {code}", page.inner_text(".chat-error"))
                self.assertEqual(page.locator("#chatRetry").count(), 0, code)
                self.clean(page, allow_ask_errors=True)
        self.each(run)

    def test_retry_on_busy_429(self):
        def run(path, scheme):
            self.replies = [(429, {"error": "Ask is busy; try again in a moment."}, {"Retry-After": "5"}),
                            (200, self.answer, None)]
            page = self.go(path, scheme)
            self.ask(page, "busy now", wait=".chat-error")
            self.assertIn("Ask is busy", page.inner_text(".chat-error"))
            page.click("#chatRetry")
            page.wait_for_selector(".chat-a")
            self.assertEqual(self.posts, [{"thread": [{"q": "busy now"}]}] * 2)
            self.clean(page, allow_ask_errors=True)
        self.each(run)

    def test_turn_limit_disables_input(self):
        def run(path, scheme):
            self.replies = [(200, dict(self.answer, turns_left=1), None),
                            (200, dict(self.answer, answer="Last one.", turns_left=0), None)]
            page = self.go(path, scheme)
            self.ask(page, "first")
            self.assertIn("1 left", page.inner_text("#chatMeta"))
            self.ask(page, "second")
            self.assertEqual(self.posts[1], {"thread": [{"q": "first"}, {"a": "Two <b>ideas</b>."}, {"q": "second"}]})
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertTrue(page.is_disabled("#chatSend"))
            self.assertEqual(page.get_attribute("#chatQ", "placeholder"), "Start a new question to keep going")
            self.assertIn("0 left", page.inner_text("#chatMeta"))
            self.clean(page)
        self.each(run)

    def test_new_question_clears(self):
        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page)
            page.click("#chatNew")
            self.assertEqual(page.locator(".chat-q").count(), 0)
            self.assertEqual(page.locator(".chat-a").count(), 0)
            self.assertEqual(page.locator("#chatThread .deal").count(), 0)
            self.assertTrue(page.is_enabled("#chatQ"))
            self.assertEqual(page.input_value("#chatQ"), "")
            self.assertIn("5 left", page.inner_text("#chatMeta"))
            self.assertEqual(page.evaluate("document.activeElement.id"), "chatQ")
            self.clean(page)
        self.each(run)

    def test_ask_instead_prefills_not_sends(self):
        def run(path, scheme):
            list_path = "/deals" if path.startswith("/deals") else "/"
            page = self.go(list_path, scheme)
            pick(page, q="zzzz")
            page.wait_for_selector("#chatInstead", state="visible")
            self.assertTrue(page.eval_on_selector("#chatInstead", "a => a.href").endswith(path + "?draft=zzzz"))
            page.click("#chatInstead")
            page.wait_for_url("**" + path + "**")
            page.wait_for_selector(READY_ASK)
            self.assertEqual(page.input_value("#chatQ"), "zzzz")
            self.assertEqual(page.evaluate("location.search"), "")
            page.wait_for_timeout(300)
            self.assertEqual(self.posts, [])
            self.assertEqual(page.locator(".chat-q").count(), 0)
            self.clean(page)
        self.each(run)

    def test_legacy_snapshot_message(self):
        def run(path, scheme):
            self.serve(legacy=True)
            page = self.go(path, scheme, ready="#chatThread a")
            self.assertIn(LEGACY_ASK, page.inner_text("#chatThread"))
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertEqual(self.posts, [])
            self.clean(page)
        self.each(run)

    def test_waits_for_snapshot(self):
        def run(path, scheme):
            held = []
            page = self.go(path, scheme, ready="#chatQ",
                           setup_ctx=lambda ctx: ctx.route(re.compile(r"/api/deals$"), lambda r: held.append(r)))
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertEqual(page.get_attribute("#chatQ", "placeholder"), "Loading your wishlist…")
            page.wait_for_timeout(700)
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertTrue(page.is_disabled("#chatSend"))
            self.assertGreaterEqual(len(held), 1)
            for r in held:
                r.continue_()
            page.wait_for_selector(READY_ASK)
            self.assertNotEqual(page.get_attribute("#chatQ", "placeholder"), "Loading your wishlist…")
            self.clean(page)
        self.each(run)

    def test_thread_not_in_storage(self):
        def run(path, scheme):
            page = self.go(path, scheme)
            self.ask(page, "storage-marker question")
            dump = page.evaluate("""() => {
                const all = (s) => { const o = {}; for (let i = 0; i < s.length; i++) o[s.key(i)] = s.getItem(s.key(i)); return o; };
                return JSON.stringify([all(localStorage), all(sessionStorage)]);
            }""")
            self.assertNotIn("storage-marker", dump)
            self.assertNotIn("ideas", dump)
            self.clean(page)
        self.each(run)

    def test_no_console_errors_all_paths(self):
        def run(path, scheme):
            for p in ("/", "/deals", "/ask", "/deals/ask"):
                ready = READY_ASK if p.endswith("ask") else "#kinds button.kind"
                page = self.go(p, scheme, ready=ready)
                page.wait_for_timeout(200)
                self.assertEqual(page.is_visible("#chatSec"), p.endswith("ask"), p)
                self.assertEqual(page.is_visible("#groups"), not p.endswith("ask"), p)
                self.assertEqual(page.is_visible("#chatTop"), True, p)
                self.assertEqual(page.title(), "Ask your wishlist" if p.endswith("ask") else "Wishlist Deals")
                self.clean(page)
                page.close()
        self.each(run, paths=("/ask",))


def titles_in(page) -> list[str]:
    return page.eval_on_selector_all("#chatThread .deal .name a.t", "els => els.map(e => e.textContent)")


if __name__ == "__main__":
    unittest.main()
