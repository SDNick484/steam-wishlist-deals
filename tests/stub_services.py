"""Local stand-ins for Steam, ITAD, the Steam store and ProtonDB, plus a runner for the watcher.

The watcher reads its API base URLs from WD_* environment variables, so it can run end to end
against canned data from tests.helpers with no network access.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests import helpers

UNTRACKED_APPIDS = (2001, 2002)  # on the wishlist, unknown to ITAD


def fake_steamid():
    return "7656119" + "0" * 10


class Stub:
    """One HTTP server that answers for all four services (paths don't overlap)."""

    def __init__(self, prices=None, bundles=None):
        self.prices = prices if prices is not None else helpers.sample_prices()
        self.bundles = bundles if bundles is not None else helpers.sample_bundles()
        self.fail = set()          # {"steam", "itad", "notify"}: answer that service with 403 (fails at once, no retries)
        self.notifications = []    # bodies posted to the fake Home Assistant notify service
        self.games = helpers.sample_games()
        self.gid_of = {f"app/{g['appid']}": g["gid"] for g in self.games}
        self.title_of = {g["gid"]: g["title"] for g in self.games}
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null")

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path.endswith("/IWishlistService/GetWishlist/v1/") and "steam" in stub.fail:
                    return self.send_error(403)
                if path == "/games/info/v2" and "itad" in stub.fail:
                    return self.send_error(403)
                if path.endswith("/IWishlistService/GetWishlist/v1/"):
                    appids = [g["appid"] for g in stub.games] + list(UNTRACKED_APPIDS)
                    return self.reply({"response": {"items": [
                        {"appid": a, "priority": i, "date_added": 1700000000 + i} for i, a in enumerate(appids)]}})
                if path == "/games/info/v2":
                    gid = self.path.split("id=", 1)[1].split("&", 1)[0]
                    return self.reply({
                        "title": stub.title_of.get(gid, "Unknown"),
                        "reviews": [{"source": "Steam", "score": 90, "count": 1234, "url": "https://example.com/r"}],
                        "tags": ["Action", "Indie"], "releaseDate": "2021-05-01", "earlyAccess": False,
                        "developers": [{"name": "Example Studio"}]})
                if path == "/saleaction/ajaxgetdeckappcompatibilityreport":
                    return self.reply({"results": {"resolved_category": 3}})
                if path.startswith("/api/v1/reports/summaries/"):
                    return self.reply({"tier": "gold", "total": 100, "trendingTier": "gold"})
                self.send_error(404)

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                data = self.body()
                if path.startswith("/api/services/notify/"):
                    if "notify" in stub.fail:
                        return self.send_error(403)
                    stub.notifications.append(data)
                    return self.reply([])
                if "itad" in stub.fail and path.startswith(("/lookup/", "/games/")):
                    return self.send_error(403)
                if path.startswith("/lookup/id/shop/"):
                    return self.reply({k: stub.gid_of.get(k) for k in data})
                if path == "/games/prices/v3":
                    return self.reply([stub.prices[g] for g in data if g in stub.prices])
                if path == "/games/overview/v2":
                    return self.reply({"bundles": stub.bundles})
                self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


CONFIG_TEMPLATE = """\
[steam]
profile = {steamid}

[itad]
key = {itad_key}
country = US

[rules]
{rules}

{targets_section}
[home_assistant]
url = {ha_url}
service = notify.mobile_app_your_phone

[output]
deals_json = {data_dir}/deals.json
"""


def write_config(work, rules="", ini_targets=None, ha_url="", itad_key="test-key"):
    """Write config.ini under `work`; returns (config_path, data_dir)."""
    data_dir = os.path.join(work, "exports")
    os.makedirs(data_dir, exist_ok=True)
    targets = ""
    if ini_targets is not None:
        targets = "[targets]\n" + "".join(f"{k} = {v}\n" for k, v in ini_targets.items())
    path = os.path.join(work, "config.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write(CONFIG_TEMPLATE.format(steamid=fake_steamid(), rules=rules, targets_section=targets, data_dir=data_dir,
                                       ha_url=ha_url, itad_key=itad_key))
    return path, data_dir


def run_watcher(stub, work, args, rules="", ini_targets=None, extra_env=None, ha=False, itad_key="test-key"):
    """Run wishlist_deals.py (or the module in WD_MODULE) against the stub.

    Returns (returncode, stdout, stderr, snapshot_dict_or_None, data_dir). State lives under `work`.
    """
    cfg, data_dir = write_config(work, rules, ini_targets, ha_url=stub.url if ha else "", itad_key=itad_key)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WD_")}
    env.update({
        "WD_STEAM_API": stub.url, "WD_ITAD_API": stub.url, "WD_STORE_API": stub.url, "WD_PROTONDB_API": stub.url,
        "WD_CONFIG": cfg, "WD_STATE": os.path.join(work, "state", "state.json"), "WD_ITAD_PACE": "0",
        "HOME": work,
    })
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, helpers.MODULE_PATH, *args], env=env, capture_output=True, text=True, timeout=120)
    snap = None
    try:
        with open(os.path.join(data_dir, "deals.json"), encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        pass
    return r.returncode, r.stdout, r.stderr, snap, data_dir


def tempdir():
    return tempfile.TemporaryDirectory(prefix="wd-test-")
