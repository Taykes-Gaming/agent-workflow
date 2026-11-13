"""The review assets `wf review` runs (src/wf/review_assets/) and the ones the wf-review plugin and the tests load
(plugins/wf-review/) must not drift apart."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAIRS = (
    ("src/wf/review_assets/review_prep.py", "plugins/wf-review/scripts/review_prep.py"),
    ("src/wf/review_assets/review-lane.md", "plugins/wf-review/agents/review-lane.md"),
)


class ReviewAssetsTest(unittest.TestCase):
    def test_copies_are_identical(self):
        for a, b in PAIRS:
            self.assertEqual((ROOT / a).read_bytes(), (ROOT / b).read_bytes(), f"{a} and {b} differ")


if __name__ == "__main__":
    unittest.main()
