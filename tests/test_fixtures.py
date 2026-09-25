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

    def test_a_refused_promote_names_what_the_draft_still_owes(self) -> None:
        # the bootstrap draft: derivation not done, and the absent approval has no blocker yet
        repo, draft = self.bootstrap(self.workspace(), "greeting-blocked")
        expected = mol_e2e.expectations("greeting-blocked")["prerequisite"]
        contract = repo / "launch-contract.json"
        result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("next: the bootstrap placeholder `semantic-derivation-required` is still open", result.stderr)
        self.assertIn(f"next: {expected['id']} names `{expected['file']}`, which is absent "
                      f"({expected['path']}:{expected['line']})", result.stderr)
        # the derived blocked contract owes nothing more: its blocker is at the prerequisite's line
        contract.write_text(json.dumps(mol_e2e.reference_contract(draft, "greeting-blocked"), indent=2),
                            encoding="utf-8")
        result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertNotIn("next:", result.stderr)
        # ready: the approval is present, so only the placeholder is named
        repo, draft = self.bootstrap(self.workspace(), "greeting-ready")
        result = mol_e2e.run_script("validate_contract.py", "--promote", repo / "launch-contract.json", cwd=repo)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertEqual(result.stderr.count("next:"), 1, result.stderr)

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

    def test_strict_validation_names_three_quiet_derivation_mistakes(self) -> None:
        """From the 2026-09-23 campaign: a preflight check filed under postconditions (the
        runtime never runs it before the pipeline), the bootstrap's `unclassified` producer
        left in place, and a present prerequisite no stage checks; and from 2026-09-24,
        file_exists on a directory and a contract written without the bootstrap."""
        base = self.workspace()
        repo, draft = self.bootstrap(base, "greeting-ready")
        contract = repo / "launch-contract.json"
        reference = mol_e2e.reference_contract(draft, "greeting-ready")

        def validate(change) -> subprocess.CompletedProcess[str]:
            value = json.loads(json.dumps(reference))
            change(value)
            contract.write_text(json.dumps(value, indent=2), encoding="utf-8")
            return mol_e2e.run_script("validate_contract.py", contract, cwd=repo)

        clean = validate(lambda value: None)
        self.assertEqual((clean.returncode, clean.stderr), (0, ""))
        moved = validate(lambda value: value["stages"][0]["postconditions"].append(
            {"type": "file_exists", "path": "approvals/release-approval.json", "timing": "preflight"}))
        self.assertEqual(moved.returncode, 20)
        self.assertIn("timing preflight applies to preconditions only", moved.stderr)
        placeholder = validate(lambda value: value["coverage"][0].update(producer="unclassified"))
        self.assertEqual(placeholder.returncode, 20)
        self.assertIn("producer is still the bootstrap's `unclassified`", placeholder.stderr)
        self.assertEqual(validate(lambda value: value["coverage"][0].update(producer="release-manager")).returncode, 0,
                         "a producer in the model's own words is its call")
        unknown_stage = validate(lambda value: value["coverage"][0].update(producer="stage:nowhere"))
        self.assertEqual(unknown_stage.returncode, 20)
        self.assertIn("producer stage:nowhere names no stage", unknown_stage.stderr)

        def hand_implemented(value: dict) -> None:   # 2026-09-23 gpt-5: the model wrote the code into a stage
            value["stages"][0].update(kind="setup", action={"argv": ["python3", "-c", "open('src/greet.py','w')"],
                                                            "timeout_seconds": 60})
        self_implemented = validate(hand_implemented)
        self.assertEqual(self_implemented.returncode, 20)
        self.assertIn("implementation obligation mapped to no specstride stage", self_implemented.stderr)

        def unwritten_plan(value: dict) -> None:   # 2026-09-24 gpt-5 pi-implicit
            value["stages"][0]["action"]["argv"] += ["--verification-commands", "verification-commands.json"]
        missing_plan = validate(unwritten_plan)
        self.assertEqual(missing_plan.returncode, 20)
        self.assertIn("--verification-commands verification-commands.json, which does not exist or is empty", missing_plan.stderr)
        (repo / "verification-commands.json").write_text("{}", encoding="utf-8")
        self.assertEqual(validate(unwritten_plan).returncode, 0)

        def unchecked(value: dict) -> None:   # 2026-09-24 gpt-5 codex-auto, prime-auto promoted past the warning
            for stage in value["stages"]:
                stage["preconditions"] = [c for c in stage["preconditions"] if c["type"] != "file_exists"]
        refused = validate(unchecked)
        self.assertEqual(refused.returncode, 20, refused.stderr)
        self.assertIn("PRE-001 names `approvals/release-approval.json`, which exists, but no stage precondition",
                      refused.stderr)

        def explained(value: dict) -> None:   # a prerequisite that gates nothing says so at its own line
            unchecked(value)
            line = next(e for e in value["inventory"]["prerequisites"] if e["present"])["source"]
            value["findings"].append({"id": "approval-read-only", "severity": "info", "status": "accepted",
                                      "message": "read for context only", "source": line, "resolution": "none"})
        self.assertEqual(validate(explained).returncode, 0)

        def directory(value: dict) -> None:   # 2026-09-24 qwen claude-auto
            value["stages"][0]["postconditions"].append(
                {"type": "file_exists", "path": ".specstride/features/001-greeting"})
        on_directory = validate(directory)
        self.assertEqual(on_directory.returncode, 20)
        self.assertIn("which is a directory: use dir_exists", on_directory.stderr)
        self.assertEqual(validate(lambda value: value["stages"][0]["postconditions"].append(
            {"type": "file_exists", "path": "specs"})).returncode, 20, "a directory on disk now")
        self.assertEqual(validate(lambda value: value["stages"][0]["postconditions"].append(
            {"type": "dir_exists", "path": ".specstride/features/001-greeting"})).returncode, 0)

        def hand_written(value: dict) -> None:   # 2026-09-24 gpt-5 prime-auto skipped the bootstrap
            value.pop("generated_by")
            value.pop("inventory")
        by_hand = validate(hand_written)
        self.assertEqual(by_hand.returncode, 20)
        self.assertIn("not started by bootstrap_contract.py", by_hand.stderr)

    def test_render_takes_validated_only_from_promote(self) -> None:
        """2026-09-24 gpt-5 prime-explicit typed `validated` itself: only --promote's stamp renders."""
        base = self.workspace()
        repo, draft = self.bootstrap(base, "greeting-ready")
        contract = repo / "launch-contract.json"
        reference = mol_e2e.reference_contract(draft, "greeting-ready")

        def render() -> subprocess.CompletedProcess[str]:
            return mol_e2e.run_script("render_launcher.py", "--contract", contract,
                                      "--output", repo / "run.sh", cwd=repo)

        by_hand = dict(reference)
        by_hand.pop("promotion")
        contract.write_text(json.dumps(by_hand, indent=2), encoding="utf-8")
        refused = render()
        self.assertEqual(refused.returncode, 20, refused.stderr)
        self.assertIn("not written by validate_contract.py --promote", refused.stderr)
        self.assertFalse((repo / "run.sh").exists())
        promoted = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
        self.assertIn("promoted to validated", promoted.stdout)
        self.assertEqual(render().returncode, 0)
        edited = json.loads(contract.read_text(encoding="utf-8"))
        edited["stages"][0]["action"]["timeout_seconds"] += 1
        contract.write_text(json.dumps(edited, indent=2), encoding="utf-8")
        (repo / "run.sh").unlink()
        self.assertEqual(render().returncode, 20, "an edit after promotion needs promoting again")

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
