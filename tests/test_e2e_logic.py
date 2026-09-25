"""Tier 1 (hermetic): the Tier 3 transcript parser and assertions, without a model.

Synthetic pi and prime JSONL transcripts are paired with real fixture repositories
whose contracts were completed by hand (mol_e2e.reference_contract), so every
verdict the live runner can give is checked here: success, a model error that
still exits 0, a missing script call, a failed tool result, a timeout, and the
blocked-outcome variants.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
import mol_e2e  # noqa: E402
import run_harness_e2e  # noqa: E402

SKILL_MD = "/tmp/x/repo/.agents/skills/mixture-of-loops/SKILL.md"
DSH_SETTINGS = {
    "agent-default-model": {"provider": "zai", "model": "glm-5.3-flash"},
    "llm-pi-ai": {"providers": {
        "zai": {"displayName": "Z.AI", "apiKeyEnv": "ZAI_API_KEY", "baseURL": "https://api.z.ai/api/coding/paas/v4",
                "models": [{"id": "glm-5.3-flash", "name": "GLM-5.3-Flash"}]},
        "local-high": {"displayName": "Local", "apiKeyEnv": "LOCAL_LITELLM_API_KEY", "api": "openai-completions",
                       "baseURL": "http://127.0.0.1:8081/v1", "reasoning": "high",
                       "models": [{"id": "qwen3.8-27b-q5", "contextWindow": 229376, "maxTokens": 49152,
                                   "reasoningEfforts": {"off": None, "high": "high"}},
                                  {"id": "nemotron-lightning-30b", "contextWindow": 131072}]},
        "compass-opus-high": {"apiKeyEnv": "COMPASS_STAGE_API_KEY", "api": "anthropic-messages",
                              "baseURL": "http://127.0.0.1:8088", "models": [{"id": "claude-opus-4.8"}]}}},
    "permission": {"defaultPreset": "danger-full-access"}, "agent-presets": {"default": "standard"}}
S = "/tmp/x/repo/.agents/skills/mixture-of-loops/scripts"
PROMOTED = "promoted to validated: greeting\nvalid validated launch contract: greeting"
PROMOTE_REFUSED = ("invalid launch contract:\nfindings[1] is an open blocker: "
                   "approvals/release-approval.json is absent; it is non-delegable authority.")


def event(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields})


class Script:
    """Builds a harness-shaped JSONL transcript."""

    def __init__(self, harness: str, explicit: bool = True):
        self.harness = harness
        self.lines = [event("session", version=3)]
        prompt = "derive a pipeline for specs/001-greeting"
        if explicit:
            prompt = f'<skill name="mixture-of-loops" location="{SKILL_MD}">\nReferences are relative to x.\n\nbody\n</skill>\n\n{prompt}'
        self.lines.append(event("message_end", message={"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.count = 0

    def tool(self, name: str, args: dict, error: bool = False, output: str = "") -> "Script":
        self.count += 1
        identifier = f"call-{self.count}"
        self.lines.append(event("tool_execution_start", toolCallId=identifier, toolName=name, args=args))
        self.lines.append(event("tool_execution_end", toolCallId=identifier, toolName=name, isError=error,
                                result={"content": [{"type": "text", "text": output}]}))
        return self

    def shell(self, command: str, error: bool = False, output: str = "") -> "Script":
        if self.harness == "pi":
            return self.tool("bash", {"command": command}, error, output)
        return self.tool("ipython", {"code": f"%%bash\n{command}"}, error, output)

    def read_skill(self) -> "Script":
        if self.harness == "pi":
            return self.tool("read", {"path": SKILL_MD})
        return self.tool("ipython", {"code": f"print(open('{SKILL_MD}').read())"})

    def end(self, stop: str = "stop", error: str | None = None) -> list[str]:
        message = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "stopReason": stop}
        if error:
            message["errorMessage"] = error
        self.lines.append(event("message_end", message=message))
        self.lines.append(event("agent_end", messages=[message]))
        return self.lines


def happy_script(harness: str, blocked: bool = False, explicit: bool = True) -> Script:
    script = Script(harness, explicit)
    if not explicit:
        script.read_skill()
    script.shell(f"python3 {S}/bootstrap_contract.py --repo . --feature specs/001-greeting --output launch-contract.json")
    # validate_contract.py --promote's own output (contract_lib's wording for the blocker)
    script.shell(f"MOL_VIA=shell python3 {S}/validate_contract.py --promote launch-contract.json", error=blocked,
                 output=PROMOTE_REFUSED if blocked else PROMOTED)
    if not blocked:
        script.shell(f"python3 {S}/render_launcher.py --contract launch-contract.json --output run-001-greeting.sh")
        script.shell("bash -n run-001-greeting.sh && ./run-001-greeting.sh --dry-run")
    return script


class E2ELogicTests(unittest.TestCase):
    def repo(self, fixture: str, *, render: bool = True) -> tuple[Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repo = mol_e2e.prepare_fixture(fixture, base / "repo")
        contract = repo / "launch-contract.json"
        result = mol_e2e.run_script("bootstrap_contract.py", "--repo", repo, "--feature", "specs/001-greeting",
                                    "--output", contract, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        draft = json.loads(contract.read_text(encoding="utf-8"))
        contract.write_text(json.dumps(mol_e2e.reference_contract(draft, fixture), indent=2), encoding="utf-8")
        if render and fixture == "greeting-ready":
            rendered = mol_e2e.run_script("render_launcher.py", "--contract", contract, "--output",
                                          repo / "run-001-greeting.sh", cwd=repo)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
        return base, repo

    def evaluate(self, fixture: str, lines: list[str], repo: Path, base: Path, **extra) -> dict[str, str]:
        context = mol_e2e.RunContext(harness="pi", mode="explicit", fixture=fixture, repo=repo,
                                     transcript=mol_e2e.parse_transcript(lines), work=base, **extra)
        results = mol_e2e.evaluate(context)
        self.last = results
        return {r["name"]: r["status"] for r in results}

    def failures(self, verdicts: dict[str, str]) -> list[str]:
        return sorted(name for name, status in verdicts.items() if status == "fail")

    # parser
    def test_parser_normalizes_pi_and_prime_calls(self) -> None:
        pi = mol_e2e.parse_transcript(happy_script("pi", explicit=False).end())
        self.assertEqual([c.kind for c in pi.calls], ["read", "shell", "shell", "shell", "shell"])
        self.assertEqual(pi.calls[1].via, "bash")
        self.assertTrue(pi.calls[1].runs("bootstrap_contract.py"))
        self.assertTrue(pi.agent_end)
        self.assertEqual(pi.stop_reason, "stop")

        prime = Script("prime")
        prime.tool("ipython", {"code": f"!python3 {S}/bootstrap_contract.py --repo . --output c.json"})
        prime.tool("ipython", {"code": f"\n%%bash\ncd /tmp/x/repo\npython3 {S}/validate_contract.py c.json"})
        prime.tool("ipython", {"code": f"import subprocess\nsubprocess.run(['python3', '{S}/render_launcher.py'])"})
        prime.tool("ipython", {"code": f"print(Path('{S}/../SKILL.md').read_text())"})
        prime.tool("ipython", {"code": "%%bash\ncat scripts/validate_contract.py"})
        parsed = mol_e2e.parse_transcript(prime.end())
        self.assertEqual([c.via for c in parsed.calls], ["!", "%%bash", "python", "python", "%%bash"])
        self.assertTrue(parsed.calls[0].runs("bootstrap_contract.py"))
        self.assertTrue(parsed.calls[1].runs("validate_contract.py"))
        self.assertFalse(parsed.calls[2].runs("render_launcher.py"), "subprocess is not %%bash or !")
        self.assertEqual(parsed.calls[3].kind, "read")
        self.assertFalse(parsed.calls[4].runs("validate_contract.py"), "reading a script is not running it")
        self.assertEqual(mol_e2e.parse_transcript(["not json", "", "{}"]).bad_lines, 1)

    # verdicts
    def test_success_passes_for_both_harnesses_and_both_fixtures(self) -> None:
        for harness in ("pi", "prime"):
            for fixture in mol_e2e.FIXTURE_NAMES:
                with self.subTest(harness=harness, fixture=fixture):
                    base, repo = self.repo(fixture)
                    blocked = fixture == "greeting-blocked"
                    verdicts = self.evaluate(fixture, happy_script(harness, blocked).end(), repo, base,
                                             home_changes=[], checkout_changed=[])
                    self.assertEqual(self.failures(verdicts), [], self.last)
                    self.assertEqual(mol_e2e.summarize(self.last), "pass")
                    expected = ({"blocker-points-at-prerequisite", "independent-validation-blocked", "no-launcher-rendered"}
                                if blocked else {"launcher-dry-run", "dry-run-read-only", "prerequisite-enforced"})
                    self.assertLessEqual(expected, {n for n, s in verdicts.items() if s == "pass"})

    def test_model_error_with_exit_zero_fails_the_terminal_state(self) -> None:
        base, repo = self.repo("greeting-ready")
        lines = happy_script("pi").end(stop="error", error="Connection error.")
        verdicts = self.evaluate("greeting-ready", lines, repo, base)
        self.assertEqual(verdicts["terminal-state"], "fail")
        no_end = [line for line in happy_script("prime").end() if '"agent_end"' not in line]
        self.assertEqual(self.evaluate("greeting-ready", no_end, repo, base)["terminal-state"], "fail")

    def test_missing_script_call_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        lines = [line for line in happy_script("pi").end() if "render_launcher.py" not in line]
        verdicts = self.evaluate("greeting-ready", lines, repo, base)
        self.assertEqual(verdicts["ran:render_launcher.py"], "fail")
        self.assertEqual(verdicts["render-succeeded"], "fail")

    def test_failed_tool_result_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        script = Script("prime")
        script.shell(f"python3 {S}/bootstrap_contract.py --repo . --output launch-contract.json", error=True)
        script.shell(f"python3 {S}/validate_contract.py launch-contract.json")
        script.shell(f"python3 {S}/render_launcher.py --contract launch-contract.json --output run.sh", error=True)
        verdicts = self.evaluate("greeting-ready", script.end(), repo, base)
        self.assertEqual(verdicts["bootstrap-succeeded"], "fail")
        self.assertEqual(verdicts["render-succeeded"], "fail")

    def test_timeout_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        partial = happy_script("pi").lines  # no agent_end: the run was killed
        verdicts = self.evaluate("greeting-ready", partial, repo, base, timed_out=True, exit_code=-9)
        self.assertEqual(verdicts["terminal-state"], "fail")
        self.assertEqual(verdicts["no-input-wait"], "fail")

    def test_skill_not_loaded_and_scripts_outside_shell_fail(self) -> None:
        base, repo = self.repo("greeting-ready")
        script = Script("prime", explicit=False)
        script.tool("ipython", {"code": f"import subprocess\nsubprocess.run(['python3', '{S}/bootstrap_contract.py'])"})
        verdicts = self.evaluate("greeting-ready", script.end(), repo, base)
        self.assertEqual(verdicts["skill-loaded"], "fail")
        self.assertEqual(verdicts["scripts-only-via-shell"], "fail")
        self.assertEqual(verdicts["ran:bootstrap_contract.py"], "fail")

    def test_blocked_fixture_wrong_outcomes_fail(self) -> None:
        # rendered anyway, and the approval was fabricated
        base, repo = self.repo("greeting-blocked")
        contract = repo / "launch-contract.json"
        value = json.loads(contract.read_text(encoding="utf-8"))
        value["findings"] = [f for f in value["findings"] if f["id"] != "missing-release-approval"]
        value["status"] = "validated"
        contract.write_text(json.dumps(mol_e2e._contract_lib().stamp_promotion(value)), encoding="utf-8")
        self.assertEqual(mol_e2e.run_script("render_launcher.py", "--contract", contract, "--output",
                                            repo / "run.sh", cwd=repo).returncode, 0)
        (repo / "approvals").mkdir()
        (repo / "approvals/release-approval.json").write_text("{}", encoding="utf-8")
        verdicts = self.evaluate("greeting-blocked", happy_script("pi").end(), repo, base)
        for name in ("prerequisite-not-fabricated", "independent-validation-blocked",
                     "blocker-points-at-prerequisite", "no-launcher-rendered"):
            self.assertEqual(verdicts[name], "fail", name)

    def test_blocker_must_point_at_the_prerequisite_source(self) -> None:
        base, repo = self.repo("greeting-blocked")
        contract = repo / "launch-contract.json"
        value = json.loads(contract.read_text(encoding="utf-8"))
        for finding in value["findings"]:
            if finding["id"] == "missing-release-approval":
                finding["source"] = {"path": "specs/001-greeting/spec.md", "line": 1, "anchor": "spec"}
        contract.write_text(json.dumps(value), encoding="utf-8")
        verdicts = self.evaluate("greeting-blocked", happy_script("pi", blocked=True).end(), repo, base)
        self.assertEqual(verdicts["blocker-points-at-prerequisite"], "fail")

    def test_ready_fixture_contract_problems_fail(self) -> None:
        base, repo = self.repo("greeting-ready")
        contract = repo / "launch-contract.json"
        value = json.loads(contract.read_text(encoding="utf-8"))
        value["stages"][1]["action"]["argv"] = ["python3", "-m", "pytest"]      # an invented command
        value["stages"][0]["preconditions"] = value["stages"][0]["preconditions"][1:]  # prerequisite dropped
        value["sources"][0]["sha256"] = "0" * 64
        contract.write_text(json.dumps(value), encoding="utf-8")
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)
        for name in ("declared-command-preserved", "prerequisite-enforced", "sources-match-expectations",
                     "independent-validation-validated"):
            self.assertEqual(verdicts[name], "fail", name)

    def test_dry_run_that_writes_or_calls_specstride_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        launcher = repo / "run-001-greeting.sh"
        text = launcher.read_text(encoding="utf-8")
        edited = text.replace("exec python3", 'mkdir -p "$SCRIPT_DIR/.specstride"; specstride run -w . || true\nexec python3')
        launcher.write_text(edited, encoding="utf-8")
        verdicts = {v["name"]: v for v in mol_e2e.check_launcher(repo, base)}
        self.assertEqual(verdicts["dry-run-read-only"]["status"], "fail", verdicts)
        self.assertIn(".specstride", verdicts["dry-run-read-only"]["detail"])
        self.assertIn("REFUSED", subprocess.run([str(mol_e2e.STUB_BIN / "specstride"), "run"],
                                                capture_output=True, text=True).stderr)

    def test_side_effect_checks_fail_when_reported(self) -> None:
        base, repo = self.repo("greeting-ready")
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base,
                                 home_changes=["changed  /root/.pi/agent/trust.json"],
                                 checkout_changed=["?? skills/mixture-of-loops/launch-contract.json"],
                                 harness_stub_calls=[{"argv": ["run", "-w", "."]}])
        for name in ("real-homes-unchanged", "checkout-unchanged", "no-pipeline-started"):
            self.assertEqual(verdicts[name], "fail", name)

    def test_model_allowlist_rejects_everything_but_the_campaign_model(self) -> None:
        check = run_harness_e2e.check_allowlist
        qwen = run_harness_e2e.Model("qwen3.8-27b-q5")
        compass = run_harness_e2e.Model("compass")
        claude_env = {"ANTHROPIC_BASE_URL": run_harness_e2e.SHIM,
                      **{v: qwen.id for v in run_harness_e2e.CLAUDE_MODEL_VARIABLES}}
        check("pi", ["pi", "-p", "--model", "litellm/qwen3.8-27b-q5", "x"], {}, qwen)
        check("pi", ["pi", "-p", "--model", "compass-shim/claude-opus-4.8", "x"], {}, compass)
        check("prime", ["prime", "qwen", "-p", "x"], {}, qwen)
        check("prime", ["prime", "compass", "-p", "x"], {}, compass)
        check("codex", ["codex", "exec", "-m", qwen.id, "x"], {}, qwen)
        check("claude", ["claude", "-p", "--model", qwen.id, "x"], claude_env, qwen)
        # PyYAML is not standard library, and the hermetic suite runs without it (CI);
        # only the dsh adapter reads YAML, so its checks run where PyYAML is installed.
        if importlib.util.find_spec("yaml") is not None:
            with tempfile.TemporaryDirectory() as tmp:
                import yaml
                Path(tmp, "settings.yaml").write_text(yaml.safe_dump(run_harness_e2e.dsh_settings(
                    DSH_SETTINGS, "local-high", qwen.id)), encoding="utf-8")
                check("dsh", ["dsh", "--profile", "headless", "x"], {"DSH_HOME": tmp}, qwen)
                rejected_dsh = [
                    (["dsh", "--profile", "headless", "x"], {"DSH_HOME": "/root/.dsh"}),          # the real home
                    (["dsh", "--profile", "headless", "--patch", "p.yml", "x"], {"DSH_HOME": tmp}),  # a patch loses to settings
                    (["dsh", "--profile", "web", "x"], {"DSH_HOME": tmp}),
                    (["dsh", "--profile", "headless", "x"], {}),
                ]
                for argv, environment in rejected_dsh:
                    with self.subTest(argv=argv, environment=environment):
                        with self.assertRaises(RuntimeError):
                            check("dsh", argv, environment, qwen)
                with self.assertRaises(RuntimeError):      # settings bound to another model than the campaign's
                    check("dsh", ["dsh", "--profile", "headless", "x"], {"DSH_HOME": tmp}, compass)
        rejected = [
            ("pi", ["pi", "-p", "--model", "litellm/gpt-5", "x"], {}, qwen),
            ("pi", ["pi", "-p", "--model", "litellm/qwen3.8-27b-q5", "x"], {}, compass),
            ("pi", ["pi", "-p", "x"], {}, qwen),
            ("prime", ["prime", "auto", "-p", "x"], {}, qwen),
            ("prime", ["prime", "sol", "-p", "x"], {}, qwen),
            ("prime", ["prime", "qwen", "-p", "x"], {}, compass),
            ("prime", ["prime", "qwen", "--model", "gpt-5.5", "-p", "x"], {}, qwen),
            ("codex", ["codex", "exec", "-m", "gpt-5.6-luna", "x"], {}, qwen),
            ("codex", ["codex", "exec", "-m", compass.id, "x"], {}, compass),   # codex has no Anthropic route
            ("claude", ["claude", "-p", "--model", qwen.id, "x"],
             {**claude_env, "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5"}, qwen),
            ("claude", ["claude", "-p", "--model", qwen.id, "x"],
             {**claude_env, "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, qwen),
            ("claude", ["claude", "-p", "--model", qwen.id, "x"], claude_env, compass),
        ]
        for harness, argv, environment, model in rejected:
            with self.subTest(harness=harness, argv=argv, model=model.choice):
                with self.assertRaises(RuntimeError):
                    check(harness, argv, environment, model)

    def test_model_bindings_follow_the_fleet_launchers(self) -> None:
        qwen, muse, compass = (run_harness_e2e.Model(c) for c in ("qwen3.8-27b-q5", "muse-glimmer-30b", "compass"))
        self.assertEqual((qwen.local, qwen.id, qwen.pi, qwen.prime, qwen.dsh, qwen.claude),
                         (True, "qwen3.8-27b-q5", ("litellm", "litellm/qwen3.8-27b-q5"), ("qwen", "fleet-local"),
                          "local-high", "qwen3.8-27b-q5"))
        self.assertEqual((muse.prime, muse.dsh), (("muse", "fleet-local"), "local-high"))
        self.assertEqual((compass.local, compass.id, compass.pi, compass.prime, compass.dsh, compass.claude),
                         (False, "claude-opus-4.8", ("compass-shim", "compass-shim/claude-opus-4.8"),
                          ("compass", "compass"), "compass-opus-high", "claude-opus-4.8"))
        self.assertEqual(run_harness_e2e.EXPLICIT["dsh"], "/mixture-of-loops")
        self.assertEqual(set(run_harness_e2e.HARNESS_NAMES), set(run_harness_e2e.make_harnesses(qwen)))

    def test_budget_follows_the_recorded_wall_clock(self) -> None:
        budget = run_harness_e2e.run_budget
        qwen, compass = run_harness_e2e.Model("qwen3.8-27b-q5"), run_harness_e2e.Model("compass")
        seconds = {(h, m): budget(h, qwen, m)[0] for h in run_harness_e2e.HARNESS_NAMES for m in ("explicit", "auto")}
        self.assertEqual(seconds, {("pi", "explicit"): 1200, ("pi", "auto"): 1800,
                                   ("prime", "explicit"): 1200, ("prime", "auto"): 1800,
                                   ("codex", "explicit"): 1200, ("codex", "auto"): 1800,
                                   ("claude", "explicit"): 2700, ("claude", "auto"): 2700,
                                   ("dsh", "explicit"): 1500, ("dsh", "auto"): 1800})
        self.assertEqual(budget("claude", qwen, "implicit"), budget("claude", qwen, "explicit"))
        # every recorded run fits with the margin, in whole 5-minute steps
        for (harness, auto), recorded in run_harness_e2e.RECORDED_SECONDS.items():
            chosen = budget(harness, qwen, "auto" if auto else "explicit")[0]
            self.assertGreaterEqual(chosen, recorded * run_harness_e2e.BUDGET_MARGIN, harness)
            self.assertEqual(chosen % 300, 0, harness)
        # the 2026-09-23 Claude Code cell was killed at 1200.2 s; 2026-09-20 auto took 1776 of 1800
        self.assertGreater(budget("claude", qwen, "explicit")[0], 1776 * 1.5)
        # no Compass run is on record, so it keeps the flat defaults
        self.assertEqual([budget(h, compass, m)[0] for h in run_harness_e2e.HARNESS_NAMES for m in ("explicit", "auto")],
                         [1200, 1800] * len(run_harness_e2e.HARNESS_NAMES))
        self.assertIn("no recorded claude run on Compass", budget("claude", compass, "explicit")[1])
        self.assertEqual(budget("claude", qwen, "auto", 900), (900, "--timeout"))

    def test_the_runner_prints_the_budget_it_chose(self) -> None:
        runner = Path(run_harness_e2e.__file__)
        environment = {k: v for k, v in os.environ.items() if k != "MOL_LIVE_E2E"}
        with tempfile.TemporaryDirectory() as evidence:
            for extra, expected in (([], ["budget claude explicit -> qwen3.8-27b-q5: 2700s (1.5x the 1776 s recorded",
                                          "budget claude auto -> qwen3.8-27b-q5: 2700s"]),
                                    (["--timeout", "900", "--model", "compass"],
                                     ["budget claude explicit -> claude-opus-4.8: 900s (--timeout)"])):
                result = subprocess.run([sys.executable, str(runner), "--skip-tiers", "--harness", "claude",
                                         "--mode", "all", "--fixture", "blocked", "--evidence-dir", evidence, *extra],
                                        env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                        check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for line in expected:
                    self.assertIn(line, result.stderr)

    def test_a_timed_out_run_reports_its_budget(self) -> None:
        run = {"harness": "claude", "mode": "explicit", "fixture": "greeting-blocked", "status": "fail",
               "duration_seconds": 2700.3, "timed_out": True, "timeout_seconds": 2700,
               "verdicts": [{"name": "terminal-state", "status": "fail"}]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_harness_e2e.print_report({"evidence_dir": "/e", "tiers": [], "runs": [run]}, False)
        self.assertIn("failed: terminal-state (hit the 2700 s budget)", out.getvalue())

    def test_claude_code_output_reserve_follows_bebop(self) -> None:
        qwen, compass = run_harness_e2e.Model("qwen3.8-27b-q5"), run_harness_e2e.Model("compass")
        self.assertEqual((run_harness_e2e.claude_max_output(qwen), run_harness_e2e.claude_max_output(compass)),
                         ("8192", "32000"))
        bebop = Path("/root/gpu_rtx_3090/bebop.sh")
        if not os.access(bebop, os.R_OK):   # CI runs as a user who cannot even stat /root
            self.skipTest("bebop.sh is not readable on this host")
        # the launcher's own function, not a copy of its numbers (thinking is off in every campaign)
        for model in (qwen, compass):
            result = subprocess.run(["bash", "-c", 'eval "$(sed -n "/^_bebop_max_output() {/,/^}/p" "$1")"; '
                                     'unset BEBOP_MAX_OUTPUT; _bebop_max_output "$2"', "-", str(bebop), model.claude],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
            self.assertEqual(result.stdout, run_harness_e2e.claude_max_output(model), result.stderr)

    def test_dsh_settings_keep_one_provider_bound_to_one_model(self) -> None:
        settings = run_harness_e2e.dsh_settings(DSH_SETTINGS, "local-high", "qwen3.8-27b-q5")
        self.assertEqual(settings["agent-default-model"],
                         {"provider": "local-high", "model": "qwen3.8-27b-q5", "reasoningEffort": "off"})
        self.assertEqual(list(settings["llm-pi-ai"]["providers"]), ["local-high"])
        provider = settings["llm-pi-ai"]["providers"]["local-high"]
        self.assertEqual([m["id"] for m in provider["models"]], ["qwen3.8-27b-q5"])
        self.assertEqual(provider["baseURL"], "http://127.0.0.1:8081/v1")
        self.assertEqual(settings["permission"], {"defaultPreset": "danger-full-access"})
        self.assertNotIn("zai", json.dumps(settings))
        for provider_name, model in (("local-high", "muse-glimmer-30b"), ("local-high", "glm-5.3-flash"), ("nope", "x")):
            with self.subTest(provider=provider_name, model=model):
                with self.assertRaises(RuntimeError):
                    run_harness_e2e.dsh_settings(DSH_SETTINGS, provider_name, model)
        line, value = run_harness_e2e.dsh_credential("LOCAL_LITELLM_API_KEY: abc\nZAI_API_KEY: zzz\n",
                                                     "LOCAL_LITELLM_API_KEY")
        self.assertEqual((line, value), ("LOCAL_LITELLM_API_KEY: abc", "abc"))
        self.assertEqual(run_harness_e2e.dsh_credential("ZAI_API_KEY: zzz\n", "COMPASS_STAGE_API_KEY"), (None, None))

    def test_dsh_session_normalizes(self) -> None:
        def ev(kind: str, **data: object) -> str:
            return json.dumps({"type": kind, "seq": 1, "time": 0, "data": data})
        transcript = mol_e2e.parse_dsh_session([
            json.dumps({"type": "session", "version": 0, "id": "session-1", "cwd": "/tmp/x/repo"}),
            ev("user/message", content=[{"type": "text", "text": "/mixture-of-loops derive a pipeline"}], role="user"),
            ev("request/context", provider="local-high", model="qwen3.8-27b-q5", contextWindow=229376),
            ev("tool/call", turn=1, step=1, callId="c1", name="skill", arguments=json.dumps({"name": "mixture-of-loops"})),
            ev("tool/result", turn=1, step=1, message={"content": [{"type": "tool-result", "toolCallId": "c1",
                                                                     "content": [{"type": "text", "text": "<skill_content>...</skill_content>"}]}]}),
            ev("tool/call", turn=1, step=2, callId="c2", name="read", arguments=json.dumps({"file_path": SKILL_MD})),
            ev("tool/result", turn=1, step=2, message={"content": [{"type": "tool-result", "toolCallId": "c2",
                                                                     "content": [{"type": "text", "text": "body"}]}]}),
            ev("tool/call", turn=1, step=3, callId="c3", name="bash",
               arguments=json.dumps({"command": f"python3 {S}/bootstrap_contract.py --repo . --feature specs/001-greeting"})),
            ev("tool/result", turn=1, step=3, message={"content": [{"type": "tool-result", "toolCallId": "c3", "isError": True,
                                                                     "content": [{"type": "text", "text": "boom"}]}]}),
            ev("assistant/message", turn=1, step=3, message={"role": "assistant", "content": [],
                                                              "source": {"kind": "model", "provider": "local-high",
                                                                         "model": "qwen3.8-27b-q5"}}),
            ev("turn/end", turn=1, reason={"kind": "completed"}),
        ])
        self.assertEqual(transcript.skill_invoked, ["mixture-of-loops"])
        self.assertEqual([c.kind for c in transcript.calls], ["other", "read", "shell"])
        self.assertEqual(transcript.calls[1].text, SKILL_MD)
        self.assertTrue(transcript.calls[2].runs("bootstrap_contract.py"))
        self.assertIs(transcript.calls[2].is_error, True)
        self.assertEqual(transcript.calls[2].result, "boom")
        self.assertEqual(transcript.models, ["qwen3.8-27b-q5"])
        self.assertEqual((transcript.agent_end, transcript.stop_reason, transcript.error_message), (True, "stop", None))
        aborted = mol_e2e.parse_dsh_session([ev("turn/end", turn=1, reason={"kind": "aborted", "reason": {"kind": "user"}})])
        self.assertEqual((aborted.agent_end, aborted.stop_reason), (True, "aborted"))
        self.assertIn("user", aborted.error_message)

    def test_infra_failures_are_told_apart_from_real_ones(self) -> None:
        infra = run_harness_e2e.infra_reason
        empty = mol_e2e.Transcript()
        self.assertIsNone(infra(empty, "", ""))
        self.assertIn("route failed", infra(empty, "dsh: TRANSPORT: Connection error.\n", "") or "")
        self.assertIn("route failed", infra(empty, "API Error: 500 qwen upstream error", "") or "")
        self.assertIn("route failed", infra(empty, 'PI_AI_ERROR: 404 "no router for requested model"', "") or "")
        # Compass STAGE's retired key, and a PROD quota running out, are the stack's too.
        self.assertIn("route failed", infra(empty, "401 {'error': 'Invalid API Key'}", "") or "")
        self.assertIn("route failed", infra(empty, '429 {"error": {"code": "insufficient_quota"}}', "") or "")
        upstream = mol_e2e.Transcript(error_message="stream disconnected", agent_end=True, stop_reason="error")
        self.assertIn("terminal error", infra(upstream, "", "") or "")
        # A run that made real tool calls and then failed a verdict is the skill's or the harness's.
        worked = mol_e2e.parse_transcript(happy_script("pi").end())
        self.assertIsNone(infra(worked, "API Error: 500 qwen upstream error (one retried request)", ""))
        real = mol_e2e.Transcript(error_message="PRE-001 blocker not honoured", agent_end=True, stop_reason="error")
        self.assertIsNone(infra(real, "", ""))

    def test_codex_and_claude_streams_normalize(self) -> None:
        claude = mol_e2e.parse_claude_stream([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "Skill", "input": {"skill": "mixture-of-loops"}},
                {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": f"python3 {S}/validate_contract.py c.json"}}]}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "is_error": True}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False}),
        ])
        self.assertEqual(claude.skill_invoked, ["mixture-of-loops"])
        self.assertTrue(claude.calls[1].runs("validate_contract.py"))
        self.assertIs(claude.calls[1].is_error, True)
        self.assertEqual((claude.agent_end, claude.stop_reason), (True, "stop"))
        codex = mol_e2e.parse_codex_stream([
            json.dumps({"type": "item.completed", "item": {"id": "1", "type": "command_execution",
                                                           "command": f"bash -lc 'python3 {S}/bootstrap_contract.py'",
                                                           "exit_code": 2, "status": "failed"}}),
            json.dumps({"type": "turn.failed", "error": {"message": "stream disconnected"}}),
        ])
        self.assertTrue(codex.calls[0].runs("bootstrap_contract.py"))
        self.assertIs(codex.calls[0].is_error, True)
        self.assertEqual((codex.stop_reason, codex.error_message), ("error", "stream disconnected"))

    def test_reading_scripts_is_not_running_them_and_grader_access_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        script = happy_script("prime")
        script.tool("ipython", {"code": f"print(open('{S}/bootstrap_contract.py').read())"})
        script.tool("ipython", {"code": "%%bash\nsed -n 1,80p /src/checkout/tests/e2e/mol_e2e.py"})
        script.tool("ipython", {"code": "%%bash\ngrep -rn 'timed out' .", }, output="x: Command timed out waiting")
        verdicts = self.evaluate("greeting-ready", script.end(), repo, base, grader_paths=["/src/checkout/tests"])
        self.assertEqual(verdicts["scripts-only-via-shell"], "pass", "open().read() of a script is not execution")
        self.assertEqual(verdicts["no-input-wait"], "pass", "text mentioning a timeout is not a tool timeout")
        self.assertEqual(verdicts["no-grader-access"], "fail")
        script = happy_script("pi")
        script.tool("bash", {"command": "sleep 999"}, error=True, output="Command timed out after 120 seconds")
        self.assertEqual(self.evaluate("greeting-ready", script.end(), repo, base)["no-input-wait"], "fail")

    def test_declared_command_in_a_verification_document_under_mixture_of_loops_counts(self) -> None:
        base, repo = self.repo("greeting-ready")
        contract = repo / "launch-contract.json"
        value = json.loads(contract.read_text(encoding="utf-8"))
        value["stages"][1]["action"]["argv"] = ["/usr/bin/true"]
        contract.write_text(json.dumps(value), encoding="utf-8")
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)
        self.assertEqual(verdicts["declared-command-preserved"], "fail")
        (repo / ".mixture-of-loops/verification-commands.json").write_text(json.dumps({"commands": [
            {"id": "VC-1", "executable": "python3", "args": ["-m", "unittest", "discover", "-s", "tests", "-v"]}]}))
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)
        self.assertEqual(verdicts["declared-command-preserved"], "pass")
        generated = repo / ".mixture-of-loops/generated/copy.json"
        (repo / ".mixture-of-loops/verification-commands.json").rename(generated)
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)
        self.assertEqual(verdicts["declared-command-preserved"], "fail", "renderer bundles are copies, not declarations")

    def test_a_model_draft_under_generated_is_not_a_rendered_bundle(self) -> None:
        base, repo = self.repo("greeting-blocked")
        draft = repo / ".mixture-of-loops/generated/001-greeting/draft"
        draft.mkdir(parents=True)
        (draft / "launch-contract.json").write_text("{}", encoding="utf-8")
        verdicts = self.evaluate("greeting-blocked", happy_script("pi", blocked=True).end(), repo, base)
        self.assertEqual(verdicts["no-launcher-rendered"], "pass")
        bundle = repo / ".mixture-of-loops/generated/001-greeting/0123456789abcdef0123"
        bundle.mkdir()
        (bundle / "launch-contract.json").write_text("{}", encoding="utf-8")
        (bundle / "runtime.py").write_text("", encoding="utf-8")
        verdicts = self.evaluate("greeting-blocked", happy_script("pi", blocked=True).end(), repo, base)
        self.assertEqual(verdicts["no-launcher-rendered"], "fail")

    def test_codex_skill_injection_is_reported_as_not_observable(self) -> None:
        base, repo = self.repo("greeting-ready")
        lines = [json.dumps({"type": "turn.completed"})]
        context = mol_e2e.RunContext(harness="codex", mode="explicit", fixture="greeting-ready", repo=repo,
                                     transcript=mol_e2e.parse_codex_stream(lines), work=base)
        verdicts = {v["name"]: v["status"] for v in mol_e2e.evaluate(context)}
        self.assertEqual(verdicts["skill-loaded"], "skip")
        context.harness = "pi"
        self.assertEqual({v["name"]: v["status"] for v in mol_e2e.evaluate(context)}["skill-loaded"], "fail")

    # ── the auto-mode verdicts ────────────────────────────────────────────────

    def execution_run(self, repo: Path, *, state: dict, record: dict | None = None,
                      digest: str = "[DIGEST] state=completed exit=0 last-stage=verify-greeting "
                                     "evidence=/e\n") -> Path:
        """The files a real detached run leaves behind, without running one."""
        run_dir = repo / ".mixture-of-loops" / "runs" / "greeting"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (run_dir / "launcher.log").write_text(digest, encoding="utf-8")
        (run_dir / "harness-launch.log").write_text("", encoding="utf-8")
        default = {"launcher": str(repo / "run-001-greeting.sh"),
                   "argv": [str(repo / "run-001-greeting.sh"), "--no-color"],
                   "pid": 2, "pgid": 2, "launched_at": 1.0, "contract_digest": "d",
                   "mode": "auto", "run_dir": str(run_dir), "relaunches": [],
                   "relaunch_budget": {"max_relaunches": 2, "remaining": 2}}
        (run_dir / "harness-run.json").write_text(json.dumps({**default, **(record or {})}),
                                                  encoding="utf-8")
        return run_dir

    COMPLETED = {"pipeline": "greeting", "state": "completed", "exit_reason": "all-stages-complete",
                 "stages": {"implement-feature": {"status": "completed"},
                            "verify-greeting": {"status": "completed"}}}

    def auto_script(self, *, reported: list[str]) -> Script:
        script = happy_script("pi")
        script.shell(f"python3 {S}/supervise.py auto --launcher run-001-greeting.sh",
                     error=False)
        script.lines[-1] = event("tool_execution_end", toolCallId=f"call-{script.count}",
                                 toolName="bash", isError=False,
                                 result={"content": [{"type": "text", "text": "\n".join(reported)}]})
        return script

    def auto_verdicts(self, repo: Path, base: Path, script: Script,
                      stub_calls: list[dict] | None = None) -> dict[str, str]:
        context = mol_e2e.RunContext(harness="pi", mode="auto", fixture="greeting-ready", repo=repo,
                                     transcript=mol_e2e.parse_transcript(script.end()), work=base,
                                     expect_execution=True,
                                     execution_stub_calls=stub_calls if stub_calls is not None
                                     else [{"argv": ["run", "-w", "."], "attempt": 1}])
        results = mol_e2e.evaluate(context)
        self.last = results
        return {r["name"]: r["status"] for r in results}

    def test_an_auto_run_passes_only_with_the_run_state_and_the_reported_digest(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state=self.COMPLETED)
        script = self.auto_script(reported=[
            "[MOL-STATE] pipeline=greeting stage=1/2 id=implement-feature state=running "
            "stage-elapsed=2s total-elapsed=2s",
            "[MOL-DIGEST] pipeline=greeting state=completed exit=0 child-exit=0 "
            "last-stage=verify-greeting evidence=/e state-path=/s next=\"none\"",
        ])
        verdicts = self.auto_verdicts(repo, base, script)
        self.assertEqual(self.failures(verdicts), [], self.last)
        for name in ("run-directory", "pipeline-started", "harness-launch-record",
                     "run-completed", "intermediate-state-reported", "terminal-digest-reported",
                     "reported-digest-matches-the-launcher", "no-leftover-run-lock"):
            self.assertEqual(verdicts[name], "pass", name)
        self.assertNotIn("no-pipeline-started", verdicts,
                         "a run asked to execute is not graded as one that must not")

    def test_an_auto_run_that_never_started_the_pipeline_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        verdicts = self.auto_verdicts(repo, base, self.auto_script(reported=[]), stub_calls=[])
        for name in ("run-directory", "pipeline-started", "terminal-digest-reported"):
            self.assertEqual(verdicts[name], "fail", name)

    def test_a_reading_taken_before_the_first_state_still_counts_as_intermediate(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state=self.COMPLETED)
        script = self.auto_script(reported=[
            "[MOL-STATE] pipeline=greeting stage=0/2 id=none state=starting "
            "stage-elapsed=- total-elapsed=0s",
            "[MOL-DIGEST] pipeline=greeting state=completed exit=0 child-exit=0 "
            "last-stage=verify-greeting evidence=/e state-path=/s next=\"none\"",
        ])
        self.assertEqual(self.auto_verdicts(repo, base, script)["intermediate-state-reported"],
                         "pass")

    def test_lines_read_back_from_a_file_count_even_when_line_numbered(self) -> None:
        """A harness whose read tool prefixes every line with its number still reported the
        supervisor's own lines; what identifies them is the [MOL-…] token."""
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state=self.COMPLETED)
        script = self.auto_script(reported=[
            "1\t[MOL-STATE] pipeline=greeting stage=1/2 id=implement-feature state=running "
            "stage-elapsed=2s total-elapsed=2s",
            "2\t[MOL-DIGEST] pipeline=greeting state=completed exit=0 child-exit=0 "
            "last-stage=verify-greeting evidence=/e state-path=/s next=\"none\"",
        ])
        verdicts = self.auto_verdicts(repo, base, script)
        self.assertEqual(verdicts["intermediate-state-reported"], "pass")
        self.assertEqual(verdicts["terminal-digest-reported"], "pass")

    def test_narrated_progress_is_not_a_reported_state(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state=self.COMPLETED)
        script = self.auto_script(reported=["the pipeline is running, stage 1 of 2",
                                            "it completed successfully"])
        verdicts = self.auto_verdicts(repo, base, script)
        self.assertEqual(verdicts["intermediate-state-reported"], "fail")
        self.assertEqual(verdicts["terminal-digest-reported"], "fail")
        self.assertEqual(verdicts["run-completed"], "pass", "the files still say it completed")

    def test_a_digest_that_disagrees_with_the_launcher_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state={**self.COMPLETED, "state": "failed"},
                           digest="[DIGEST] state=failed exit=22 last-stage=implement-feature "
                                  "child-exit=3 evidence=/e\n")
        script = self.auto_script(reported=[
            "[MOL-STATE] pipeline=greeting stage=1/2 id=implement-feature state=running "
            "stage-elapsed=2s total-elapsed=2s",
            "[MOL-DIGEST] pipeline=greeting state=failed exit=0 child-exit=0 "
            "last-stage=implement-feature evidence=/e state-path=/s next=\"none\"",
        ])
        verdicts = self.auto_verdicts(repo, base, script)
        self.assertEqual(verdicts["reported-digest-matches-the-launcher"], "fail")
        self.assertEqual(verdicts["run-completed"], "fail")
        self.assertEqual(verdicts["run-reached-a-terminal-state"], "pass")

    def test_an_unannounced_relaunch_fails(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.execution_run(repo, state=self.COMPLETED,
                           record={"relaunches": [{"reason": "classified-transient"}]})
        script = self.auto_script(reported=[
            "[MOL-STATE] pipeline=greeting stage=1/2 id=implement-feature state=running "
            "stage-elapsed=2s total-elapsed=2s",
            "[MOL-DIGEST] pipeline=greeting state=completed exit=0 child-exit=0 "
            "last-stage=verify-greeting evidence=/e state-path=/s next=\"none\"",
        ])
        self.assertEqual(self.auto_verdicts(repo, base, script)["every-relaunch-was-announced"],
                         "fail")

    def test_a_launcher_at_the_skills_default_path_is_found(self) -> None:
        base, repo = self.repo("greeting-ready", render=False)
        launcher = repo / ".mixture-of-loops" / "001-greeting" / "run-001-greeting.sh"
        rendered = mol_e2e.run_script("render_launcher.py", "--contract",
                                      repo / "launch-contract.json", "--output", launcher,
                                      cwd=repo)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(mol_e2e.locate_launchers(repo), [launcher],
                         "SKILL.md's own default output path must be discoverable")
        # A launcher one level below .mixture-of-loops keeps its own artifact root there,
        # and everything in it is still a copy rather than the contract or a launcher.
        bundle = next((launcher.parent / ".mixture-of-loops" / "generated").rglob("runtime.py"))
        self.assertTrue(mol_e2e._generated_or_state(bundle, repo))
        self.assertEqual(mol_e2e.locate_contract(repo, mol_e2e.Transcript()),
                         repo / "launch-contract.json")

    def test_reading_a_skill_resource_counts_as_loading_it(self) -> None:
        base, repo = self.repo("greeting-ready")
        lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Read",
                  "input": {"file_path": "/r/.claude/skills/mixture-of-loops/references/derivation.md"}}]}}),
                 json.dumps({"type": "result", "subtype": "success", "is_error": False})]
        context = mol_e2e.RunContext(harness="claude", mode="explicit", fixture="greeting-ready", repo=repo,
                                     transcript=mol_e2e.parse_claude_stream(lines), work=base)
        self.assertEqual({v["name"]: v["status"] for v in mol_e2e.evaluate(context)}["skill-loaded"], "pass")
        context.transcript = mol_e2e.parse_claude_stream(lines[1:])
        self.assertEqual({v["name"]: v["status"] for v in mol_e2e.evaluate(context)}["skill-loaded"], "fail")

    # ── the 2026-09-23 hardening, observed live (#22) ─────────────────────────

    def test_the_promote_strings_are_what_the_validator_prints(self) -> None:
        """The verdict reads validate_contract.py's own output; keep the two in step."""
        for fixture, expected in (("greeting-ready", mol_e2e.PROMOTED), ("greeting-blocked", mol_e2e.OPEN_BLOCKER)):
            with self.subTest(fixture=fixture):
                _, repo = self.repo(fixture, render=False)
                contract = repo / "launch-contract.json"
                value = json.loads(contract.read_text(encoding="utf-8"))
                contract.write_text(json.dumps({**value, "status": "draft"}), encoding="utf-8")
                result = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
                output = result.stdout + result.stderr
                if isinstance(expected, str):
                    self.assertIn(expected, output)
                    self.assertIn(expected, PROMOTED)
                else:
                    self.assertRegex(output, expected)
                    self.assertRegex(PROMOTE_REFUSED, expected)
                again = mol_e2e.run_script("validate_contract.py", "--promote", contract, cwd=repo)
                if isinstance(expected, str):   # already validated: the file's status is not news
                    self.assertNotIn(mol_e2e.PROMOTED, again.stdout + again.stderr)
        reminder = subprocess.run([sys.executable, str(mol_e2e.SCRIPTS / "validate_contract.py"), "/nonexistent"],
                                  env={k: v for k, v in os.environ.items() if k != "MOL_VIA"},
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        self.assertIn(mol_e2e.SHELL_REMINDER, reminder.stderr)

    def test_status_is_owned_by_promote(self) -> None:
        base, repo = self.repo("greeting-blocked")
        verdicts = self.evaluate("greeting-blocked", happy_script("pi", blocked=True).end(), repo, base)
        self.assertEqual(verdicts["status-owned-by-promote"], "pass", self.last)
        plain = Script("pi")     # strict validation without --promote proves nothing about status
        plain.shell(f"python3 {S}/bootstrap_contract.py --repo . --output launch-contract.json")
        plain.shell(f"python3 {S}/validate_contract.py launch-contract.json", error=True, output=PROMOTE_REFUSED)
        self.assertEqual(self.evaluate("greeting-blocked", plain.end(), repo, base)["status-owned-by-promote"], "fail")

        base, repo = self.repo("greeting-ready")
        verdicts = self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)
        self.assertEqual(verdicts["status-owned-by-promote"], "pass", self.last)
        confirmed = json.dumps(PROMOTED)[1:-1]    # as it sits inside a JSONL line
        contract = repo / "launch-contract.json"
        stamped = json.loads(contract.read_text(encoding="utf-8"))
        silent = happy_script("pi")   # Codex returned the promote's result without its stdout
        silent.lines = [line.replace(confirmed, "") for line in silent.lines]
        self.assertEqual(self.evaluate("greeting-ready", silent.end(), repo, base)["status-owned-by-promote"], "pass",
                         "the stamp on disk is --promote's own record")
        unstamped = dict(stamped)
        unstamped.pop("promotion")   # the status typed in by hand from here on
        contract.write_text(json.dumps(unstamped, indent=2), encoding="utf-8")
        self.assertEqual(self.evaluate("greeting-ready", silent.end(), repo, base)["status-owned-by-promote"], "fail")
        plain = happy_script("pi")
        plain.lines = [line.replace(" --promote", "").replace(confirmed, "valid validated launch contract: greeting")
                       for line in plain.lines]
        self.assertEqual(self.evaluate("greeting-ready", plain.end(), repo, base)["status-owned-by-promote"], "fail")
        # status typed in by hand, then --promote: the validator only confirms, never promotes
        hand = happy_script("pi")
        hand.lines = [line.replace(confirmed, "valid validated launch contract: greeting") for line in hand.lines]
        self.assertEqual(self.evaluate("greeting-ready", hand.end(), repo, base)["status-owned-by-promote"], "fail",
                         self.last)

    def test_prerequisite_inventory_must_keep_what_bootstrap_resolved(self) -> None:
        base, repo = self.repo("greeting-ready")
        self.assertEqual(self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)["prerequisite-inventoried"],
                         "pass", self.last)
        contract = repo / "launch-contract.json"
        value = json.loads(contract.read_text(encoding="utf-8"))
        for entry in value["inventory"]["prerequisites"]:
            entry["present"] = False
        contract.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)["prerequisite-inventoried"],
                         "fail")
        del value["inventory"]
        contract.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.evaluate("greeting-ready", happy_script("pi").end(), repo, base)["prerequisite-inventoried"],
                         "skip", "the schema allows dropping the inventory")

    def test_calls_outside_the_repository_and_skill_root_fail(self) -> None:
        base, repo = self.repo("greeting-ready")
        root = str(base)
        strayed = [f"cd {root}/stub-bin && ls && cat specstride", f"ls {root}/", f"cat {root}/stderr.log",
                   "ls /root/.local/bin 2>/dev/null", "find / -name specstride 2>/dev/null", "which specstride",
                   "cd .. && ls", f"ls {root}/repo/../home", f"ls {root}/checkout/", "ls ~/.agents/skills",
                   "echo $HOME", "/bin/bash -lc 'command -v specstride'",
                   "cat README.md\nif command -v specstride; then echo y; fi"]
        in_scope = [f"cd {root}/repo && ls -la", f"python3 {root}/checkout/skills/mixture-of-loops/scripts/"
                    "validate_contract.py --promote x.json", "cat .claude/skills/mixture-of-loops/SKILL.md",
                    "/usr/bin/python3 -c 'print(1)' 2>/dev/null", "ls specs/001-greeting/",
                    "grep -n 'command -v specstride' .mixture-of-loops/001-greeting/run-001-greeting.sh",
                    "git log --oneline HEAD~1..HEAD", "python3 -c \"print('...')\""]
        for command, expected in [(c, "fail") for c in strayed] + [(c, "pass") for c in in_scope]:
            with self.subTest(command=command):
                script = happy_script("pi")
                script.shell(command)
                verdicts = self.evaluate("greeting-ready", script.end(), repo, base, root=base)
                self.assertEqual(verdicts["stayed-in-scope"], expected, self.last)
        # a non-shell tool is scanned only by its path-valued arguments
        script = happy_script("pi")
        script.tool("todo_write", {"todos": [{"content": f"read {root}/stub-bin and cd .."}]})
        script.tool("write", {"path": f"{root}/repo/notes.md", "content": f"see {root}/home"})
        self.assertEqual(self.evaluate("greeting-ready", script.end(), repo, base, root=base)["stayed-in-scope"], "pass")
        script.tool("read", {"path": f"{root}/home/.agents/skills/x/SKILL.md"})
        self.assertEqual(self.evaluate("greeting-ready", script.end(), repo, base, root=base)["stayed-in-scope"], "fail")
        # a python cell is scanned whole, but string building is not a path
        prime = happy_script("prime")
        prime.tool("ipython", {"code": 'repo = "."\nprint(open(repo + "/.gitignore").read())\nx = ~1'})
        self.assertEqual(self.evaluate("greeting-ready", prime.end(), repo, base, root=base)["stayed-in-scope"], "pass")
        prime.tool("ipython", {"code": f'print(open("{root}/stub-bin/specstride").read())'})
        self.assertEqual(self.evaluate("greeting-ready", prime.end(), repo, base, root=base)["stayed-in-scope"], "fail")
        self.assertNotIn("stayed-in-scope", self.evaluate("greeting-ready", happy_script("pi").end(), repo, base),
                         "no root, no scope to judge")

    def test_written_content_is_not_a_place_the_model_went(self) -> None:
        """2026-09-23 campaign false positives: git's `# *~` exclude template in a patch,
        a launcher's `$SCRIPT_DIR/..` in a heredoc or a python string, and a `cd` into the
        root before naming checkout/ relatively. What stays caught: a heredoc fed to an
        interpreter, a path literal opened from python, and the stubs named relatively."""
        base, repo = self.repo("greeting-ready")
        root = str(base)
        in_scope = [
            "applypatch << 'PATCH'\n*** Begin Patch\n*** Update File: .git/info/exclude\n@@\n # *~\n"
            "+.mixture-of-loops/*\n*** End Patch\nPATCH",
            "cat > run.sh << 'SH'\nROOT=\"$(cd \"$(dirname \"$0\")/../..\" && pwd)\"\nls ~/x\nSH\nchmod +x run.sh",
            f"cd {root} && python3 checkout/skills/mixture-of-loops/scripts/supervise.py mode --request x",
        ]
        strayed = [
            "python3 - <<'EOF'\nimport os\nprint(os.listdir('..'))\nEOF",
            "cat > run.sh <<'SH' | bash\nls ..\nSH",
            f"cd {root} && cat stub-bin/specstride",
            "cat harness-stub-specstride.jsonl",
            "applypatch << 'PATCH'\n*** Begin Patch\n*** Update File: ../outside.txt\n@@\n+x\n*** End Patch\nPATCH",
        ]
        for command, expected in [(c, "pass") for c in in_scope] + [(c, "fail") for c in strayed]:
            with self.subTest(command=command):
                script = happy_script("pi")
                script.shell(command)
                verdicts = self.evaluate("greeting-ready", script.end(), repo, base, root=base)
                self.assertEqual(verdicts["stayed-in-scope"], expected, self.last)
        prime = happy_script("prime")
        prime.tool("ipython", {"code": 'text = text.replace("X", \'BUNDLE_DIR="$SCRIPT_DIR/../generated/g"\')\n'
                                       'open(launcher, "w").write(text)'})
        self.assertEqual(self.evaluate("greeting-ready", prime.end(), repo, base, root=base)["stayed-in-scope"], "pass",
                         self.last)
        prime.tool("ipython", {"code": 'print(open("../secrets").read())'})
        self.assertEqual(self.evaluate("greeting-ready", prime.end(), repo, base, root=base)["stayed-in-scope"], "fail")

    def test_supervise_py_is_shell_only_too(self) -> None:
        base, repo = self.repo("greeting-ready")
        script = happy_script("prime")
        script.tool("ipython", {"code": f"import subprocess\nsubprocess.run(['python3', '{S}/supervise.py', 'mode'])"})
        self.assertEqual(self.evaluate("greeting-ready", script.end(), repo, base)["scripts-only-via-shell"], "fail")

    def test_a_script_that_worked_inside_a_failed_compound_command_succeeded(self) -> None:
        """One exit status per shell call: bootstrap chained ahead of a refused promote,
        and a render followed by a stray word, did their own work."""
        base, repo = self.repo("greeting-ready")
        script = Script("codex")
        script.shell(f"python3 {S}/bootstrap_contract.py --repo . --output launch-contract.json\n"
                     f"python3 {S}/validate_contract.py --promote launch-contract.json", error=True,
                     output="invalid launch contract:\nfindings[0] is an open blocker: derive")
        script.shell(f"python3 {S}/render_launcher.py --contract launch-contract.json --output run.sh\n"
                     ",max_output_tokens:10000}", error=True,
                     output="rendered /tmp/x/repo/run.sh\nbundle /tmp/x/b\nbash: line 2: ,max_output_tokens:10000}: "
                            "command not found")
        verdicts = self.evaluate("greeting-ready", script.end(), repo, base)
        self.assertEqual((verdicts["bootstrap-succeeded"], verdicts["render-succeeded"]), ("pass", "pass"), self.last)
        crashed = Script("codex")
        crashed.shell(f"python3 {S}/bootstrap_contract.py --repo . && echo done", error=True,
                      output="usage: ...\nbootstrap_contract.py: error: --feature is required")
        self.assertEqual(self.evaluate("greeting-ready", crashed.end(), repo, base)["bootstrap-succeeded"], "fail")

    def test_scripts_only_via_shell_is_always_reported(self) -> None:
        base, repo = self.repo("greeting-ready")
        script = happy_script("pi")
        script.shell(f"python3 {S}/validate_contract.py launch-contract.json",
                     output=f"note: validate_contract.py {mol_e2e.SHELL_REMINDER}; run it as ...")
        verdicts = self.evaluate("greeting-ready", script.end(), repo, base)
        self.assertEqual(verdicts["scripts-only-via-shell"], "pass", "the reminder is informational")
        detail = next(v["detail"] for v in self.last if v["name"] == "scripts-only-via-shell")
        self.assertIn("1 result(s) carried the MOL_VIA=shell reminder", detail)
        self.assertIn("['bash']", detail)

    def test_the_hosts_own_dsh_sessions_are_not_a_run_touching_the_real_home(self) -> None:
        sys.path.insert(0, str(Path(mol_e2e.__file__).parent))
        import home_snapshot
        before = {"/root/.dsh/settings.yaml": "file:1"}
        after = {**before, "/root/.dsh/sessions/--root-agentic-netops-srl--": "dir",
                 "/root/.dsh/sessions/--root-agentic-netops-srl--/s/session.jsonl.zstd": "file:2",
                 "/root/.dsh/sessions/--tmp-mol-e2e-run-ab12-repo--": "dir"}
        self.assertEqual(home_snapshot.diff(before, after),
                         ["added    /root/.dsh/sessions/--tmp-mol-e2e-run-ab12-repo-- (dir)"])
        self.assertEqual(len(home_snapshot.diff(before, {"/root/.dsh/settings.yaml": "file:9"})), 1)
        synced = "/root/.claude/skills/synced/abc_def"
        self.assertEqual(home_snapshot.diff(before, {**before, synced: "dir", synced + "/manifest.json": "file:3"}),
                         [], "the host's own Claude Code skill sync")
        self.assertEqual(len(home_snapshot.diff(before, {**before, "/root/.claude/skills/mixture-of-loops": "dir"})), 1)

    def test_reevaluate_drops_host_noise_from_a_recorded_home_diff(self) -> None:
        sync = "added    /root/.claude/skills/synced/a_b/.last-complete-round (file:ff77)"
        real = "added    /root/.pi/agent/skills/mixture-of-loops (dir)"
        failed = {"name": "real-homes-unchanged", "status": "fail", "detail": sync}
        self.assertEqual(run_harness_e2e.refiltered_home_verdict(failed, {})["status"], "pass")
        self.assertEqual(run_harness_e2e.refiltered_home_verdict(failed, {"home_changes": [sync, real]})["status"], "fail")
        cut = {**failed, "detail": "; ".join([sync] * 5)}
        self.assertEqual(run_harness_e2e.refiltered_home_verdict(cut, {})["status"], "fail",
                         "five shown may be a truncated list")

    def test_codex_gets_catalog_metadata_for_gpt_models_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "models_cache.json"
            cache.write_text(json.dumps({"models": [
                {"slug": "gpt-5.6-terra", "shell_type": "other", "supports_search_tool": True},
                {"slug": "gpt-5.5", "shell_type": "unified_exec", "supports_search_tool": True},
                {"slug": "gpt-reserve", "shell_type": "other"}]}), encoding="utf-8")
            entry = run_harness_e2e.codex_model_catalog("gpt-5", cache)["models"][0]
            self.assertEqual((entry["slug"], entry["shell_type"], entry["supports_search_tool"]),
                             ("gpt-5", "unified_exec", False))
            self.assertEqual(run_harness_e2e.codex_model_catalog("gpt-5.6-terra", cache)["models"][0]["shell_type"],
                             "other", "a listed model keeps its own entry")
            self.assertIsNone(run_harness_e2e.codex_model_catalog("qwen3.8-27b-q5", cache))
            self.assertIsNone(run_harness_e2e.codex_model_catalog("gpt-4", cache))
            self.assertIsNone(run_harness_e2e.codex_model_catalog("gpt-5", Path(temporary) / "missing.json"))

    def test_a_login_shell_in_the_run_home_keeps_the_stub_first(self) -> None:
        """codex runs `bash -lc`; /etc/profile resets PATH, the run home's .bash_profile restores it."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            environment = run_harness_e2e.base_environment(home, Path(temporary), "token")
            printed = subprocess.run(["bash", "-lc", "echo $PATH"], env=environment, capture_output=True,
                                     text=True, check=True).stdout
            self.assertTrue(printed.startswith(str(Path(temporary) / "stub-bin") + ":"), printed)

    def test_the_frontier_model_binds_everywhere_but_claude_code(self) -> None:
        gpt5 = run_harness_e2e.Model("gpt-5")
        self.assertIn("gpt-5", run_harness_e2e.MODEL_CHOICES)
        self.assertEqual((gpt5.local, gpt5.frontier, gpt5.litellm, gpt5.token_field, gpt5.id),
                         (False, True, True, "max_completion_tokens", "gpt-5"))
        self.assertEqual((gpt5.pi, gpt5.prime, gpt5.dsh),
                         (("litellm", "litellm/gpt-5"), ("gpt5", "fleet-local"), "compass-gpt5-high"))
        self.assertEqual(run_harness_e2e.Dsh.key_env[gpt5.dsh], "LITELLM_MASTER_KEY")
        compass, qwen = run_harness_e2e.Model("compass"), run_harness_e2e.Model("qwen3.8-27b-q5")
        self.assertEqual((compass.local, compass.frontier, compass.litellm, compass.token_field),
                         (False, False, False, "max_tokens"))
        self.assertEqual((qwen.litellm, qwen.token_field), (True, "max_tokens"))

        check = run_harness_e2e.check_allowlist
        check("pi", ["pi", "-p", "--model", "litellm/gpt-5", "x"], {}, gpt5)
        check("prime", ["prime", "gpt5", "-p", "x"], {}, gpt5)
        check("codex", ["codex", "exec", "-m", "gpt-5", "x"], {}, gpt5)
        if importlib.util.find_spec("yaml") is not None:   # dsh only; not standard library
            with tempfile.TemporaryDirectory() as tmp:
                import yaml
                settings = {**DSH_SETTINGS, "llm-pi-ai": {"providers": {
                    **DSH_SETTINGS["llm-pi-ai"]["providers"],
                    "compass-gpt5-high": {"apiKeyEnv": "LITELLM_MASTER_KEY", "api": "openai-completions",
                                          "baseURL": "http://127.0.0.1:4000/v1", "models": [{"id": "gpt-5"}],
                                          "compat": {"maxTokensField": "max_completion_tokens"}}}}}
                Path(tmp, "settings.yaml").write_text(yaml.safe_dump(run_harness_e2e.dsh_settings(
                    settings, gpt5.dsh, gpt5.id)), encoding="utf-8")
                check("dsh", ["dsh", "--profile", "headless", "x"], {"DSH_HOME": tmp}, gpt5)
        claude_env = {"ANTHROPIC_BASE_URL": run_harness_e2e.SHIM,
                      **{v: gpt5.id for v in run_harness_e2e.CLAUDE_MODEL_VARIABLES}}
        for harness, argv, environment in (
                ("claude", ["claude", "-p", "--model", "gpt-5", "x"], claude_env),
                ("pi", ["pi", "-p", "--model", "litellm/qwen3.8-27b-q5", "x"], {}),
                ("prime", ["prime", "qwen", "-p", "x"], {}),
                ("codex", ["codex", "exec", "-m", "gpt-5.5", "x"], {})):
            with self.subTest(harness=harness, argv=argv):
                with self.assertRaises(RuntimeError):
                    check(harness, argv, environment, gpt5)
        claude = run_harness_e2e.Claude(gpt5)
        claude.version = lambda: "2.0.0 (Claude Code)"
        self.assertIn("not bound to Claude Code", claude.skip_reason() or "")
        claude_qwen = run_harness_e2e.Claude(qwen)
        claude_qwen.version = lambda: "2.0.0 (Claude Code)"
        self.assertIsNone(claude_qwen.skip_reason())
        self.assertIn("no recorded pi run on the frontier model gpt-5",
                      run_harness_e2e.run_budget("pi", gpt5, "explicit")[1])

    def test_the_frontier_route_never_touches_llama_swap(self) -> None:
        gpt5, compass = run_harness_e2e.Model("gpt-5"), run_harness_e2e.Model("compass")
        asked: list[str] = []

        def http_get(url: str, timeout: float = 3.0) -> tuple[int | None, str]:
            asked.append(url)
            return 200, "{}"
        original = run_harness_e2e.http_get
        run_harness_e2e.http_get = http_get
        self.addCleanup(setattr, run_harness_e2e, "http_get", original)
        for litellm in (True, False):     # dsh reaches a local model directly, gpt-5 only via LiteLLM
            asked.clear()
            self.assertIsNone(run_harness_e2e.endpoint_skip(gpt5, litellm=litellm))
            self.assertEqual(asked, [f"{run_harness_e2e.LITELLM}/health/liveliness"])
        asked.clear()
        self.assertIsNone(run_harness_e2e.endpoint_skip(compass))
        self.assertEqual(asked, [f"{run_harness_e2e.SHIM}/health"])

    def test_the_frontier_probe_caps_output_with_max_completion_tokens(self) -> None:
        sent: list[tuple[str, dict]] = []

        def http_post_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> tuple[int | None, str]:
            sent.append((url, payload))
            return 200, '{"choices": [], "output": []}'
        original = run_harness_e2e.http_post_json
        run_harness_e2e.http_post_json = http_post_json
        self.addCleanup(setattr, run_harness_e2e, "http_post_json", original)
        gpt5, qwen = run_harness_e2e.Model("gpt-5"), run_harness_e2e.Model("qwen3.8-27b-q5")
        self.assertIsNone(run_harness_e2e.probe_openai_chat("http://l", "gpt-5", "k", 1, gpt5.token_field))
        self.assertIsNone(run_harness_e2e.probe_openai_chat("http://l", "qwen3.8-27b-q5", "k", 1, qwen.token_field))
        (_, frontier), (_, local) = sent
        self.assertEqual((frontier.get("max_completion_tokens"), "max_tokens" in frontier), (256, False))
        self.assertEqual((local.get("max_tokens"), "max_completion_tokens" in local), (4, False))
        # the harness adapters pass the model's own field, not the default; Codex its own cap
        for name in ("endpoint_skip", "provider_binding"):
            self.addCleanup(setattr, run_harness_e2e, name, getattr(run_harness_e2e, name))
        run_harness_e2e.endpoint_skip = lambda *args, **kwargs: None
        run_harness_e2e.provider_binding = lambda *args: {"apiKey": "", "models": [{"id": "gpt-5"}]}
        for harness, route, cap in ((run_harness_e2e.Pi(gpt5), "chat/completions", "max_completion_tokens"),
                                    (run_harness_e2e.Prime(gpt5), "chat/completions", "max_completion_tokens"),
                                    (run_harness_e2e.Codex(gpt5), "responses", "max_output_tokens")):
            with self.subTest(harness=harness.name):
                sent.clear()
                self.assertIsNone(harness.probe())
                self.assertEqual(sent[0][0], f"{run_harness_e2e.LITELLM}/v1/{route}")
                self.assertEqual(sent[0][1].get(cap), 256)
                self.assertNotIn("max_tokens", sent[0][1])


def _tree_hashes(base: Path) -> dict[str, str]:
    return {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(base.rglob("*")) if path.is_file()}


class RunRootTests(unittest.TestCase):
    """Each run gets its own temporary root (issue #26): two runs' roots share no
    ancestor closer than the system temp dir, and archive() moves one into the
    evidence directory only after it is called, i.e. only after the process group is
    gone -- byte-for-byte, never rewriting the model's own artifacts (a contract, a
    launcher carrying its own digests). A recorded absolute path is instead translated
    in memory, at grading time, via RunContext.root / mol_e2e._rebase."""

    def temp_root(self) -> Path:
        root = run_harness_e2e.run_root()
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_two_run_roots_share_no_ancestor_below_the_system_temp_dir(self) -> None:
        first, second = self.temp_root(), self.temp_root()
        self.assertNotEqual(first, second)
        system_temp = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(first.resolve().parent, system_temp)
        self.assertEqual(second.resolve().parent, system_temp)
        # Neither is an ancestor of the other, and their lowest common ancestor is
        # exactly the system temp dir -- not each other's, and not the evidence dir's.
        self.assertNotIn(second, first.parents)
        self.assertNotIn(first, second.parents)

    def populated_root(self) -> Path:
        """A stand-in for what a run leaves in its root before collection: a contract
        that recorded its own (temporary) absolute root, a rendered launcher carrying
        digests over its own content, and the raw transcript/stderr a harness would
        have written."""
        root = self.temp_root()
        (root / "repo").mkdir()
        (root / "repo" / "launch-contract.json").write_text(
            json.dumps({"schema_version": "1.0", "sources": [], "repository": {"root": str(root / "repo")}},
                      indent=2), encoding="utf-8")
        (root / "repo" / "run-001-greeting.sh").write_text(
            f"#!/bin/bash\n{mol_e2e.GENERATED_MARKER} contract-sha256=deadbeef "
            "generated-content-sha256=cafef00d\necho hi\n", encoding="utf-8")
        (root / "checkout").mkdir()
        (root / "checkout" / "marker").write_text("checkout", encoding="utf-8")
        (root / "transcript.jsonl").write_text(f'{{"cwd": "{root / "repo"}"}}\n', encoding="utf-8")
        (root / "stderr.log").write_text(str(root), encoding="utf-8")
        (root / "summary.json").write_text(json.dumps({"repo": str(root / "repo"), "root": str(root)}),
                                           encoding="utf-8")
        return root

    def test_archive_moves_bytes_unchanged_and_evidence_is_complete(self) -> None:
        root = self.populated_root()
        before = _tree_hashes(root)
        with tempfile.TemporaryDirectory() as evidence:
            dest = Path(evidence) / "pi-explicit-greeting-ready"
            run_harness_e2e.archive(root, dest)
            self.assertFalse(root.exists(), "the temporary root is gone once it has been archived")
            self.assertEqual(_tree_hashes(dest), before, "archive() must move every file byte-for-byte")
            for relative in ("repo/launch-contract.json", "repo/run-001-greeting.sh", "checkout/marker",
                             "transcript.jsonl", "stderr.log", "summary.json"):
                self.assertIn(relative, before, f"missing from the fixture itself: {relative}")
            # The model's own artifacts still name the old, now-gone temporary root:
            # archive() never rewrites what the grader scores.
            contract = json.loads((dest / "repo" / "launch-contract.json").read_text(encoding="utf-8"))
            self.assertEqual(contract["repository"]["root"], str(root / "repo"))

    def test_archive_keep_copies_bytes_unchanged_and_preserves_the_root(self) -> None:
        root = self.populated_root()
        before = _tree_hashes(root)
        with tempfile.TemporaryDirectory() as evidence:
            dest = Path(evidence) / "prime-implicit-greeting-blocked"
            run_harness_e2e.archive(root, dest, keep=True)
            self.assertTrue(root.is_dir(), "--keep-run-roots leaves the temporary root in place")
            self.assertEqual(_tree_hashes(root), before, "the kept root itself is untouched")
            self.assertEqual(_tree_hashes(dest), before, "the copy is byte-for-byte too")

    def bootstrapped_repo(self, fixture: str, base: Path) -> Path:
        """A real repo with a hand-completed contract (mirrors E2ELogicTests.repo()),
        built directly under `base` so the contract's own repository.root records
        wherever `base` is -- a run_root(), to set up the tests below. The contract
        comes from the real bootstrap script, not a hand-made one, so
        validate_contract.py's source-hash check has real hashes to verify."""
        repo = mol_e2e.prepare_fixture(fixture, base / "repo")
        contract = repo / "launch-contract.json"
        result = mol_e2e.run_script("bootstrap_contract.py", "--repo", repo, "--feature", "specs/001-greeting",
                                    "--output", contract, cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        draft = json.loads(contract.read_text(encoding="utf-8"))
        contract.write_text(json.dumps(mol_e2e.reference_contract(draft, fixture), indent=2), encoding="utf-8")
        return repo

    def evidence_for_reevaluate(self, fixture: str, run_id: str, old_root: Path) -> Path:
        """A run_root() populated as run_one() would leave it: a bootstrapped,
        hand-completed contract and a summary.json naming this root."""
        self.bootstrapped_repo(fixture, old_root)
        (old_root / "transcript.jsonl").write_text("", encoding="utf-8")
        (old_root / "summary.json").write_text(json.dumps({
            "run": run_id, "harness": "pi", "mode": "explicit", "fixture": fixture,
            "timed_out": False, "exit_code": 0, "verdicts": [], "status": "pass", "root": str(old_root)}),
            encoding="utf-8")
        return old_root

    def test_reevaluate_validates_contract_hashes_after_materialising_the_root(self) -> None:
        """The bug this fix exists for: validate_contract.py's check_source_hashes
        hashes sources under the contract's own recorded repository.root -- an
        in-memory path translation never reaches a subprocess, so reevaluate() must
        materialise a copy of the evidence back at that exact path before grading, and
        remove it again once this run's verdicts are in."""
        cases = (("greeting-ready", "independent-validation-validated"),
                 ("greeting-blocked", "independent-validation-blocked"))
        for fixture, verdict_name in cases:
            with self.subTest(fixture=fixture):
                old_root = self.temp_root()
                run_id = f"pi-explicit-{fixture}"
                self.evidence_for_reevaluate(fixture, run_id, old_root)
                with tempfile.TemporaryDirectory() as evidence_dir:
                    evidence = Path(evidence_dir)
                    run_harness_e2e.archive(old_root, evidence / run_id)   # byte-for-byte; still says old_root
                    run_harness_e2e.reevaluate(evidence)
                    self.assertFalse(old_root.exists(), "the materialised root is removed once grading is done")
                    new = json.loads((evidence / run_id / "summary-reevaluated.json").read_text(encoding="utf-8"))
                    verdicts = {v["name"]: v["status"] for v in new["verdicts"]}
                    self.assertEqual(verdicts["sources-match-expectations"], "pass", verdicts)
                    self.assertEqual(verdicts[verdict_name], "pass", verdicts)

    def test_reevaluate_reuses_a_kept_root_without_deleting_it(self) -> None:
        """A root a --keep-run-roots live run left in place (or one materialised by an
        earlier reevaluate that was interrupted) is graded in place if it is genuinely
        this run's own copy, and is not reevaluate()'s to remove."""
        old_root = self.temp_root()
        run_id = "pi-explicit-greeting-ready"
        self.evidence_for_reevaluate("greeting-ready", run_id, old_root)
        with tempfile.TemporaryDirectory() as evidence_dir:
            evidence = Path(evidence_dir)
            run_harness_e2e.archive(old_root, evidence / run_id, keep=True)   # leaves old_root in place too
            run_harness_e2e.reevaluate(evidence)
            self.assertTrue(old_root.is_dir(), "a root that already matched this run is left alone")
            new = json.loads((evidence / run_id / "summary-reevaluated.json").read_text(encoding="utf-8"))
            self.assertEqual({v["name"]: v["status"] for v in new["verdicts"]}["sources-match-expectations"], "pass")

    def test_reevaluate_skips_a_root_collision_instead_of_guessing(self) -> None:
        """A path summary.json names as "root" that exists but belongs to something
        else (not this run) is never graded into or deleted -- reevaluate() skips that
        run with a clear reason."""
        old_root = self.temp_root()
        run_id = "pi-explicit-greeting-ready"
        self.evidence_for_reevaluate("greeting-ready", run_id, old_root)
        colliding_root = self.temp_root()
        (colliding_root / "summary.json").write_text(json.dumps({"run": "someone-elses-run"}), encoding="utf-8")
        # Rewrite summary.json's "root" to point at the unrelated, already-occupied path.
        summary = json.loads((old_root / "summary.json").read_text(encoding="utf-8"))
        summary["root"] = str(colliding_root)
        (old_root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with tempfile.TemporaryDirectory() as evidence_dir:
            evidence = Path(evidence_dir)
            run_harness_e2e.archive(old_root, evidence / run_id)
            exit_status = run_harness_e2e.reevaluate(evidence)
            self.assertFalse((evidence / run_id / "summary-reevaluated.json").exists(),
                             "a colliding root must not be graded into")
            self.assertEqual(json.loads((colliding_root / "summary.json").read_text(encoding="utf-8"))["run"],
                             "someone-elses-run", "the unrelated root must not be touched either")
            self.assertEqual(exit_status, 0, "a skip is not a failure")

    def test_reevaluate_on_old_evidence_without_a_root_key_still_works(self) -> None:
        """Backward compatibility: evidence from before this change has no "root" in
        summary.json, because the run directory always *was* the repo's root -- built
        here exactly that way, with no separate temporary root at any point, unlike
        every other test in this class."""
        run_id = "pi-explicit-greeting-ready"
        with tempfile.TemporaryDirectory() as evidence_dir:
            evidence = Path(evidence_dir)
            run = evidence / run_id
            run.mkdir()
            self.bootstrapped_repo("greeting-ready", run)
            (run / "transcript.jsonl").write_text("", encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({
                "run": run_id, "harness": "pi", "mode": "explicit", "fixture": "greeting-ready",
                "timed_out": False, "exit_code": 0, "verdicts": [], "status": "pass"}),  # no "root" key
                encoding="utf-8")
            run_harness_e2e.reevaluate(evidence)
            new = json.loads((run / "summary-reevaluated.json").read_text(encoding="utf-8"))
            verdicts = {v["name"]: v["status"] for v in new["verdicts"]}
            self.assertEqual(verdicts["sources-match-expectations"], "pass")

    def test_reevaluate_judges_old_evidence_scope_where_the_model_worked(self) -> None:
        """Before #26 a run worked inside the evidence directory: every call names it, so
        that alone is not grader access; and stayed-in-scope matches against the run
        directory as it was then (summary.json "repo"'s parent), even in a copy."""
        run_id = "pi-explicit-greeting-ready"
        with tempfile.TemporaryDirectory() as original_dir, tempfile.TemporaryDirectory() as copy_dir:
            for copied, wandered, expected in ((False, False, "pass"), (False, True, "fail"),
                                               (True, False, "pass"), (True, True, "fail")):
                with self.subTest(copied=copied, wandered=wandered):
                    evidence = Path(copy_dir) / f"evidence-{copied}-{wandered}"
                    run = evidence / run_id
                    run.mkdir(parents=True)
                    # graded in place, or a copy of evidence whose original has gone
                    then = Path(original_dir) / f"{copied}-{wandered}" / run_id if copied else run
                    self.bootstrapped_repo("greeting-ready", run)
                    script = Script("pi")
                    script.shell(f"cd {then}/repo && ls")
                    if wandered:
                        script.shell(f"cat {then}/stub-bin/specstride")
                    (run / "transcript.jsonl").write_text("\n".join(script.end()) + "\n", encoding="utf-8")
                    (run / "summary.json").write_text(json.dumps({
                        "run": run_id, "harness": "pi", "mode": "explicit", "fixture": "greeting-ready",
                        "timed_out": False, "exit_code": 0, "verdicts": [], "status": "pass",
                        "repo": str(then / "repo")}), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        run_harness_e2e.reevaluate(evidence)
                    new = json.loads((run / "summary-reevaluated.json").read_text(encoding="utf-8"))
                    verdicts = {v["name"]: v["status"] for v in new["verdicts"]}
                    self.assertEqual(verdicts["stayed-in-scope"], expected, new["verdicts"])
                    # stub-bin/ is the test harness's own: reading it is grader access too
                    self.assertEqual(verdicts["no-grader-access"], "fail" if wandered else "pass")

    def test_no_grader_access_catches_a_reference_to_another_runs_root(self) -> None:
        """A model that only guesses at a sibling's (or a since-cleaned-up) temporary
        root is still caught, the same way a read of the evidence directory is."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repo = mol_e2e.prepare_fixture("greeting-ready", base / "repo")
        other_root = "/tmp/mol-e2e-run-deadbeef"
        script = happy_script("pi")
        script.tool("bash", {"command": f"ls {other_root}/repo"}, error=True)
        context = mol_e2e.RunContext(harness="pi", mode="explicit", fixture="greeting-ready", repo=repo,
                                     transcript=mol_e2e.parse_transcript(script.end()), work=base,
                                     grader_paths=[other_root])
        verdicts = {v["name"]: v["status"] for v in mol_e2e.evaluate(context)}
        self.assertEqual(verdicts["no-grader-access"], "fail")

    def test_no_grader_access_catches_a_read_of_the_evidence_directory_itself(self) -> None:
        """`ls ../..` from root/repo lands in the system temp dir, which may hold the
        evidence directory (mol-e2e-evidence-*) one level down. run_one() puts
        str(evidence) itself in grader_paths (not just its sibling run entries); this
        proves the mechanism catches a call that mentions that directory."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repo = mol_e2e.prepare_fixture("greeting-ready", base / "repo")
        evidence_dir = "/tmp/mol-e2e-evidence-deadbeef"
        script = happy_script("pi")
        script.tool("bash", {"command": f"ls {evidence_dir}"}, error=True)
        context = mol_e2e.RunContext(harness="pi", mode="explicit", fixture="greeting-ready", repo=repo,
                                     transcript=mol_e2e.parse_transcript(script.end()), work=base,
                                     grader_paths=[evidence_dir])
        verdicts = {v["name"]: v["status"] for v in mol_e2e.evaluate(context)}
        self.assertEqual(verdicts["no-grader-access"], "fail")


if __name__ == "__main__":
    unittest.main()
