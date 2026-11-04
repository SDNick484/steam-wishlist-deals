"""targets.json: reading it, and the one-time move from config.ini's [targets]."""

import configparser
import errno
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from tests import helpers, stub_services


def make_cfg(data_dir, ini_targets=None, country="US"):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"steam": {}, "itad": {"country": country}, "rules": {},
                   "output": {"deals_json": os.path.join(data_dir, "deals.json")}})
    if ini_targets is not None:
        cfg.add_section("targets")
        for k, v in ini_targets.items():
            cfg["targets"][k] = v
    return cfg


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)


class TargetsFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = helpers.load_watcher()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.path = os.path.join(self.dir, "targets.json")
        self.logs = []
        p = mock.patch.object(self.wd, "log", self.logs.append)
        p.start()
        self.addCleanup(p.stop)

    def write(self, doc):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))

    def files(self):
        return sorted(os.listdir(self.dir))

    # ---- migration cases

    def test_no_file_and_no_ini_targets(self):
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir)), {})
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir, {})), {})
        self.assertEqual(self.files(), [])
        self.assertEqual(self.logs, [])

    def test_ini_only_moves_into_a_new_file(self):
        cfg = make_cfg(self.dir, {"1006": "10.00", "1245620": "20"})
        self.assertEqual(self.wd.load_targets(cfg), {1006: 10.0, 1245620: 20.0})
        self.assertEqual(self.files(), ["targets.json"])  # no temp file left behind
        doc = load_json(self.path)
        self.assertEqual(doc, {"version": 1, "currency": "USD", "targets": {"1006": 10.0, "1245620": 20.0}})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode) & 0o007, 0)  # not world-readable
        self.assertIn("Moved 2 targets from config.ini to targets.json", self.logs)
        # the next run reads the file, ignores the ini and says so
        self.logs.clear()
        self.assertEqual(self.wd.load_targets(cfg), {1006: 10.0, 1245620: 20.0})
        self.assertEqual(len(self.logs), 1)
        self.assertIn("Ignoring [targets]", self.logs[0])

    def test_one_target_is_singular_and_currency_follows_country(self):
        self.wd.load_targets(make_cfg(self.dir, {"7": "3"}, country="GB"))
        self.assertIn("Moved 1 target from config.ini", self.logs[0])
        self.assertEqual(load_json(self.path)["currency"], "GBP")

    def test_file_exists_wins_over_ini(self):
        self.write({"version": 1, "currency": "USD", "targets": {"1006": 7.5}})
        got = self.wd.load_targets(make_cfg(self.dir, {"1006": "10", "1007": "20"}))
        self.assertEqual(got, {1006: 7.5})
        self.assertEqual(load_json(self.path)["targets"], {"1006": 7.5})  # untouched
        self.assertEqual(len(self.logs), 1)
        self.assertIn("Ignoring [targets]", self.logs[0])

    def test_file_exists_and_no_ini(self):
        self.write({"targets": {"5": 12.5}})
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir)), {5: 12.5})
        self.assertEqual(self.logs, [])

    def test_empty_file_means_no_targets_not_a_migration(self):
        # The user cleared every target on the page; the old ini entries must not come back.
        self.write({"version": 1, "currency": "USD", "targets": {}})
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir, {"1006": "10"})), {})

    def test_file_created_concurrently_by_the_server(self):
        real_link = os.link

        def server_wins(src, dst, *a, **kw):
            self.write({"version": 1, "currency": "USD", "targets": {"1006": 4.0}})  # the server saves first
            return real_link(src, dst, *a, **kw)  # now raises FileExistsError

        with mock.patch("os.link", side_effect=server_wins):
            got = self.wd.load_targets(make_cfg(self.dir, {"1006": "10", "1007": "20"}))
        self.assertEqual(got, {1006: 4.0})                       # the server's file wins
        self.assertEqual(load_json(self.path)["targets"], {"1006": 4.0})
        self.assertEqual(self.files(), ["targets.json"])
        self.assertFalse(any("Moved" in m for m in self.logs))

    def test_dry_run_reads_the_ini_and_writes_nothing(self):
        got = self.wd.load_targets(make_cfg(self.dir, {"1006": "10"}), dry_run=True)
        self.assertEqual(got, {1006: 10.0})
        self.assertEqual(self.files(), [])

    def test_dry_run_with_a_file_reads_the_file(self):
        self.write({"targets": {"1006": 3}})
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir, {"1006": "10"}), dry_run=True), {1006: 3.0})

    def test_filesystem_without_hard_links_falls_back_to_the_ini_for_that_run(self):
        with mock.patch("os.link", side_effect=OSError(errno.EPERM, "no links")):
            got = self.wd.load_targets(make_cfg(self.dir, {"1006": "10"}))
        self.assertEqual(got, {1006: 10.0})
        self.assertEqual(self.files(), [])
        self.assertTrue(any("Couldn't create" in m for m in self.logs))

    def test_damaged_file_is_left_alone_and_reads_as_empty(self):
        self.write("{ not json")
        self.assertEqual(self.wd.load_targets(make_cfg(self.dir, {"1006": "10"})), {})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ not json")
        self.assertTrue(any("Couldn't read" in m for m in self.logs))

    def test_bad_entries_are_skipped(self):
        self.write({"targets": {"1": 5, "2": "x", "3": 0, "4": -2, "five": 3, "6": "7.5"}})
        self.assertEqual(self.wd.read_targets_file(self.path), {1: 5.0, 6: 7.5})

    def test_missing_file_reads_as_none(self):
        self.assertIsNone(self.wd.read_targets_file(self.path))

    def test_data_dir_follows_deals_json(self):
        self.assertEqual(self.wd.targets_path(make_cfg("/srv/x")), "/srv/x/targets.json")

    # ---- evaluate() takes the targets it's given

    def test_evaluate_uses_given_targets_over_the_ini(self):
        games, prices = helpers.sample_games(), helpers.sample_prices()
        cfg = helpers.make_cfg(targets={"1006": "10.00"})
        ini = self.wd.evaluate(games, prices, [], cfg)
        self.assertEqual(ini["deal:1006"]["kind"], "target")
        given = self.wd.evaluate(games, prices, [], cfg, {})
        self.assertEqual(given["deal:1006"]["kind"], "low")
        moved = self.wd.evaluate(games, prices, [], helpers.make_cfg(), {1007: 30.0})
        self.assertEqual(moved["deal:1007"]["kind"], "target")


class WholeRunMigration(unittest.TestCase):
    """What the cron job and the page's Check now button actually do."""

    def setUp(self):
        self.stub = stub_services.Stub()
        self.addCleanup(self.stub.close)
        self.work = stub_services.tempdir()
        self.addCleanup(self.work.cleanup)

    def run_it(self, args, ini):
        code, out, err, snap, data_dir = stub_services.run_watcher(self.stub, self.work.name, args, ini_targets=ini)
        self.assertEqual(code, 0, err)
        return out, err, snap, os.path.join(data_dir, "targets.json")

    def test_snapshot_only_run_migrates_and_the_game_shows_its_target(self):
        _, err, snap, path = self.run_it(["--snapshot-only"], {"1006": "10.00", "1005": "25"})
        self.assertIn("Moved 2 targets from config.ini to targets.json", err)
        self.assertEqual(load_json(path)["targets"], {"1005": 25.0, "1006": 10.0})
        by = {g["appid"]: g for g in snap["games"]}
        self.assertEqual((by[1006]["target"], by[1006]["atTarget"]), (10.0, True))
        self.assertEqual((by[1005]["target"], by[1005]["atTarget"]), (25.0, True))
        self.assertIsNone(by[1001]["target"])

    def test_dry_run_does_not_create_the_file(self):
        _, err, snap, path = self.run_it(["--dry-run"], {"1006": "10.00"})
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("Moved", err)
        self.assertEqual({g["appid"]: g["target"] for g in snap["games"]}[1006], 10.0)

    def test_page_edits_to_the_file_drive_the_next_run(self):
        _, _, _, path = self.run_it(["--snapshot-only"], {"1006": "10.00"})
        doc = load_json(path)
        doc["targets"] = {"1007": 30.0}      # as if saved on the page
        save_json(path, doc)
        out, err, snap, _ = self.run_it(["--all", "--dry-run"], {"1006": "10.00"})
        self.assertIn("Ignoring [targets]", err)
        by = {g["appid"]: g for g in snap["games"]}
        self.assertIsNone(by[1006]["target"])
        self.assertEqual((by[1007]["target"], by[1007]["atTarget"]), (30.0, True))
        self.assertIn("Target Miss: $25.00", out)   # now an alert, under the new $30 target


if __name__ == "__main__":
    unittest.main()
