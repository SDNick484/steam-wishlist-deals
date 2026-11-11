"""Cloudflare Access JWT verification (the Cf-Access-Jwt-Assertion header), standard library only.
Copied from steam-backlog-triage 7468859 (last changed there in 586e809); change it there first.

    v = Verifier("your-team.cloudflareaccess.com", "<aud tag>", frozenset({"player@example.com"}))
    who, reason = v.verify(token)     # who is the lower-cased email, "access-user", or None; reason is a short code

Only RS256 is accepted. The signature check recomputes the whole EMSA-PKCS1-v1_5 encoding and compares it with
hmac.compare_digest; the padding of the decrypted signature is never parsed. Token contents are never logged or
returned: a failure yields only a reason code ("format", "alg", "kid", "jwks", "sig", "iss", "aud", "exp", "nbf",
"email").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.request
from typing import Callable

SKEW = 60
JWKS_TTL = 3600
REFETCH_MIN = 60
MAX_TOKEN = 8192
DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64u(s: str) -> bytes:
    if not isinstance(s, str) or not s.isascii():
        raise ValueError("b64")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s) -> int:
    return int.from_bytes(_b64u(s), "big")


class Verifier:
    def __init__(self, team: str, aud: str, emails: frozenset = frozenset(), *, certs_url: str | None = None,
                 now: Callable[[], float] = time.time, fetch_timeout: float = 5.0):
        team = team.strip()
        if team.startswith("https://"):
            team = team[len("https://"):]
        self.team = team.rstrip("/")
        self.aud = aud
        self.emails = frozenset(e.strip().lower() for e in emails)
        self.certs_url = certs_url or "https://%s/cdn-cgi/access/certs" % self.team
        self.now = now
        self.fetch_timeout = fetch_timeout
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched: float | None = None   # time of the last attempt, successful or not
        self._keys_at: float | None = None   # time of the last successful fetch
        self._logged = -1e18

    def _fetch(self) -> None:
        """Refresh the key cache; the caller holds the lock. A failure keeps the old keys and logs once a minute."""
        t = self.now()
        self._fetched = t
        try:
            with urllib.request.urlopen(self.certs_url, timeout=self.fetch_timeout) as r:
                doc = json.loads(r.read(1 << 20))
            keys = {}
            for k in doc["keys"]:
                if k.get("kty") == "RSA" and isinstance(k.get("kid"), str):
                    keys[k["kid"]] = (_int(k["n"]), _int(k["e"]))
            self._keys, self._keys_at = keys, t
        except Exception as e:
            if t - self._logged >= 60:
                self._logged = t
                print("access_jwt: JWKS fetch failed (%s)" % type(e).__name__, file=sys.stderr)

    def _key(self, kid: str):
        """(n, e) for kid, or a reason string."""
        with self._lock:
            t = self.now()
            if self._keys_at is None or t - self._keys_at >= JWKS_TTL:
                if self._fetched is None or t - self._fetched >= REFETCH_MIN:
                    self._fetch()
            elif kid not in self._keys and t - self._fetched >= REFETCH_MIN:
                self._fetch()
            if self._keys_at is None or t - self._keys_at >= JWKS_TTL:
                return "jwks"   # no grace period: a cache past its TTL that could not be refreshed is unusable
            return self._keys[kid] if kid in self._keys else "kid"

    def verify(self, token: str) -> tuple[str | None, str]:
        def fail(reason):
            return None, reason

        if not isinstance(token, str) or len(token) > MAX_TOKEN:
            return fail("format")
        parts = token.split(".")
        if len(parts) != 3:
            return fail("format")
        try:
            header = json.loads(_b64u(parts[0]))
            claims = json.loads(_b64u(parts[1]))
            sig = _b64u(parts[2])
        except Exception:
            return fail("format")
        if not isinstance(header, dict) or not isinstance(claims, dict):
            return fail("format")
        if header.get("alg") != "RS256":
            return fail("alg")
        kid = header.get("kid")
        if not isinstance(kid, str):
            return fail("kid")
        key = self._key(kid)
        if isinstance(key, str):
            return fail(key)
        n, e = key
        k = (n.bit_length() + 7) // 8
        if len(sig) != k:
            return fail("sig")
        s = int.from_bytes(sig, "big")
        if s >= n:
            return fail("sig")
        em = pow(s, e, n).to_bytes(k, "big")
        t = DIGEST_INFO_SHA256 + hashlib.sha256((parts[0] + "." + parts[1]).encode("ascii")).digest()
        pad = k - len(t) - 3
        want = b"\x00\x01" + b"\xff" * max(pad, 0) + b"\x00" + t
        if pad < 8 or not hmac.compare_digest(em, want):
            return fail("sig")

        if claims.get("iss") != "https://" + self.team:
            return fail("iss")
        aud = claims.get("aud")
        if not (aud == self.aud if isinstance(aud, str) else isinstance(aud, list) and self.aud in aud):
            return fail("aud")
        now = self.now()
        exp, nbf = claims.get("exp"), claims.get("nbf")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not now < exp + SKEW:
            return fail("exp")
        if nbf is not None and (isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or now < nbf - SKEW):
            return fail("nbf")
        email = claims.get("email")
        email = email.strip().lower() if isinstance(email, str) and email.strip() else None
        if self.emails:
            if email not in self.emails:
                return fail("email")
            return email, "ok"
        return email or "access-user", "ok"
