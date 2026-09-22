"""Tier 1 (hermetic): the harness-side execution policy, without a harness or a model.

Mode selection, launcher resolution, the launch gate, the telemetry reader, the
reporting contract and the relaunch classifier are all plain functions over plain data,
so every rule they carry is asserted here in a table rather than observed later in a
live run. Nothing in this module calls a model, and the only processes it starts are
real rendered launchers running instant local commands, each reaped in teardown.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "mixture-of-loops" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract_lib  # noqa: E402
import supervisor_lib as mol  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contract_for(repository: Path, stages: list[dict], *, root: Path | None = None,
                 authorized: list[Path] | None = None, prefix: str = "") -> dict:
    source = repository / f"{prefix}spec.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("fixture\n", encoding="utf-8")
    relative = f"{prefix}spec.md"
    return {
        "schema_version": "1.0",
        "id": "fixture-pipeline",
        "status": "validated",
        "repository": {"root": str(root or repository), "revision": None, "dirty": None},
        "authorized_roots": [str(path) for path in (authorized or [repository])],
        "sources": [{"path": relative, "kind": "spec", "sha256": digest(source), "lines": 1}],
        "coverage": [{"id": "OBL-1", "source": {"path": relative, "line": 1, "anchor": "T001"},
                      "kind": "implementation", "timing": "pipeline-start",
                      "producer": f"stage:{stages[0]['id']}", "disposition": "mapped",
                      "stage_ids": [stage["id"] for stage in stages], "verification_ids": [],
                      "rationale": "fixture obligation", "evidence": ["done"]}],
        "findings": [],
        "stages": stages,
    }


def instant_stage(cwd: str = ".") -> dict:
    return {"id": "make-evidence", "kind": "command", "depends_on": [], "cwd": cwd,
            "action": {"argv": ["/usr/bin/touch", "done"], "timeout_seconds": 10},
            "preconditions": [], "postconditions": [{"type": "file_exists", "path": "done"}],
            "evidence": ["done"]}


def render(contract: dict, repository: Path, name: str = "run-fixture.sh") -> Path:
    path = repository / "launch-contract.json"
    path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    launcher = repository / name
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_launcher.py"), "--contract", str(path),
         "--output", str(launcher)],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if result.returncode != 0:
        raise AssertionError(f"render failed: {result.stdout}{result.stderr}")
    return launcher


def dead_pid() -> int:
    """A PID that is certainly not a live process: a child, started and fully reaped."""
    child = subprocess.Popen(["/bin/true"])
    child.wait()
    return child.pid


class Fixture(unittest.TestCase):
    def workspace(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def repository(self, stages: list[dict] | None = None, **kwargs) -> tuple[Path, Path]:
        base = self.workspace()
        contract = contract_for(base, stages or [instant_stage()], **kwargs)
        return base, render(contract, base)

    def reap(self, record: dict) -> None:
        """No test leaves a process, a lock or an unreaped child behind, failure or not."""
        self.addCleanup(self._terminate, record.get("pgid"), record.get("pid"))

    @staticmethod
    def _terminate(group: object, pid: object) -> None:
        if isinstance(group, int) and group > 1:
            for number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(group, number)
                except (ProcessLookupError, PermissionError, OSError):
                    break
                time.sleep(0.2)
        child = mol._CHILDREN.pop(pid, None) if isinstance(pid, int) else None
        if child is not None:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


# ── mode selection ────────────────────────────────────────────────────────────

class ModeSelectionTests(unittest.TestCase):
    CASES = [
        # request, tokens, mode, source
        ("derive a pipeline for specs/007-example", [], "generate", "default"),
        ("turn specs/001-greeting into a launch contract", [], "generate", "default"),
        ("just derive it", [], "generate", "refusal"),
        ("just generate the pipeline, don't run it", [], "generate", "refusal"),
        ("derive it but do not run the pipeline", [], "generate", "refusal"),
        ("build the pipeline without running it", [], "generate", "refusal"),
        ("derive the pipeline, no execution", [], "generate", "refusal"),
        ("generation only please", [], "generate", "refusal"),
        ("derive a pipeline and show me a dry-run", [], "generate", "refusal"),
        ("auto", [], "auto", "prose"),
        ("use auto mode for specs/001-greeting", [], "auto", "prose"),
        ("generate and run it", [], "auto", "prose"),
        ("derive a pipeline for specs/007 and then run it", [], "auto", "prose"),
        ("create the launcher and execute it", [], "auto", "prose"),
        ("run it", [], "run", "prose"),
        ("run the pipeline", [], "run", "prose"),
        ("launch it for me", [], "run", "prose"),
        ("execute the launcher", [], "run", "prose"),
        ("resume the run", [], "run", "prose"),
        # an explicit token beats every kind of prose, including a refusal
        ("derive a pipeline", ["--auto"], "auto", "token"),
        ("derive a pipeline", ["--run"], "run", "token"),
        ("run it now", ["--generate"], "generate", "token"),
        ("don't run it", ["--auto"], "auto", "token"),
        ("--run the launcher please", [], "run", "token"),
        ("please --auto this one", [], "auto", "token"),
    ]

    def test_mode_is_read_from_tokens_then_refusals_then_prose(self) -> None:
        for request, tokens, mode, source in self.CASES:
            with self.subTest(request=request, tokens=tokens):
                decision = mol.select_mode(request, tokens)
                self.assertEqual(decision.mode, mode, decision)
                self.assertEqual(decision.source, source, decision)

    def test_silence_is_not_consent_to_execute(self) -> None:
        for request in ("", "specs/001-greeting", "what does this repository build?",
                        "inventory the spec kit artifacts", "hello"):
            with self.subTest(request=request):
                self.assertEqual(mol.select_mode(request).mode, "generate")

    def test_passthrough_flags_are_only_used_when_asked_for(self) -> None:
        plain = mol.select_mode("run it")
        self.assertEqual((plain.implement, plain.smoke), (False, False))
        asked = mol.select_mode("run it with --implement and --smoke")
        self.assertEqual((asked.mode, asked.implement, asked.smoke), ("run", True, True))
        prose = mol.select_mode("run it and include the smoke stages")
        self.assertTrue(prose.smoke)

    def test_dry_run_is_the_generation_gate_not_an_execution_mode(self) -> None:
        decision = mol.select_mode("derive a pipeline and dry-run it")
        self.assertEqual(decision.mode, "generate")
        self.assertNotIn("--dry-run", mol.launch_argv(Path("/x/run.sh"), implement=True, smoke=True))

    def test_the_message_names_the_decision_and_its_evidence(self) -> None:
        line = mol.select_mode("generate and run it").message()
        self.assertTrue(line.startswith("[MOL-MODE] mode=auto source=prose "))
        self.assertIn('evidence="generate and run"', line)


# ── launcher resolution ───────────────────────────────────────────────────────

class LauncherResolutionTests(Fixture):
    def test_an_explicit_path_wins_and_a_missing_one_is_named(self) -> None:
        repository, launcher = self.repository()
        found = mol.resolve_launcher(repository, requested=str(launcher))
        self.assertEqual((found.launcher, found.source), (launcher, "request"))
        relative = mol.resolve_launcher(repository, requested=launcher.name)
        self.assertEqual(relative.launcher, launcher)
        missing = mol.resolve_launcher(repository, requested="nowhere/run.sh")
        self.assertIsNone(missing.launcher)
        self.assertEqual(missing.reason, "launcher-not-found")

    def test_the_feature_default_path_is_used_next(self) -> None:
        base = self.workspace()
        feature = base / ".mixture-of-loops" / "001-greeting"
        feature.mkdir(parents=True)
        contract = contract_for(base, [instant_stage()])
        launcher = render(contract, base, name=".mixture-of-loops/001-greeting/run-001-greeting.sh")
        found = mol.resolve_launcher(base, feature="specs/001-greeting")
        self.assertEqual((found.launcher, found.source), (launcher, "feature-default"))

    def test_an_operator_chosen_path_is_recovered_from_the_generated_marker(self) -> None:
        base = self.workspace()
        (base / "ops").mkdir()
        contract = contract_for(base, [instant_stage()])
        launcher = render(contract, base, name="ops/pipeline.sh")
        found = mol.resolve_launcher(base, feature="specs/001-greeting")
        self.assertEqual((found.launcher, found.source), (launcher, "generated-marker"))

    def test_no_launcher_refuses_by_name_and_offers_auto(self) -> None:
        base = self.workspace()
        (base / "notes.sh").write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
        found = mol.resolve_launcher(base, feature="specs/001-greeting")
        self.assertIsNone(found.launcher)
        self.assertEqual(found.reason, "launcher-missing")
        self.assertIn("`auto`", found.message())

    def test_several_launchers_are_reported_rather_than_guessed_between(self) -> None:
        base = self.workspace()
        contract = contract_for(base, [instant_stage()])
        render(contract, base, name="run-a.sh")
        render(contract, base, name="run-b.sh")
        found = mol.resolve_launcher(base)
        self.assertIsNone(found.launcher)
        self.assertEqual(found.reason, "launcher-ambiguous")
        self.assertEqual(len(found.candidates), 2)

    def test_bundles_and_run_state_are_not_launchers(self) -> None:
        repository, launcher = self.repository()
        self.assertEqual(mol.generated_launchers(repository), [launcher])


# ── the launch gate ───────────────────────────────────────────────────────────

class GateTests(Fixture):
    def assert_refused(self, report: mol.GateReport, name: str, reason: str) -> None:
        self.assertEqual(report.decision, "refuse", report.messages())
        self.assertEqual(report.reason, reason, report.messages())
        failed = [check for check in report.checks if check.status == "fail"]
        self.assertEqual([check.name for check in failed], [name], report.messages())
        self.assertTrue(report.next_action, "a refusal must name the smallest clearing action")

    def test_a_clean_launcher_clears_every_check(self) -> None:
        _, launcher = self.repository()
        report = mol.gate(launcher)
        self.assertEqual(report.decision, "launch", report.messages())
        self.assertEqual([check.name for check in report.checks],
                         ["launcher-intact", "contract-validated", "authorized-root",
                          "launcher-syntax", "launcher-dry-run", "run-directory-free"])
        self.assertTrue(all(check.status == "pass" for check in report.checks))

    def test_an_edited_launcher_is_never_run_under_this_skill(self) -> None:
        _, launcher = self.repository()
        launcher.write_text(launcher.read_text(encoding="utf-8") + "# local edit\n",
                            encoding="utf-8")
        report = mol.gate(launcher)
        self.assert_refused(report, "launcher-intact", "launcher-edited")
        self.assertIn("--replace-edited", report.next_action)
        self.assertEqual([check.status for check in report.checks[1:]],
                         ["not-evaluated"] * 5)

    def test_a_missing_launcher_is_named_rather_than_generated(self) -> None:
        base = self.workspace()
        self.assert_refused(mol.gate(base / "absent.sh"), "launcher-intact", "launcher-not-found")

    def test_a_draft_contract_stops_at_generation(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.bundle_of(launcher)
        contract = json.loads((bundle / "launch-contract.json").read_text(encoding="utf-8"))
        contract["status"] = "draft"
        (bundle / "launch-contract.json").write_text(json.dumps(contract), encoding="utf-8")
        self.assert_refused(mol.gate(launcher), "contract-validated", "draft-contract")

    def test_an_open_blocker_stops_at_generation(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.bundle_of(launcher)
        contract = json.loads((bundle / "launch-contract.json").read_text(encoding="utf-8"))
        contract["findings"] = [{"id": "missing-release-approval", "severity": "blocker",
                                 "status": "open", "message": "non-delegable authority is absent",
                                 "source": {"path": "spec.md", "line": 1, "anchor": "PRE-001"},
                                 "resolution": "the release manager must sign it"}]
        (bundle / "launch-contract.json").write_text(json.dumps(contract), encoding="utf-8")
        report = mol.gate(launcher)
        self.assert_refused(report, "contract-validated", "open-blocker")
        self.assertIn("non-delegable authority is absent", report.detail)

    def test_stale_sources_are_the_runtimes_own_authority_and_offer_auto(self) -> None:
        repository, launcher = self.repository()
        (repository / "spec.md").write_text("the requirement changed\n", encoding="utf-8")
        report = mol.gate(launcher)
        self.assert_refused(report, "contract-validated", "stale-sources")
        self.assertIn("`auto`", report.next_action)

    def test_a_repository_root_outside_authorized_roots_refuses(self) -> None:
        base = self.workspace()
        work = base / "work"
        work.mkdir()
        # Everything the validator checks stays inside work/, so only the repository
        # root itself is outside the authorized roots.
        contract = contract_for(base, [instant_stage(cwd="work")], authorized=[work],
                                prefix="work/")
        launcher = render(contract, base)
        self.assert_refused(mol.gate(launcher), "authorized-root", "unauthorized-root")

    def test_a_launcher_that_cannot_dry_run_refuses(self) -> None:
        repository, launcher = self.repository()
        # A truncated bundle: the contract still validates in process, but the launcher
        # itself cannot run, which only executing --dry-run can find out.
        (mol.bundle_of(launcher) / "runtime.py").write_text("def (\n", encoding="utf-8")
        report = mol.gate(launcher)
        self.assert_refused(report, "launcher-dry-run", "dry-run-failed")

    def test_a_live_run_is_attached_to_rather_than_started_twice(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        bundle.run_dir.mkdir(parents=True)
        handle = (bundle.run_dir / "lock").open("a+")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = mol.gate(launcher)
        self.assertEqual(report.decision, "attach")
        self.assertEqual(report.reason, "already-running")
        self.assertIn("attach", report.next_action)

    def test_the_gate_never_leaves_state_behind(self) -> None:
        repository, launcher = self.repository()
        mol.gate(launcher)
        self.assertFalse((repository / ".mixture-of-loops" / "runs").exists())
        self.assertFalse((repository / "done").exists())


# ── the telemetry reader ──────────────────────────────────────────────────────

class TelemetryTests(Fixture):
    def run_record(self, *, pid: int | None = None, offset: int = 0,
                   stage_count: int = 2, **extra) -> dict:
        run_dir = self.workspace() / "runs" / "fixture-pipeline"
        run_dir.mkdir(parents=True)
        record = {"schema": "mixture-of-loops/harness-run/1", "pipeline": "fixture-pipeline",
                  "mode": "auto", "launcher": "/x/run.sh", "argv": ["/x/run.sh", "--no-color"],
                  "cwd": "/x", "contract": "/x/c.json", "bundle": "/x/b",
                  "cmdline_markers": ["/x/run.sh"], "contract_digest": "digest-a",
                  "flags": {"implement": False, "smoke": False},
                  "pid": pid if pid is not None else dead_pid(), "pgid": 1,
                  "launched_at": time.time() - 30, "first_launched_at": time.time() - 30,
                  "launcher_log_offset": offset, "run_dir": str(run_dir),
                  "state_path": str(run_dir / "state.json"),
                  "launcher_log": str(run_dir / "launcher.log"),
                  "launch_log": str(run_dir / "harness-launch.log"),
                  "stage_count": stage_count,
                  "relaunch_budget": {"max_relaunches": 2, "remaining": 2,
                                      "wall_clock_seconds": 600,
                                      "deadline": time.time() + 600,
                                      "declared_pipeline_seconds": 200, "source": "default"},
                  "relaunches": [], "supervision": {}}
        record.update(extra)
        return record

    @staticmethod
    def write_state(record: dict, value: object) -> None:
        text = value if isinstance(value, str) else json.dumps(value)
        Path(record["state_path"]).write_text(text, encoding="utf-8")

    @staticmethod
    def write_log(record: dict, *lines: str) -> None:
        with Path(record["launcher_log"]).open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def test_every_file_may_be_absent(self) -> None:
        record = self.run_record()
        observation = mol.observe(record)
        self.assertEqual(observation.status, "unknown-terminal")
        self.assertIn("wrote no terminal state", " ".join(observation.anomalies))
        self.assertTrue(observation.terminal)

    def test_a_missing_state_before_preflight_is_normal_while_the_process_lives(self) -> None:
        record = self.run_record(pid=os.getpid(), cmdline_markers=[])
        observation = mol.observe(record)
        self.assertEqual(observation.status, "starting")
        self.assertEqual(observation.anomalies, [])
        self.assertFalse(observation.terminal)

    def test_the_full_sequence_reads_through_to_completed(self) -> None:
        record = self.run_record(pid=os.getpid(), cmdline_markers=[])
        stages = {"one": {"status": "completed", "started_at": 10, "finished_at": 20,
                          "child_exit_code": 0, "reason": "completed"}}
        seen = []
        for state, stage in (("preflight", None), ("ready", None), ("running", "one")):
            self.write_state(record, {"pipeline": "fixture-pipeline", "state": state,
                                      "contract_digest": "digest-a", "last_stage": stage,
                                      "stages": stages if stage else {}})
            seen.append(mol.observe(record).status)
        self.assertEqual(seen, ["starting", "running", "running"])
        record["pid"] = dead_pid()
        self.write_state(record, {"pipeline": "fixture-pipeline", "state": "completed",
                                  "contract_digest": "digest-a", "last_stage": "one",
                                  "exit_reason": "all-stages-complete", "stages": stages})
        self.write_log(record, "[DIGEST] state=completed exit=0 last-stage=one evidence=/e")
        observation = mol.observe(record)
        self.assertEqual((observation.status, observation.launcher_exit), ("completed", 0))
        self.assertEqual(observation.evidence, "/e")

    def test_each_terminal_state_reads_as_itself(self) -> None:
        cases = [
            ("completed", "[DIGEST] state=completed exit=0 last-stage=one evidence=/e", "completed", 0),
            ("failed", "[DIGEST] state=failed exit=22 last-stage=one child-exit=4 evidence=/e", "failed", 22),
            ("stopped", "[DIGEST] state=stopped exit=143 last-stage=one child-exit=None evidence=/e", "stopped", 143),
            ("failed", "[DIGEST] state=failed exit=21 last-stage=none evidence=/e", "failed", 21),
        ]
        for run_state, line, status, exit_code in cases:
            with self.subTest(line=line):
                record = self.run_record()
                self.write_state(record, {"state": run_state, "last_stage": "one",
                                          "contract_digest": "digest-a", "stages": {}})
                self.write_log(record, line)
                observation = mol.observe(record)
                self.assertEqual(observation.status, status)
                self.assertEqual(observation.launcher_exit, exit_code)

    def test_a_lock_conflict_is_not_this_runs_state(self) -> None:
        record = self.run_record()
        self.write_state(record, {"state": "running", "last_stage": "one",
                                  "contract_digest": "digest-a", "stages": {}})
        self.write_log(record, "[BLOCKED] another launcher owns /x",
                       "[DIGEST] state=failed exit=24 last-stage=none evidence=/x")
        self.assertEqual(mol.observe(record).status, "lock-conflict")

    def test_an_unparsable_state_is_an_anomaly_not_a_crash(self) -> None:
        record = self.run_record(pid=os.getpid(), cmdline_markers=[])
        self.write_state(record, "{not json")
        observation = mol.observe(record)
        self.assertEqual(observation.status, "starting")
        self.assertIn("could not be read", " ".join(observation.anomalies))
        self.write_state(record, "[1, 2]")
        self.assertIn("not a JSON object", " ".join(mol.observe(record).anomalies))

    def test_a_running_state_with_a_dead_process_is_unknown_terminal(self) -> None:
        record = self.run_record()
        self.write_state(record, {"state": "running", "last_stage": "one",
                                  "contract_digest": "digest-a",
                                  "stages": {"one": {"status": "running", "started_at": 1}}})
        observation = mol.observe(record)
        self.assertEqual(observation.status, "unknown-terminal")
        self.assertFalse(observation.status == "completed")
        self.assertIn("wrote no terminal state", " ".join(observation.anomalies))

    def test_only_this_runs_own_log_lines_are_read(self) -> None:
        record = self.run_record()
        self.write_log(record, "[DIGEST] state=completed exit=0 last-stage=old evidence=/old")
        record["launcher_log_offset"] = Path(record["launcher_log"]).stat().st_size
        self.write_state(record, {"state": "failed", "last_stage": "one",
                                  "contract_digest": "digest-a", "stages": {}})
        observation = mol.observe(record)
        self.assertEqual(observation.status, "failed")
        self.assertIsNone(observation.launcher_exit, "an earlier run's digest is not ours")

    def test_a_launch_that_never_started_surfaces_the_harness_launch_log(self) -> None:
        record = self.run_record()
        Path(record["launch_log"]).write_text(
            "/x/run.sh: line 1: python3: No such file or directory\n", encoding="utf-8")
        observation = mol.observe(record)
        self.assertEqual(observation.status, "unknown-terminal")
        self.assertIn("No such file", observation.launch_log_tail)
        self.assertIn("launch-log=", " ".join(mol.anomaly_messages(observation)))

    def test_a_stale_digest_on_stderr_is_still_read(self) -> None:
        record = self.run_record()
        Path(record["launch_log"]).write_text(
            "[STALE] sources[0] hash changed: /x/spec.md\n"
            "[DIGEST] state=failed exit=23 last-stage=none evidence=none\n", encoding="utf-8")
        observation = mol.observe(record)
        self.assertEqual((observation.status, observation.launcher_exit), ("failed", 23))

    def test_the_latest_recovery_detail_is_carried(self) -> None:
        record = self.run_record(pid=os.getpid(), cmdline_markers=[])
        self.write_state(record, {"state": "running", "last_stage": "one",
                                  "contract_digest": "digest-a",
                                  "stages": {"one": {"status": "running", "started_at": 1}}})
        self.write_log(record, "[RUN] one attempt 1/2", "[RECOVER] one exit 4, "
                       "reason=wall_budget; retry in 5s", "[RUN] one attempt 2/2")
        observation = mol.observe(record)
        self.assertTrue(observation.detail.startswith("RECOVER: one exit 4"))
        self.assertIn("detail=", mol.state_message(observation))

    def test_the_stage_position_comes_from_contract_order(self) -> None:
        repository, launcher = self.repository(
            [instant_stage(), {"id": "verify", "kind": "command", "depends_on": ["make-evidence"],
                               "cwd": ".", "action": {"argv": ["/bin/true"], "timeout_seconds": 5},
                               "preconditions": [], "evidence": [],
                               "postconditions": [{"type": "file_exists", "path": "done"}]}])
        bundle = mol.read_bundle(launcher)
        record = self.run_record()
        self.write_state(record, {"state": "running", "last_stage": "verify",
                                  "contract_digest": "digest-a",
                                  "stages": {"verify": {"status": "running", "started_at": 1}}})
        observation = mol.observe(record, bundle)
        self.assertEqual((observation.stage_index, observation.stage_count), (2, 2))
        self.assertIn("stage=2/2", mol.state_message(observation))

    def test_progress_never_comes_from_prose(self) -> None:
        record = self.run_record()
        self.write_log(record, "the model believes stage 2 of 2 is nearly done",
                       "Progress: 90% complete")
        observation = mol.observe(record)
        self.assertEqual((observation.stage_index, observation.detail), (0, ""))


# ── the reporting contract ────────────────────────────────────────────────────

class ReportingTests(TelemetryTests):
    def test_a_scripted_sequence_produces_exactly_the_specified_messages(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = self.run_record(stage_count=1)
        record["contract_digest"] = bundle.digest
        steps = [
            {"state": "preflight", "stages": {}},
            {"state": "running", "last_stage": "make-evidence",
             "stages": {"make-evidence": {"status": "running", "started_at": int(time.time())}}},
            {"state": "running", "last_stage": "make-evidence",
             "stages": {"make-evidence": {"status": "running", "started_at": int(time.time())}}},
            {"state": "completed", "last_stage": "make-evidence", "exit_reason": "all-stages-complete",
             "stages": {"make-evidence": {"status": "completed", "child_exit_code": 0,
                                          "reason": "completed", "started_at": int(time.time()),
                                          "finished_at": int(time.time())}}},
        ]
        lines: list[str] = []
        alive = [True, True, True, False]

        def clock() -> float:
            return time.time()

        for index, step in enumerate(steps):
            record["pid"] = os.getpid() if alive[index] else dead_pid()
            record["cmdline_markers"] = []
            self.write_state(record, {**step, "contract_digest": bundle.digest})
            if step["state"] == "completed":
                self.write_log(record, "[DIGEST] state=completed exit=0 "
                               "last-stage=make-evidence evidence=/e")
            observation, _ = mol.report_once(record, bundle, emit=lines.append, clock=clock,
                                             base=1, cap=2)
            if observation.terminal:
                lines.append(mol.digest_message(observation, record,
                                                mol.classify_relaunch(observation, record, bundle)))
        labels = [line.split(" ", 1)[0] for line in lines]
        self.assertEqual(labels, ["[MOL-STATE]", "[MOL-STATE]", "[MOL-HOLD]", "[MOL-DIGEST]"])
        self.assertIn("unchanged=1", lines[2])
        self.assertIn("state=completed exit=0", lines[3])
        self.assertIn("next=", lines[3])

    def test_each_terminal_reading_is_different(self) -> None:
        record = self.run_record()
        readings = {}
        for status in ("completed", "failed", "stopped", "unknown-terminal"):
            observation = mol.Observation(status=status, pipeline="p", alive=False)
            readings[status] = mol.digest_message(observation, record, None)
        self.assertEqual(len(set(readings.values())), 4)
        self.assertIn("intentional", readings["stopped"])
        self.assertIn("without writing a terminal state", readings["unknown-terminal"])
        self.assertNotIn("completed", readings["unknown-terminal"].split("next=")[1])

    def test_the_launch_message_names_everything_the_operator_needs(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="auto")
        self.reap(record)
        line = mol.launch_message(record)
        for fragment in ("pipeline=fixture-pipeline", "mode=auto", f"launcher={launcher}",
                         f"run-dir={bundle.run_dir}", "stages=1", 'flags="--no-color"',
                         "relaunch-budget=2/2", "wall-clock-ceiling=", "stop=\"kill -TERM -"):
            self.assertIn(fragment, line)

    def test_every_message_survives_a_lost_stream_in_the_run_directory(self) -> None:
        run_dir = self.workspace() / "runs" / "fixture-pipeline"
        out = mol.report_log(run_dir)
        with contextlib.redirect_stdout(io.StringIO()):
            out("[MOL-LAUNCH] pipeline=p mode=auto")
            out('[MOL-STATE] pipeline=p stage=1/2 id=one state=running detail="x"')
        written = (run_dir / "harness-report.log").read_text(encoding="utf-8")
        self.assertEqual(written.splitlines(),
                         ["[MOL-LAUNCH] pipeline=p mode=auto",
                          '[MOL-STATE] pipeline=p stage=1/2 id=one state=running detail="x"'])
        self.assertNotIn("\x1b[", written)

    def test_the_report_log_never_stops_a_run_from_being_supervised(self) -> None:
        blocked = self.workspace() / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            mol.report_log(blocked / "runs")("[MOL-STATE] pipeline=p state=running")

    def test_the_launch_record_points_at_the_report_log(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="auto")
        self.reap(record)
        self.assertEqual(record["report_log"], str(bundle.run_dir / "harness-report.log"))

    def test_the_cadence_starts_no_tighter_than_a_minute_and_backs_off(self) -> None:
        self.assertEqual(mol.BASE_POLL_SECONDS, 60)
        observation = mol.Observation(status="running", pipeline="p", alive=True)
        interval = mol.BASE_POLL_SECONDS
        seen = []
        for _ in range(8):
            interval = mol.next_interval(interval, observation, changed=False)
            seen.append(interval)
        self.assertEqual(seen[0], 90)
        self.assertTrue(all(b >= a for a, b in zip(seen, seen[1:])))
        self.assertEqual(seen[-1], min(seen[-1], mol.MAX_POLL_SECONDS))
        self.assertLessEqual(max(seen), mol.MAX_POLL_SECONDS)

    def test_a_stage_boundary_or_a_recovery_polls_sooner(self) -> None:
        observation = mol.Observation(status="running", pipeline="p", alive=True)
        self.assertEqual(mol.next_interval(400, observation, changed=True), mol.BASE_POLL_SECONDS)
        recovering = mol.Observation(status="running", pipeline="p", alive=True,
                                     detail="RECOVER: one exit 4")
        self.assertEqual(mol.next_interval(400, recovering, changed=False), mol.BASE_POLL_SECONDS)

    def test_a_quiet_hold_is_one_line_rather_than_the_full_state_repeated(self) -> None:
        observation = mol.Observation(status="running", pipeline="p", alive=True,
                                      stage_id="one", stage_index=1, stage_count=2)
        hold = mol.hold_message(observation, 4)
        self.assertIn("unchanged=4", hold)
        self.assertNotIn("stage-elapsed", hold)


# ── the relaunch classifier ───────────────────────────────────────────────────

RETRY_STAGE = {
    "id": "run-feature", "kind": "specstride", "depends_on": [], "cwd": ".",
    "action": {"argv": ["/bin/true"], "timeout_seconds": 60},
    "preconditions": [], "postconditions": [{"type": "file_exists", "path": "done"}],
    "recovery": {"max_attempts": 2, "backoff_seconds": [0], "retry_exit_codes": [4],
                 "total_timeout_seconds": 120,
                 "reason": {"jsonl": "events.jsonl", "event": "run_stop", "field": "reason",
                            "allowed": ["wall_budget"]}},
    "evidence": ["done"],
}


class RelaunchClassifierTests(Fixture):
    def setUp(self) -> None:
        self.repo, self.launcher = self.repository([RETRY_STAGE])
        self.bundle = mol.read_bundle(self.launcher)

    def record(self, **extra) -> dict:
        record = {"pipeline": "fixture-pipeline", "launcher": str(self.launcher),
                  "run_dir": str(self.bundle.run_dir), "contract_digest": self.bundle.digest,
                  "relaunch_budget": {"max_relaunches": 2, "remaining": 2,
                                      "wall_clock_seconds": 600, "deadline": 10_000,
                                      "declared_pipeline_seconds": 120, "source": "default"},
                  "relaunches": []}
        record.update(extra)
        return record

    def observation(self, **extra) -> mol.Observation:
        values = {"status": "failed", "pipeline": "fixture-pipeline", "alive": False,
                  "run_state": "failed", "stage_id": "run-feature", "stage_index": 1,
                  "stage_count": 1, "launcher_exit": mol.E_STAGE, "child_exit": 4,
                  "stage_reason": "exit", "stop_reason": "wall_budget",
                  "contract_digest": self.bundle.digest}
        values.update(extra)
        return mol.Observation(**values)

    def test_the_only_relaunchable_combination_relaunches(self) -> None:
        decision = mol.classify_relaunch(self.observation(), self.record(), self.bundle, now=0)
        self.assertTrue(decision.relaunch)
        self.assertEqual(decision.reason, "classified-transient")
        self.assertIn("wall_budget", decision.detail)

    # exit code x state x stage reason x run_stop.reason x budget, exhaustively
    TABLE = [
        # what differs from the one relaunchable combination, and why it refuses
        ({"status": "running", "alive": True}, "still-running"),
        ({"status": "completed", "launcher_exit": 0, "run_state": "completed"}, "completed"),
        ({"status": "stopped", "launcher_exit": 143, "run_state": "stopped"}, "deliberate-stop"),
        ({"status": "stopped", "launcher_exit": 22, "run_state": "stopped",
          "child_exit": 6}, "deliberate-stop"),
        ({"status": "unknown-terminal", "launcher_exit": None}, "unknown-terminal"),
        ({"status": "lock-conflict", "launcher_exit": 24}, "lock-conflict"),
        ({"launcher_exit": None}, "unknown-launcher-exit"),
        ({"launcher_exit": 20}, "invalid-contract"),
        ({"launcher_exit": 21}, "preflight-blocked"),
        ({"launcher_exit": 23}, "stale-sources"),
        ({"launcher_exit": 24}, "lock-conflict"),
        ({"launcher_exit": 130}, "signal-stop"),
        ({"launcher_exit": 143}, "signal-stop"),
        ({"run_state": "running"}, "state-not-failed"),
        ({"run_state": None}, "state-not-failed"),
        ({"stage_id": "no-such-stage"}, "unknown-stage"),
        ({"stage_reason": "postcondition"}, "postcondition-failure"),
        ({"stage_reason": "stage-precondition"}, "stage-precondition"),
        ({"stage_reason": "setup-required"}, "setup-required"),
        ({"stage_reason": "interrupted"}, "interrupted"),
        ({"stage_reason": "exec-error:[Errno 2] no such file"}, "exec-error"),
        ({"stage_reason": "something-new"}, "stage-reason-not-transient"),
        ({"stage_reason": None}, "stage-reason-not-transient"),
        ({"child_exit": 1}, "child-exit-not-retryable"),
        ({"child_exit": None}, "child-exit-not-retryable"),
        ({"stop_reason": "consecutive_errors"}, "stop-reason-not-allowed"),
        ({"stop_reason": None}, "stop-reason-not-allowed"),
        ({"contract_digest": "a-different-digest"}, "contract-digest-changed"),
    ]

    def test_every_non_transient_combination_refuses_for_its_own_reason(self) -> None:
        for difference, reason in self.TABLE:
            with self.subTest(difference=difference):
                decision = mol.classify_relaunch(self.observation(**difference),
                                                 self.record(), self.bundle, now=0)
                self.assertFalse(decision.relaunch, decision)
                self.assertEqual(decision.reason, reason, decision)
                self.assertTrue(decision.detail, "every refusal states why")

    def test_the_transient_stage_reasons_are_the_ones_the_runtime_records(self) -> None:
        for reason in ("exit", "timeout", "recovery-deadline", "recovery-exhausted"):
            with self.subTest(reason=reason):
                decision = mol.classify_relaunch(self.observation(stage_reason=reason),
                                                 self.record(), self.bundle, now=0)
                self.assertTrue(decision.relaunch, decision)

    def test_an_exhausted_or_expired_budget_refuses(self) -> None:
        spent = self.record()
        spent["relaunch_budget"]["remaining"] = 0
        decision = mol.classify_relaunch(self.observation(), spent, self.bundle, now=0)
        self.assertEqual((decision.relaunch, decision.reason), (False, "budget-exhausted"))
        expired = self.record()
        decision = mol.classify_relaunch(self.observation(), expired, self.bundle, now=20_000)
        self.assertEqual((decision.relaunch, decision.reason), (False, "budget-deadline-exceeded"))

    def test_an_operator_stop_is_intentional_and_is_never_relaunched(self) -> None:
        stopped = self.record(stop_requested={"signal": 15, "at": 1})
        decision = mol.classify_relaunch(self.observation(), stopped, self.bundle, now=0)
        self.assertEqual((decision.relaunch, decision.reason), (False, "operator-stop"))

    def test_a_changed_contract_refuses_rather_than_silently_resetting_the_stage_map(self) -> None:
        # the run state moved to another contract
        moved = mol.classify_relaunch(self.observation(contract_digest="other"), self.record(),
                                      self.bundle, now=0)
        self.assertEqual(moved.reason, "contract-digest-changed")
        self.assertIn("silently reset", moved.detail)
        # the launcher now points at another contract
        rendered = mol.classify_relaunch(self.observation(),
                                         self.record(contract_digest="stale-record"),
                                         self.bundle, now=0)
        self.assertEqual(rendered.reason, "contract-digest-changed")

    def test_a_launcher_edited_since_launch_refuses(self) -> None:
        self.launcher.write_text(self.launcher.read_text(encoding="utf-8") + "# edit\n",
                                 encoding="utf-8")
        decision = mol.classify_relaunch(self.observation(), self.record(), self.bundle, now=0)
        self.assertEqual((decision.relaunch, decision.reason), (False, "launcher-edited"))

    def test_a_stage_without_a_reason_constraint_only_needs_the_exit_code(self) -> None:
        # Only a Specstride stage retrying exit 4 must bind a reason (contract_lib);
        # a plain command stage may retry on the exit code alone.
        stage = json.loads(json.dumps(RETRY_STAGE))
        stage["kind"] = "command"
        del stage["recovery"]["reason"]
        repository, launcher = self.repository([stage])
        bundle = mol.read_bundle(launcher)
        record = self.record(launcher=str(launcher), contract_digest=bundle.digest,
                             run_dir=str(bundle.run_dir))
        observation = self.observation(contract_digest=bundle.digest, stop_reason=None)
        self.assertTrue(mol.classify_relaunch(observation, record, bundle, now=0).relaunch)


# ── the launch record and the budget ──────────────────────────────────────────

class LaunchRecordTests(Fixture):
    def test_the_record_carries_every_field_a_supervisor_needs(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="auto", implement=False, smoke=False)
        self.reap(record)
        for key in ("launcher", "argv", "pid", "pgid", "launched_at", "first_launched_at",
                    "contract_digest", "mode", "relaunch_budget", "run_dir", "state_path",
                    "launcher_log", "launch_log", "launcher_log_offset", "stage_count"):
            self.assertIn(key, record)
        self.assertEqual(record["argv"], [str(launcher), "--no-color"])
        self.assertTrue(all(isinstance(item, str) for item in record["argv"]))
        self.assertEqual(record["contract_digest"], bundle.digest)
        self.assertEqual(record["relaunch_budget"]["remaining"], 2)
        # the supervisor's own memory lives beside the run state, never outside it
        written = json.loads((bundle.run_dir / "harness-run.json").read_text(encoding="utf-8"))
        self.assertEqual(written["pid"], record["pid"])
        self.assertTrue(str(bundle.run_dir).startswith(str(repository / ".mixture-of-loops")))

    def test_the_argv_is_an_array_and_never_a_shell_string(self) -> None:
        argv = mol.launch_argv(Path("/a b/run.sh"), implement=True, smoke=True)
        self.assertEqual(argv, ["/a b/run.sh", "--no-color", "--implement", "--smoke"])
        self.assertNotIn("-c", argv)

    def test_the_flags_only_travel_when_they_were_asked_for(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="run", implement=True, smoke=True)
        self.reap(record)
        self.assertEqual(record["argv"][1:], ["--no-color", "--implement", "--smoke"])
        self.assertEqual(record["flags"], {"implement": True, "smoke": True})

    def test_both_logs_stay_free_of_escape_sequences(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="run")
        self.reap(record)
        deadline = time.time() + 30
        while mol.process_alive(record["pid"], record["cmdline_markers"]) and time.time() < deadline:
            time.sleep(0.1)
        for name in ("launcher.log", "harness-launch.log"):
            content = (bundle.run_dir / name).read_bytes()
            self.assertNotIn(b"\x1b[", content, name)

    def test_a_relaunch_decrements_the_budget_and_records_its_justification(self) -> None:
        repository, launcher = self.repository()
        bundle = mol.read_bundle(launcher)
        record = mol.launch(bundle, mode="auto")
        self.reap(record)
        first = record["pid"]
        record = mol.relaunch(record, justification={"reason": "classified-transient",
                                                     "launcher_exit": 22, "child_exit": 4,
                                                     "stop_reason": "wall_budget"})
        self.reap(record)
        self.assertEqual(record["relaunch_budget"]["remaining"], 1)
        self.assertEqual(len(record["relaunches"]), 1)
        self.assertEqual(record["relaunches"][0]["reason"], "classified-transient")
        self.assertEqual(record["relaunches"][0]["remaining"], 1)
        self.assertNotEqual(record["pid"], first)
        self.assertEqual(record["first_launched_at"],
                         json.loads((bundle.run_dir / "harness-run.json")
                                    .read_text(encoding="utf-8"))["first_launched_at"])


class BudgetTests(Fixture):
    def declared(self, **auto) -> dict:
        base = self.workspace()
        contract = contract_for(base, [RETRY_STAGE])
        if auto:
            contract["configuration"] = {"auto": auto}
        return contract

    def validate(self, contract: dict) -> subprocess.CompletedProcess[str]:
        base = self.workspace()
        path = base / "c.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPTS / "validate_contract.py"), str(path),
                               "--no-source-check"], capture_output=True, text=True, check=False,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_the_default_applies_when_the_block_is_absent(self) -> None:
        budget = contract_lib.auto_budget(self.declared())
        self.assertEqual(budget["max_relaunches"], 2)
        self.assertEqual(budget["source"], "default")
        # 2 attempts x 60s + 0s backoff, three launcher attempts
        self.assertEqual(budget["declared_pipeline_seconds"], 120)
        self.assertEqual(budget["wall_clock_seconds"], 360)

    def test_a_declared_budget_is_used_as_declared(self) -> None:
        budget = contract_lib.auto_budget(self.declared(max_relaunches=1, wall_clock_seconds=200))
        self.assertEqual((budget["max_relaunches"], budget["wall_clock_seconds"],
                          budget["source"]), (1, 200.0, "declared"))

    def test_existing_contracts_without_the_block_still_validate(self) -> None:
        self.assertEqual(self.validate(self.declared()).returncode, 0)

    def test_a_budget_outside_its_declared_bounds_is_rejected(self) -> None:
        cases = [
            ({"max_relaunches": -1}, "max_relaunches must be an integer in 0..5"),
            ({"max_relaunches": 6}, "max_relaunches must be an integer in 0..5"),
            ({"max_relaunches": "two"}, "max_relaunches must be an integer in 0..5"),
            ({"max_relaunches": True}, "max_relaunches must be an integer in 0..5"),
            ({"wall_clock_seconds": 0}, "wall_clock_seconds must be a positive number"),
            ({"wall_clock_seconds": -5}, "wall_clock_seconds must be a positive number"),
            # 2 relaunches means 3 attempts of a 120s pipeline: 360s is the ceiling
            ({"max_relaunches": 2, "wall_clock_seconds": 361}, "above the 360s the stages declare"),
            ({"max_relaunches": 0, "wall_clock_seconds": 121}, "above the 120s the stages declare"),
            ({"relaunches": 2}, "unsupported keys: relaunches"),
        ]
        for auto, message in cases:
            with self.subTest(auto=auto):
                result = self.validate(self.declared(**auto))
                self.assertEqual(result.returncode, 20, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)

    def test_a_budget_at_its_bound_is_accepted(self) -> None:
        for auto in ({"max_relaunches": 0}, {"max_relaunches": 5},
                     {"max_relaunches": 2, "wall_clock_seconds": 360},
                     {"wall_clock_seconds": 1}):
            with self.subTest(auto=auto):
                self.assertEqual(self.validate(self.declared(**auto)).returncode, 0)

    def test_the_declared_budget_reaches_the_launch_record(self) -> None:
        base = self.workspace()
        contract = contract_for(base, [RETRY_STAGE])
        contract["configuration"] = {"auto": {"max_relaunches": 1, "wall_clock_seconds": 90}}
        bundle = mol.read_bundle(render(contract, base))
        record = mol.launch(bundle, mode="auto")
        self.reap(record)
        self.assertEqual(record["relaunch_budget"]["max_relaunches"], 1)
        self.assertEqual(record["relaunch_budget"]["source"], "declared")
        self.assertAlmostEqual(record["relaunch_budget"]["deadline"] - record["launched_at"], 90, 1)


# ── the stop path ─────────────────────────────────────────────────────────────

SLEEPER = ("#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\n"
           "Path('started').touch()\ntime.sleep(120)\n")


class StopTests(Fixture):
    def test_a_stop_is_reported_as_intentional_and_never_relaunched(self) -> None:
        base = self.workspace()
        worker = base / "sleeper"
        worker.write_text(SLEEPER, encoding="utf-8")
        worker.chmod(0o755)
        stage = {"id": "long-run", "kind": "command", "depends_on": [], "cwd": ".",
                 "action": {"argv": [str(worker)], "timeout_seconds": 300},
                 "preconditions": [], "postconditions": [{"type": "file_exists", "path": "done"}],
                 "evidence": ["started"]}
        bundle = mol.read_bundle(render(contract_for(base, [stage]), base))
        record = mol.launch(bundle, mode="auto")
        self.reap(record)
        deadline = time.time() + 30
        while not (base / "started").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue((base / "started").exists(), "the stage never started")

        sent, detail = mol.request_stop(record)
        self.assertTrue(sent, detail)
        deadline = time.time() + 30
        while mol.process_alive(record["pid"], record["cmdline_markers"]) and time.time() < deadline:
            time.sleep(0.1)
        observation = mol.observe(record, bundle)
        self.assertEqual(observation.status, "stopped")
        self.assertEqual(observation.launcher_exit, 128 + signal.SIGTERM)
        decision = mol.classify_relaunch(observation, record, bundle)
        self.assertFalse(decision.relaunch)
        self.assertEqual(decision.reason, "operator-stop")
        self.assertIn("intentional", mol.digest_message(observation, record, decision))


if __name__ == "__main__":
    unittest.main()


# ── retro: a read-only retrospective of the pipeline's learning state ─────────

STUB_SPECSTRIDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
mode = next((a for a in args if a in ("--summarize", "--evaluate")), None)
if mode == "--evaluate" and os.environ.get("STUB_NO_EVALUATE"):
    sys.stderr.write("specstride learn: unknown option '--evaluate'\\n")
    sys.exit(1)
if "--help" in args:
    print("usage: learn.py ...")
    sys.exit(0)
if mode == "--summarize":
    print(json.dumps({"schema": "specstride.learn.summary/7", "totals": {"runs": 2},
                      "phases": {"3": {"title": "Build", "attempts_total": 4, "cost_usd": 9.5}}}))
elif mode == "--evaluate":
    print("learn: phase 3 proposer_timeout 1800s→2700s [learn-0a1b2c]: regressed — cost r=+1.40 (MDE ±1.15, n=7/6)")
    print("learn: phase 4 proposer_timeout 1800s→1200s [learn-3d4e5f]: neutral — cost r=-0.20 (MDE ±1.15, n=6/6)")
"""


def specstride_stage(*, recovery: bool = True, feature_paths: bool = True) -> dict:
    stage = {"id": "run-feature", "kind": "specstride", "depends_on": [], "cwd": ".",
             "action": {"argv": ["specstride", "run", "-w", ".", "--feature", "007"],
                        "timeout_seconds": 30},
             "preconditions": [],
             "postconditions": [{"type": "file_exists",
                                 "path": ".specstride/features/007/PROGRESS.md" if feature_paths else "done"}],
             "evidence": [".specstride/features/007" if feature_paths else "done"]}
    if recovery:
        stage["recovery"] = {"max_attempts": 2, "backoff_seconds": [1], "retry_exit_codes": [4],
                             "reason": {"jsonl": ".specstride/features/007/events.jsonl", "event": "run_stop",
                                        "field": "reason", "allowed": ["wall_budget"]}}
    return stage


class RetrospectiveTests(Fixture):
    def setup_stub(self, base: Path, **env: str) -> Path:
        bin_dir = base / "stub-bin"
        bin_dir.mkdir()
        stub = bin_dir / "specstride"
        stub.write_text(STUB_SPECSTRIDE, encoding="utf-8")
        stub.chmod(0o755)
        log = base / "stub-calls.jsonl"
        patched = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "STUB_LOG": str(log), **env}
        patcher = unittest.mock.patch.dict(os.environ, patched)
        patcher.start()
        self.addCleanup(patcher.stop)
        return log

    def snapshot(self, base: Path, run_dir: Path) -> dict:
        out = {}
        for path in base.rglob("*"):
            if path.is_file() and run_dir not in path.parents and path.name != "stub-calls.jsonl":
                out[str(path)] = path.read_bytes()
        return out

    def test_retro_reads_learning_state_and_writes_only_under_its_run_dir(self) -> None:
        base, launcher = self.repository([specstride_stage()])
        log = self.setup_stub(base)
        bundle = mol.read_bundle(launcher, check_sources=False)
        before = self.snapshot(base, bundle.run_dir)
        document = mol.retrospective(bundle)
        path = mol.write_retrospective(bundle.run_dir, document)
        self.assertEqual(path, bundle.run_dir / "retrospectives" / f"{bundle.digest}.json")
        written = json.loads(path.read_text())
        self.assertEqual(written["contract_digest"], bundle.digest)
        stage = written["stages"][0]
        self.assertEqual(stage["status"], "ok", stage)
        self.assertEqual(stage["feature"], "007")
        self.assertEqual(stage["workdir"], str(base))
        self.assertEqual(stage["totals"], {"runs": 2})
        self.assertEqual(len(stage["evaluations"]), 2)
        self.assertEqual(stage["suggestions"],
                         [f"specstride learn -w {base} --feature 007 --revert learn-0a1b2c"])
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(all(call[0] == "learn" for call in calls))
        # it suggests a revert; it never applies or reverts anything
        self.assertFalse(any(flag in call for call in calls for flag in ("--apply", "--revert", "--off")))
        self.assertEqual(self.snapshot(base, bundle.run_dir), before)

    def test_retro_reports_unavailable_when_specstride_lacks_evaluate(self) -> None:
        base, launcher = self.repository([specstride_stage()])
        self.setup_stub(base, STUB_NO_EVALUATE="1")
        stage = mol.retrospective(mol.read_bundle(launcher, check_sources=False))["stages"][0]
        self.assertEqual(stage["status"], "unavailable")
        self.assertIn("--evaluate", stage["reason"])

    def test_retro_on_a_stage_without_any_feature_path_is_unavailable(self) -> None:
        base, launcher = self.repository([specstride_stage(recovery=False, feature_paths=False)])
        self.setup_stub(base)
        stage = mol.retrospective(mol.read_bundle(launcher, check_sources=False))["stages"][0]
        self.assertEqual(stage["status"], "unavailable")
        self.assertIn("no Specstride feature path", stage["reason"])

    def test_retro_command_exits_zero_and_names_the_file(self) -> None:
        base, launcher = self.repository([specstride_stage(recovery=False)])   # evidence still names it
        self.setup_stub(base, STUB_NO_EVALUATE="1")
        result = subprocess.run([sys.executable, str(SCRIPTS / "supervise.py"), "retro", "--launcher",
                                 str(launcher)], capture_output=True, text=True, check=False,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[MOL-RETRO]", result.stdout)
        self.assertIn("status=unavailable", result.stdout)
        self.assertIn("retrospectives/", result.stdout)


# ── configuration.learning: a bound prefix of Specstride's decision log ──────

import bootstrap_contract  # noqa: E402

DECISIONS = [
    {"schema": "specstride.learn.applied/2", "action": "apply", "run_id": "learn-aaa", "knob": "proposer_timeout",
     "phase": 3, "shape": "s3", "value": 2400, "previous": 1800},
    {"schema": "specstride.learn.applied/2", "action": "evaluate", "run_id": "learn-bbb",
     "evaluates_run_id": "learn-aaa", "knob": "proposer_timeout", "phase": 3, "label": "neutral"},
]


def learning_stage(env: dict) -> dict:
    stage = json.loads(json.dumps(RETRY_STAGE))
    stage["action"]["env"] = dict(env)
    return stage


class LearningBindingTests(Fixture):
    def bound(self, mode: str = "apply", env: dict | None = None) -> tuple[Path, Path, Path, dict]:
        base = self.workspace()
        applied = base / ".specstride" / "features" / "007" / "learning" / "applied.json"
        applied.parent.mkdir(parents=True)
        applied.write_text("".join(json.dumps(d) + "\n" for d in DECISIONS), encoding="utf-8")
        block = bootstrap_contract.learning_block(base, applied, mode)
        stage_env = env if env is not None else {"SPECSTRIDE_LEARNING": mode,
                                                  "SPECSTRIDE_LEARNING_THROUGH": block["decisions_through"]}
        contract = contract_for(base, [learning_stage(stage_env)])
        contract["configuration"] = {"learning": block}
        return base, applied, render(contract, base), contract

    def test_bootstrap_binds_the_whole_log_with_its_hash_and_effective_values(self) -> None:
        base, applied, _launcher, contract = self.bound()
        block = contract["configuration"]["learning"]
        self.assertEqual(set(block), contract_lib.LEARNING_KEYS)
        self.assertEqual(block["decisions_through"], "learn-bbb")
        self.assertEqual(block["decisions_sha256"], digest(applied))
        self.assertEqual(block["effective"], {"proposer_timeout": {"3": 2400}})
        self.assertEqual(block["source_path"], ".specstride/features/007/learning/applied.json")

    def test_later_appends_keep_the_contract_valid(self) -> None:
        _base, applied, launcher, _contract = self.bound()
        with applied.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"action": "apply", "run_id": "learn-ccc", "knob": "proposer_timeout",
                                     "phase": 3, "value": 3000}) + "\n")
        self.assertEqual(mol.read_bundle(launcher).pipeline, "fixture-pipeline")

    def test_a_changed_prefix_is_refused_by_name_at_the_gate_and_the_relaunch(self) -> None:
        _base, applied, launcher, _contract = self.bound()
        bundle = mol.read_bundle(launcher)
        applied.write_text(applied.read_text().replace("2400", "2700"), encoding="utf-8")
        with self.assertRaises(contract_lib.LearningDecisionsChanged):
            mol.read_bundle(launcher)
        self.assertEqual(mol.gate(launcher).reason, "learning-decisions-changed")
        record = {"pipeline": "fixture-pipeline", "launcher": str(launcher), "run_dir": str(bundle.run_dir),
                  "contract_digest": bundle.digest,
                  "relaunch_budget": {"max_relaunches": 2, "remaining": 2, "wall_clock_seconds": 600,
                                      "deadline": 10_000, "declared_pipeline_seconds": 120, "source": "default"},
                  "relaunches": []}
        observation = mol.Observation(status="failed", pipeline="fixture-pipeline", alive=False,
                                      run_state="failed", stage_id="run-feature", stage_index=1,
                                      stage_count=1, launcher_exit=mol.E_STAGE, child_exit=4,
                                      stage_reason="exit", stop_reason="wall_budget",
                                      contract_digest=bundle.digest)
        stale = mol.read_bundle(launcher, check_sources=False)
        decision = mol.classify_relaunch(observation, record, stale, now=0)
        self.assertFalse(decision.relaunch)
        self.assertEqual(decision.reason, "learning-decisions-changed")

    def test_stage_literals_must_match_the_block(self) -> None:
        base = self.workspace()
        applied = base / "applied.json"
        applied.write_text("".join(json.dumps(d) + "\n" for d in DECISIONS), encoding="utf-8")
        block = bootstrap_contract.learning_block(base, applied, "apply")
        cases = {
            "mode": {"SPECSTRIDE_LEARNING": "suggest", "SPECSTRIDE_LEARNING_THROUGH": "learn-bbb"},
            "through": {"SPECSTRIDE_LEARNING": "apply", "SPECSTRIDE_LEARNING_THROUGH": "learn-aaa"},
            "missing": {"SPECSTRIDE_LEARNING": "apply"},
        }
        for name, env in cases.items():
            with self.subTest(name):
                contract = contract_for(base, [learning_stage(env)])
                contract["configuration"] = {"learning": block}
                with self.assertRaises(contract_lib.ContractError):
                    contract_lib.validate_contract(contract, check_sources=False)
        unbound = contract_for(base, [learning_stage({"SPECSTRIDE_LEARNING": "apply",
                                                      "SPECSTRIDE_LEARNING_THROUGH": "learn-bbb"})])
        with self.assertRaisesRegex(contract_lib.ContractError, "needs a configuration.learning"):
            contract_lib.validate_contract(unbound, check_sources=False)
        referenced = contract_for(base, [learning_stage({
            "SPECSTRIDE_LEARNING": "apply",
            "SPECSTRIDE_LEARNING_THROUGH": {"from_env": "THROUGH", "required": False}})])
        referenced["configuration"] = {"learning": block}
        with self.assertRaisesRegex(contract_lib.ContractError, "literal run id"):
            contract_lib.validate_contract(referenced, check_sources=False)
        broken = dict(block, extra=1)
        contract = contract_for(base, [learning_stage(cases["through"])])
        contract["configuration"] = {"learning": broken}
        with self.assertRaisesRegex(contract_lib.ContractError, "must have exactly"):
            contract_lib.validate_contract(contract, check_sources=False)

    def test_bootstrap_cli_emits_the_block_for_an_explicit_applied_file(self) -> None:
        base = self.workspace()
        feature = base / "specs" / "007-demo"
        feature.mkdir(parents=True)
        (feature / "tasks.md").write_text("## Phase 3: Build\n\n- [ ] T001 build it\n", encoding="utf-8")
        applied = base / "applied.json"
        applied.write_text(json.dumps(DECISIONS[0]) + "\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPTS / "bootstrap_contract.py"), "--repo", str(base),
                                 "--feature", str(feature), "--applied-file", str(applied),
                                 "--learning-mode", "suggest"],
                                capture_output=True, text=True, check=False,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        block = json.loads(result.stdout)["configuration"]["learning"]
        self.assertEqual(block["mode"], "suggest")
        self.assertEqual(block["decisions_through"], "learn-aaa")
