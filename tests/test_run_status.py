"""run_status.json (written by the watcher), the failure and recovery notices, the status route, the deep health
check and the build id."""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests import helpers, stub_services
from tests.test_server import PREFIXES, ServerCase, ds

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
KEYS = {"version", "last_attempt", "last_ok", "consecutive_failures", "error_class"}
FAILING = "Wishlist checks are failing: {}"
RECOVERED = "Wishlist checks are working again"


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self.stub = stub_services.Stub()
        self.addCleanup(self.stub.close)
        tmp = stub_services.tempdir()
        self.work = tmp.name
        self.addCleanup(tmp.cleanup)

    def run_watcher(self, args=(), **kw):
        return stub_services.run_watcher(self.stub, self.work, list(args), **kw)

    @property
    def status_path(self):
        return os.path.join(self.work, "exports", "run_status.json")

    def status(self):
        with open(self.status_path, encoding="utf-8") as f:
            return json.load(f)

    def messages(self):
        return [n["message"] for n in self.stub.notifications]


class StatusFile(WatcherCase):
    def test_a_successful_run_writes_the_documented_shape(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        code, _, err, _, _ = self.run_watcher()
        self.assertEqual(code, 0, err)
        doc = self.status()
        self.assertEqual(set(doc), KEYS)
        self.assertEqual(doc["version"], 1)
        self.assertRegex(doc["last_attempt"], STAMP)
        self.assertEqual(doc["last_ok"], doc["last_attempt"])
        self.assertEqual((doc["consecutive_failures"], doc["error_class"]), (0, None))
        self.assertEqual(stat.S_IMODE(os.stat(self.status_path).st_mode), 0o640)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.status_path))), ["deals.json", "run_status.json"])

    def test_a_run_that_sends_no_alert_is_still_a_success(self):
        self.run_watcher()                       # everything is now marked as sent
        self.run_watcher()
        doc = self.status()
        self.assertEqual((doc["consecutive_failures"], doc["error_class"]), (0, None))

    def test_dry_runs_and_the_quiet_refresh_write_nothing(self):
        for args in (["--dry-run"], ["--all", "--dry-run"], ["--snapshot-only"]):
            with self.subTest(args=args):
                code, _, err, _, _ = self.run_watcher(args)
                self.assertEqual(code, 0, err)
                self.assertFalse(os.path.exists(self.status_path))

    def test_a_failing_dry_run_writes_nothing_and_sends_nothing(self):
        self.stub.fail = {"steam"}
        for _ in range(4):
            code, *_ = self.run_watcher(["--dry-run"], ha=True)
            self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(self.status_path))
        self.assertEqual(self.messages(), [])

    def test_no_config_file_means_nowhere_to_write(self):
        cfg = os.path.join(self.work, "missing.ini")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WD_")}
        env["HOME"] = self.work
        r = subprocess.run([sys.executable, helpers.MODULE_PATH, "--config", cfg], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No config at", r.stderr)
        self.assertEqual(os.listdir(self.work), [])


class Classification(WatcherCase):
    def failed(self, **kw):
        code, _, err, _, _ = self.run_watcher(**kw)
        self.assertNotEqual(code, 0, err)
        doc = self.status()
        self.assertEqual(set(doc), KEYS)
        self.assertEqual(doc["consecutive_failures"], 1)
        self.assertIsNone(doc["last_ok"])
        self.assertRegex(doc["last_attempt"], STAMP)
        return doc["error_class"]

    def test_steam_stage(self):
        self.stub.fail = {"steam"}
        self.assertEqual(self.failed(), "steam")

    def test_itad_stage(self):
        self.stub.fail = {"itad"}
        self.assertEqual(self.failed(), "itad")

    def test_config_stage(self):
        self.assertEqual(self.failed(itad_key="YOUR_ITAD_API_KEY"), "config")

    def test_a_bad_setting_is_a_config_error(self):
        self.assertEqual(self.failed(rules="top_n = lots"), "config")

    def test_notify_stage_when_no_channel_delivers(self):
        self.stub.fail = {"notify"}
        code, _, err, _, _ = self.run_watcher(ha=True)
        self.assertEqual(code, 0, err)            # unchanged: the deals are retried next run, exit status as before
        doc = self.status()
        self.assertEqual((doc["error_class"], doc["consecutive_failures"]), ("notify", 1))
        self.assertIn("will be retried", err)

    def test_other_is_the_default(self):
        wd = helpers.load_watcher()
        wd.STEAM_API = wd.ITAD_API = wd.STORE_API = wd.PROTONDB_API = self.stub.url
        wd.STATE_PATH = os.path.join(self.work, "state", "state.json")
        wd.ITAD_PACE = 0
        cfg, _ = stub_services.write_config(self.work)
        with mock.patch.object(sys, "argv", ["wishlist_deals.py", "--config", cfg]), \
                mock.patch.object(wd, "build_games", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            wd.main()
        doc = self.status()
        self.assertEqual((doc["error_class"], doc["consecutive_failures"]), ("other", 1))
        self.assertNotIn("boom", json.dumps(doc))

    def test_no_error_text_reaches_the_file(self):
        self.stub.fail = {"steam"}
        self.failed()
        with open(self.status_path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("403", text)
        self.assertNotIn("HTTP", text)

    def test_stage_classes_are_the_documented_ones(self):
        wd = helpers.load_watcher()
        self.assertEqual(wd.ERROR_CLASSES, ("steam", "itad", "notify", "config", "other"))
        st = wd.Stage()
        for name, want in (("steam", "steam"), ("itad", "itad"), ("config", "config"), ("other", "other"),
                           ("notify", "notify"), ("weird", "other")):
            st.name = name
            self.assertEqual(st.error_class(), want)


class Counting(WatcherCase):
    def test_a_failure_keeps_last_ok_and_counts(self):
        self.run_watcher()
        ok = self.status()
        self.stub.fail = {"steam"}
        for n in (1, 2):
            self.run_watcher()
            doc = self.status()
            self.assertEqual((doc["consecutive_failures"], doc["error_class"]), (n, "steam"))
            self.assertEqual(doc["last_ok"], ok["last_ok"])
            self.assertRegex(doc["last_attempt"], STAMP)

    def test_success_resets_the_counter_and_the_class(self):
        self.stub.fail = {"steam"}
        self.run_watcher()
        self.stub.fail = set()
        self.run_watcher()
        doc = self.status()
        self.assertEqual((doc["consecutive_failures"], doc["error_class"]), (0, None))
        self.assertEqual(doc["last_ok"], doc["last_attempt"])

    def test_a_damaged_file_starts_over(self):
        os.makedirs(os.path.dirname(self.status_path))
        with open(self.status_path, "w", encoding="utf-8") as f:
            f.write("{ nope")
        self.run_watcher()
        self.assertEqual(self.status()["consecutive_failures"], 0)


class Notices(WatcherCase):
    def streak(self, n, fail=("steam",)):
        self.stub.fail = set(fail)
        for _ in range(n):
            self.run_watcher(ha=True)

    def test_one_failure_notice_at_exactly_the_third_failure(self):
        self.run_watcher(ha=True)                         # a good run first; it sends the digest
        before = len(self.stub.notifications)
        self.streak(2)
        self.assertEqual(len(self.stub.notifications), before)
        self.streak(1)                                    # the 3rd consecutive failure: the notice goes out
        self.assertEqual(self.messages().count(FAILING.format("steam")), 1)
        self.assertEqual(self.status()["consecutive_failures"], 3)
        self.streak(2)
        self.assertEqual(self.status()["consecutive_failures"], 5)
        self.assertEqual(self.messages().count(FAILING.format("steam")), 1)    # not on every later failure
        self.assertEqual(self.messages().count(RECOVERED), 0)

    def test_notice_names_the_class_of_the_third_failure(self):
        self.stub.fail = {"steam"}
        self.run_watcher(ha=True)
        self.run_watcher(ha=True)
        self.stub.fail = {"itad"}
        self.run_watcher(ha=True)
        self.assertEqual(self.messages(), [FAILING.format("itad")])

    def test_one_recovery_notice_after_a_notified_streak(self):
        self.run_watcher(ha=True)
        self.streak(3)
        self.stub.fail = set()
        self.run_watcher(ha=True)
        self.assertEqual(self.messages().count(RECOVERED), 1)
        self.run_watcher(ha=True)
        self.run_watcher(ha=True)
        self.assertEqual(self.messages().count(RECOVERED), 1)
        self.assertEqual(self.messages().count(FAILING.format("steam")), 1)

    def test_no_recovery_notice_after_a_short_streak(self):
        self.run_watcher(ha=True)
        self.streak(2)
        self.stub.fail = set()
        self.run_watcher(ha=True)
        self.assertNotIn(RECOVERED, self.messages())
        self.assertNotIn(FAILING.format("steam"), self.messages())

    def test_a_streak_that_restarts_notifies_again(self):
        self.streak(3)
        self.stub.fail = set()
        self.run_watcher(ha=True)
        self.streak(3)
        self.assertEqual(self.messages().count(FAILING.format("steam")), 2)

    def test_the_notice_failing_is_logged_and_the_run_carries_on(self):
        self.stub.fail = {"steam", "notify"}
        for _ in range(2):
            self.run_watcher(ha=True)
        code, _, err, _, _ = self.run_watcher(ha=True)
        self.assertEqual(self.status()["consecutive_failures"], 3)
        self.assertIn("Home Assistant notice failed", err)
        self.assertIn("failed: HTTP 403", err)             # the run's own error is still what ends it
        self.assertNotEqual(code, 0)

    def test_no_channel_configured_is_fine(self):
        self.stub.fail = {"steam"}
        for _ in range(3):
            self.run_watcher()
        self.assertEqual(self.status()["consecutive_failures"], 3)
        self.assertEqual(self.messages(), [])


class StatusRoute(ServerCase):
    def write_status(self, doc):
        with open(os.path.join(self.dir, "run_status.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def test_default_when_there_is_no_file_on_both_prefixes(self):
        want = {"version": 1, "last_attempt": None, "last_ok": None, "consecutive_failures": 0, "error_class": None}
        for p in PREFIXES:
            self.assertEqual(self.call("GET", f"{p}/status"), (200, want))
            self.assertEqual(self.call("GET", f"{p}/status?x=1"), (200, want))

    def test_serves_the_files_content_on_both_prefixes(self):
        doc = {"version": 1, "last_attempt": "2026-11-10T19:40:03Z", "last_ok": "2026-11-09T19:40:02Z",
               "consecutive_failures": 2, "error_class": "itad"}
        self.write_status(doc)
        for p in PREFIXES:
            self.assertEqual(self.call("GET", f"{p}/status"), (200, doc))

    def test_unusable_content_falls_back_to_the_default(self):
        for bad in ('{ nope', '[]', '{"consecutive_failures": "3"}', '{"consecutive_failures": -1}', '{}'):
            with self.subTest(content=bad):
                with open(os.path.join(self.dir, "run_status.json"), "w", encoding="utf-8") as f:
                    f.write(bad)
                self.assertEqual(self.call("GET", "/api/deals/status")[1]["consecutive_failures"], 0)
                self.assertIsNone(self.call("GET", "/api/deals/status")[1]["last_attempt"])

    def test_unknown_classes_and_extra_keys_are_dropped(self):
        self.write_status({"version": 9, "last_attempt": "2026-11-10T19:40:03Z", "last_ok": None,
                           "consecutive_failures": 1, "error_class": "/srv/secret/path", "extra": 1})
        st, doc = self.call("GET", "/api/deals/status")
        self.assertEqual(set(doc), KEYS)
        self.assertIsNone(doc["error_class"])

    def test_status_is_not_cached(self):
        self.assertEqual(self.call("GET", "/api/deals/status")[0], 200)
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/deals/api/deals/status")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.getheader("Cache-Control"), "no-store")


class DeepHealth(ServerCase):
    def stamp(self, ago_seconds):
        return (datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write_status(self, last_ok, failures=0, cls=None):
        with open(os.path.join(self.dir, "run_status.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "last_attempt": self.stamp(5), "last_ok": last_ok,
                       "consecutive_failures": failures, "error_class": cls}, f)

    def deep(self, prefix=""):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", f"{prefix}/healthz?deep=1")
        r = c.getresponse()
        body = json.loads(r.read().decode())
        c.close()
        return r.status, body, r

    def test_plain_healthz_is_unchanged_on_both_paths(self):
        self.assertEqual(self.call("GET", "/healthz"), (200, "ok"))
        self.assertEqual(self.call("GET", "/deals/healthz"), (200, "ok"))
        self.assertEqual(self.call("GET", "/healthz?deep=0"), (200, "ok"))
        self.assertEqual(self.call("GET", "/healthz?x=deep=1"), (200, "ok"))

    def test_nothing_exists(self):
        for p in ("", "/deals"):
            st, body, r = self.deep(p)
            self.assertEqual(st, 503)
            self.assertEqual(body, {"ok": False, "build": ds.build_id(), "last_ok": None, "age_seconds": None,
                                    "consecutive_failures": 0})
            self.assertEqual(r.getheader("Content-Type"), "application/json")
            self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_healthy(self):
        ok = self.stamp(3600)
        self.write_status(ok)
        for p in ("", "/deals"):
            st, body, _ = self.deep(p)
            self.assertEqual(st, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["last_ok"], ok)
            self.assertTrue(3590 <= body["age_seconds"] <= 3700, body)
            self.assertEqual(body["consecutive_failures"], 0)
            self.assertEqual(set(body), {"ok", "build", "last_ok", "age_seconds", "consecutive_failures"})

    def test_stale(self):
        self.write_status(self.stamp(37 * 3600))
        st, body, _ = self.deep()
        self.assertEqual((st, body["ok"]), (503, False))
        self.assertGreater(body["age_seconds"], 36 * 3600)

    def test_a_failed_last_run_is_unhealthy_even_with_recent_data(self):
        self.write_status(self.stamp(60), failures=1, cls="steam")
        st, body, _ = self.deep()
        self.assertEqual((st, body["ok"], body["consecutive_failures"]), (503, False, 1))

    def test_never_succeeded(self):
        self.write_status(None, failures=2, cls="itad")
        st, body, _ = self.deep()
        self.assertEqual((st, body["ok"], body["last_ok"], body["age_seconds"]), (503, False, None, None))

    def test_the_36_hour_boundary(self):
        app = ds.App("page", self.dir, "w", [], [])
        now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
        for age, want in ((36 * 3600, 200), (36 * 3600 + 1, 503), (0, 200)):
            with self.subTest(age=age):
                self.write_status((now - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                st, body = app.deep_health(now)
                self.assertEqual((st, body["age_seconds"]), (want, age))

    def test_without_a_status_file_the_snapshots_mtime_stands_in(self):
        path = os.path.join(self.dir, "deals.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"deals":[]}')
        st, body, _ = self.deep()
        self.assertEqual(st, 200)
        self.assertRegex(body["last_ok"], STAMP)
        self.assertEqual(body["consecutive_failures"], 0)
        old = datetime.now().timestamp() - 40 * 3600
        os.utime(path, (old, old))
        st, body, _ = self.deep("/deals")
        self.assertEqual((st, body["ok"]), (503, False))
        self.assertGreater(body["age_seconds"], 36 * 3600)

    def test_a_damaged_status_file_falls_back_to_the_snapshot(self):
        with open(os.path.join(self.dir, "run_status.json"), "w", encoding="utf-8") as f:
            f.write("{ nope")
        with open(os.path.join(self.dir, "deals.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertEqual(self.deep()[0], 200)

    def test_the_status_file_wins_over_a_fresh_snapshot(self):
        with open(os.path.join(self.dir, "deals.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.write_status(self.stamp(50 * 3600))
        self.assertEqual(self.deep()[0], 503)


class Build(unittest.TestCase):
    ROOT = helpers.ROOT

    def load(self, path):
        spec = importlib.util.spec_from_file_location("deals_server_build_check", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_an_unexpanded_placeholder_reads_as_unknown(self):
        mod = self.load(os.path.join(self.ROOT, "deals_server.py"))
        self.assertTrue(mod.BUILD.startswith("$Format"))
        self.assertEqual(mod.build_id(), "unknown")

    def test_the_file_is_marked_export_subst(self):
        with open(os.path.join(self.ROOT, ".gitattributes"), encoding="utf-8") as f:
            lines = [ln.split() for ln in f if ln.strip() and not ln.startswith("#")]
        self.assertIn(["deals_server.py", "export-subst"], lines)
        with open(os.path.join(self.ROOT, "deals_server.py"), encoding="utf-8") as f:
            self.assertEqual(sum(1 for ln in f if re.match(r'^BUILD = "\$Format:%h\$"$', ln)), 1)

    def test_git_archive_substitutes_the_short_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = os.path.join(tmp, "repo"), os.path.join(tmp, "out")
            os.makedirs(repo)
            os.makedirs(out)
            for name in ("deals_server.py", ".gitattributes"):
                shutil.copy(os.path.join(self.ROOT, name), repo)

            def git(*args, cwd=repo):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                                      env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}).stdout
            git("init", "-q")
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
            short = git("rev-parse", "--short", "HEAD").strip()
            archive = os.path.join(tmp, "a.tar")
            git("archive", "-o", archive, "HEAD")
            subprocess.run(["tar", "-xf", archive, "-C", out], check=True)
            mod = self.load(os.path.join(out, "deals_server.py"))
            self.assertEqual(mod.BUILD, short)
            self.assertEqual(mod.build_id(), short)
            self.assertRegex(short, r"^[0-9a-f]{7,}$")


if __name__ == "__main__":
    unittest.main()
