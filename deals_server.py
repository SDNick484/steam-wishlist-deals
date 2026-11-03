#!/usr/bin/env python3
"""
deals_server.py - serve the Wishlist Deals page.

Standard library only. Serves:
  GET  /                    the deals page (also at /deals)
  GET  /api/deals           the latest deals snapshot written by wishlist_deals.py
  POST /api/deals/refresh   re-run the watcher quietly (no notifications) to refresh the snapshot
  GET  /healthz             "ok"

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
import os
import pwd
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
    "padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px/1.5 system-ui,"
    "-apple-system,'Segoe UI',sans-serif}img{max-width:100%}[hidden]{display:none!important}"
    "</style>{config}</head><body>"
)
SKELETON_TAIL = "</body></html>"


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
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            self.json(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_POST(self):
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
