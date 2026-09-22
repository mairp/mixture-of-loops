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
SKILL = ROOT / "skills" / "mixture-of-loops"
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


class MixtureOfLoopsTests(unittest.TestCase):
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
            self.assertFalse((repository / ".mixture-of-loops" / "runs").exists())

            executed = run(launcher, "--no-color", cwd=repository)
            self.assertEqual(executed.returncode, 0, executed.stdout)
            self.assertTrue((repository / "done").is_file())
            state = json.loads(
                (repository / ".mixture-of-loops" / "runs" / "fixture-pipeline" / "state.json").read_text()
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

    def test_launcher_inside_the_artifact_dir_keeps_one_level_and_the_same_state(self) -> None:
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
            artifacts = repository / ".mixture-of-loops"
            (artifacts / "fixture").mkdir(parents=True)
            contract = artifacts / "fixture" / "launch-contract.json"
            contract.write_text(json.dumps(base_contract(repository, source, [stage]), indent=2), encoding="utf-8")
            launcher = artifacts / "run-fixture.sh"
            rendered = run(sys.executable, SCRIPTS / "render_launcher.py",
                           "--contract", contract, "--output", launcher)
            self.assertEqual(rendered.returncode, 0, rendered.stdout)

            bundle = next((artifacts / "generated").rglob("launch-contract.json")).parent
            self.assertEqual(bundle.parent.parent, artifacts / "generated")
            self.assertFalse((artifacts / ".mixture-of-loops").exists())

            executed = run(launcher, "--no-color", cwd=repository)
            self.assertEqual(executed.returncode, 0, executed.stdout)
            # The stage resolved its cwd from the contract's repository root, not the launcher.
            self.assertTrue((repository / "done").is_file())
            # Run state holds the path a repository-root launcher would have used.
            state = json.loads((artifacts / "runs" / "fixture-pipeline" / "state.json").read_text())
            self.assertEqual(state["state"], "completed")
            self.assertEqual(sorted(path.name for path in repository.iterdir()),
                             [".mixture-of-loops", "done", "spec.md"])

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
                    "kind": "specstride",
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

    def test_specstride_color_modes_preserve_or_disable_tty_view(self) -> None:
        for mode, expected_tty, expected_flag in (
            ("never", False, "--no-live"),
            ("always", True, "--live"),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary)
                source = repository / "spec.md"
                source.write_text("fixture\n", encoding="utf-8")
                worker = repository / "fake-specstride"
                worker.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "Path('display.json').write_text(json.dumps({'isatty': os.isatty(1), 'args': sys.argv[1:]}))\n"
                    "print('fake specstride output')\n",
                    encoding="utf-8",
                )
                worker.chmod(0o755)
                stage = {
                    "id": "specstride-run",
                    "kind": "specstride",
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
                ".agents/skills/mixture-of-loops",
                ".claude/skills/mixture-of-loops",
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


    # ── Specstride was formerly Wiggum: the legacy stage kind is an alias ───────
    FAKE_SPECSTRIDE = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "state = Path('.specstride/features/007')\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "(state / 'PROGRESS.md').write_text('done\\n')\n"
        "Path('invocation.json').write_text(json.dumps({\n"
        "    'argv0': Path(sys.argv[0]).name, 'args': sys.argv[1:],\n"
        "    'env': {k: v for k, v in os.environ.items()\n"
        "            if k.startswith(('SPECSTRIDE_', 'WIGGUM_'))}}))\n"
    )

    def _alias_fixture(self, repository: Path, kind: str) -> tuple[Path, dict[str, str]]:
        source = repository / "spec.md"
        source.write_text("fixture\n", encoding="utf-8")
        bin_dir = repository / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "specstride"
        fake.write_text(self.FAKE_SPECSTRIDE, encoding="utf-8")
        fake.chmod(0o755)
        legacy = kind == "wiggum"
        state = ".wiggum" if legacy else ".specstride"
        command = "wiggum" if legacy else "specstride"
        prefix = "WIGGUM_" if legacy else "SPECSTRIDE_"
        stage = {
            "id": "run-feature",
            "kind": kind,
            "depends_on": [],
            "cwd": ".",
            "action": {
                "argv": [command, "run", "-w", ".", "-s", "spec.md", "--feature", "007"],
                "env": {prefix + "AGENT_STREAM": "true", prefix + "LIVE_DETAIL": "full"},
                "timeout_seconds": 30,
            },
            "preconditions": [{"type": "command_available", "name": command, "timing": "preflight"}],
            "postconditions": [{"type": "file_exists", "path": f"{state}/features/007/PROGRESS.md"}],
            "evidence": [f"{state}/features/007"],
        }
        value = base_contract(repository, source, [stage])
        value["configuration"] = {("wiggum_live" if legacy else "specstride_live"): True}
        value["coverage"][0]["timing"] = ("wiggum" if legacy else "specstride") + "-phase:1"
        contract = repository / "launch-contract.json"
        contract.write_text(json.dumps(value, indent=2), encoding="utf-8")
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = f"{bin_dir}:{environment.get('PATH', '/usr/bin:/bin')}"
        return contract, environment

    def _run_env(self, environment: dict[str, str], *args: object, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(item) for item in args], cwd=cwd, env=environment, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def test_legacy_wiggum_kind_validates_with_a_warning_and_launches_specstride(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            contract, environment = self._alias_fixture(repository, "wiggum")

            validated = self._run_env(environment, sys.executable, SCRIPTS / "validate_contract.py",
                                      contract, cwd=repository)
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("valid validated launch contract", validated.stdout)
            self.assertIn("warning: stages[0].kind 'wiggum' is deprecated; read as 'specstride'",
                          validated.stderr)
            self.assertIn("configuration.wiggum_live is deprecated", validated.stderr)
            self.assertIn("'wiggum-phase:' prefix", validated.stderr)

            launcher = repository / "run-007.sh"
            rendered = self._run_env(environment, sys.executable, SCRIPTS / "render_launcher.py",
                                     "--contract", contract, "--output", launcher, cwd=repository)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertIn("deprecated", rendered.stderr)
            bundle = next((repository / ".mixture-of-loops" / "generated").rglob("launch-contract.json"))
            published = json.loads(bundle.read_text())
            stage = published["stages"][0]
            self.assertEqual(stage["kind"], "specstride")
            self.assertEqual(stage["action"]["argv"][:2], ["specstride", "run"])
            self.assertEqual(sorted(stage["action"]["env"]),
                             ["SPECSTRIDE_AGENT_STREAM", "SPECSTRIDE_LIVE_DETAIL"])
            self.assertEqual(stage["preconditions"][0]["name"], "specstride")
            self.assertEqual(published["configuration"], {"specstride_live": True})
            self.assertEqual(published["coverage"][0]["timing"], "specstride-phase:1")

            planned = self._run_env(environment, launcher, "--dry-run", "--no-color", cwd=repository)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertIn('"specstride" "run" "-w" "." "-s" "spec.md" "--feature" "007"', planned.stdout)

            executed = self._run_env(environment, launcher, "--no-color", cwd=repository)
            self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
            invocation = json.loads((repository / "invocation.json").read_text())
            self.assertEqual(invocation["argv0"], "specstride")
            self.assertIn("--no-live", invocation["args"])
            self.assertEqual(invocation["env"]["SPECSTRIDE_LIVE"], "false")
            self.assertEqual(invocation["env"]["WIGGUM_LIVE"], "false")
            self.assertEqual(invocation["env"]["SPECSTRIDE_LIVE_DETAIL"], "full")
            # the legacy `.wiggum/…` postcondition resolved to the fresh `.specstride/`
            self.assertFalse((repository / ".wiggum").exists())

    def test_specstride_kind_validates_without_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            contract, environment = self._alias_fixture(repository, "specstride")
            validated = self._run_env(environment, sys.executable, SCRIPTS / "validate_contract.py",
                                      contract, cwd=repository)
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(validated.stderr, "")
            launcher = repository / "run-007.sh"
            rendered = self._run_env(environment, sys.executable, SCRIPTS / "render_launcher.py",
                                     "--contract", contract, "--output", launcher, cwd=repository)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertEqual(rendered.stderr, "")
            planned = self._run_env(environment, launcher, "--dry-run", "--no-color", cwd=repository)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertIn('"specstride" "run"', planned.stdout)
            self.assertNotIn("[WARN]", planned.stderr)

    # ── Specstride's learning mode is declared by the stage, never inherited ────
    def _launch_learning(self, repository: Path, stage_env: dict | None,
                         inherited: dict[str, str]) -> dict:
        contract, environment = self._alias_fixture(repository, "specstride")
        value = json.loads(contract.read_text())
        if stage_env is None:
            value["stages"][0]["action"].pop("env")
        else:
            value["stages"][0]["action"]["env"] = stage_env
        contract.write_text(json.dumps(value, indent=2), encoding="utf-8")
        environment.update(inherited)
        launcher = repository / "run-007.sh"
        rendered = self._run_env(environment, sys.executable, SCRIPTS / "render_launcher.py",
                                 "--contract", contract, "--output", launcher, cwd=repository)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        executed = self._run_env(environment, launcher, "--no-color", cwd=repository)
        self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
        return json.loads((repository / "invocation.json").read_text())["env"]

    def test_an_inherited_learning_mode_never_reaches_a_specstride_stage(self) -> None:
        inherited = {"SPECSTRIDE_LEARNING": "apply", "WIGGUM_LEARNING": "apply"}
        with tempfile.TemporaryDirectory() as temporary:
            env = self._launch_learning(Path(temporary), {"SPECSTRIDE_AGENT_STREAM": "true"}, inherited)
            self.assertEqual(env["SPECSTRIDE_LEARNING"], "off")
            self.assertNotIn("WIGGUM_LEARNING", env)
        with tempfile.TemporaryDirectory() as temporary:
            env = self._launch_learning(Path(temporary), None, inherited)   # a stage with no env at all
            self.assertEqual(env["SPECSTRIDE_LEARNING"], "off")
            self.assertNotIn("WIGGUM_LEARNING", env)

    def test_a_declared_learning_mode_is_what_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = self._launch_learning(Path(temporary), {"SPECSTRIDE_LEARNING": "suggest"},
                                        {"SPECSTRIDE_LEARNING": "apply"})
            self.assertEqual(env["SPECSTRIDE_LEARNING"], "suggest")
        with tempfile.TemporaryDirectory() as temporary:   # the legacy key is normalized, then honoured
            env = self._launch_learning(Path(temporary), {"WIGGUM_LEARNING": "apply"}, {})
            self.assertEqual(env["SPECSTRIDE_LEARNING"], "apply")

    def test_resolve_env_strips_the_learning_mode_for_checks_and_defaults_only_stages(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            import runtime
        finally:
            sys.path.remove(str(SCRIPTS))
        base = {"PATH": "/bin", "SPECSTRIDE_LEARNING": "apply", "WIGGUM_LEARNING": "apply"}
        self.assertEqual(runtime.resolve_env(None, base), {"PATH": "/bin"})   # a command_success check
        self.assertEqual(runtime.resolve_env({"X": "1"}, base, kind="command"), {"PATH": "/bin", "X": "1"})
        self.assertEqual(runtime.resolve_env(None, base, kind="specstride"),
                         {"PATH": "/bin", "SPECSTRIDE_LEARNING": "off"})

    def _validate_learning(self, repository: Path, env: dict,
                           extra_source: str | None = None) -> subprocess.CompletedProcess[str]:
        contract, environment = self._alias_fixture(repository, "specstride")
        value = json.loads(contract.read_text())
        value["stages"][0]["action"]["env"] = env
        if extra_source is not None:
            path = repository / extra_source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
            value["sources"].append({"path": extra_source, "kind": "state", "sha256": digest(path)})
        contract.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return self._run_env(environment, sys.executable, SCRIPTS / "validate_contract.py",
                             contract, cwd=repository)

    def test_validation_accepts_the_three_learning_modes_and_rejects_anything_else(self) -> None:
        for mode in ("off", "suggest", "apply"):
            with tempfile.TemporaryDirectory() as temporary:
                result = self._validate_learning(Path(temporary), {"SPECSTRIDE_LEARNING": mode})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            result = self._validate_learning(Path(temporary), {"SPECSTRIDE_LEARNING": "on"})
            self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
            self.assertIn("must be one of off, suggest, apply", result.stdout + result.stderr)

    def test_validation_rejects_a_learning_mode_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._validate_learning(
                Path(temporary), {"SPECSTRIDE_LEARNING": {"from_env": "MY_MODE", "required": False}})
            self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
            self.assertIn("must be a literal, not from_env", result.stdout + result.stderr)

    def test_validation_rejects_learning_state_as_a_contract_source(self) -> None:
        for state in (".specstride", ".wiggum"):
            with tempfile.TemporaryDirectory() as temporary:
                result = self._validate_learning(Path(temporary), {},
                                                 f"{state}/features/007/learning/phase-3.json")
                self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
                self.assertIn("is Specstride learning state", result.stdout + result.stderr)
                self.assertIn("exit 23", result.stdout + result.stderr)

    def test_state_paths_follow_the_legacy_state_dir_when_only_it_exists(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            import runtime
        finally:
            sys.path.remove(str(SCRIPTS))
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.assertEqual(runtime.path_for(".wiggum/x", repository), repository / ".specstride" / "x")
            (repository / ".wiggum").mkdir()
            self.assertEqual(runtime.path_for(".specstride/x", repository), repository / ".wiggum" / "x")
            (repository / ".specstride").mkdir()
            self.assertEqual(runtime.path_for(".wiggum/x", repository), repository / ".specstride" / "x")
            self.assertEqual(runtime.path_for("other/x", repository), repository / "other" / "x")

    def test_rename_guard_only_alias_sites_mention_the_old_name(self) -> None:
        """Specstride was formerly Wiggum. Only the alias sites and the historical
        prompt may still carry the old name; a reintroduction elsewhere fails here."""
        historical = {"prompts/mixture-of-loops-skill-prompt.md", "tests/test_mixture_of_loops.py"}
        alias_lines = {
            "skills/mixture-of-loops/scripts/contract_lib.py": [
                r'^LEGACY_(KIND|COMMAND) = "wiggum"$', r'^LEGACY_ENV_PREFIX = "WIGGUM_"$',
                r'^LEGACY_TIMING_PREFIX = "wiggum-phase:"$', r'^LEGACY_LIVE_KEY = "wiggum_live"$',
                r'^LEGACY_STATE_DIRNAME = "\.wiggum"$', r"^# Specstride was formerly Wiggum\.",
                r"Stage kind \"wiggum\" becomes", r"coverage timing \"wiggum-phase:N\"",
                r"configuration\.wiggum_live becomes$",
                r"\(argv or command_available check\) becomes `specstride` and WIGGUM_\* action$",
                r"\(Specstride was formerly Wiggum\)\"\)$",
            ],
            "skills/mixture-of-loops/scripts/runtime.py": [
                r'effective\["env"\]\["WIGGUM_LIVE"\] = "false"   # older checkouts \(formerly Wiggum\)$',
            ],
            "skills/mixture-of-loops/assets/launch-contract.schema.json": [
                r'"kind": \{"enum": \["setup", "decision", "command", "specstride", "wiggum", "smoke"\]',
            ],
            "README.md": [
                r"^Specstride was formerly Wiggum: contracts that still use the$",
                r"^`wiggum` stage kind keep validating with a deprecation warning",
            ],
            "skills/mixture-of-loops/references/contract.md": [
                r"^Specstride was formerly Wiggum\. Contracts written before the rename still validate:$",
                r"^the stage kind `wiggum` is a deprecated alias",
                r"^legacy `wiggum` command \(argv or `command_available` check\)",
                r"^`WIGGUM_\*` action env keys to `SPECSTRIDE_\*`, reads `configuration\.wiggum_live`",
                r"^`specstride_live`, and reads coverage timing `wiggum-phase:N`",
                r"^Paths under a workdir's `\.specstride/` or legacy `\.wiggum/` state dir",
                r"^and `WIGGUM_LIVE=false`, so older checkouts behave the same\.$",
            ],
            "skills/mixture-of-loops/references/derivation.md": [
                r"the local Specstride checkout \(then still named Wiggum\)",
            ],
        }
        import re
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                                 check=True).stdout.decode().split("\0")
        offenders = []
        for relative in filter(None, tracked):
            path = ROOT / relative
            if relative in historical or path.is_symlink() or not path.is_file():
                continue
            allowed = [re.compile(pattern) for pattern in alias_lines.get(relative, [])]
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search("wiggum", line, re.I) and not any(rx.search(line) for rx in allowed):
                    offenders.append(f"{relative}:{number}: {line.strip()[:120]}")
        self.assertEqual(offenders, [])
        for relative, patterns in alias_lines.items():   # no stale allowlist entries
            lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            for pattern in patterns:
                self.assertTrue(any(re.search(pattern, line) for line in lines), (relative, pattern))


if __name__ == "__main__":
    unittest.main()
