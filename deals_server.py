#!/usr/bin/env python3
"""
deals_server.py - serve the Wishlist Deals page.

Standard library only. Serves:
  GET  /                    the deals page (also at /deals)
  GET  /api/deals           the latest deals snapshot written by wishlist_deals.py
  POST /api/deals/refresh   re-run the watcher quietly (no notifications) to refresh the snapshot
  GET    /api/deals/targets          the per-game target prices (targets.json in the data directory)
  PUT    /api/deals/targets/APPID    set one: body {"price": 12.5}
  DELETE /api/deals/targets/APPID    clear one
  GET  /api/deals/status    the last watcher run (run_status.json in the data directory)
  GET  /healthz             "ok"
  GET  /healthz?deep=1      JSON: 200 when the last check succeeded and is under 36 hours old, else 503
Every /api/deals/... route also answers under /deals/api/deals/..., so one path rule on a shared
hostname ("/deals" and "/api/deals" go here) covers the page and everything it calls.

There is no authentication. Bind it to your LAN or a VPN, or put it behind an
authenticating proxy (for example Cloudflare Access); don't expose it directly.
"Check now" runs wishlist_deals.py as the server's user, so that user needs the
watcher's config (~/.config/wishlist-deals/config.ini).

Usage:
  deals_server.py [--port 8485] [--bind 0.0.0.0] [--data DIR] [--check-times 10:40,19:40]
                  [--nav "Backlog=https://backlog.example.com/"] ...
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pwd
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
    "padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px/1.5 system-ui,"
    "-apple-system,'Segoe UI',sans-serif}img{max-width:100%}[hidden]{display:none!important}"
    "</style>{config}</head><body>"
)
SKELETON_TAIL = "</body></html>"

TARGETS_RE = re.compile(r"^(?:/deals)?/api/deals/targets(?:/([^/]*))?$")
# Short commit the code came from. `git archive` fills this in (export-subst, see .gitattributes), and
# install/alpine.sh stamps the installed copy. A checkout that was never archived or installed keeps the
# placeholder, which build_id() reports as "unknown".
BUILD = "$Format:%h$"
STALE_AFTER = 36 * 3600     # seconds without a successful check before the data counts as stale
RUN_STATUS_FILE = "run_status.json"
ERROR_CLASSES = ("steam", "itad", "notify", "config", "other")
NO_RUN_STATUS = {"version": 1, "last_attempt": None, "last_ok": None, "consecutive_failures": 0, "error_class": None}
STATUS_RE = re.compile(r"^(?:/deals)?/api/deals/status$")
HEALTHZ_RE = re.compile(r"^(?:/deals)?/healthz$")

# Sent with every response. No script-src: the page's own inline script has to run.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)
MAX_BODY = 1024          # bytes; a target is one small JSON object
MAX_TARGETS = 5000
MAX_PRICE = 1000.0


def cross_site_request(headers) -> bool:
    """True when a browser says this request comes from another site.

    Origin present and its host[:port] is not the request's Host, or Sec-Fetch-Site is "cross-site".
    Origin "null" counts as cross-site. Requests with neither header (curl, cron) are not cross-site.
    """
    origin = (headers.get("Origin") or "").strip().lower()
    if origin:
        host = (headers.get("Host") or "").strip().lower()
        try:
            origin_host = urlsplit(origin).netloc
        except ValueError:
            return True
        if not origin_host or origin_host != host:
            return True
    return (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site"


def build_id() -> str:
    """The short commit, or "unknown" unless BUILD is a lowercase hex hash of 4 to 40 digits."""
    text = BUILD.strip()
    return text if re.fullmatch(r"[0-9a-f]{4,40}", text) else "unknown"


def parse_utc(text) -> datetime | None:
    """An ISO 8601 time like 2026-11-10T19:40:03Z (or with +00:00) as an aware datetime, else None."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


class BadRequest(Exception):
    """A request the client got wrong; the message is safe to send back."""


class TargetsUnreadable(Exception):
    """targets.json exists but isn't valid. Writes stop so the file isn't overwritten."""


def _reject_constant(name):
    raise ValueError(f"{name} is not a number")


def parse_appid(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", text or "") or int(text) < 1:
        raise BadRequest("app id must be a positive whole number")
    return int(text)


def parse_price(raw: bytes) -> float:
    """The price from a PUT body {"price": number}, rounded to cents."""
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise BadRequest("body must be JSON like {\"price\": 12.5}")
    price = doc.get("price") if isinstance(doc, dict) else None
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise BadRequest("price must be a number")
    price = round(float(price), 2)
    if price <= 0 or price > MAX_PRICE:
        raise BadRequest(f"price must be more than 0 and at most {MAX_PRICE:g}")
    return price


def config_script(cfg: dict) -> str:
    """Expose server settings (page links, check times) to the page as window.APP_CONFIG."""
    js = json.dumps(cfg).replace("</", "<\\/")
    return f"<script>window.APP_CONFIG={js};</script>"


class App:
    def __init__(self, page: str, data_dir: str, watcher: str, nav: list[dict], check_times: list[str]):
        self.page = page
        self.watcher = watcher
        self.deals = os.path.join(data_dir, "deals.json")
        self.head = SKELETON_HEAD.replace("{config}", config_script({"nav": nav, "checkTimes": check_times}))
        self.refresh_lock = threading.Lock()
        self.targets_file = os.path.join(data_dir, "targets.json")
        self.status_file = os.path.join(data_dir, RUN_STATUS_FILE)
        self.targets_lock = threading.Lock()  # one read-modify-write at a time

    # ---- target prices. The watcher reads targets.json; this server is the only thing that edits it.

    def _read_targets(self) -> dict | None:
        """The parsed file, or None if there isn't one yet."""
        try:
            with open(self.targets_file, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise TargetsUnreadable()
        if not isinstance(doc, dict) or not isinstance(doc.get("targets"), dict):
            raise TargetsUnreadable()
        return doc

    def _currency(self) -> str:
        """Currency for a new targets file: whatever the deals snapshot uses."""
        try:
            with open(self.deals, encoding="utf-8") as f:
                cur = json.load(f).get("currency")
            if isinstance(cur, str) and re.fullmatch(r"[A-Z]{3}", cur):
                return cur
        except (OSError, ValueError, AttributeError):
            pass
        return "USD"

    def _write_targets(self, doc: dict) -> None:
        tmp = self.targets_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.targets_file)

    def get_targets(self) -> dict:
        with self.targets_lock:
            return self._read_targets() or {"targets": {}}

    def set_target(self, appid: int, price: float) -> dict:
        with self.targets_lock:
            doc = self._read_targets() or {"version": 1, "currency": self._currency(), "targets": {}}
            key = str(appid)
            if key not in doc["targets"] and len(doc["targets"]) >= MAX_TARGETS:
                raise BadRequest(f"too many targets (the limit is {MAX_TARGETS})")
            previous = doc["targets"].get(key)
            doc["targets"][key] = price
            self._write_targets(doc)
        return {"appid": appid, "price": price, "previous": previous}

    def clear_target(self, appid: int) -> dict:
        with self.targets_lock:
            doc = self._read_targets()
            previous = (doc or {"targets": {}})["targets"].pop(str(appid), None)
            if doc is not None and previous is not None:
                self._write_targets(doc)
        return {"appid": appid, "previous": previous}

    # ---- the watcher's last run. The watcher writes run_status.json; this server only reads it.

    def run_status(self) -> dict | None:
        """The file's content, cleaned to the documented shape; None when it is missing or unusable."""
        try:
            with open(self.status_file, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        failures = doc.get("consecutive_failures")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            return None
        text = lambda v: v if isinstance(v, str) and len(v) <= 40 else None  # noqa: E731
        return {"version": 1, "last_attempt": text(doc.get("last_attempt")), "last_ok": text(doc.get("last_ok")),
                "consecutive_failures": failures,
                "error_class": doc.get("error_class") if doc.get("error_class") in ERROR_CLASSES else None}

    def status_doc(self) -> dict:
        return self.run_status() or dict(NO_RUN_STATUS)

    def deep_health(self, now: datetime | None = None) -> tuple[int, dict]:
        """(status, body) for /healthz?deep=1. Healthy = the last run succeeded and a check finished in 36 h."""
        now = now or datetime.now(timezone.utc)
        status = self.run_status()
        if status is not None:
            last_ok, failures = status["last_ok"], status["consecutive_failures"]
        else:  # no status file (an older watcher, or none yet): the snapshot's own age stands in
            failures = 0
            try:
                last_ok = datetime.fromtimestamp(os.stat(self.deals).st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                last_ok = None
        when = parse_utc(last_ok)
        age = max(0, int((now - when).total_seconds())) if when else None
        ok = failures == 0 and age is not None and age <= STALE_AFTER
        return (200 if ok else 503), {"ok": ok, "build": build_id(), "last_ok": last_ok if when else None,
                                      "age_seconds": age, "consecutive_failures": failures}

    def page_bytes(self) -> bytes:
        with open(self.page, "rb") as f:
            return self.head.encode() + f.read() + SKELETON_TAIL.encode()

    def refresh_deals(self) -> tuple[int, dict]:
        """Run the watcher in snapshot-only mode. One at a time; usually takes a few seconds."""
        if not self.refresh_lock.acquire(blocking=False):
            return 409, {"error": "a check is already running"}
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
            r = subprocess.run([sys.executable, self.watcher, "--snapshot-only"],
                               env={**os.environ, "HOME": home}, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                msg = (r.stderr.strip().splitlines() or ["the watcher failed"])[-1]
                return 502, {"error": msg}
            return 200, {"ok": True}
        except subprocess.TimeoutExpired:
            return 504, {"error": "the check took too long"}
        finally:
            self.refresh_lock.release()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "deals-server/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def end_headers(self) -> None:
            # Every response carries these, including errors the http.server module sends itself.
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            super().end_headers()

        def refuse_cross_site(self) -> bool:
            """Send 403 and return True for a cross-site request. Called first in every mutating method."""
            if cross_site_request(self.headers):
                self.json(403, {"error": "cross-site request refused"})
                return True
            return False

        def send(self, status: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status: int, obj) -> None:
            self.send(status, json.dumps(obj).encode())

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/deals", "/deals/"):
                try:
                    return self.send(200, app.page_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.json(500, {"error": "deals page missing"})
            if path in ("/api/deals", "/deals/api/deals"):
                try:
                    with open(app.deals, "rb") as f:
                        return self.send(200, f.read())
                except FileNotFoundError:
                    return self.json(404, {"error": "no deals snapshot yet"})
            m = TARGETS_RE.match(path)
            if m and m.group(1) is None:
                return self.targets_call(app.get_targets)
            if STATUS_RE.match(path):
                return self.json(200, app.status_doc())
            if HEALTHZ_RE.match(path):
                if "deep=1" in self.path.partition("?")[2].split("&"):
                    status, body = app.deep_health()
                    return self.json(status, body)
                return self.send(200, b"ok", "text/plain")
            self.json(404, {"error": "not found"})

        do_HEAD = do_GET

        def targets_call(self, fn, *args) -> None:
            try:
                return self.json(200, fn(*args))
            except BadRequest as e:
                return self.json(400, {"error": str(e)})
            except TargetsUnreadable:
                return self.json(500, {"error": "targets.json can't be read; fix or remove it on the server"})
            except OSError as e:
                sys.stderr.write(f"targets: {e}\n")
                return self.json(500, {"error": "couldn't save the target"})

        def target_route(self):
            """(appid_text) when the path is /api/deals/targets/<id>, else None."""
            m = TARGETS_RE.match(self.path.split("?", 1)[0])
            return m.group(1) if m and m.group(1) is not None else None

        def read_body(self) -> bytes:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise BadRequest("send the body as application/json")
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise BadRequest("a request body is required")
            if n < 1:
                raise BadRequest("a request body is required")
            if n > MAX_BODY:
                raise BadRequest(f"body too large (the limit is {MAX_BODY} bytes)")
            return self.rfile.read(n)

        def do_PUT(self):
            if self.refuse_cross_site():
                return
            text = self.target_route()
            if text is None:
                return self.json(404, {"error": "not found"})

            def go():
                appid = parse_appid(text)
                return app.set_target(appid, parse_price(self.read_body()))
            return self.targets_call(go)

        def do_DELETE(self):
            if self.refuse_cross_site():
                return
            text = self.target_route()
            if text is None:
                return self.json(404, {"error": "not found"})
            return self.targets_call(lambda: app.clear_target(parse_appid(text)))

        def do_POST(self):
            if self.refuse_cross_site():
                return
            if self.path.split("?", 1)[0] in ("/api/deals/refresh", "/deals/api/deals/refresh"):
                status, obj = app.refresh_deals()
                return self.json(status, obj)
            self.json(404, {"error": "not found"})

    return Handler


def parse_nav(items: list[str]) -> list[dict]:
    out = []
    for item in items:
        label, sep, url = item.partition("=")
        if not sep or not label.strip() or not url.strip():
            raise SystemExit(f"--nav expects LABEL=URL, got {item!r}")
        out.append({"label": label.strip(), "url": url.strip()})
    return out


def parse_times(s: str) -> list[str]:
    times = [t.strip() for t in (s or "").split(",") if t.strip()]
    for t in times:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            raise SystemExit(f"--check-times expects HH:MM[,HH:MM...], got {t!r}")
    return times


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Serve the Wishlist Deals page.")
    ap.add_argument("--port", type=int, default=8485)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data", default=os.path.expanduser("~/exports"),
                    help="Directory holding deals.json (default: ~/exports)")
    ap.add_argument("--page", default=os.path.join(here, "deals.html"))
    ap.add_argument("--watcher", default=os.path.join(here, "wishlist_deals.py"))
    ap.add_argument("--check-times", default="",
                    help="Scheduled check times shown on the page, e.g. 10:40,19:40 (local time)")
    ap.add_argument("--nav", action="append", default=[], metavar="LABEL=URL",
                    help="Add a link to the page header, e.g. --nav 'Backlog=https://backlog.example.com/'")
    args = ap.parse_args()
    os.makedirs(args.data, exist_ok=True)
    app = App(args.page, args.data, args.watcher, parse_nav(args.nav), parse_times(args.check_times))
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(app))
    print(f"Wishlist Deals on http://{args.bind}:{args.port}/ (data in {args.data})", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
