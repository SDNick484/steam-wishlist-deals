"""evaluate() must give the same alerts for the same inputs.

The golden file holds the output of evaluate() as it was before is_low() was factored out.
Regenerate it only on purpose:  UPDATE_GOLDEN=1 python3 -m unittest tests.test_evaluate
"""

import json
import os
import unittest

from tests import helpers

GOLDEN = os.path.join(helpers.ROOT, "tests", "fixtures", "evaluate_golden.json")


def run_variant(wd, spec):
    cfg = helpers.make_cfg(**spec)
    alerts = wd.evaluate(helpers.sample_games(), helpers.sample_prices(), helpers.sample_bundles(), cfg)
    return json.loads(json.dumps(alerts, sort_keys=True))


class EvaluateGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = helpers.load_watcher()

    def test_matches_golden(self):
        got = {name: run_variant(self.wd, spec) for name, spec in helpers.VARIANTS.items()}
        if os.environ.get("UPDATE_GOLDEN"):
            with open(GOLDEN, "w", encoding="utf-8") as f:
                json.dump(got, f, indent=1, sort_keys=True)
                f.write("\n")
            self.skipTest("golden file rewritten")
        with open(GOLDEN, encoding="utf-8") as f:
            want = json.load(f)
        self.assertEqual(sorted(got), sorted(want))
        for name in want:
            with self.subTest(variant=name):
                self.assertEqual(got[name], want[name])

    def test_expected_cases(self):
        """Spot checks that document the rules the golden file pins down."""
        base = run_variant(self.wd, {})
        self.assertEqual(base["deal:1001"]["kind"], "low")
        self.assertNotIn("deal:1003", base)            # above the low, no other reason
        self.assertNotIn("deal:1005", base)            # at its low, but not on sale
        self.assertNotIn("deal:1009", base)            # voucher deals never count
        self.assertNotIn("deal:1018", base)            # not discounted anywhere
        self.assertTrue(base["deal:1016"]["new_low"])
        self.assertIn("bundle:b1:1013", base)
        self.assertNotIn("bundle:b2:1014", base)       # expired bundle
        tol = run_variant(self.wd, {"rules": {"low_tolerance_pct": "10"}})
        self.assertEqual(tol["deal:1002"]["kind"], "low")
        self.assertNotIn("deal:1002", base)
        t = run_variant(self.wd, helpers.VARIANTS["targets"])
        self.assertEqual(t["deal:1006"]["kind"], "target")
        self.assertNotIn("deal:1007", t)


class IsLow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wd = helpers.load_watcher()

    def check(self, price, regular, low, tol=0.0, min_cut=0.0):
        d = helpers.deal("Steam", price, regular)
        return self.wd.is_low(d, helpers.money(low) if low is not None else None, tol, min_cut)

    def test_at_and_around_the_low(self):
        self.assertTrue(self.check(10.0, 20.0, 10.0))
        self.assertTrue(self.check(8.0, 20.0, 10.0))           # below the old low
        self.assertFalse(self.check(10.01, 20.0, 10.0))
        self.assertTrue(self.check(11.0, 20.0, 10.0, tol=10))  # exactly on the tolerance edge
        self.assertFalse(self.check(11.01, 20.0, 10.0, tol=10))

    def test_needs_a_real_discount(self):
        self.assertFalse(self.check(20.0, 20.0, 20.0))         # full price is never "a low"
        self.assertTrue(self.check(18.0, 20.0, 18.0))          # 10% off, no minimum
        self.assertFalse(self.check(18.0, 20.0, 18.0, min_cut=40))
        self.assertTrue(self.check(12.0, 20.0, 12.0, min_cut=40))  # 40% off meets a 40% minimum

    def test_no_history(self):
        self.assertFalse(self.check(5.0, 20.0, None))


if __name__ == "__main__":
    unittest.main()
