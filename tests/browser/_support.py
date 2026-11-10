"""Shared pieces of the page tests. Not a test module (unittest only collects test*.py).

  build_fixture()  A 60-game wishlist snapshot plus a targets.json. The snapshot is written by the watcher's own
                   evaluate(), build_games() and write_snapshot() over canned ITAD data, so the page is tested
                   against what the watcher really produces, flags and all, not against hand-typed JSON.
  DealsServer      deals_server.py as a subprocess on a free port, serving a throwaway data directory.

Standard library only. The Playwright part lives in test_page.py.
"""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tests import helpers  # noqa: E402

SCOPES = ("all", "sale", "low", "bundle", "target")
CUT_SCOPES = ("sale", "low", "target")   # the page applies the minimum-% filter to these scopes only

# The game the target-editing tests work on: no target to start with, full price, so it is not on sale.
TARGET_GAME = "Full price 06"
TARGET_GAME_PRICE = 29.99

SHOPS = ("Steam", "GOG", "Fanatical", "Humble Store")
TAGS = ("Roguelike", "Indie", "Puzzle", "Strategy", "RPG", "Co-op", "Pixel Graphics", "Story Rich",
        "Open World", "Metroidvania")
REGULARS = (40.0, 80.0, 60.0)
FULL_PRICES = (9.99, 14.99, 19.99, 24.99, 29.99, 29.99, 39.99, 49.99, 59.99, 59.99, 4.99, 12.99, 69.99, 34.99)
LONG_TITLE = "An Extraordinarily Long Game Title That Keeps Going: The Definitive Complete Collection Edition"


# ---------------------------------------------------------------------------------------------- the wishlist

class _World:
    """The wishlist as the watcher sees it: its games, ITAD's prices by game id, and what kind each game is."""

    def __init__(self):
        self.games: list[dict] = []
        self.prices: dict[str, dict] = {}
        self._per_kind: dict[str, int] = {}

    def add(self, kind: str, deals: list[dict] | None, low: float | None = None) -> dict:
        n = len(self.games) + 1
        k = self._per_kind[kind] = self._per_kind.get(kind, 0) + 1
        game = {"appid": 700000 + n, "gid": f"g{n}", "title": f"{kind} {k:02d}", "rank": (n * 7) % 60 + 1}
        self.games.append(game)
        if deals is not None:    # None: ITAD's response doesn't mention the game at all
            self.prices[game["gid"]] = {"id": game["gid"], "deals": deals,
                                         "historyLow": {"all": helpers.money(low) if low is not None else None}}
        return game

    def title(self, title: str) -> dict:
        return next(g for g in self.games if g["title"] == title)

    def price_of(self, title: str) -> float:
        return self.prices[self.title(title)["gid"]]["deals"][0]["price"]["amount"]


def _sale_price(regular: float, cut: int) -> float:
    return round(regular * (100 - cut) / 100, 2)


def _build_world() -> _World:
    """60 games: every case the deals page tells apart, so every view has something in it."""
    w = _World()
    for i, cut in enumerate((10, 20, 25, 30, 33, 40, 50, 60, 66, 75, 80, 90, 15, 35, 45, 55)):       # 16
        regular = REGULARS[i % 3]
        price = _sale_price(regular, cut)
        w.add("Sale", [helpers.deal(SHOPS[i % 4], price, regular)], low=round(price * 0.6, 2))
    for i, cut in enumerate((40, 50, 60, 70, 75, 80, 85, 90, 45, 55)):                              # 10: at the low
        regular = REGULARS[i % 3]
        price = _sale_price(regular, cut)
        w.add("Low", [helpers.deal(SHOPS[i % 4], price, regular)], low=price)
    for i, cut in enumerate((20, 30)):                                                              # 2: at the low, but under the 40% minimum
        price = _sale_price(80.0, cut)
        w.add("Small-cut low", [helpers.deal("Steam", price, 80.0)], low=price)
    for i, cut in enumerate((80, 90)):                                                              # 2: cheaper than the old low
        price = _sale_price(60.0, cut)
        w.add("New low", [helpers.deal("Steam", price, 60.0)], low=round(price * 1.25, 2))
    for i in range(6):                                                                              # 6: no store price, in a live bundle
        w.add("Bundle only", [], low=10.0)
    for cut in (20, 35, 55, 70):                                                                    # 4: discounted and bundled
        price = _sale_price(80.0, cut)
        w.add("Sale+bundle", [helpers.deal("Steam", price, 80.0)], low=round(price * 0.5, 2))
    for p in FULL_PRICES:                                                                           # 14: not on sale
        w.add("Full price", [helpers.deal("Steam", p, p)], low=round(p * 0.5, 2))
    for i in range(4):                                                                              # 4: ITAD lists no price
        w.add("No price", [], low=None)
    for i in range(2):                                                                              # 2: only deal needs a voucher
        w.add("Voucher only", [helpers.deal("Steam", 5.0, 20.0, voucher="CODE")], low=5.0)
    assert len(w.games) == 60
    return w


def _bundles(w: _World) -> list[dict]:
    gid = {g["title"]: g["gid"] for g in w.games}
    return [
        helpers.bundle("b1", "Fixture Bundle One", [
            helpers.tier(9.0, gid["Bundle only 01"], gid["Bundle only 02"], gid["Bundle only 03"]),
            helpers.tier(15.0, gid["Bundle only 01"], gid["Bundle only 02"], gid["Bundle only 03"],
                         gid["Bundle only 04"], gid["Sale+bundle 01"])]),
        helpers.bundle("b2", "Fixture Bundle Two", [helpers.tier(5.0, gid["Bundle only 05"], gid["Sale+bundle 02"])]),
        helpers.bundle("b3", "Fixture Bundle Three", [        # a tier with no price: "see bundle"
            helpers.tier(None, gid["Bundle only 06"], gid["Sale+bundle 03"], gid["Sale+bundle 04"])]),
    ]


def _meta(w: _World) -> dict[str, dict]:
    """The cached ratings, tags and Deck details the watcher keeps per game."""
    out = {}
    steam_scores = (97, 92, 88, 81, 75, 66, 55, None)
    steam_counts = (48000, 12000, 2400, 600, 90, 35, 12, 0)
    protondb = ("platinum", "gold", "silver", "bronze", "borked")
    for i, g in enumerate(w.games):
        reviews = {}
        if steam_scores[i % 8] is not None:
            reviews["steam"] = {"score": steam_scores[i % 8], "count": steam_counts[i % 8], "url": "https://example.com/r/steam"}
        if i % 3 == 0:
            reviews["metacritic"] = {"score": 70 + i % 25, "count": 40, "url": "https://example.com/r/metacritic"}
        if i % 5 == 0:
            reviews["opencritic"] = {"score": 65 + i % 30, "count": 30, "url": "https://example.com/r/opencritic"}
        out[g["gid"]] = {
            "r": reviews, "tags": [TAGS[(i + j) % len(TAGS)] for j in range(1 + i % 4)],
            "released": f"{2012 + i % 13}-0{1 + i % 9}-15", "ea": i % 11 == 0, "dev": f"Studio {chr(65 + i % 12)}",
            "deck": (3, 2, 1, 0, None)[i % 5],
            "proton": {"tier": protondb[i % 5], "total": 40 + i * 7, "trending": protondb[(i + 1) % 5]} if i % 4 else {},
        }
    # One row built to be as wide as a row can get, for the "no horizontal scroll at 390 px" check.
    stress = w.title("Full price 14")
    stress["title"] = LONG_TITLE
    out[stress["gid"]].update({
        "tags": ["Open World Survival Crafting", "Atmospheric", "Difficult", "Souls-like", "Female Protagonist", "Great Soundtrack"],
        "dev": "A Developer With A Remarkably Long Studio Name Incorporated",
        "r": {"steam": {"score": 97, "count": 48000, "url": "https://example.com/r/steam"},
              "metacritic": {"score": 96, "count": 90, "url": "https://example.com/r/metacritic"},
              "opencritic": {"score": 95, "count": 120, "url": "https://example.com/r/opencritic"}},
        "deck": 3, "proton": {"tier": "platinum", "total": 12345, "trending": "gold"}, "ea": True})
    return out


def _targets(w: _World) -> dict[int, float]:
    t = {}
    for title in ("Sale 01", "Sale 02", "Sale 03", "Full price 03"):      # target above the price: at target
        t[w.title(title)["appid"]] = round(w.price_of(title) + 5, 2)
    t[w.title("Low 01")["appid"]] = w.price_of("Low 01")                   # exactly at the target: still "at"
    for title in ("Sale 04", "Sale 05"):                                    # target below the price: above target
        t[w.title(title)["appid"]] = round(w.price_of(title) * 0.5, 2)
    t[w.title("No price 01")["appid"]] = 10.0                               # nothing to compare against
    return t


# ---------------------------------------------------------------------------------------------- the fixture

def at_target(game: dict, targets: dict[str, float]) -> bool:
    t = targets.get(str(game["appid"]))
    return t is not None and game["price"] is not None and game["price"]["amount"] <= t


def in_scope(game: dict, scope: str, targets: dict[str, float]) -> bool:
    return {"all": True, "sale": game["onSale"], "low": game["isLow"], "bundle": game["inBundle"],
            "target": at_target(game, targets)}[scope]


class Fixture:
    """The snapshot and targets as the watcher and server would leave them, and what the page should show."""

    def __init__(self, snapshot_text: str, targets: dict[str, float]):
        self.snapshot_text = snapshot_text
        self.snapshot = json.loads(snapshot_text)
        self.games: list[dict] = self.snapshot["games"]
        self.targets = targets
        legacy = {k: v for k, v in self.snapshot.items() if k not in ("games", "currency")}
        self.legacy_text = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))

    def game(self, title: str) -> dict:
        return next(g for g in self.games if g["title"] == title)

    def install(self, data_dir, *, legacy: bool = False, targets: bool = True) -> None:
        """Put deals.json (an older watcher's, without "games", when legacy) and targets.json in data_dir."""
        d = Path(data_dir)
        (d / "deals.json").write_text(self.legacy_text if legacy else self.snapshot_text, encoding="utf-8")
        if targets:
            doc = {"version": 1, "currency": "USD", "targets": self.targets}
            (d / "targets.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")

    def counts(self, targets: dict[str, float] | None = None, min_cut: int = 0) -> dict[str, int]:
        """How many games each scope button should show, for these targets and this minimum % off."""
        t = self.targets if targets is None else targets
        return {s: sum(1 for g in self.games
                       if in_scope(g, s, t) and (not min_cut or s not in CUT_SCOPES or g["cut"] >= min_cut))
                for s in SCOPES}

    def bundle_only_count(self) -> int:
        """Games on sale only because a bundle has them: no % off, so a minimum-% filter hides them."""
        return sum(1 for g in self.games if g["onSale"] and g["inBundle"] and not g["cut"] > 0)

    def hidden_at_target(self, min_cut: int) -> int:
        return sum(1 for g in self.games if at_target(g, self.targets) and g["cut"] < min_cut)


def build_fixture() -> Fixture:
    wd = helpers.load_watcher()
    w = _build_world()
    bundles, meta, targets = _bundles(w), _meta(w), _targets(w)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "deals.json"
        cfg = helpers.make_cfg(rules={"low_tolerance_pct": "0", "low_min_discount_pct": "40", "big_discount_pct": "75"})
        cfg.add_section("output")
        cfg["output"]["deals_json"] = str(path)
        # What main() does: evaluate, work out what is new, add the details, build the page's games, write.
        current = wd.evaluate(w.games, w.prices, bundles, cfg, targets)
        already_sent = {k: {"amountInt": (a.get("price") or {}).get("amountInt")} for k, a in list(current.items())[:4]}
        unsent = wd.new_alerts(current, already_sent)
        gid_of = {g["appid"]: g["gid"] for g in w.games}
        for a in current.values():
            a.update(wd.meta_fields(meta.get(gid_of[a["appid"]]) or {}))
        games = wd.build_games(w.games, w.prices, bundles, cfg, targets, meta, {a["appid"] for a in unsent})
        wd.write_snapshot(cfg, current, unsent, 62, 2, games, wd.snapshot_currency(games, cfg))
        text = path.read_text(encoding="utf-8")
    return Fixture(text, {str(k): v for k, v in targets.items()})


# ---------------------------------------------------------------------------------------------- the server

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DealsServer:
    """deals_server.py on a free loopback port, serving data_dir. Its output goes to server.log in that directory."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> "DealsServer":
        self._log = open(self.dir / "server.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "deals_server.py"), "--port", str(self.port), "--bind", "127.0.0.1",
             "--data", str(self.dir), "--check-times", "10:40,19:40", "--nav", "Backlog=https://backlog.example.com/"],
            stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("deals_server.py exited early:\n" + self.output())
            try:
                if self.get("/healthz")[0] == 200:
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("deals_server.py did not answer /healthz in 15 s:\n" + self.output())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._log:
            self._log.close()

    def output(self) -> str:
        try:
            return (self.dir / "server.log").read_text(errors="replace")
        except OSError:
            return ""

    def get(self, path: str):
        """-> (status, body text)"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            c.close()

    def targets_on_disk(self) -> dict[str, float]:
        """The targets the server has saved, read from targets.json itself (not through the API)."""
        return json.loads((self.dir / "targets.json").read_text(encoding="utf-8"))["targets"]
