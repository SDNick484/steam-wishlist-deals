"""Alerts, digest and the snapshot's "deals" list must not change.

The golden files were generated from the watcher as it was before the wishlist-views work, by a
full run against the local stub services. Regenerate only on purpose:
  UPDATE_GOLDEN=1 python3 -m unittest tests.test_e2e_unchanged
"""

import json
import os
import re
import unittest

from tests import helpers, stub_services

FIX = os.path.join(helpers.ROOT, "tests", "fixtures")
DIGEST = os.path.join(FIX, "digest_golden.txt")
DEALS = os.path.join(FIX, "snapshot_deals_golden.json")

RULES = "low_min_discount_pct = 40\nbig_discount_pct = 50\n"
INI_TARGETS = {"1006": "10.00", "1005": "25"}


def norm(text):
    """"ends in 26355 days" depends on today's date; compare everything else."""
    return re.sub(r"ends in \d+ days?", "ends in N days", text)


class Unchanged(unittest.TestCase):
    def test_digest_and_deals_list(self):
        stub = stub_services.Stub()
        self.addCleanup(stub.close)
        with stub_services.tempdir() as work:
            code, out, err, snap, _ = stub_services.run_watcher(
                stub, work, ["--all", "--dry-run"], rules=RULES, ini_targets=INI_TARGETS)
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(snap)
        deals = json.loads(json.dumps(snap["deals"], sort_keys=True))
        if os.environ.get("UPDATE_GOLDEN"):
            with open(DIGEST, "w", encoding="utf-8") as f:
                f.write(norm(out))
            with open(DEALS, "w", encoding="utf-8") as f:
                json.dump(deals, f, indent=1, sort_keys=True)
                f.write("\n")
            self.skipTest("golden files rewritten")
        with open(DIGEST, encoding="utf-8") as f:
            self.assertEqual(norm(out), f.read())
        with open(DEALS, encoding="utf-8") as f:
            self.assertEqual(deals, json.load(f))
        self.assertEqual(snap["wishlistCount"], 20)
        self.assertEqual(snap["untracked"], 2)


if __name__ == "__main__":
    unittest.main()
