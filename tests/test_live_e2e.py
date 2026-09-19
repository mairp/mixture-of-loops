"""Tier 3 entry point for unittest: the live headless runs, opt-in only.

Skipped unless MOL_LIVE_E2E=1, so `python3 -m unittest discover -s tests` stays
hermetic. When enabled it runs tests/e2e/run_harness_e2e.py (Tier 3 only; Tiers
1 and 2 are the rest of this suite) and fails if any live run or the
cross-harness comparison fails.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

RUNNER = Path(__file__).resolve().parent / "e2e" / "run_harness_e2e.py"


@unittest.skipUnless(os.environ.get("MOL_LIVE_E2E") == "1",
                     "MOL_LIVE_E2E=1 is not set: live model runs are opt-in")
class LiveHarnessE2ETests(unittest.TestCase):
    def test_live_runs(self) -> None:
        harness = os.environ.get("MOL_LIVE_HARNESS", "all")
        result = subprocess.run([sys.executable, str(RUNNER), "--skip-tiers", "--harness", harness],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
