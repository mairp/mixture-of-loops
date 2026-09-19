"""Tier 1 (hermetic): the Tier 3 fixtures, their independent expectations, and the stub.

Proves the expectations and the implementation agree before any model is involved,
and that the scripts run through a symlinked SKILL_ROOT with no controlling terminal
and stdin closed (the portability audit's claim for ipython-only harnesses).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
import mol_e2e  # noqa: E402

ROOT = mol_e2e.ROOT


class FixtureTests(unittest.TestCase):
    def workspace(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def bootstrap(self, base: Path, fixture: str) -> tuple[Path, dict]:
        repo = mol_e2e.prepare_fixture(fixture, base / "repo")
        link = base / "skill-link"
        link.symlink_to(mol_e2e.SKILL)            # SKILL_ROOT as a harness sees it
        contract = repo / "launch-contract.json"
        result = subprocess.run(
            ["setsid", sys.executable, str(link / "scripts/bootstrap_contract.py"), "--repo", str(repo),
             "--feature", mol_e2e.expectations(fixture)["feature"], "--output", str(contract)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        return repo, json.loads(contract.read_text(encoding="utf-8"))

    def test_expectations_are_current(self) -> None:
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/fixtures/derive_expectations.py"), "--check"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixtures_differ_only_by_the_prerequisite(self) -> None:
        blocked = mol_e2e.tree_state(mol_e2e.FIXTURES / "repos/greeting-blocked")
        ready = mol_e2e.tree_state(mol_e2e.FIXTURES / "repos/greeting-ready")
        self.assertEqual(set(ready) - set(blocked), {"approvals", "approvals/release-approval.json"})
        self.assertEqual({k: v for k, v in ready.items() if k in blocked}, blocked)

    # T030: bootstrap agrees with the independently derived facts.
    def test_bootstrap_matches_independent_expectations(self) -> None:
        for fixture in mol_e2e.FIXTURE_NAMES:
            with self.subTest(fixture=fixture):
                expected = mol_e2e.expectations(fixture)
                repo, draft = self.bootstrap(self.workspace(), fixture)
                facts = mol_e2e.deterministic_facts(draft, repo)
                self.assertEqual(facts["sources"], expected["sources"])
                self.assertEqual(facts["tasks"], expected["tasks"])
                self.assertEqual(facts["declared_commands"], [c["argv"] for c in expected["declared_commands"]])
                candidate = draft["inventory"]["shell_candidates"][0]
                self.assertEqual((candidate["path"], candidate["start_line"]),
                                 (expected["declared_commands"][0]["path"], expected["declared_commands"][0]["start_line"]))
                self.assertEqual(draft["status"], "draft")
                self.assertEqual([f["id"] for f in mol_e2e._open_blockers(draft)], ["semantic-derivation-required"])
                self.assertEqual(mol_e2e.run_script("validate_contract.py", repo / "launch-contract.json",
                                                    "--allow-draft", cwd=repo).returncode, 0)

    def test_reference_completion_has_exactly_one_correct_outcome(self) -> None:
        for fixture in mol_e2e.FIXTURE_NAMES:
            with self.subTest(fixture=fixture):
                base = self.workspace()
                repo, draft = self.bootstrap(base, fixture)
                contract = repo / "launch-contract.json"
                contract.write_text(json.dumps(mol_e2e.reference_contract(draft, fixture), indent=2), encoding="utf-8")
                validated = mol_e2e.run_script("validate_contract.py", contract, cwd=repo)
                rendered = mol_e2e.run_script("render_launcher.py", "--contract", contract,
                                              "--output", repo / "run-001-greeting.sh", cwd=repo)
                if mol_e2e.expectations(fixture)["expected_outcome"] == "blocked":
                    self.assertEqual(validated.returncode, 20, validated.stderr)
                    self.assertIn("open blocker", validated.stderr)
                    self.assertEqual(rendered.returncode, 20, rendered.stderr)
                    self.assertEqual(mol_e2e.locate_launchers(repo), [])
                else:
                    self.assertEqual(validated.returncode, 0, validated.stderr)
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    verdicts = mol_e2e.check_launcher(repo, base)
                    self.assertEqual([v["name"] for v in verdicts if v["status"] != "pass"], [], verdicts)

    # T031: the stub refuses anything but --version/--help and creates nothing.
    def test_stub_specstride_refuses_pipeline_work(self) -> None:
        base = self.workspace()
        log = base / "stub.jsonl"
        stub = mol_e2e.STUB_BIN / "specstride"
        environment = {**os.environ, "MOL_STUB_LOG": str(log)}
        work = base / "work"
        work.mkdir()
        version = subprocess.run([str(stub), "--version"], cwd=work, env=environment, capture_output=True, text=True)
        self.assertEqual(version.returncode, 0)
        for argv in (["run", "-w", "."], ["resume", "-w", "."], ["setup"], ["init"]):
            refused = subprocess.run([str(stub), *argv], cwd=work, env=environment, capture_output=True, text=True)
            self.assertEqual(refused.returncode, 97, argv)
            self.assertIn("REFUSED", refused.stderr)
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
