import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from greet import greet  # noqa: E402


class GreetTests(unittest.TestCase):
    def test_greets_by_name(self) -> None:
        self.assertEqual(greet("Ada"), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
