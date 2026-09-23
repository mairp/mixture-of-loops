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
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MOL_VIA": "shell"})
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

    def test_bootstrap_resolves_prerequisite_paths_against_the_repository_root(self) -> None:
        for fixture in mol_e2e.FIXTURE_NAMES:
            with self.subTest(fixture=fixture):
                expected = mol_e2e.expectations(fixture)["prerequisite"]
                repo, draft = self.bootstrap(self.workspace(), fixture)
                entries = draft["inventory"]["prerequisites"]
                self.assertEqual(len(entries), 1, entries)
                entry = entries[0]
                self.assertEqual((entry["id"], entry["path"], entry["resolved"], entry["present"]),
                                 (expected["id"], expected["file"], expected["file"], expected["present"]))
                self.assertEqual((entry["source"]["path"], entry["source"]["line"], entry["source"]["anchor"]),
                                 (expected["path"], expected["line"], expected["id"]))
                # the same path named from the feature directory would be the wrong base
                self.assertFalse((repo / expected["path"]).parent.joinpath(expected["file"]).exists())

    def test_promote_owns_the_status_field(self) -> None:
        # blocked: a hand-set `validated` on a contract with an open blocker is undone and refused
        base = self.workspace()
        repo, draft = self.bootstrap(base, "greeting-blocked")
        contract = repo / "launch-contract.json"
        blocked = mol_e2e.reference_contract(draft, "greeting-blocked")
        blocked["status"] = "validated"
        contract.write_text(json.dumps(blocked, indent=2), encoding="utf-8")
        result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertIn("open blocker", result.stderr)
        self.assertIn("status set back to draft", result.stderr)
        self.assertEqual(json.loads(contract.read_text(encoding="utf-8"))["status"], "draft")
        # promoting again changes nothing and still refuses
        result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertEqual(result.returncode, 20)
        self.assertNotIn("status set back", result.stderr)
        self.assertEqual(json.loads(contract.read_text(encoding="utf-8"))["status"], "draft")
        # ready: a correct draft is promoted, the file says validated, and the renderer accepts it
        base = self.workspace()
        repo, draft = self.bootstrap(base, "greeting-ready")
        contract = repo / "launch-contract.json"
        ready = mol_e2e.reference_contract(draft, "greeting-ready")
        ready["status"] = "draft"
        contract.write_text(json.dumps(ready, indent=2), encoding="utf-8")
        result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("promoted to validated", result.stdout)
        self.assertEqual(json.loads(contract.read_text(encoding="utf-8"))["status"], "validated")
        self.assertEqual(mol_e2e.run_script("validate_contract.py", contract, cwd=repo).returncode, 0)
        self.assertEqual(mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo).returncode, 0)
        rendered = mol_e2e.run_script("render_launcher.py", "--contract", contract,
                                      "--output", repo / "run.sh", cwd=repo)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        exclusive = mol_e2e.run_script("validate_contract.py", "--promote", "--allow-draft", contract, cwd=repo)
        self.assertEqual(exclusive.returncode, 2)

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
