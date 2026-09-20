"""Tier 2: the real rendered launcher, a stubbed Specstride, and no model.

Everything here is the production path: the real `render_launcher.py` output, the real
`runtime.py` inside its content-addressed bundle, and the real supervisor. Only
Specstride is stubbed, by `tests/e2e/exec-stub-bin/specstride`, which acts like a
pipeline instead of refusing like the dry-run stub in `tests/e2e/stub-bin`.

The one reviewer command:

    MOL_SHOW_MESSAGES=1 python3 -m unittest tests.test_execution_e2e -v

which echoes every message the supervisor emitted for each scenario.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "mixture-of-loops" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import mol_e2e  # noqa: E402
import supervisor_lib as mol  # noqa: E402

EXEC_STUB_BIN = ROOT / "tests" / "e2e" / "exec-stub-bin"
FEATURE = "001-greeting"
STATE = f".specstride/features/{FEATURE}"
SHOW = os.environ.get("MOL_SHOW_MESSAGES") == "1"


def execution_contract(draft: dict, fixture: str) -> dict:
    """The reference completion, with the Specstride stage bound to what a real run
    leaves behind: its progress file, its event stream, and a declared transient."""
    contract = mol_e2e.reference_contract(draft, fixture)
    stage = contract["stages"][0]
    stage["postconditions"] = [{"type": "file_exists", "path": f"{STATE}/PROGRESS.md"}]
    stage["evidence"] = [STATE]
    stage["action"]["timeout_seconds"] = 120
    stage["recovery"] = {
        "max_attempts": 2,
        "backoff_seconds": [0],
        "retry_exit_codes": [4],
        "total_timeout_seconds": 240,
        "reason": {"jsonl": f"{STATE}/events.jsonl", "event": "run_stop",
                   "field": "reason", "allowed": ["wall_budget"]},
    }
    contract["stages"][1]["action"]["timeout_seconds"] = 120
    for entry in contract["coverage"]:
        entry["evidence"] = [STATE]
    return contract


class ExecutionCase(unittest.TestCase):
    """One fixture repository per test, with the execution stub on PATH."""

    maxDiff = None

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mol-exec-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.stub_log = self.work / "stub-calls.jsonl"
        self.messages: list[str] = []
        self.records: list[dict] = []
        self.addCleanup(self._reap_everything)
        self.addCleanup(self._show)

    # ── fixture and launcher ────────────────────────────────────────────────

    def prepare(self, fixture: str = "greeting-ready") -> Path:
        self.repo = mol_e2e.prepare_fixture(fixture, self.work / "repo")
        contract = self.repo / "launch-contract.json"
        result = mol_e2e.run_script("bootstrap_contract.py", "--repo", self.repo,
                                    "--feature", f"specs/{FEATURE}", "--output", contract,
                                    cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        draft = json.loads(contract.read_text(encoding="utf-8"))
        contract.write_text(json.dumps(execution_contract(draft, fixture), indent=2),
                            encoding="utf-8")
        self.contract = contract
        return self.repo

    def render(self) -> Path:
        launcher = self.repo / f".mixture-of-loops/{FEATURE}/run-{FEATURE}.sh"
        result = mol_e2e.run_script("render_launcher.py", "--contract", self.contract,
                                    "--output", launcher, cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.launcher = launcher
        return launcher

    def stub_environment(self, mode: str, **settings: str) -> None:
        """Put the execution stub on PATH for this test only, and set its behaviour."""
        previous = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(previous)))
        os.environ["PATH"] = f"{EXEC_STUB_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        os.environ["MOL_EXEC_STUB_MODE"] = mode
        os.environ["MOL_EXEC_STUB_LOG"] = str(self.stub_log)
        os.environ["MOL_EXEC_STUB_FEATURE"] = FEATURE
        os.environ["NO_COLOR"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        for key, value in settings.items():
            os.environ[f"MOL_EXEC_STUB_{key.upper()}"] = str(value)

    @property
    def stub_calls(self) -> list[dict]:
        if not self.stub_log.is_file():
            return []
        return [json.loads(line) for line in
                self.stub_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ── supervision ─────────────────────────────────────────────────────────

    def emit(self, line: str) -> None:
        self.messages.append(line)

    def gate(self, **flags) -> mol.GateReport:
        report = mol.gate(self.launcher, **flags)
        self.messages.extend(report.messages())
        return report

    def launch(self, mode: str = "auto", **flags) -> dict:
        report = self.gate(**flags)
        self.assertEqual(report.decision, "launch", report.messages())
        self.bundle = report.bundle
        record = mol.launch(report.bundle, mode=mode, **flags)
        self.records.append(record)
        self.emit(mol.launch_message(record))
        return record

    def watch(self, record: dict, *, no_relaunch: bool = False, **settings) -> tuple[str, dict]:
        options = {"base": 0.4, "cap": 1.0, "max_wait": 180, "allow_relaunch": not no_relaunch}
        options.update(settings)
        status, record = mol.supervise(record, self.bundle, emit=self.emit, **options)
        self.records.append(record)
        return status, record

    def wait_until(self, predicate, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def lines(self, label: str) -> list[str]:
        return [line for line in self.messages if line.startswith(f"[{label}]")]

    # ── teardown ────────────────────────────────────────────────────────────

    def _reap_everything(self) -> None:
        for record in self.records:
            group, pid = record.get("pgid"), record.get("pid")
            if isinstance(group, int) and group > 1:
                for number in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(group, number)
                    except (ProcessLookupError, PermissionError, OSError):
                        break
                    time.sleep(0.2)
            mol._forget_child(pid)

    def _show(self) -> None:
        if SHOW and self.messages:
            print(f"\n--- {self.id().rsplit('.', 1)[-1]} ---", file=sys.stderr)
            for line in self.messages:
                print("  " + line, file=sys.stderr)

    # ── shared assertions ───────────────────────────────────────────────────

    def assert_nothing_left_behind(self, record: dict) -> None:
        run_dir = Path(record["run_dir"])
        self.assertFalse(mol.process_alive(record["pid"], record["cmdline_markers"]),
                         "the launcher process outlived the run")
        self.assertTrue(mol.run_directory_free(run_dir), "the run lock is still held")
        for name in ("launcher.log", "harness-launch.log"):
            self.assertNotIn(b"\x1b[", (run_dir / name).read_bytes(), name)
        report = run_dir / "harness-report.log"
        if report.is_file():
            self.assertNotIn(b"\x1b[", report.read_bytes(), "harness-report.log")
        state = Path(record["state_path"])
        self.assertTrue(state.is_file(), "the run left no state to read")

    def assert_digest_matches_the_launcher(self, record: dict, observation) -> None:
        """The reported digest mirrors the launcher's own DIGEST line, not a summary."""
        labels = mol.read_labels(Path(record["launcher_log"]), record["launcher_log_offset"])
        own = [mol.parse_digest(f"[DIGEST] {message}") for label, message in labels
               if label == "DIGEST"]
        own = [value for value in own if value]
        self.assertTrue(own, "the launcher printed no DIGEST line")
        self.assertEqual(observation.launcher_exit, own[-1]["exit"])
        self.assertEqual(observation.stage_id, own[-1]["last_stage"])
        if own[-1]["child_exit"] is not None:
            # The launcher only prints child-exit on a failing path; on success the
            # stage record in state.json is where the child's 0 comes from.
            self.assertEqual(observation.child_exit, own[-1]["child_exit"])


class AutoRunTests(ExecutionCase):
    def test_auto_runs_the_ready_fixture_through_to_a_matching_digest(self) -> None:
        self.prepare()
        self.stub_environment("succeed")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "completed", self.messages)
        self.assertTrue((self.repo / STATE / "PROGRESS.md").is_file())
        state = json.loads(Path(record["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["exit_reason"], "all-stages-complete")
        self.assertEqual({name: value["status"] for name, value in state["stages"].items()},
                         {"implement-feature": "completed", "verify-greeting": "completed"})
        observation = mol.observe(record, self.bundle)
        self.assert_digest_matches_the_launcher(record, observation)
        self.assertTrue(self.lines("MOL-DIGEST")[-1].startswith(
            f"[MOL-DIGEST] pipeline={record['pipeline']} state=completed exit=0"))
        self.assert_nothing_left_behind(record)
        self.assertEqual(record["relaunch_budget"]["remaining"], 2, "nothing was relaunched")
        self.assertEqual(record["relaunches"], [])

    def test_the_run_is_detached_and_reported_while_it_is_still_running(self) -> None:
        self.prepare()
        self.stub_environment("sleep", sleep="4")
        self.render()
        record = self.launch("auto")
        self.assertTrue(self.wait_until(lambda: (self.repo / STATE / "started").is_file()),
                        "the stage never started")
        # The launcher is in a session of its own, with stdin closed: no terminal, no
        # parent shell, and it keeps going while the supervisor only reads.
        self.assertNotEqual(record["pgid"], os.getpgrp())
        live = mol.observe(record, self.bundle)
        self.assertEqual(live.status, "running")
        self.assertEqual((live.stage_index, live.stage_count), (1, 2))
        self.assertEqual(live.stage_id, "implement-feature")
        self.assertFalse(live.terminal)

        status, record = self.watch(record)
        self.assertEqual(status, "completed", self.messages)
        running = [line for line in self.lines("MOL-STATE") if "state=running" in line]
        self.assertTrue(running, "no intermediate state was reported from telemetry")
        self.assertIn("stage=1/2 id=implement-feature", running[0])
        self.assert_nothing_left_behind(record)

    def test_the_flags_only_reach_the_launcher_when_they_were_asked_for(self) -> None:
        self.prepare()
        self.stub_environment("succeed")
        self.render()
        record = self.launch("auto")
        self.assertEqual(record["argv"], [str(self.launcher), "--no-color"])
        self.assertNotIn("--dry-run", record["argv"])
        self.watch(record)


class RelaunchTests(ExecutionCase):
    def test_one_classified_transient_is_relaunched_within_budget(self) -> None:
        self.prepare()
        # Two failures exhaust the stage's own two attempts, so the launcher exits 22;
        # the third invocation is the supervisor's single relaunch.
        self.stub_environment("transient", fail_times="2")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "completed", self.messages)
        announced = self.lines("MOL-RELAUNCH")
        self.assertEqual(len(announced), 1, self.messages)
        self.assertIn("reason=classified-transient", announced[0])
        self.assertIn("exit=22 child-exit=4", announced[0])
        self.assertIn("stop-reason='wall_budget'", announced[0])
        self.assertIn("budget-remaining=1", announced[0])
        # announced before it happened
        self.assertLess(self.messages.index(announced[0]),
                        len(self.messages) - len(self.lines("MOL-LAUNCH")))
        self.assertEqual(record["relaunch_budget"]["remaining"], 1)
        self.assertEqual(len(record["relaunches"]), 1)
        self.assertEqual(record["relaunches"][0]["reason"], "classified-transient")
        self.assertEqual(record["relaunches"][0]["child_exit"], 4)
        self.assertEqual(record["relaunches"][0]["stop_reason"], "wall_budget")
        self.assertEqual([call["attempt"] for call in self.stub_calls], [1, 2, 3])
        self.assertEqual(self.lines("MOL-DIGEST")[-1].count("state=completed"), 1)
        self.assert_nothing_left_behind(record)

    def test_a_non_transient_exit_code_is_refused_rather_than_retried(self) -> None:
        self.prepare()
        self.stub_environment("fail", exit="3", reason="postcondition_failed")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "failed")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        digest = self.lines("MOL-DIGEST")[-1]
        self.assertIn("state=failed exit=22 child-exit=3", digest)
        self.assertIn("no relaunch (child-exit-not-retryable)", digest)
        self.assertEqual(record["relaunch_budget"]["remaining"], 2, "the budget was not spent")
        self.assertEqual(len(self.stub_calls), 1, "a non-retryable exit is not retried at all")
        self.assert_nothing_left_behind(record)

    def test_a_retryable_exit_with_a_disallowed_reason_is_refused(self) -> None:
        self.prepare()
        self.stub_environment("fail", exit="4", reason="consecutive_errors")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "failed")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        self.assertIn("no relaunch (stop-reason-not-allowed)", self.lines("MOL-DIGEST")[-1])
        self.assertEqual(len(self.stub_calls), 1,
                         "the launcher's own classification refused the retry too")
        self.assert_nothing_left_behind(record)

    def test_relaunching_can_be_turned_off_and_the_transient_is_then_reported(self) -> None:
        self.prepare()
        self.stub_environment("transient", fail_times="2")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record, no_relaunch=True)

        self.assertEqual(status, "failed")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        self.assertIn("no relaunch (relaunch-disabled)", self.lines("MOL-DIGEST")[-1])
        self.assertEqual(record["relaunch_budget"]["remaining"], 2)
        self.assertEqual([call["attempt"] for call in self.stub_calls], [1, 2])
        # the run would still have been relaunchable: the refusal was the operator's
        observation = mol.observe(record, self.bundle)
        self.assertTrue(mol.classify_relaunch(observation, record, self.bundle).relaunch)
        self.assert_nothing_left_behind(record)

    def test_a_deliberate_child_stop_stays_stopped(self) -> None:
        self.prepare()
        self.stub_environment("stop")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "stopped")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        self.assertIn("intentional", self.lines("MOL-DIGEST")[-1])
        self.assert_nothing_left_behind(record)

    def test_an_exhausted_budget_reports_instead_of_relaunching_forever(self) -> None:
        self.prepare()
        # never succeeds: two relaunches are the declared default, then it stops
        self.stub_environment("transient", fail_times="99")
        self.render()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "failed")
        self.assertEqual(len(self.lines("MOL-RELAUNCH")), 2)
        self.assertEqual(record["relaunch_budget"]["remaining"], 0)
        self.assertIn("no relaunch (budget-exhausted)", self.lines("MOL-DIGEST")[-1])
        self.assertEqual([call["attempt"] for call in self.stub_calls], [1, 2, 3, 4, 5, 6])
        self.assert_nothing_left_behind(record)


class StopTests(ExecutionCase):
    def test_a_sigterm_is_reported_as_an_intentional_stop_and_never_relaunched(self) -> None:
        self.prepare()
        self.stub_environment("sleep", sleep="120")
        self.render()
        record = self.launch("auto")
        self.assertTrue(self.wait_until(lambda: (self.repo / STATE / "started").is_file()))

        sent, detail = mol.request_stop(record)
        self.assertTrue(sent, detail)
        self.emit(f"[MOL-STOP] sent={sent} detail={detail}")
        status, record = self.watch(record, base=0.3)

        self.assertEqual(status, "stopped")
        observation = mol.observe(record, self.bundle)
        self.assertEqual(observation.launcher_exit, 128 + signal.SIGTERM)
        state = json.loads(Path(record["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "stopped")
        self.assertEqual(state["stages"]["implement-feature"]["status"], "stopped")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        digest = self.lines("MOL-DIGEST")[-1]
        self.assertIn("state=stopped", digest)
        self.assertIn("intentional", digest)
        self.assertEqual(mol.classify_relaunch(observation, record, self.bundle).reason,
                         "operator-stop")
        self.assert_nothing_left_behind(record)


class BlockedFixtureTests(ExecutionCase):
    def test_the_blocked_fixture_never_renders_never_launches_and_runs_nothing(self) -> None:
        self.prepare("greeting-blocked")
        self.stub_environment("succeed")
        launcher = self.repo / f".mixture-of-loops/{FEATURE}/run-{FEATURE}.sh"
        rendered = mol_e2e.run_script("render_launcher.py", "--contract", self.contract,
                                      "--output", launcher, cwd=self.repo)
        self.assertEqual(rendered.returncode, 20, rendered.stdout + rendered.stderr)
        self.assertIn("open blocker", rendered.stderr)
        self.assertFalse(launcher.exists())

        # There is nothing for `run` to resolve, and the refusal offers `auto` instead
        # of silently generating one.
        resolution = mol.resolve_launcher(self.repo, feature=f"specs/{FEATURE}")
        self.emit(resolution.message())
        self.assertIsNone(resolution.launcher)
        self.assertEqual(resolution.reason, "launcher-missing")

        self.assertEqual(self.stub_calls, [], "nothing ran for a blocked derivation")
        self.assertFalse((self.repo / ".mixture-of-loops" / "runs").exists())
        self.assertFalse((self.repo / STATE).exists())

    def test_a_blocked_contract_behind_an_intact_launcher_is_refused_at_the_gate(self) -> None:
        """A launcher rendered before the blocker appeared: the gate reads the bundle's
        own contract, so the open blocker still stops the launch."""
        self.prepare("greeting-ready")
        self.stub_environment("succeed")
        self.render()
        bundle = mol.bundle_of(self.launcher)
        contract = json.loads((bundle / "launch-contract.json").read_text(encoding="utf-8"))
        contract["findings"] = [{"id": "missing-release-approval", "severity": "blocker",
                                 "status": "open", "message": "PRE-001 approval is absent",
                                 "source": {"path": f"specs/{FEATURE}/plan.md", "line": 17,
                                            "anchor": "PRE-001"},
                                 "resolution": "the release manager must sign it"}]
        (bundle / "launch-contract.json").write_text(json.dumps(contract), encoding="utf-8")

        report = self.gate()
        self.assertEqual(report.decision, "refuse")
        self.assertEqual(report.reason, "open-blocker")
        self.assertEqual(self.stub_calls, [], "the gate started no pipeline")
        self.assertFalse((self.repo / ".mixture-of-loops" / "runs").exists())

    def test_a_preflight_block_is_not_a_transient(self) -> None:
        """The approval is removed after rendering, so the launcher's own preflight
        blocks. Exit 21 is never relaunched."""
        self.prepare("greeting-ready")
        self.stub_environment("succeed")
        self.render()
        (self.repo / "approvals" / "release-approval.json").unlink()
        record = self.launch("auto")
        status, record = self.watch(record)

        self.assertEqual(status, "failed")
        self.assertEqual(self.lines("MOL-RELAUNCH"), [])
        self.assertIn("state=failed exit=21", self.lines("MOL-DIGEST")[-1])
        self.assertIn("no relaunch (preflight-blocked)", self.lines("MOL-DIGEST")[-1])
        self.assertEqual(self.stub_calls, [], "preflight blocked before any stage action")
        self.assert_nothing_left_behind(record)


class ConcurrencyTests(ExecutionCase):
    def test_a_second_launcher_attaches_rather_than_starting_a_second_run(self) -> None:
        self.prepare()
        self.stub_environment("sleep", sleep="120")
        self.render()
        record = self.launch("auto")
        self.assertTrue(self.wait_until(lambda: (self.repo / STATE / "started").is_file()))

        report = mol.gate(self.launcher)
        self.messages.extend(report.messages())
        self.assertEqual(report.decision, "attach")
        self.assertEqual(report.reason, "already-running")

        mol.request_stop(record)
        status, record = self.watch(record, base=0.3)
        self.assertEqual(status, "stopped")
        self.assertEqual(len(self.stub_calls), 1, "the second gate started nothing")
        self.assert_nothing_left_behind(record)

    def test_the_dry_run_guarantee_is_unchanged_with_the_refusing_stub(self) -> None:
        """The dry-run stub still refuses everything: a dry run writes and runs nothing,
        whatever order its flags arrive in."""
        self.prepare()
        self.render()
        before = mol_e2e.tree_state(self.repo)
        log = self.work / "dry-run-calls.jsonl"
        environment = {**os.environ, "PATH": f"{mol_e2e.STUB_BIN}:{os.environ.get('PATH', '')}",
                       "MOL_STUB_LOG": str(log), "NO_COLOR": "1",
                       "PYTHONDONTWRITEBYTECODE": "1"}
        for argv in (["--dry-run"], ["--implement", "--smoke", "--dry-run"],
                     ["--dry-run", "--implement"]):
            with self.subTest(argv=argv):
                planned = subprocess.run([str(self.launcher), *argv], cwd=self.launcher.parent,
                                         env=environment, stdin=subprocess.DEVNULL,
                                         capture_output=True, text=True, timeout=120,
                                         start_new_session=True, check=False)
                self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
                self.assertIn("[DRY-RUN]", planned.stdout)
        after = mol_e2e.tree_state(self.repo)
        self.assertEqual(sorted(set(after) - set(before)), [])
        self.assertEqual(sorted(p for p in set(after) & set(before) if after[p] != before[p]), [])
        self.assertFalse(log.exists(), "the dry run invoked specstride")
        self.assertEqual(self.stub_calls, [])


class ReportLogTests(ExecutionCase):
    def test_the_cli_leaves_every_message_in_the_run_directory(self) -> None:
        """A harness that backgrounds or truncates the command can still recover the
        exact lines, from a path this skill owns rather than a host-specific task file."""
        self.prepare()
        self.stub_environment("succeed")
        self.render()
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "supervise.py"), "auto", "--launcher",
             str(self.launcher), "--poll-seconds", "0.4", "--max-poll-seconds", "1",
             "--max-wait-seconds", "180"],
            cwd=self.repo, env=os.environ, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.messages.extend(result.stdout.splitlines())
        bundle = mol.read_bundle(self.launcher)
        record = mol.load_record(bundle.run_dir)
        self.records.append(record)
        self.bundle = bundle
        report = Path(record["report_log"]).read_text(encoding="utf-8").splitlines()
        # the file holds exactly what was printed, in order
        printed = [line for line in result.stdout.splitlines() if line.startswith("[MOL-")]
        self.assertEqual(report, printed)
        self.assertTrue(any(line.startswith("[MOL-LAUNCH]") for line in report))
        self.assertTrue(any(line.startswith("[MOL-DIGEST]") and "state=completed" in line
                            for line in report))
        self.assert_nothing_left_behind(record)


class SingleShotSupervisionTests(ExecutionCase):
    def test_a_scheduling_harness_sees_the_same_messages_as_the_loop(self) -> None:
        """A harness that wakes itself up calls observe once per wake-up. The quiet-hold
        count lives in the launch record, so the messages match a loop that never let go."""
        self.prepare()
        self.stub_environment("sleep", sleep="3")
        self.render()
        record = self.launch("auto")
        labels: list[str] = []
        deadline = time.time() + 90
        while time.time() < deadline:
            record = mol.load_record(Path(record["run_dir"]))
            observation, delay = mol.report_once(record, self.bundle, emit=self.emit,
                                                 base=0.4, cap=1.0)
            labels.append(self.messages[-1].split(" ", 1)[0])
            if observation.terminal:
                decision = mol.classify_relaunch(observation, record, self.bundle)
                self.emit(mol.digest_message(observation, record, decision))
                break
            time.sleep(min(delay, 0.5))
        else:
            self.fail("the run never reached a terminal state")

        self.assertIn("[MOL-STATE]", labels)
        self.assertIn("[MOL-HOLD]", labels)
        self.assertTrue(self.lines("MOL-DIGEST")[-1].count("state=completed"))
        holds = self.lines("MOL-HOLD")
        counts = [int(line.split("unchanged=")[1].split()[0]) for line in holds]
        self.assertEqual(counts, list(range(1, len(counts) + 1)),
                         "consecutive unchanged readings are one running hold, not repeats")
        self.assert_nothing_left_behind(record)


if __name__ == "__main__":
    unittest.main()
