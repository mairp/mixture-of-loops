"""Tier 1 (hermetic): the Tier 3 transcript parser and assertions, without a model.

Synthetic pi and prime JSONL transcripts are paired with real fixture repositories
whose contracts were completed by hand (mol_e2e.reference_contract), so every
verdict the live runner can give is checked here: success, a model error that
still exits 0, a missing script call, a failed tool result, a timeout, and the
blocked-outcome variants.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
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

    def shell(self, command: str, error: bool = False) -> "Script":
        if self.harness == "pi":
            return self.tool("bash", {"command": command}, error)
        return self.tool("ipython", {"code": f"%%bash\n{command}"}, error)

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
    script.shell(f"python3 {S}/validate_contract.py launch-contract.json", error=blocked)
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
        contract.write_text(json.dumps(value), encoding="utf-8")
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
        self.assertNotIn("scripts-only-via-shell", verdicts, "open().read() of a script is not execution")
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


if __name__ == "__main__":
    unittest.main()
