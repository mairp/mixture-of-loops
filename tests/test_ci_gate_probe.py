"""TEMPORARY: proves the ci gate refuses a red pull request. Not for merging.

Deleted as soon as the branch protection and Mergify behaviour is confirmed.
"""

import unittest


class CIGateProbe(unittest.TestCase):
    def test_this_must_fail(self) -> None:
        self.fail("deliberate failure: the ci gate must refuse this pull request")
