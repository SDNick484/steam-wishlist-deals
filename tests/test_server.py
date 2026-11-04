"""deals_server.py: the target-price routes, over real HTTP on a free port."""

import http.client
import importlib.util
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from tests import helpers

spec = importlib.util.spec_from_file_location("deals_server_under_test", os.path.join(helpers.ROOT, "deals_server.py"))
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)

PREFIXES = ("/api/deals", "/deals/api/deals")  # direct, and behind the shared hostname's path rule
JSON = {"Content-Type": "application/json"}


class BigBacklogServer(ThreadingHTTPServer):
    request_queue_size = 128  # the default of 5 resets some of the dozens of simultaneous test connections


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.targets = os.path.join(self.dir, "targets.json")
        app = ds.App(os.path.join(helpers.ROOT, "deals.html"), self.dir, "/nonexistent/watcher.py", [], [])
        self.server = BigBacklogServer(("127.0.0.1", 0), ds.make_handler(app))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, body=None, headers=None, raw=None):
        """-> (status, parsed JSON or text). body is JSON-encoded unless raw bytes are given."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = dict(headers if headers is not None else (JSON if data is not None else {}))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=data, headers=hdrs)
            r = c.getresponse()
            text = r.read().decode()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, text
        finally:
            c.close()

    def put(self, appid, price, prefix="/api/deals"):
        return self.call("PUT", f"{prefix}/targets/{appid}", {"price": price})

    def file(self):
        with open(self.targets, encoding="utf-8") as f:
            return json.load(f)


class Routes(ServerCase):
    def test_get_with_no_file(self):
        for p in PREFIXES:
            self.assertEqual(self.call("GET", f"{p}/targets"), (200, {"targets": {}}))
        self.assertFalse(os.path.exists(self.targets))

    def test_set_change_clear_on_both_prefixes(self):
        for p in PREFIXES:
            with self.subTest(prefix=p):
                self.assertEqual(self.put(1245620, 12.5, p), (200, {"appid": 1245620, "price": 12.5, "previous": None}))
                self.assertEqual(self.put(1245620, 9.999, p), (200, {"appid": 1245620, "price": 10.0, "previous": 12.5}))
                st, doc = self.call("GET", f"{p}/targets")
                self.assertEqual((st, doc["targets"]), (200, {"1245620": 10.0}))
                self.assertEqual(self.call("DELETE", f"{p}/targets/1245620"), (200, {"appid": 1245620, "previous": 10.0}))
                self.assertEqual(self.call("DELETE", f"{p}/targets/1245620"), (200, {"appid": 1245620, "previous": None}))
                self.assertEqual(self.call("GET", f"{p}/targets")[1]["targets"], {})

    def test_file_format_and_permissions(self):
        self.put(7, 3)
        doc = self.file()
        self.assertEqual(doc, {"version": 1, "currency": "USD", "targets": {"7": 3}})
        self.assertEqual(stat.S_IMODE(os.stat(self.targets).st_mode) & 0o007, 0)
        self.assertEqual(sorted(os.listdir(self.dir)), ["targets.json"])  # no temp file left

    def test_new_file_takes_the_snapshots_currency(self):
        with open(os.path.join(self.dir, "deals.json"), "w", encoding="utf-8") as f:
            json.dump({"deals": [], "currency": "GBP"}, f)
        self.put(7, 3)
        self.assertEqual(self.file()["currency"], "GBP")

    def test_existing_file_keeps_its_other_fields(self):
        with open(self.targets, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "currency": "EUR", "targets": {"5": 1.5}, "note": "keep"}, f)
        self.put(7, 3)
        self.assertEqual(self.file(), {"version": 1, "currency": "EUR", "targets": {"5": 1.5, "7": 3}, "note": "keep"})

    def test_query_strings_are_ignored(self):
        self.assertEqual(self.call("PUT", "/api/deals/targets/9?x=1", {"price": 2})[0], 200)
        self.assertEqual(self.call("GET", "/api/deals/targets?x=1")[1]["targets"], {"9": 2})

    def test_head_works(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("HEAD", "/api/deals/targets")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual((r.status, r.getheader("Cache-Control")), (200, "no-store"))

    def test_other_routes_still_work(self):
        self.assertEqual(self.call("GET", "/healthz"), (200, "ok"))
        self.assertEqual(self.call("GET", "/api/deals")[0], 404)  # no snapshot yet
        with open(os.path.join(self.dir, "deals.json"), "w", encoding="utf-8") as f:
            f.write('{"deals":[]}')
        self.assertEqual(self.call("GET", "/api/deals"), (200, {"deals": []}))
        self.assertEqual(self.call("GET", "/deals/api/deals"), (200, {"deals": []}))
        for page in ("/", "/deals", "/deals/"):
            st, html = self.call("GET", page)
            self.assertEqual(st, 200)
            self.assertIn("Wishlist Deals", html)

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.call("GET", "/api/deals/targets/5")[0], 404)       # no per-game GET
        self.assertEqual(self.call("PUT", "/api/deals/targets", {"price": 1})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/deals/targets")[0], 404)
        self.assertEqual(self.call("POST", "/api/deals/targets/5", {"price": 1})[0], 404)
        self.assertEqual(self.call("PUT", "/api/deals/targets/5/extra", {"price": 1})[0], 404)
        self.assertEqual(self.call("PUT", "/api/deals", {"price": 1})[0], 404)
        self.assertEqual(self.call("DELETE", "/api/deals/refresh")[0], 404)
        self.assertEqual(self.call("PUT", "/api/deals/targetsx/5", {"price": 1})[0], 404)
        self.assertFalse(os.path.exists(self.targets))


class Validation(ServerCase):
    def bad(self, status, method, path, **kw):
        st, doc = self.call(method, path, **kw)
        self.assertEqual(st, status, doc)
        self.assertIsInstance(doc.get("error"), str)
        self.assertFalse(os.path.exists(self.targets), "a rejected request must not write")

    def test_bad_app_ids(self):
        for appid in ("abc", "0", "-5", "1.5", "99999999999", "%20", "1e3", "0x10", ""):
            with self.subTest(appid=appid):
                if appid == "":
                    self.assertEqual(self.call("PUT", "/api/deals/targets/", {"price": 1})[0], 400)
                else:
                    self.bad(400, "PUT", f"/api/deals/targets/{appid}", body={"price": 1})
                    self.bad(400, "DELETE", f"/api/deals/targets/{appid}")

    def test_bad_prices(self):
        for body in ({"price": 0}, {"price": -1}, {"price": 1000.01}, {"price": 0.004}, {"price": "12"},
                     {"price": True}, {"price": None}, {"price": [1]}, {"cost": 5}, {}, [1], 12, "x", None):
            with self.subTest(body=body):
                self.bad(400, "PUT", "/api/deals/targets/5", body=body if body is not None else None,
                         raw=b"null" if body is None else None)

    def test_non_finite_numbers_in_the_body(self):
        for text in (b'{"price": NaN}', b'{"price": Infinity}', b'{"price": -Infinity}', b'{"price": 1e999}'):
            with self.subTest(body=text):
                self.bad(400, "PUT", "/api/deals/targets/5", raw=text)

    def test_bad_bodies(self):
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b"")                                  # empty
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b"{nope")                             # not JSON
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b'{"price": 5}', headers={})          # no content type
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b'{"price": 5}',
                 headers={"Content-Type": "text/plain"})
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b'{"price": 5, "pad": "' + b"x" * 1100 + b'"}')  # > 1 KB
        self.bad(400, "PUT", "/api/deals/targets/5", raw=b'\xff\xfe{"price": 5}')              # not UTF-8

    def test_accepted_edges(self):
        self.assertEqual(self.put(5, 1000)[1]["price"], 1000.0)
        self.assertEqual(self.put(6, 0.01)[1]["price"], 0.01)
        self.assertEqual(self.put(7, 12)[1]["price"], 12.0)                      # integers are fine
        self.assertEqual(self.call("PUT", "/api/deals/targets/8", raw=b'{"price": 4}',
                                   headers={"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.assertEqual(self.call("PUT", "/api/deals/targets/00012", {"price": 4})[1]["appid"], 12)

    def test_the_5000_target_limit(self):
        with open(self.targets, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "currency": "USD", "targets": {str(i): 1.0 for i in range(1, 5001)}}, f)
        st, doc = self.put(9999, 5)
        self.assertEqual(st, 400)
        self.assertIn("5000", doc["error"])
        self.assertEqual(len(self.file()["targets"]), 5000)
        self.assertEqual(self.put(1, 2.5)[0], 200)                                # changing an existing one is fine
        self.assertEqual(self.call("DELETE", "/api/deals/targets/2")[0], 200)
        self.assertEqual(self.put(9999, 5)[0], 200)                               # room again

    def test_damaged_file_is_never_overwritten(self):
        for broken in ("{ not json", '["a list"]', '{"targets": 5}'):
            with self.subTest(content=broken):
                with open(self.targets, "w", encoding="utf-8") as f:
                    f.write(broken)
                self.assertEqual(self.call("GET", "/api/deals/targets")[0], 500)
                self.assertEqual(self.put(1, 5)[0], 500)
                self.assertEqual(self.call("DELETE", "/api/deals/targets/1")[0], 500)
                with open(self.targets, encoding="utf-8") as f:
                    self.assertEqual(f.read(), broken)

    def test_errors_do_not_leak_paths(self):
        with open(self.targets, "w", encoding="utf-8") as f:
            f.write("{ nope")
        _, doc = self.call("GET", "/api/deals/targets")
        self.assertNotIn(self.dir, json.dumps(doc))


class Concurrency(ServerCase):
    def run_threads(self, fns):
        out, errs = [None] * len(fns), []

        def run(i, fn):
            try:
                out[i] = fn()
            except Exception as e:  # noqa: BLE001 - surfaced below
                errs.append(e)
        ts = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errs, [])
        return out

    def test_many_games_at_once_all_land(self):
        n = 40
        res = self.run_threads([lambda i=i: self.put(1000 + i, 1 + i) for i in range(n)])
        self.assertTrue(all(st == 200 for st, _ in res))
        self.assertEqual(self.file()["targets"], {str(1000 + i): float(1 + i) for i in range(n)})

    def test_same_game_at_once_ends_consistent(self):
        n = 30
        res = self.run_threads([lambda i=i: self.put(5, 1 + i) for i in range(n)])
        prices = {1.0 * (1 + i) for i in range(n)}
        self.assertTrue(all(st == 200 for st, _ in res))
        previous = [doc["previous"] for _, doc in res]
        self.assertEqual(previous.count(None), 1)                        # exactly one write came first
        self.assertTrue(all(p in prices for p in previous if p is not None))
        self.assertEqual(len({p for p in previous if p is not None}), n - 1)  # the writes form one chain
        self.assertIn(self.file()["targets"]["5"], prices)

    def test_puts_and_deletes_mixed_with_reads(self):
        for i in range(10):
            self.put(i + 1, 5)
        fns = [lambda i=i: self.call("DELETE", f"/api/deals/targets/{i + 1}") for i in range(10)]
        fns += [lambda i=i: self.put(100 + i, 2) for i in range(10)]
        fns += [lambda: self.call("GET", "/api/deals/targets") for _ in range(20)]
        res = self.run_threads(fns)
        self.assertTrue(all(st == 200 for st, _ in res))                  # a read never sees a half-written file
        self.assertEqual(set(self.file()["targets"]), {str(100 + i) for i in range(10)})


if __name__ == "__main__":
    unittest.main()
