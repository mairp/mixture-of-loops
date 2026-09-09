from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "mixture-of-tasks"
SCRIPTS = SKILL / "scripts"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [str(item) for item in args],
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def coverage_entry(source: str, stage_ids: list[str], identifier: str = "OBL-1") -> dict:
    return {
        "id": identifier,
        "source": {"path": source, "line": 1, "anchor": "T001"},
        "kind": "implementation",
        "timing": "pipeline-start",
        "producer": f"stage:{stage_ids[0]}",
        "disposition": "mapped",
        "stage_ids": stage_ids,
        "verification_ids": [],
        "rationale": "fixture obligation",
        "evidence": ["done"],
    }


def base_contract(repository: Path, source: Path, stages: list[dict]) -> dict:
    relative = str(source.relative_to(repository))
    return {
        "schema_version": "1.0",
        "id": "fixture-pipeline",
        "status": "validated",
        "repository": {"root": str(repository), "revision": None, "dirty": None},
        "authorized_roots": [str(repository)],
        "sources": [{"path": relative, "kind": "spec", "sha256": digest(source), "lines": 1}],
        "coverage": [coverage_entry(relative, [stage["id"] for stage in stages])],
        "findings": [],
        "stages": stages,
    }


class MixtureOfTasksTests(unittest.TestCase):
    def test_bootstrap_inventory_and_semantic_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            feature = repository / "specs" / "007-example"
            feature.mkdir(parents=True)
            (feature / "spec.md").write_text("# Feature\n", encoding="utf-8")
            (feature / "plan.md").write_text(
                "# Plan\n```bash\npython3 scripts/check.py --json\n```\n", encoding="utf-8"
            )
            tasks = feature / "tasks.md"
            tasks.write_text(
                "# Tasks\n\n## Phase 1: Setup\n- [ ] T001 [P] Create the fixture\n"
                "## Phase 2: Verify\n- [x] T002 [US1] Run declared verification\n",
                encoding="utf-8",
            )
            contract = repository / "draft.json"
            result = run(
                sys.executable,
                SCRIPTS / "bootstrap_contract.py",
                "--repo",
                repository,
                "--feature",
                "specs/007-example",
                "--output",
                contract,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            value = json.loads(contract.read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in value["inventory"]["tasks"]], ["T001", "T002"])
            self.assertTrue(value["inventory"]["tasks"][0]["parallel_marker"])
            self.assertEqual(len(value["inventory"]["shell_candidates"]), 1)
            self.assertEqual(len(value["coverage"]), 2)

            valid_draft = run(
                sys.executable,
                SCRIPTS / "validate_contract.py",
                contract,
                "--allow-draft",
            )
            self.assertEqual(valid_draft.returncode, 0, valid_draft.stdout)

            tasks.write_text(tasks.read_text(encoding="utf-8").replace("- [ ] T001", "- [x] T001"), encoding="utf-8")
            checkbox_only = run(
                sys.executable,
                SCRIPTS / "validate_contract.py",
                contract,
                "--allow-draft",
            )
            self.assertEqual(checkbox_only.returncode, 0, checkbox_only.stdout)

            tasks.write_text(tasks.read_text(encoding="utf-8").replace("Create the fixture", "Create two fixtures"), encoding="utf-8")
            changed_requirement = run(
                sys.executable,
                SCRIPTS / "validate_contract.py",
                contract,
                "--allow-draft",
            )
            self.assertEqual(changed_requirement.returncode, 23, changed_requirement.stdout)

    def test_render_dry_run_execution_resume_and_edit_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "spec.md"
            source.write_text("fixture\n", encoding="utf-8")
            stage = {
                "id": "make-evidence",
                "kind": "command",
                "depends_on": [],
                "cwd": ".",
                "action": {"argv": ["/usr/bin/touch", "done"], "timeout_seconds": 10},
                "preconditions": [{"type": "command_available", "name": "touch", "timing": "preflight"}],
                "postconditions": [{"type": "file_exists", "path": "done"}],
                "evidence": ["done"],
            }
            contract = repository / "launch-contract.json"
            contract.write_text(json.dumps(base_contract(repository, source, [stage]), indent=2), encoding="utf-8")
            launcher = repository / "run-fixture.sh"
            rendered = run(
                sys.executable,
                SCRIPTS / "render_launcher.py",
                "--contract",
                contract,
                "--output",
                launcher,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout)
            self.assertEqual(run("bash", "-n", launcher).returncode, 0)

            dry = run(launcher, "--implement", "--smoke", "--dry-run", cwd=repository)
            self.assertEqual(dry.returncode, 0, dry.stdout)
            self.assertFalse((repository / "done").exists())
            self.assertFalse((repository / ".mixture-of-tasks" / "runs").exists())

            executed = run(launcher, "--no-color", cwd=repository)
            self.assertEqual(executed.returncode, 0, executed.stdout)
            self.assertTrue((repository / "done").is_file())
            state = json.loads(
                (repository / ".mixture-of-tasks" / "runs" / "fixture-pipeline" / "state.json").read_text()
            )
            self.assertEqual(state["state"], "completed")

            resumed = run(launcher, "--no-color", cwd=repository)
            self.assertEqual(resumed.returncode, 0, resumed.stdout)
            self.assertIn("postconditions still hold", resumed.stdout)

            launcher.write_text(launcher.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
            refused = run(
                sys.executable,
                SCRIPTS / "render_launcher.py",
                "--contract",
                contract,
                "--output",
                launcher,
            )
            self.assertEqual(refused.returncode, 20, refused.stdout)
            self.assertIn("refusing to replace", refused.stdout)

    def test_setup_opt_in_and_reason_bound_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "spec.md"
            source.write_text("fixture\n", encoding="utf-8")
            worker = repository / "transient-worker"
            worker.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "from pathlib import Path\n"
                "counter=Path('counter')\n"
                "n=int(counter.read_text())+1 if counter.exists() else 1\n"
                "counter.write_text(str(n))\n"
                "if n == 1:\n"
                "  with Path('events.jsonl').open('a') as f: f.write(json.dumps({'event':'run_stop','reason':'wall_budget'})+'\\n')\n"
                "  raise SystemExit(4)\n"
                "Path('recovered').touch()\n",
                encoding="utf-8",
            )
            worker.chmod(0o755)
            stages = [
                {
                    "id": "prepare",
                    "kind": "setup",
                    "depends_on": [],
                    "cwd": ".",
                    "action": {"argv": ["/usr/bin/touch", "prepared"], "timeout_seconds": 10},
                    "preconditions": [],
                    "postconditions": [{"type": "file_exists", "path": "prepared"}],
                    "evidence": ["prepared"],
                },
                {
                    "id": "recover-run",
                    "kind": "wiggum",
                    "depends_on": ["prepare"],
                    "cwd": ".",
                    "action": {"argv": [str(worker)], "timeout_seconds": 10},
                    "resume": {"argv": [str(worker)], "timeout_seconds": 10},
                    "preconditions": [],
                    "postconditions": [{"type": "file_exists", "path": "recovered"}],
                    "recovery": {
                        "max_attempts": 2,
                        "backoff_seconds": [0],
                        "retry_exit_codes": [4],
                        "total_timeout_seconds": 30,
                        "reason": {
                            "jsonl": "events.jsonl",
                            "event": "run_stop",
                            "field": "reason",
                            "allowed": ["wall_budget"],
                        },
                    },
                    "evidence": ["events.jsonl", "recovered"],
                },
            ]
            contract_value = base_contract(repository, source, stages)
            contract_value["coverage"] = [
                coverage_entry("spec.md", ["prepare"], "OBL-SETUP"),
                coverage_entry("spec.md", ["recover-run"], "OBL-RUN"),
            ]
            contract = repository / "launch-contract.json"
            contract.write_text(json.dumps(contract_value, indent=2), encoding="utf-8")
            launcher = repository / "run-fixture.sh"
            rendered = run(
                sys.executable,
                SCRIPTS / "render_launcher.py",
                "--contract",
                contract,
                "--output",
                launcher,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout)

            blocked = run(launcher, "--no-color", cwd=repository)
            self.assertEqual(blocked.returncode, 21, blocked.stdout)
            self.assertFalse((repository / "prepared").exists())
            self.assertFalse((repository / "counter").exists())

            recovered = run(launcher, "--implement", "--no-color", cwd=repository)
            self.assertEqual(recovered.returncode, 0, recovered.stdout)
            self.assertEqual((repository / "counter").read_text(), "2")
            self.assertTrue((repository / "recovered").is_file())
            self.assertIn("[RECOVER]", recovered.stdout)

    def test_wiggum_color_modes_preserve_or_disable_tty_view(self) -> None:
        for mode, expected_tty, expected_flag in (
            ("never", False, "--no-live"),
            ("always", True, "--live"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary)
                source = repository / "spec.md"
                source.write_text("fixture\n", encoding="utf-8")
                worker = repository / "fake-wiggum"
                worker.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "Path('display.json').write_text(json.dumps({'isatty': os.isatty(1), 'args': sys.argv[1:]}))\n"
                    "print('fake wiggum output')\n",
                    encoding="utf-8",
                )
                worker.chmod(0o755)
                stage = {
                    "id": "wiggum-run",
                    "kind": "wiggum",
                    "depends_on": [],
                    "cwd": ".",
                    "action": {"argv": [str(worker), "--live"], "timeout_seconds": 10},
                    "preconditions": [],
                    "postconditions": [{"type": "file_exists", "path": "display.json"}],
                    "evidence": ["display.json"],
                }
                contract = repository / "launch-contract.json"
                contract.write_text(json.dumps(base_contract(repository, source, [stage]), indent=2), encoding="utf-8")
                launcher = repository / "run-fixture.sh"
                rendered = run(
                    sys.executable,
                    SCRIPTS / "render_launcher.py",
                    "--contract",
                    contract,
                    "--output",
                    launcher,
                )
                self.assertEqual(rendered.returncode, 0, rendered.stdout)
                executed = run(launcher, f"--color={mode}", cwd=repository)
                self.assertEqual(executed.returncode, 0, executed.stdout)
                display = json.loads((repository / "display.json").read_text())
                self.assertEqual(display["isatty"], expected_tty)
                self.assertIn(expected_flag, display["args"])
                if mode == "never":
                    self.assertNotIn("--live", display["args"])

    def test_onboarding_links_all_repository_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            onboard = ROOT / "bin" / "onboard-skill"
            result = run(onboard, "--harness", "all", "--scope", "repo", "--repo", repository)
            self.assertEqual(result.returncode, 0, result.stdout)
            for relative in (
                ".agents/skills/mixture-of-tasks",
                ".claude/skills/mixture-of-tasks",
            ):
                target = repository / relative
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.resolve(), SKILL.resolve())
            checked = run(
                onboard,
                "--harness",
                "all",
                "--scope",
                "repo",
                "--repo",
                repository,
                "--check",
            )
            self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_validation_rejects_shell_strings_literal_secrets_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "spec.md"
            source.write_text("fixture\n", encoding="utf-8")
            stage = {
                "id": "unsafe",
                "kind": "command",
                "depends_on": [],
                "cwd": ".",
                "action": {
                    "argv": ["bash", "-c", "touch escaped"],
                    "env": {"API_TOKEN": "embedded"},
                    "timeout_seconds": 10,
                },
                "preconditions": [],
                "postconditions": [{"type": "file_exists", "path": "../escaped"}],
                "evidence": ["../escaped"],
            }
            contract = repository / "launch-contract.json"
            contract.write_text(json.dumps(base_contract(repository, source, [stage]), indent=2), encoding="utf-8")
            result = run(sys.executable, SCRIPTS / "validate_contract.py", contract)
            self.assertEqual(result.returncode, 20, result.stdout)
            self.assertIn("shell command string", result.stdout)
            self.assertIn("literal secrets", result.stdout)
            self.assertIn("escapes authorized_roots", result.stdout)


if __name__ == "__main__":
    unittest.main()
