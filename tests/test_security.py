"""Same-origin guard on every mutating route, and the headers every response carries."""

import http.client
import os
import unittest

from tests.test_server import JSON, PREFIXES, ServerCase, ds


class Headers(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class CrossSiteFunction(unittest.TestCase):
    def check(self, headers):
        return ds.cross_site_request(Headers(headers))

    def test_no_headers_pass(self):
        self.assertFalse(self.check({}))
        self.assertFalse(self.check({"Host": "deals.example.com"}))

    def test_origin_matching_host_passes(self):
        self.assertFalse(self.check({"Origin": "https://deals.example.com", "Host": "deals.example.com"}))
        self.assertFalse(self.check({"Origin": "http://127.0.0.1:8485", "Host": "127.0.0.1:8485"}))
        self.assertFalse(self.check({"Origin": " HTTPS://Deals.Example.com ", "Host": "deals.EXAMPLE.com"}))

    def test_origin_for_another_host_or_port_is_cross_site(self):
        self.assertTrue(self.check({"Origin": "https://evil.example.net", "Host": "deals.example.com"}))
        self.assertTrue(self.check({"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:8485"}))
        self.assertTrue(self.check({"Origin": "https://deals.example.com.evil.net", "Host": "deals.example.com"}))
        self.assertTrue(self.check({"Origin": "https://deals.example.com"}))  # no Host to match

    def test_origin_null_and_junk_are_cross_site(self):
        for o in ("null", "NULL", "file://", "not a url", "http://[bad"):
            with self.subTest(origin=o):
                self.assertTrue(self.check({"Origin": o, "Host": "deals.example.com"}))

    def test_sec_fetch_site(self):
        self.assertTrue(self.check({"Sec-Fetch-Site": "cross-site"}))
        self.assertTrue(self.check({"Sec-Fetch-Site": " Cross-Site "}))
        for v in ("same-origin", "same-site", "none"):
            with self.subTest(value=v):
                self.assertFalse(self.check({"Sec-Fetch-Site": v}))

    def test_cross_site_fetch_metadata_wins_over_a_matching_origin(self):
        self.assertTrue(self.check({"Origin": "https://deals.example.com", "Host": "deals.example.com",
                                    "Sec-Fetch-Site": "cross-site"}))


class MutatingRoutes(ServerCase):
    """Every PUT, DELETE and POST route, on both path prefixes, with and without browser headers."""

    def routes(self, prefix):
        return (("PUT", f"{prefix}/targets/42", {"price": 5}),
                ("DELETE", f"{prefix}/targets/42", None),
                ("POST", f"{prefix}/refresh", None))

    def host(self):
        return f"127.0.0.1:{self.port}"

    def hdrs(self, extra, method, body):
        h = dict(JSON) if body is not None else {}
        h.update(extra)
        return h

    def refused(self, extra):
        for prefix in PREFIXES:
            for method, path, body in self.routes(prefix):
                with self.subTest(method=method, path=path, headers=extra):
                    st, doc = self.call(method, path, body, headers=self.hdrs(extra, method, body))
                    self.assertEqual((st, doc), (403, {"error": "cross-site request refused"}))
        self.assertFalse(os.path.exists(self.targets), "a refused request must not write")

    def test_cross_origin_is_refused_everywhere(self):
        self.refused({"Origin": "https://evil.example.net"})

    def test_origin_null_is_refused_everywhere(self):
        self.refused({"Origin": "null"})

    def test_cross_site_fetch_metadata_is_refused_everywhere(self):
        self.refused({"Sec-Fetch-Site": "cross-site"})

    def test_a_bodyless_cross_site_refresh_never_runs_the_watcher(self):
        # The watcher path doesn't exist here; reaching it would be a 502, not a 403.
        for prefix in PREFIXES:
            st, _ = self.call("POST", f"{prefix}/refresh", headers={"Origin": "https://evil.example.net"})
            self.assertEqual(st, 403)

    def test_same_origin_passes(self):
        for extra in ({"Origin": f"http://{self.host()}"}, {"Sec-Fetch-Site": "same-origin"},
                      {"Sec-Fetch-Site": "same-site"}, {}):
            for prefix in PREFIXES:
                with self.subTest(headers=extra, prefix=prefix):
                    h = dict(JSON, **extra)
                    self.assertEqual(self.call("PUT", f"{prefix}/targets/42", {"price": 5}, headers=h)[0], 200)
                    self.assertEqual(self.call("DELETE", f"{prefix}/targets/42", headers=extra)[0], 200)
                    # Past the guard the refresh runs; the watcher path is missing here, so it fails with 502.
                    self.assertEqual(self.call("POST", f"{prefix}/refresh", headers=extra)[0], 502)

    def test_no_headers_pass(self):
        self.assertEqual(self.put(42, 5)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/deals/targets/42")[0], 200)

    def test_get_is_not_guarded(self):
        self.assertEqual(self.call("GET", "/api/deals/targets", headers={"Origin": "https://evil.example.net"})[0], 200)

    def test_guard_runs_before_routing(self):
        st, _ = self.call("PUT", "/nope", {"price": 1}, headers=dict(JSON, Origin="null"))
        self.assertEqual(st, 403)


class ResponseHeaders(ServerCase):
    WANT = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "frame-ancestors 'none'"}

    def raw(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            r.read()
            return r.status, r
        finally:
            c.close()

    def assert_headers(self, status, r, want_status):
        self.assertEqual(status, want_status)
        for k, v in self.WANT.items():
            self.assertEqual(r.getheader(k), v, f"{k} on {want_status}")
        self.assertEqual(r.getheader("Content-Security-Policy").count("script-src"), 0)

    def test_on_200_400_403_404(self):
        with open(os.path.join(self.dir, "deals.json"), "w", encoding="utf-8") as f:
            f.write('{"deals":[]}')
        cases = (
            ("GET", "/", None, {}, 200),
            ("GET", "/deals", None, {}, 200),
            ("GET", "/healthz", None, {}, 200),
            ("GET", "/api/deals", None, {}, 200),
            ("GET", "/api/deals/targets", None, {}, 200),
            ("HEAD", "/api/deals/targets", None, {}, 200),
            ("PUT", "/api/deals/targets/abc", b'{"price": 1}', JSON, 400),
            ("PUT", "/api/deals/targets/5", b"{nope", JSON, 400),
            ("PUT", "/api/deals/targets/5", b'{"price": 1}', dict(JSON, Origin="null"), 403),
            ("POST", "/api/deals/refresh", None, {"Sec-Fetch-Site": "cross-site"}, 403),
            ("DELETE", "/deals/api/deals/targets/5", None, {"Origin": "https://evil.example.net"}, 403),
            ("GET", "/nope", None, {}, 404),
            ("PUT", "/api/deals", b'{"price": 1}', JSON, 404),
            ("POST", "/nope", None, {}, 404),
            ("OPTIONS", "/api/deals", None, {}, 501),     # http.server's own error page
        )
        for method, path, body, hdrs, want in cases:
            with self.subTest(method=method, path=path, want=want):
                st, r = self.raw(method, path, body, hdrs)
                self.assert_headers(st, r, want)

    def test_404_for_a_missing_snapshot(self):
        st, r = self.raw("GET", "/api/deals")
        self.assert_headers(st, r, 404)

    def test_caching_headers_are_unchanged(self):
        for path in ("/", "/api/deals/targets", "/healthz"):
            st, r = self.raw("GET", path)
            self.assertEqual(r.getheader("Cache-Control"), "no-store", path)
            self.assertIsNone(r.getheader("ETag"))


if __name__ == "__main__":
    unittest.main()
