#!/usr/bin/env python3
"""Tier 3: live headless end-to-end runs of the mixture-of-loops skill.

The one reviewer command:

    MOL_LIVE_E2E=1 python3 tests/e2e/run_harness_e2e.py --harness all

runs Tier 1 (hermetic unittest) and Tier 2 (real discovery, no model) first and
reports them, then runs each harness headlessly against the local model on both
fixture repositories, in explicit (/skill:) and implicit (plain request) mode.
Without MOL_LIVE_E2E=1 the live runs are reported as skips and the exit status
is that of Tiers 1 and 2.

Only the local model is allowed (pi: litellm/qwen3.8-27b-q5 --thinking off;
prime: variant qwen --thinking off). Every run uses a temporary HOME, temporary
agent directories, a private TMPDIR and daemon socket, telemetry off, stdin
/dev/null and no controlling terminal, and a hard wall-clock timeout that kills
the process group. Processes are tagged with a unique MOL_E2E_RUN value and
every process still carrying it is reaped afterwards (`prime-agent shutdown` is
host-wide and is never called). The real ~/.pi, ~/.prime, ~/.agents and
~/.claude/skills are snapshotted before and compared after every run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_snapshot  # noqa: E402
import mol_e2e  # noqa: E402

ROOT = mol_e2e.ROOT
ONBOARD = ROOT / "bin" / "onboard-skill"
HARNESS_NAMES = ("pi", "prime", "codex", "claude", "dsh")
LITELLM = "http://127.0.0.1:4000"
LLAMA_SWAP = "http://127.0.0.1:8081"
LOCAL_MODEL = "qwen3.8-27b-q5"
# The only model selections this runner will launch. Anything else is rejected before launch.
ALLOWED_MODELS = {"pi": ("--model", "litellm/qwen3.8-27b-q5"), "prime": ("variant", "qwen"),
                  "claude": ("--model", LOCAL_MODEL), "codex": ("-m", LOCAL_MODEL)}
# Every model slot Claude Code can use; any other name would pass through the shim to Compass (off-host).
CLAUDE_MODEL_VARIABLES = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                          "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                          "CLAUDE_CODE_SUBAGENT_MODEL")
SHIM = "http://127.0.0.1:8088"
EXPLICIT = {"pi": "/skill:mixture-of-loops", "prime": "/skill:mixture-of-loops", "claude": "/mixture-of-loops",
            "codex": "$mixture-of-loops"}
PROMPTS = {
    "explicit": "{invocation} derive a pipeline for specs/001-greeting",
    "implicit": ("Derive an unattended Specstride pipeline for the Spec Kit feature in specs/001-greeting: "
                 "write its launch contract and the run script."),
    # The model must read this as `auto`: generate, clear the gate, launch detached, and
    # report the run from its own telemetry until it terminates.
    "auto": "{invocation} derive a pipeline for specs/001-greeting and run it",
}
# A run that is asked to execute gets the pipeline-acting stub instead of the refusing
# one, in sleep mode so an intermediate running state is there to be observed.
EXECUTION_STUB_ENV = {"MOL_EXEC_STUB_MODE": "sleep", "MOL_EXEC_STUB_SLEEP": "5",
                      "MOL_EXEC_STUB_FEATURE": "001-greeting"}
# `auto` does strictly more than deriving: it also runs the pipeline and supervises it to
# a terminal state, which costs at least one poll interval on top. A local model that
# derives in ~18 minutes has nothing left of a 20-minute budget, so auto gets its own.
DEFAULT_TIMEOUT = 1200
AUTO_TIMEOUT = 1800
FIXTURE_ALIASES = {"blocked": "greeting-blocked", "ready": "greeting-ready"}
KERNEL_PYTHON = Path("/root/.prime/agent/kernel-venv/bin/python")
PRIME_VARIANTS = Path("/root/prime-agent/variants.tsv")


# ── helpers ───────────────────────────────────────────────────────────────────

def http_get(url: str, timeout: float = 3.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any failure is a skip reason
        return None, str(exc)


def command_version(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                                env={**os.environ, "HOME": tempfile.gettempdir() + "/mol-e2e-version-home"})
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else None


def pristine_checkout(run: Path, *, execution: bool = False) -> Path:
    """A copy of bin/ and skills/ only, plus the stub, inside the run's temporary root.

    Onboarding from it keeps the one canonical package (byte-identical, verified) while
    the symlink's real path no longer leads a model to this repository's tests, fixtures
    and expectations -- the grader. The live prime run of 2026-09-19 did follow it there.
    """
    checkout = run / "checkout"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "bin", checkout / "bin", ignore=ignore)
    shutil.copytree(mol_e2e.SKILL, checkout / "skills" / mol_e2e.NAME, ignore=ignore)
    shutil.copytree(mol_e2e.EXEC_STUB_BIN if execution else mol_e2e.STUB_BIN, run / "stub-bin")
    canonical = {k: v for k, v in mol_e2e.tree_state(mol_e2e.SKILL).items() if "__pycache__" not in k}
    if mol_e2e.tree_state(checkout / "skills" / mol_e2e.NAME) != canonical:
        raise RuntimeError("the pristine copy differs from the canonical package")
    return checkout


def base_environment(home: Path, tmp: Path, token: str) -> dict[str, str]:
    """A clean environment: no inherited keys, tokens, or harness configuration."""
    keep = {key: os.environ[key] for key in ("LANG", "LC_ALL", "USER", "LOGNAME", "SHELL") if key in os.environ}
    stub = home.parent / "stub-bin"
    return {**keep, "PATH": f"{stub}:{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "HOME": str(home), "TMPDIR": str(tmp), "TERM": "dumb", "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "MOL_E2E_RUN": token}


def marked_processes(token: str) -> list[tuple[int, str]]:
    needle = f"MOL_E2E_RUN={token}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                found.append((int(entry.name), command[:200]))
        except OSError:
            continue
    return found


def reap(token: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Terminate every process tagged with this run's marker; return (reaped, left over)."""
    reaped = marked_processes(token)
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
        for pid, _ in marked_processes(token):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while marked_processes(token) and time.monotonic() < deadline:
            time.sleep(0.2)
    return reaped, marked_processes(token)


def resolve_key_reference(reference: str) -> str | None:
    """Resolve a models.json apiKey reference the way the harnesses do, only to redact it."""
    if reference.startswith("!"):
        result = subprocess.run(["bash", "-c", reference[1:]], capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL, check=False)
        return result.stdout.strip() or None
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", reference):
        return os.environ.get(reference)
    return reference or None


def provider_binding(models_json: Path, provider: str) -> dict:
    """One provider with only the local model entry; apiKey stays a reference, never a value."""
    value = json.loads(models_json.read_text(encoding="utf-8"))["providers"][provider]
    models = [model for model in value.get("models", []) if model.get("id") == LOCAL_MODEL]
    if len(models) != 1:
        raise RuntimeError(f"{models_json}: expected one {LOCAL_MODEL} entry under {provider}")
    return {**{k: v for k, v in value.items() if k != "models"}, "models": models}


# ── harness adapters ──────────────────────────────────────────────────────────

@dataclass
class Prepared:
    argv: list[str]
    environment: dict[str, str]
    model: str
    secrets: list[str]
    onboarding: str


class Harness:
    name = ""
    events = True          # the JSONL event stream is parsed

    def version(self) -> str | None:
        raise NotImplementedError

    def skip_reason(self) -> str | None:
        raise NotImplementedError

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        raise NotImplementedError


def onboard(harness: str, repo: Path, environment: dict[str, str]) -> str:
    lines = []
    linker = Path(environment["HOME"]).parent / "checkout" / "bin" / "onboard-skill"
    for flags in ([], ["--check"]):
        result = subprocess.run([str(linker), "--harness", harness, "--scope", "repo", "--repo", str(repo), *flags],
                                env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, check=False)
        lines.append(f"$ onboard-skill --harness {harness} --scope repo {' '.join(flags)}\n{result.stdout}exit {result.returncode}")
        if not flags and result.returncode:
            raise RuntimeError("onboarding failed:\n" + "\n".join(lines))
    return "\n".join(lines)


def local_endpoint_skip() -> str | None:
    status, _ = http_get(f"{LITELLM}/health/liveliness")
    if status != 200:
        return f"LiteLLM {LITELLM} is not answering (status {status})"
    status, body = http_get(f"{LLAMA_SWAP}/running")
    if status != 200:
        return f"llama-swap {LLAMA_SWAP}/running is not answering (status {status})"
    try:
        running = [entry.get("model") for entry in json.loads(body).get("running", [])]
    except ValueError:
        return "llama-swap /running returned invalid JSON"
    others = [model for model in running if model != LOCAL_MODEL]
    if others:
        return (f"llama-swap has {others} loaded; running {LOCAL_MODEL} would force a model swap and evict "
                "someone else's work (local-model-ops rule), so the live run is skipped")
    return None


class Pi(Harness):
    name = "pi"
    models_json = Path("/root/.pi/agent/models.json")

    def version(self) -> str | None:
        return command_version(["pi", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "pi is not installed or --version failed"
        if not self.models_json.is_file():
            return f"{self.models_json} is missing (needed for the litellm provider binding)"
        return local_endpoint_skip()

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, agent = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "agent-pi"
        home.mkdir()
        agent.mkdir()
        binding = provider_binding(self.models_json, "litellm")
        (agent / "models.json").write_text(json.dumps({"providers": {"litellm": binding}}, indent=2), encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "PI_CODING_AGENT_DIR": str(agent),
                       "PI_OFFLINE": "1", "PI_OTEL_DISABLE": "1"}
        flag, model = ALLOWED_MODELS["pi"]
        argv = ["pi", "-p", "--mode", "json", "--no-session", "--offline", "-a", flag, model,
                "--thinking", "off", prompt]
        return Prepared(argv, environment, model, [s for s in [resolve_key_reference(binding.get("apiKey", ""))] if s],
                        onboard("pi", repo, environment))


class Prime(Harness):
    name = "prime"
    models_json = Path("/root/.prime/agent/models.json")

    def version(self) -> str | None:
        return command_version(["prime-agent", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "prime-agent is not installed or --version failed"
        if shutil.which("prime") is None:
            return "the prime variant launcher is not on PATH"
        rows = [line.split("\t") for line in PRIME_VARIANTS.read_text(encoding="utf-8").splitlines()
                if line.startswith("prime-qwen\t")] if PRIME_VARIANTS.is_file() else []
        if len(rows) != 1 or rows[0][1:5] != ["backend", "qwen", "fleet-local", LOCAL_MODEL]:
            return f"variant qwen does not resolve to backend fleet-local/{LOCAL_MODEL} in {PRIME_VARIANTS}"
        if not KERNEL_PYTHON.is_file():
            return f"{KERNEL_PYTHON} is missing; a fresh kernel venv would need an online bootstrap"
        return local_endpoint_skip()

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp = run / "home", Path(tempfile.mkdtemp(prefix="mol-"))
        agent = home / ".prime" / "agent"
        agent.mkdir(parents=True)
        binding = provider_binding(self.models_json, "fleet-local")
        (agent / "models.json").write_text(json.dumps({"providers": {"fleet-local": binding}}, indent=2),
                                           encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "PRIME_AGENT_CODING_AGENT_DIR": str(agent),
                       "PRIME_AGENT_TELEMETRY": "0", "DO_NOT_TRACK": "1", "PI_OFFLINE": "1",
                       "PRIME_AGENT_KERNEL_PYTHON": str(KERNEL_PYTHON), "AGENTOPS_BIN": "/nonexistent/agentops"}
        _, variant = ALLOWED_MODELS["prime"]
        argv = ["prime", variant, "--mode", "json", "--no-session", "--offline", "--thinking", "off",
                "--daemon-socket", str(tmp / "d.sock"), "-p", prompt]
        return Prepared(argv, environment, f"prime-{variant} -> fleet-local/{LOCAL_MODEL}",
                        [s for s in [resolve_key_reference(binding.get("apiKey", ""))] if s],
                        onboard("prime", repo, environment))


class Claude(Harness):
    """Claude Code through the local compass shim (:8088 -> LiteLLM -> llama-swap), as `bebop qwen` does."""
    name = "claude"

    def version(self) -> str | None:
        return command_version(["claude", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "claude is not installed or --version failed"
        status, _ = http_get(f"{SHIM}/health")
        if status is None:
            return f"the local compass shim {SHIM} is not answering"
        return local_endpoint_skip()

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, config = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "claude-config"
        home.mkdir()
        config.mkdir()
        environment = {**base_environment(home, tmp, token), "ANTHROPIC_BASE_URL": SHIM,
                       # the shim has no key configured (loopback only); this is not a credential
                       "ANTHROPIC_AUTH_TOKEN": "mol-e2e-local-shim",
                       **{variable: LOCAL_MODEL for variable in CLAUDE_MODEL_VARIABLES},
                       "CLAUDE_CONFIG_DIR": str(config), "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                       "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                       "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16000"}
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                "--model", LOCAL_MODEL, "--permission-mode", "acceptEdits",
                # one argument: the variadic `--allowedTools <tools...>` form swallows the prompt
                "--allowed-tools=Bash,Read,Write,Edit,Glob,Grep,Skill", prompt]
        return Prepared(argv, environment, f"claude -> shim -> {LOCAL_MODEL}", [], onboard("claude", repo, environment))


class Codex(Harness):
    """Codex with a LiteLLM provider defined in a temporary CODEX_HOME."""
    name = "codex"
    models_json = Pi.models_json

    def version(self) -> str | None:
        return command_version(["codex", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "codex is not installed or --version failed"
        return local_endpoint_skip()

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, codex_home = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "codex-home"
        home.mkdir()
        codex_home.mkdir()
        binding = provider_binding(self.models_json, "litellm")
        key = resolve_key_reference(binding.get("apiKey", "")) or ""
        (codex_home / "config.toml").write_text(
            f'model = "{LOCAL_MODEL}"\nmodel_provider = "mol-litellm"\n\n[model_providers.mol-litellm]\n'
            f'name = "LiteLLM (local)"\nbase_url = "{LITELLM}/v1"\nenv_key = "MOL_LITELLM_KEY"\n'
            'wire_api = "responses"\n', encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "CODEX_HOME": str(codex_home), "MOL_LITELLM_KEY": key}
        argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write",
                "-m", LOCAL_MODEL, prompt]
        return Prepared(argv, environment, f"codex -> LiteLLM responses -> {LOCAL_MODEL}", [key] if key else [],
                        onboard("codex", repo, environment))


class Unsupported(Harness):
    """Codex, Claude Code and dsh: reported with the reason they are not run live."""

    def __init__(self, name: str, version_argv: list[str], reason: str):
        self.name, self._version_argv, self._reason = name, version_argv, reason

    def version(self) -> str | None:
        return command_version(self._version_argv)

    def skip_reason(self) -> str | None:
        return self._reason


HARNESSES: dict[str, Harness] = {
    "pi": Pi(),
    "prime": Prime(),
    "codex": Codex(),
    "claude": Claude(),
    "dsh": Unsupported("dsh", ["dsh", "--version"], (
        "no local backend within the boundaries: dsh has no per-run model flag and takes its model from "
        "$DSH_HOME/settings.yaml (default zai/glm-5.3-flash, off-host); switching it means editing the real "
        "~/.dsh/settings.yaml, and a temporary DSH_HOME re-installs the profile plugins with pnpm (network) "
        "while the real one writes sessions under ~/.dsh")),
}


def check_allowlist(harness: str, argv: list[str], environment: dict[str, str]) -> None:
    flag, value = ALLOWED_MODELS[harness]
    if harness == "claude":
        wrong = {v: environment.get(v) for v in CLAUDE_MODEL_VARIABLES if environment.get(v) != LOCAL_MODEL}
        if wrong or environment.get("ANTHROPIC_BASE_URL") != SHIM:
            raise RuntimeError(f"refusing to launch claude: every model slot must be {LOCAL_MODEL} via {SHIM}: {wrong}")
    if flag == "variant":
        if argv[:2] != ["prime", value]:
            raise RuntimeError(f"refusing to launch {argv[:2]}: only `prime {value}` is allowed")
        if "--model" in argv or "--provider" in argv:
            raise RuntimeError("refusing to override the prime variant's model")
    elif argv.count(flag) != 1 or argv[argv.index(flag) + 1] != value:
        raise RuntimeError(f"refusing to launch: {harness} model must be exactly {value}")


# ── one run ───────────────────────────────────────────────────────────────────

def run_one(harness: Harness, mode: str, fixture: str, evidence: Path, timeout: int,
            snapshot_before: dict[str, str]) -> dict:
    run_id = f"{harness.name}-{mode}-{fixture}"
    run = evidence / run_id
    run.mkdir(parents=True)
    repo = mol_e2e.prepare_fixture(fixture, run / "repo")
    token = uuid.uuid4().hex
    summary: dict = {"run": run_id, "harness": harness.name, "mode": mode, "fixture": fixture,
                     "version": harness.version(), "transcript": str(run / "transcript.jsonl"),
                     "stderr": str(run / "stderr.log"), "repo": str(repo)}
    checkout_before = checkout_state()
    pristine_checkout(run, execution=mode == "auto")
    prompt = PROMPTS[mode].format(invocation=EXPLICIT[harness.name])
    prepared = harness.prepare(run, repo, prompt, token)
    check_allowlist(harness.name, prepared.argv, prepared.environment)
    summary.update(model=prepared.model, argv=prepared.argv[:-1] + ["<prompt>"], prompt=prompt,
                   onboarding=prepared.onboarding)
    stub_log = run / "harness-stub-specstride.jsonl"
    exec_stub_log = run / "harness-exec-stub-specstride.jsonl"
    environment = {**prepared.environment, "MOL_STUB_LOG": str(stub_log)}
    if mode == "auto":
        environment.update(EXECUTION_STUB_ENV, MOL_EXEC_STUB_LOG=str(exec_stub_log))
    timed_out, exit_code = False, None
    started = time.monotonic()
    reaped: list = []
    left: list = []
    try:
        with (run / "transcript.jsonl").open("w", encoding="utf-8") as out, \
                (run / "stderr.log").open("w", encoding="utf-8") as err:
            process = subprocess.Popen(prepared.argv, cwd=repo, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        exit_code = process.wait(timeout=10)
                        break
                    except subprocess.TimeoutExpired:
                        continue
    finally:
        # Reached on success, timeout, SIGINT and SIGTERM alike.
        reaped, left = reap(token)
        tmp = Path(environment["TMPDIR"])
        shutil.rmtree(tmp, ignore_errors=True)
    summary["duration_seconds"] = round(time.monotonic() - started, 1)
    summary.update(exit_code=exit_code, timed_out=timed_out, reaped=reaped, left_over=left)

    transcript = mol_e2e.PARSERS[harness.name]((run / "transcript.jsonl").read_text(encoding="utf-8").splitlines())
    stub_calls = [json.loads(line) for line in stub_log.read_text(encoding="utf-8").splitlines()] \
        if stub_log.exists() else []
    exec_stub_calls = [json.loads(line) for line in
                       exec_stub_log.read_text(encoding="utf-8").splitlines()] \
        if exec_stub_log.exists() else []
    home_changes = home_snapshot.diff(snapshot_before, home_snapshot.take())
    checkout_after = checkout_state()
    checkout_changed = [f"{k}: {checkout_before.get(k)} -> {checkout_after.get(k)}"
                        for k in sorted(set(checkout_before) | set(checkout_after))
                        if checkout_before.get(k) != checkout_after.get(k)]
    context = mol_e2e.RunContext(harness=harness.name, mode=mode, fixture=fixture, repo=repo,
                                 transcript=transcript, timed_out=timed_out, exit_code=exit_code,
                                 home_changes=home_changes, checkout_changed=checkout_changed,
                                 harness_stub_calls=stub_calls,
                                 execution_stub_calls=exec_stub_calls,
                                 expect_execution=mode == "auto", work=run,
                                 grader_paths=[str(ROOT / "tests"), str(ROOT / "bin"), str(ROOT / "skills"),
                                               "mol_e2e", "expectations/greeting", "reference_contract",
                                               "summary.json", "report.json", "home-snapshot",
                                               *[str(evidence / other) for other in os.listdir(evidence)
                                                 if other != run_id]])
    verdicts = mol_e2e.evaluate(context)
    verdicts.append(mol_e2e.verdict("no-processes-left", not left, f"reaped {len(reaped)}; left {left}"))
    contract = mol_e2e.locate_contract(repo, transcript)
    summary["contract"] = str(contract) if contract else None
    summary["launchers"] = [str(p) for p in mol_e2e.locate_launchers(repo)]
    if contract:
        summary["deterministic_facts"] = mol_e2e.deterministic_facts(
            json.loads(contract.read_text(encoding="utf-8")), repo)
    summary["tool_calls"] = [{"tool": c.tool, "via": c.via, "kind": c.kind, "error": c.is_error,
                              "text": c.text[:400]} for c in transcript.calls]
    summary["stop_reason"] = transcript.stop_reason
    summary["error_message"] = transcript.error_message
    summary["verdicts"] = verdicts
    summary["status"] = mol_e2e.summarize(verdicts)
    redaction = redact(run, prepared.secrets)
    summary["redaction"] = redaction
    if redaction["remaining"]:
        summary["verdicts"].append(mol_e2e.verdict("evidence-redacted", False, str(redaction)))
        summary["status"] = "fail"
    (run / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def checkout_state() -> dict[str, str]:
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--ignored=no"], capture_output=True,
                            text=True, check=False).stdout
    state = {f"git:{line[3:]}": line[:2] for line in status.splitlines()}
    state.update({f"skill:{k}": v for k, v in mol_e2e.tree_state(mol_e2e.SKILL).items()
                  if "__pycache__" not in k})
    return state


KEY_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})")


def redact(run: Path, secrets: list[str]) -> dict:
    """Replace provider key values in every evidence file, then scan to prove none remain."""
    replaced, remaining = 0, []
    for path in run.rglob("*"):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        for secret in secrets:
            new = new.replace(secret, "[REDACTED]")
        new = KEY_PATTERN.sub("[REDACTED]", new)
        if new != text:
            replaced += 1
            path.write_text(new, encoding="utf-8")
        if any(secret in new for secret in secrets) or KEY_PATTERN.search(new):
            remaining.append(str(path))
    return {"files_redacted": replaced, "remaining": remaining, "keys_checked": len(secrets)}


# ── tiers 1 and 2 ─────────────────────────────────────────────────────────────

TIER_MODULES = {
    "tier1": ["tests.test_mixture_of_loops", "tests.test_onboarding_harnesses", "tests.test_fixtures",
              "tests.test_e2e_logic"],
    "tier2": ["tests.test_harness_discovery"],
}


def run_tier(name: str, evidence: Path) -> dict:
    environment = {k: v for k, v in os.environ.items() if k != "MOL_LIVE_E2E"}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", *TIER_MODULES[name]], cwd=ROOT,
                            env=environment, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    log = evidence / f"{name}.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    tail = (result.stdout + result.stderr).strip().splitlines()
    ran = next((line for line in reversed(tail) if line.startswith("Ran ")), "Ran ? tests")
    covers = next((line for line in tail if line.startswith("Tier 2 covers:")), "")
    skipped = re.search(r"skipped=(\d+)", tail[-1] if tail else "")
    return {"tier": name, "status": "pass" if result.returncode == 0 else "fail", "summary": f"{ran}; {tail[-1] if tail else ''}",
            "covers": covers, "skipped": int(skipped.group(1)) if skipped else 0,
            "duration_seconds": round(time.monotonic() - started, 1), "log": str(log)}


# ── reporting ─────────────────────────────────────────────────────────────────

COLORS = {"pass": "\033[32m", "fail": "\033[31m", "skip": "\033[33m"}


def paint(status: str, color: bool) -> str:
    return f"{COLORS[status]}{status}\033[0m" if color and status in COLORS else status


def cross_harness(runs: list[dict]) -> list[dict]:
    """Deterministic facts must be identical across every harness that produced a contract."""
    rows = []
    for fixture in mol_e2e.FIXTURE_NAMES:
        facts = {r["run"]: r["deterministic_facts"] for r in runs
                 if r.get("fixture") == fixture and r.get("deterministic_facts")}
        expected = mol_e2e.expectations(fixture)
        reference = {"sources": expected["sources"], "tasks": expected["tasks"],
                     "declared_commands": [c["argv"] for c in expected["declared_commands"]]}
        # Equal to the independent expectations implies equal across every harness. Only the
        # bootstrap-derived sources must match; sources a model adds are semantic output.
        differing, extra = {}, {}
        for run, value in facts.items():
            sources = value.get("sources", {})
            bootstrap = {path: sources.get(path) for path in reference["sources"]}
            keys = [key for key in ("tasks", "declared_commands")
                    if not value.get("inventory_removed") and value.get(key) != reference[key]]
            if value.get("inventory_removed"):
                extra.setdefault(run, []).append("(inventory removed from the contract; tasks and argv "
                                                 "verified by tasks-match-expectations / declared-command-preserved)")
            if bootstrap != reference["sources"]:
                keys.insert(0, "sources")
            if keys:
                differing[run] = keys
            added = sorted(set(sources) - set(reference["sources"]))
            if added:
                extra[run] = added
        rows.append({"fixture": fixture, "runs": sorted(facts), "differing": differing, "model_added_sources": extra,
                     "status": "skip" if not facts else ("pass" if not differing else "fail")})
    return rows


def print_report(report: dict, color: bool) -> None:
    print("\nmixture-of-loops end-to-end report")
    print(f"evidence: {report['evidence_dir']}")
    for tier in report["tiers"]:
        print(f"  {tier['tier']:<8} {paint(tier['status'], color):<6} {tier['summary']} {tier.get('covers', '')}")
    print(f"  {'harness':<7} {'mode':<9} {'fixture':<17} {'status':<6} {'secs':>6}  detail")
    for run in report["runs"]:
        if run["status"] == "skip":
            detail = run.get("reason", "")
        else:
            failed = [v["name"] for v in run.get("verdicts", []) if v["status"] == "fail"]
            detail = ("failed: " + ", ".join(failed)) if failed else f"{sum(v['status'] == 'pass' for v in run['verdicts'])} assertions passed"
        print(f"  {run['harness']:<7} {run['mode']:<9} {run['fixture']:<17} {paint(run['status'], color):<6} "
              f"{run.get('duration_seconds', ''):>6}  {detail}")
    for row in report.get("cross_harness", []):
        print(f"  cross-harness {row['fixture']:<17} {paint(row['status'], color):<6} runs={len(row['runs'])} "
              f"differing={row['differing'] or 'none'}")


def reevaluate(evidence: Path) -> int:
    """Recompute verdicts from a saved transcript and repository. Facts that only the live
    run could observe (timeout, exit status, reaped processes, home and checkout snapshots,
    stub calls) are carried over from the original summary, never recomputed."""
    carried = ("real-homes-unchanged", "checkout-unchanged", "no-processes-left", "evidence-redacted")
    runs = []
    for summary_path in sorted(evidence.glob("*/summary.json")):
        old = json.loads(summary_path.read_text(encoding="utf-8"))
        run = summary_path.parent
        transcript = mol_e2e.PARSERS[old["harness"]]((run / "transcript.jsonl").read_text(encoding="utf-8").splitlines())
        stub_log = run / "harness-stub-specstride.jsonl"
        stub_calls = [json.loads(line) for line in stub_log.read_text(encoding="utf-8").splitlines()] \
            if stub_log.exists() else []
        exec_log = run / "harness-exec-stub-specstride.jsonl"
        exec_calls = [json.loads(line) for line in exec_log.read_text(encoding="utf-8").splitlines()] \
            if exec_log.exists() else []
        context = mol_e2e.RunContext(harness=old["harness"], mode=old["mode"], fixture=old["fixture"], repo=run / "repo",
                                     transcript=transcript, timed_out=old["timed_out"], exit_code=old["exit_code"],
                                     harness_stub_calls=stub_calls, execution_stub_calls=exec_calls,
                                     expect_execution=old["mode"] == "auto", work=run,
                                     grader_paths=[str(ROOT / "tests"), str(ROOT / "bin"), str(ROOT / "skills"),
                                                   "mol_e2e", "expectations/greeting", "reference_contract",
                                                   "summary.json", "report.json", "home-snapshot",
                                                   *[str(evidence / o) for o in os.listdir(evidence) if o != run.name]])
        verdicts = mol_e2e.evaluate(context) + [v for v in old["verdicts"] if v["name"] in carried]
        changed = {v["name"]: v["status"] for v in verdicts} != {v["name"]: v["status"] for v in old["verdicts"]}
        new = {**old, "verdicts": verdicts, "status": mol_e2e.summarize(verdicts), "reevaluated": True,
               "original_status": old["status"],
               "changed_verdicts": sorted({v["name"] for v in verdicts} ^ {v["name"] for v in old["verdicts"]} |
                                          {v["name"] for v in verdicts for w in old["verdicts"]
                                           if v["name"] == w["name"] and v["status"] != w["status"]}) if changed else []}
        (run / "summary-reevaluated.json").write_text(json.dumps(new, indent=2, default=str), encoding="utf-8")
        runs.append(new)
        failed = [v["name"] for v in verdicts if v["status"] == "fail"]
        print(f"{new['run']:<34} {old['status']:>4} -> {new['status']:<4} changed={new['changed_verdicts']} failed={failed}")
    return 1 if any(r["status"] == "fail" for r in runs) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", default="all", help="pi|prime|codex|claude|dsh|all, or a comma list")
    parser.add_argument("--mode", default="both",
                        choices=("explicit", "implicit", "auto", "both", "all"),
                        help="both: explicit and implicit generation; all: those plus auto")
    parser.add_argument("--fixture", default="all", choices=("blocked", "ready", "all"))
    parser.add_argument("--timeout", type=int, default=None,
                        help=f"wall-clock seconds per harness run (default: {DEFAULT_TIMEOUT}, "
                             f"or {AUTO_TIMEOUT} in auto mode)")
    parser.add_argument("--evidence-dir", help="default: a new temporary directory (never inside the repository)")
    parser.add_argument("--skip-tiers", action="store_true", help="do not run Tiers 1 and 2 first")
    parser.add_argument("--reevaluate", metavar="EVIDENCE_DIR",
                        help="re-score saved runs with the current assertions (no model call); "
                             "writes summary-reevaluated.json next to each summary.json")
    args = parser.parse_args(argv)
    if args.reevaluate:
        return reevaluate(Path(args.reevaluate))

    names = HARNESS_NAMES if args.harness == "all" else tuple(args.harness.split(","))
    unknown = [n for n in names if n not in HARNESSES]
    if unknown:
        parser.error(f"unknown harness: {', '.join(unknown)}")
    modes = {"both": ("explicit", "implicit"),
             "all": ("explicit", "implicit", "auto")}.get(args.mode, (args.mode,))
    fixtures = mol_e2e.FIXTURE_NAMES if args.fixture == "all" else (FIXTURE_ALIASES[args.fixture],)
    evidence = Path(args.evidence_dir).resolve() if args.evidence_dir else Path(tempfile.mkdtemp(prefix="mol-e2e-evidence-"))
    if evidence == ROOT or ROOT in evidence.parents:
        parser.error(f"--evidence-dir must be outside the repository: {evidence}")
    evidence.mkdir(parents=True, exist_ok=True)

    def interrupted(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)

    report: dict = {"evidence_dir": str(evidence), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "tiers": [], "runs": []}
    if not args.skip_tiers:
        report["tiers"] = [run_tier("tier1", evidence), run_tier("tier2", evidence)]
    live = os.environ.get("MOL_LIVE_E2E") == "1"
    snapshot = home_snapshot.take() if live else None
    if snapshot is not None:
        home_snapshot.write(snapshot, evidence / "home-snapshot-before.json")
    try:
        for name in names:
            harness = HARNESSES[name]
            reason = None if live else "MOL_LIVE_E2E=1 is not set (live runs are opt-in)"
            reason = reason or harness.skip_reason()
            for fixture in fixtures:
                for mode in modes:
                    base = {"run": f"{name}-{mode}-{fixture}", "harness": name, "mode": mode, "fixture": fixture,
                            "version": harness.version()}
                    if reason:
                        report["runs"].append({**base, "status": "skip", "reason": reason})
                        continue
                    timeout = args.timeout or (AUTO_TIMEOUT if mode == "auto" else DEFAULT_TIMEOUT)
                    print(f"running {base['run']} (timeout {timeout}s) ...", file=sys.stderr, flush=True)
                    try:
                        report["runs"].append(run_one(harness, mode, fixture, evidence, timeout, snapshot))
                    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                        report["runs"].append({**base, "status": "fail", "reason": f"runner error: {exc}",
                                               "verdicts": [mol_e2e.verdict("runner", False, str(exc))]})
    except KeyboardInterrupt:
        report["interrupted"] = True
    report["cross_harness"] = cross_harness(report["runs"])
    if snapshot is not None:
        report["home_changes_final"] = home_snapshot.diff(snapshot, home_snapshot.take())
    (evidence / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print_report(report, sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    failed = (any(t["status"] == "fail" for t in report["tiers"]) or any(r["status"] == "fail" for r in report["runs"])
              or any(r["status"] == "fail" for r in report["cross_harness"]) or report.get("interrupted")
              or bool(report.get("home_changes_final")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
