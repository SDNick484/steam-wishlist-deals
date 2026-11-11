"""Tests for access_jwt.Verifier, with real RS256 signatures made by the openssl command line.

Standard library only. A missing openssl is a skip unless REQUIRE_OPENSSL=1 (CI sets it), when it is a failure.

Reusable by other tests (tests/test_ask.py imports it):

    from tests.test_access_jwt import JwtFixture, TEAM, AUD, good_claims
    fx = JwtFixture(); fx.start()            # generates a key, serves its JWKS on 127.0.0.1
    v = Verifier(TEAM, AUD, certs_url=fx.certs_url)
    token = fx.sign({"alg": "RS256", "kid": "k1", "typ": "JWT"}, good_claims(time.time()))
    ...; fx.stop()

fx.hits counts JWKS requests; fx.modulus is n as bytes. require_openssl() raises SkipTest (or fails) as above.
    python3 -m unittest tests.test_access_jwt
"""
import base64
import contextlib
import io
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import access_jwt
from access_jwt import Verifier

TEAM = "your-team.cloudflareaccess.com"
AUD = "aud-tag-123"
NOW = 1_800_000_000.0


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def good_claims(now, **over):
    c = {"iss": "https://" + TEAM, "aud": [AUD], "email": "player@example.com", "exp": int(now) + 300}
    c.update(over)
    return c


def require_openssl():
    if shutil.which("openssl") is None:
        if os.environ.get("REQUIRE_OPENSSL") == "1":
            raise AssertionError("openssl is required (REQUIRE_OPENSSL=1) but not installed")
        raise unittest.SkipTest("openssl not installed")


class JwtFixture:
    def __init__(self, kid="k1"):
        require_openssl()
        self.kid = kid
        self.hits = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.key = os.path.join(self.tmp.name, "k.pem")
        run = lambda *a, **kw: subprocess.run(["openssl", *a], check=True, capture_output=True, **kw)
        run("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", self.key)
        mod = run("rsa", "-in", self.key, "-noout", "-modulus").stdout.decode().strip()
        self.n = int(mod.split("=", 1)[1], 16)
        self.modulus = self.n.to_bytes((self.n.bit_length() + 7) // 8, "big")
        text = run("rsa", "-in", self.key, "-noout", "-text").stdout.decode()
        assert "65537" in text
        self.jwks = {"keys": [{"kid": kid, "kty": "RSA", "alg": "RS256", "n": b64u(self.modulus), "e": "AQAB"}]}
        fx = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                fx.hits += 1
                body = json.dumps(fx.jwks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.certs_url = "http://127.0.0.1:%d/cdn-cgi/access/certs" % self.server.server_address[1]

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def signature(self, signing_input):
        p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key], input=signing_input,
                           check=True, capture_output=True)
        return p.stdout

    def sign(self, header, payload):
        si = (b64u(json.dumps(header).encode()) + "." + b64u(json.dumps(payload).encode())).encode()
        return si.decode() + "." + b64u(self.signature(si))


class VerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = JwtFixture().start()

    @classmethod
    def tearDownClass(cls):
        cls.fx.stop()

    def setUp(self):
        self.t = NOW
        self.fx.hits = 0
        self.hdr = {"alg": "RS256", "kid": "k1", "typ": "JWT"}

    def verifier(self, emails=frozenset(), team=TEAM, certs_url=None):
        return Verifier(team, AUD, emails, certs_url=certs_url or self.fx.certs_url, now=lambda: self.t)

    def token(self, **over):
        return self.fx.sign(self.hdr, good_claims(self.t, **over))

    def test_good_token(self):
        self.assertEqual(self.verifier().verify(self.token()), ("player@example.com", "ok"))

    def test_no_email_no_allowlist(self):
        c = good_claims(self.t)
        del c["email"]
        self.assertEqual(self.verifier().verify(self.fx.sign(self.hdr, c)), ("access-user", "ok"))

    def test_aud_string(self):
        self.assertEqual(self.verifier().verify(self.token(aud=AUD))[1], "ok")

    def test_wrong_aud(self):
        self.assertEqual(self.verifier().verify(self.token(aud=["other"])), (None, "aud"))

    def test_wrong_iss(self):
        self.assertEqual(self.verifier().verify(self.token(iss="https://evil.example.com")), (None, "iss"))

    def test_expired(self):
        self.assertEqual(self.verifier().verify(self.token(exp=int(self.t) - 61)), (None, "exp"))

    def test_missing_exp(self):
        c = good_claims(self.t)
        del c["exp"]
        self.assertEqual(self.verifier().verify(self.fx.sign(self.hdr, c)), (None, "exp"))

    def test_exp_within_skew(self):
        self.assertEqual(self.verifier().verify(self.token(exp=int(self.t) - 30))[1], "ok")

    def test_not_yet_valid(self):
        self.assertEqual(self.verifier().verify(self.token(nbf=int(self.t) + 61)), (None, "nbf"))

    def test_nbf_within_skew(self):
        self.assertEqual(self.verifier().verify(self.token(nbf=int(self.t) + 30))[1], "ok")

    def test_alg_none(self):
        h = b64u(json.dumps({"alg": "none", "kid": "k1"}).encode())
        p = b64u(json.dumps(good_claims(self.t)).encode())
        self.assertEqual(self.verifier().verify(h + "." + p + "."), (None, "alg"))
        self.assertEqual(self.verifier().verify(h + "." + p + ".AAAA"), (None, "alg"))

    def test_hs256(self):
        h = b64u(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
        p = b64u(json.dumps(good_claims(self.t)).encode())
        mac = hmac.new(self.fx.modulus, (h + "." + p).encode(), hashlib.sha256).digest()
        self.assertEqual(self.verifier().verify(h + "." + p + "." + b64u(mac)), (None, "alg"))

    def test_unknown_kid_refetches_once_then_rate_limited(self):
        v = self.verifier()
        self.assertEqual(v.verify(self.token())[1], "ok")
        self.assertEqual(self.fx.hits, 1)
        self.t += 61
        bad = dict(self.hdr, kid="nope")
        tok = self.fx.sign(bad, good_claims(self.t))
        self.assertEqual(v.verify(tok), (None, "kid"))
        self.assertEqual(self.fx.hits, 2)
        self.t += 1
        self.assertEqual(v.verify(self.fx.sign(bad, good_claims(self.t))), (None, "kid"))
        self.assertEqual(self.fx.hits, 2)

    def test_cache_used_within_ttl(self):
        v = self.verifier()
        v.verify(self.token())
        v.verify(self.token())
        self.assertEqual(self.fx.hits, 1)

    def test_tampered_payload(self):
        h, p, s = self.token().split(".")
        p2 = b64u(json.dumps(good_claims(self.t, email="admin@example.com")).encode())
        self.assertEqual(self.verifier().verify(".".join([h, p2, s])), (None, "sig"))

    def test_tampered_signature(self):
        h, p, s = self.token().split(".")
        raw = bytearray(base64.urlsafe_b64decode(s + "=="))
        raw[-1] ^= 1
        self.assertEqual(self.verifier().verify(".".join([h, p, b64u(bytes(raw))])), (None, "sig"))

    def test_signature_wrong_length(self):
        h, p, s = self.token().split(".")
        raw = base64.urlsafe_b64decode(s + "==")
        self.assertEqual(self.verifier().verify(".".join([h, p, b64u(raw[1:])])), (None, "sig"))
        self.assertEqual(self.verifier().verify(".".join([h, p, b64u(b"\x00" + raw)])), (None, "sig"))

    def test_email_not_allowed(self):
        v = self.verifier(frozenset({"other@example.com"}))
        self.assertEqual(v.verify(self.token()), (None, "email"))

    def test_email_missing_with_allowlist(self):
        c = good_claims(self.t)
        del c["email"]
        v = self.verifier(frozenset({"player@example.com"}))
        self.assertEqual(v.verify(self.fx.sign(self.hdr, c)), (None, "email"))

    def test_email_case_and_spaces(self):
        v = self.verifier(frozenset({"player@example.com"}))
        self.assertEqual(v.verify(self.token(email=" Player@Example.COM")), ("player@example.com", "ok"))

    def test_jwks_unreachable(self):
        v = self.verifier(certs_url="http://127.0.0.1:1/certs")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(v.verify(self.token()), (None, "jwks"))
            self.t += 30
            self.assertEqual(v.verify(self.token()), (None, "jwks"))
        self.assertEqual(err.getvalue().count("JWKS fetch failed"), 1)
        self.assertNotIn("eyJ", err.getvalue())
        self.t += 61
        with contextlib.redirect_stderr(err):
            self.assertEqual(v.verify(self.token()), (None, "jwks"))
        self.assertEqual(err.getvalue().count("JWKS fetch failed"), 2)

    def test_stale_keys_refused_when_refetch_fails(self):
        v = self.verifier()
        self.assertEqual(v.verify(self.token())[1], "ok")
        v.certs_url = "http://127.0.0.1:1/certs"
        self.t += access_jwt.JWKS_TTL - 1
        self.assertEqual(v.verify(self.token())[1], "ok")   # still within the TTL
        self.t += 2
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(v.verify(self.token()), (None, "jwks"))

    def test_token_too_long(self):
        def padded(n):
            return self.token(pad="x" * n)
        n = 7000
        while len(padded(n)) < 8193:
            n += 100
        while len(padded(n)) > 8193:
            n -= 1
        self.assertEqual(len(padded(n)), 8193)
        self.assertEqual(self.verifier().verify(padded(n)), (None, "format"))
        self.assertEqual(len(padded(n - 1)), 8192)
        self.assertEqual(self.verifier().verify(padded(n - 1))[1], "ok")

    def test_malformed(self):
        v = self.verifier()
        for t in ["", "a.b", "a.b.c.d", "!!.!!.!!", b64u(b"[]") + ".e30.AA"]:
            self.assertEqual(v.verify(t)[0], None, t)

    def test_team_with_scheme_and_slash(self):
        v = self.verifier(team="https://" + TEAM + "/")
        self.assertEqual(v.verify(self.token())[1], "ok")


if __name__ == "__main__":
    unittest.main()
