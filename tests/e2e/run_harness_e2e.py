#!/usr/bin/env python3
"""Tier 3: live headless end-to-end runs of the mixture-of-loops skill.

The one reviewer command:

    MOL_LIVE_E2E=1 python3 tests/e2e/run_harness_e2e.py --harness all

runs Tier 1 (hermetic unittest) and Tier 2 (real discovery, no model) first and
reports them, then runs each harness headlessly against the local model on both
fixture repositories, in explicit (/skill:) and implicit (plain request) mode.
Without MOL_LIVE_E2E=1 the live runs are reported as skips and the exit status
is that of Tiers 1 and 2.

One model per campaign, chosen with --model: a local llama-swap model
(qwen3.8-27b-q5 by default, muse-glimmer-30b, nemotron-lightning-30b), `gpt-5`
(Compass PROD through LiteLLM; pi, prime, Codex and dsh -- never Claude Code) or
`compass` (claude-opus-4.8 through the local cc-compass-shim, what plain `bebop`
runs; its STAGE access has ended, so every cell on it is `infra`). Every harness is
bound to that one model the way the fleet's own launchers bind it -- pi and Codex
through LiteLLM, prime through its variant, Claude Code through the shim exactly as
`bebop <backend>` does, dsh through a temporary DSH_HOME whose settings.yaml names
the provider -- and the run refuses to start if any slot names anything else.
Thinking is off everywhere.

Before a harness runs, its exact route is probed with one real completion. A
route that does not answer, or a run that dies on an upstream error, is reported
as `infra` (the serving stack), never as `fail` (the skill or the harness), and
is retried once the route answers again (--retry-infra). A cell is `skip` only
for a reason that is printed and that no configuration on this host removes.

Every run uses a temporary HOME, temporary agent directories, a private TMPDIR
and daemon socket, telemetry off, stdin /dev/null and no controlling terminal,
and a hard wall-clock timeout that kills the process group. Processes are tagged
with a unique MOL_E2E_RUN value and every process still carrying it is reaped
afterwards (`prime-agent shutdown` is host-wide and is never called). The real
~/.pi, ~/.prime, ~/.agents, ~/.dsh and ~/.claude/skills are snapshotted before
and compared after every run.

Each run also gets its own temporary root -- a fresh `mkdtemp`, outside the
evidence directory and sharing no ancestor with any other run's root below the
system temp dir. The harness's cwd and HOME-like directories live there, so a
model that lists its parent directory finds only its own run, never a sibling's
or the evidence directory's. That root is moved into the evidence directory,
and only then removed, once the process group is gone.
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
import urllib.error
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
SHIM = "http://127.0.0.1:8088"

# ── the model ─────────────────────────────────────────────────────────────────
# One campaign, one model. The local ones are llama-swap names (one loads at a time, so
# every harness in a campaign shares the load); `compass` is claude-opus-4.8 through the
# cc-compass-shim, the route plain `bebop` takes. The bindings below are the fleet's own:
# /root/.pi/agent/models.json, /root/prime-agent/variants.tsv, /root/.dsh/settings.yaml
# and /root/gpu_rtx_3090/bebop.sh all name them the same way.
LOCAL_MODELS = ("qwen3.8-27b-q5", "muse-glimmer-30b", "nemotron-lightning-30b")
# A frontier model LiteLLM serves from Compass PROD: pi, prime, Codex and dsh reach it
# through LiteLLM with `max_completion_tokens`. Claude Code is never bound to it (GPT
# output through the shim's Anthropic translation renders garbled). Nothing of it runs
# on this GPU, so no llama-swap check or model swap applies.
FRONTIER_MODELS = ("gpt-5",)
COMPASS_MODEL = "claude-opus-4.8"
MODEL_CHOICES = LOCAL_MODELS + FRONTIER_MODELS + ("compass",)
DEFAULT_MODEL = LOCAL_MODELS[0]
# bebop.sh's ctxs table: what llama-swap serves each model with (`-c`); a client that
# advertises more turns auto-compaction into a context overflow.
CONTEXT_WINDOWS = {"qwen3.8-27b-q5": 229376, "muse-glimmer-30b": 131072, "nemotron-lightning-30b": 131072}


@dataclass(frozen=True)
class Model:
    """The one model of a campaign, and how each harness names it."""
    choice: str

    @property
    def id(self) -> str:
        return COMPASS_MODEL if self.choice == "compass" else self.choice

    @property
    def local(self) -> bool:
        """Served by llama-swap on this host's GPU."""
        return self.choice in LOCAL_MODELS

    @property
    def frontier(self) -> bool:
        return self.choice in FRONTIER_MODELS

    @property
    def litellm(self) -> bool:
        """Reached through LiteLLM; `compass` goes through the shim instead."""
        return self.local or self.frontier

    @property
    def token_field(self) -> str:
        """The chat-completions field that caps output: GPT-5 rejects `max_tokens`."""
        return "max_completion_tokens" if self.frontier else "max_tokens"

    @property
    def pi(self) -> tuple[str, str]:
        """(provider, --model value) in /root/.pi/agent/models.json terms."""
        provider = "litellm" if self.litellm else "compass-shim"
        return provider, f"{provider}/{self.id}"

    @property
    def prime(self) -> tuple[str, str]:
        """(variant, provider): `prime <variant>` must resolve to provider/id in variants.tsv."""
        if self.choice == "compass":
            return "compass", "compass"
        return {"qwen3.8-27b-q5": "qwen", "muse-glimmer-30b": "muse", "nemotron-lightning-30b": "nemotron",
                "gpt-5": "gpt5"}[self.id], "fleet-local"

    @property
    def dsh(self) -> str:
        """The llm-pi-ai provider in /root/.dsh/settings.yaml that serves this model."""
        return "local-high" if self.local else "compass-gpt5-high" if self.frontier else "compass-opus-high"

    @property
    def claude(self) -> str:
        """ANTHROPIC_MODEL for the shim: local names route to LiteLLM, claude-* to Compass."""
        return self.id


# Every model slot Claude Code can use; any other name would pass through the shim to Compass (off-host).
CLAUDE_MODEL_VARIABLES = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                          "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                          "CLAUDE_CODE_SUBAGENT_MODEL")
EXPLICIT = {"pi": "/skill:mixture-of-loops", "prime": "/skill:mixture-of-loops", "claude": "/mixture-of-loops",
            "codex": "$mixture-of-loops", "dsh": "/mixture-of-loops"}
# A run that ends on one of these is the serving stack failing, not the skill or the harness.
INFRA_PATTERNS = re.compile(
    r"upstream error|\b50[0-4]\b|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out|"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|Connection (error|refused|reset)|TRANSPORT:|PI_AI_ERROR|"
    r"no router for requested model|model_not_found|Model \"[^\"]+\" not found|not a valid model|"
    r"AuthenticationError|invalid input model|Incorrect API key|Invalid API Key|insufficient_quota|"
    r"rate limit|overloaded_error|"
    r"fetch failed|socket hang up|stream disconnected|Daemon worker client closed", re.IGNORECASE)
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
                      "MOL_EXEC_STUB_FEATURE": "001-greeting",
                      "MOL_EXEC_STUB_IMPLEMENTATION": str(mol_e2e.IMPLEMENTATION)}
# ── the budget ────────────────────────────────────────────────────────────────
# Wall-clock seconds per run, by harness, model class (local, gpt-5 or Compass) and run kind:
# `auto` does strictly more than deriving (it also runs the pipeline and supervises it
# to a terminal state), so it is budgeted apart. A cell's budget is 1.5x the slowest run
# recorded for it, rounded up to 5 minutes, never below the old flat defaults below.
# Slowest recorded runs, all qwen3.8-27b-q5 with thinking off:
#   harness  generate (explicit/implicit)                 auto
#   pi        446 s  (2026-09-23)            -> 1200      537 s (2026-09-20) -> 1800
#   prime     577 s  (2026-09-23)            -> 1200      331 s (2026-09-20) -> 1800
#   codex     718 s  (2026-09-23)            -> 1200      615 s (2026-09-20) -> 1800
#   claude   1776 s  (the auto run; it holds -> 2700     1776 s (2026-09-20) -> 2700
#            a whole generation. The 2026-09-23 generation hit 1200 s without finishing.)
#   dsh       829 s  (2026-09-23 trial)      -> 1500      none               -> 1800
# Claude Code is the slow one because every turn resends its ~30k-token context (30 to
# 45 s a turn through the shim). Compass has no completed run on this host (STAGE is
# retired, so every cell is infra) and keeps the flat defaults; so does gpt-5, until it
# has recorded runs of its own. --timeout overrides all.
DEFAULT_TIMEOUT = 1200
AUTO_TIMEOUT = 1800
BUDGET_MARGIN = 1.5
RECORDED_SECONDS = {  # (harness, auto) -> the slowest recorded run on a local model
    ("pi", False): 446, ("pi", True): 537,
    ("prime", False): 577, ("prime", True): 331,
    ("codex", False): 718, ("codex", True): 615,
    ("claude", False): 1776, ("claude", True): 1776,
    ("dsh", False): 829,
}


def run_budget(harness: str, model: Model, mode: str, override: int | None = None) -> tuple[int, str]:
    """(seconds, where the number comes from) for one run of `harness` in `mode`."""
    auto = mode == "auto"
    floor = AUTO_TIMEOUT if auto else DEFAULT_TIMEOUT
    if override:
        return override, "--timeout"
    recorded = RECORDED_SECONDS.get((harness, auto)) if model.local else None
    if recorded is None:
        why = "a local model" if model.local else f"the frontier model {model.id}" if model.frontier else "Compass"
        return floor, f"flat default: no recorded {harness} {'auto ' if auto else ''}run on {why}"
    derived = -(-int(recorded * BUDGET_MARGIN) // 300) * 300
    if derived <= floor:
        return floor, f"flat default: {BUDGET_MARGIN}x the {recorded} s recorded fits in it"
    return derived, f"{BUDGET_MARGIN}x the {recorded} s recorded for {harness} on a local model"


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


def run_root() -> Path:
    """A fresh temporary root for one run. Always a direct child of the system temp
    directory (never of the evidence directory or of another run's root), so two
    runs' roots share no ancestor closer than the system temp dir itself."""
    return Path(tempfile.mkdtemp(prefix="mol-e2e-run-"))


def archive(root: Path, dest: Path, *, keep: bool = False) -> None:
    """Move (or, with `keep`, copy) one run's temporary root into its place in the
    evidence directory -- called only after the harness's process group is gone.

    Byte-for-byte: nothing under `root` is rewritten. The model's own artifacts (a
    contract, a rendered launcher carrying its own contract-sha256/generated-content-
    sha256) must stay exactly what the model produced, or the grader would be scoring
    bytes the model never wrote and the launcher's self-digest would break -- and
    validate_contract.py hashes sources under whatever `repository.root` the contract
    itself recorded, so a path swapped in memory would not even reach it. Instead,
    summary.json's own "root" field (written before this is called) records where
    `root` was, so --reevaluate can materialise a copy back at that exact path before
    grading -- see reevaluate()."""
    if keep:
        shutil.copytree(root, dest)
    else:
        shutil.move(str(root), str(dest))


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


def provider_binding(models_json: Path, provider: str, model_id: str) -> dict:
    """One provider with only the chosen model's entry; apiKey stays a reference, never a value."""
    providers = json.loads(models_json.read_text(encoding="utf-8"))["providers"]
    if provider not in providers:
        raise RuntimeError(f"{models_json}: no provider {provider}")
    value = providers[provider]
    models = [model for model in value.get("models", []) if model.get("id") == model_id]
    if len(models) != 1:
        raise RuntimeError(f"{models_json}: expected one {model_id} entry under {provider}")
    return {**{k: v for k, v in value.items() if k != "models"}, "models": models}


def http_post_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> tuple[int | None, str]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")[:2000]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:2000]
    except Exception as exc:  # noqa: BLE001 - any failure is the probe's finding
        return None, f"{exc.__class__.__name__}: {exc}"


# Output caps for a probe, by the field that carries them: `max_completion_tokens` is a
# reasoning model's, which spends tokens before it answers, so 4 would end it empty.
PROBE_TOKENS = {"max_tokens": 4, "max_completion_tokens": 256}


def probe_openai_chat(base: str, model_id: str, key: str | None, timeout: float,
                      token_field: str = "max_tokens") -> str | None:
    """One real short completion on an OpenAI-compatible chat route; None means it answered."""
    status, body = http_post_json(f"{base}/v1/chat/completions",
                                  {"model": model_id, token_field: PROBE_TOKENS[token_field], "stream": False,
                                   "messages": [{"role": "user", "content": "Reply with the word pong."}]},
                                  {"Authorization": f"Bearer {key}"} if key else {}, timeout)
    if status == 200 and '"choices"' in body:
        return None
    return f"POST {base}/v1/chat/completions model={model_id}: status {status}: {body[:300]}"


def probe_openai_responses(base: str, model_id: str, key: str | None, timeout: float,
                           max_output_tokens: int = 16) -> str | None:
    """The Responses API route Codex uses (`wire_api = "responses"`)."""
    status, body = http_post_json(f"{base}/v1/responses",
                                  {"model": model_id, "max_output_tokens": max_output_tokens,
                                   "input": "Reply with the word pong.",
                                   "stream": False},
                                  {"Authorization": f"Bearer {key}"} if key else {}, timeout)
    if status == 200 and '"output"' in body:
        return None
    return f"POST {base}/v1/responses model={model_id}: status {status}: {body[:300]}"


def probe_anthropic_messages(base: str, model_id: str, key: str, timeout: float) -> str | None:
    """The Anthropic Messages route the shim serves to Claude Code, pi and prime."""
    status, body = http_post_json(f"{base}/v1/messages",
                                  {"model": model_id, "max_tokens": 4,
                                   "messages": [{"role": "user", "content": "Reply with the word pong."}]},
                                  {"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"},
                                  timeout)
    if status == 200 and '"content"' in body:
        return None
    return f"POST {base}/v1/messages model={model_id}: status {status}: {body[:300]}"


def litellm_key() -> str | None:
    """The fleet gateway key, only to probe and to redact; read the way the harness configs do."""
    env_file = Path("/root/litellm/.env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip() or None
    return os.environ.get("LITELLM_MASTER_KEY")


# A local model's first request loads it into VRAM (a minute or more for a 27B), so probes wait.
PROBE_TIMEOUT = 420.0


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
    stdout_name = "transcript.jsonl"   # where the process's stdout goes; the transcript unless collect() writes it

    def __init__(self, model: Model, allow_swap: bool = False):
        self.model = model
        self.allow_swap = allow_swap   # let llama-swap evict another loaded model for this one

    def version(self) -> str | None:
        raise NotImplementedError

    def skip_reason(self) -> str | None:
        """A reason no configuration on this host removes; None when the harness can run."""
        raise NotImplementedError

    def probe(self) -> str | None:
        """One real completion through this harness's exact route; None when it answered."""
        raise NotImplementedError

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        raise NotImplementedError

    def collect(self, run: Path, environment: dict[str, str]) -> None:
        """Copy what the harness left in its temporary state into the run directory (before cleanup)."""
        return None


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


def endpoint_skip(model: Model, *, litellm: bool = True, allow_swap: bool = False) -> str | None:
    """Reasons a local route cannot be used right now; None when it can.

    `litellm=False` is for a harness that reaches a local model on llama-swap directly
    (dsh); a frontier model is only ever reached through LiteLLM."""
    if model.choice == "compass":
        status, _ = http_get(f"{SHIM}/health")
        return None if status == 200 else f"the local compass shim {SHIM} is not answering (status {status})"
    if litellm or model.frontier:
        status, _ = http_get(f"{LITELLM}/health/liveliness")
        if status != 200:
            return f"LiteLLM {LITELLM} is not answering (status {status})"
    if model.frontier:
        return None   # nothing on this GPU: no llama-swap load to check, no swap to force
    status, body = http_get(f"{LLAMA_SWAP}/running")
    if status != 200:
        return f"llama-swap {LLAMA_SWAP}/running is not answering (status {status})"
    try:
        running = [entry.get("model") for entry in json.loads(body).get("running", [])]
    except ValueError:
        return "llama-swap /running returned invalid JSON"
    status, body = http_get(f"{LLAMA_SWAP}/v1/models")
    served = re.findall(r'"id":\s*"([^"]+)"', body) if status == 200 else []
    if served and model.id not in served:
        return f"llama-swap does not serve {model.id} (it serves {served})"
    others = [name for name in running if name != model.id]
    if others and not allow_swap:
        return (f"llama-swap has {others} loaded; running {model.id} would force a model swap and evict "
                "someone else's work (local-model-ops rule); pass --allow-swap to do it anyway")
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
            return f"{self.models_json} is missing (needed for the provider binding)"
        try:
            provider_binding(self.models_json, self.model.pi[0], self.model.id)
        except RuntimeError as exc:
            return str(exc)
        return None

    def _binding(self) -> dict:
        return provider_binding(self.models_json, self.model.pi[0], self.model.id)

    def probe(self) -> str | None:
        blocked = endpoint_skip(self.model, allow_swap=self.allow_swap)
        if blocked:
            return blocked
        binding = self._binding()
        key = resolve_key_reference(binding.get("apiKey", ""))
        if self.model.litellm:
            return probe_openai_chat(LITELLM, self.model.id, key, PROBE_TIMEOUT, self.model.token_field)
        return probe_anthropic_messages(SHIM, self.model.id, key or "dummy", PROBE_TIMEOUT)

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, agent = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "agent-pi"
        home.mkdir()
        agent.mkdir()
        provider, model = self.model.pi
        binding = self._binding()
        (agent / "models.json").write_text(json.dumps({"providers": {provider: binding}}, indent=2), encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "PI_CODING_AGENT_DIR": str(agent),
                       "PI_OFFLINE": "1", "PI_OTEL_DISABLE": "1"}
        argv = ["pi", "-p", "--mode", "json", "--no-session", "--offline", "-a", "--model", model,
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
        variant, provider = self.model.prime
        rows = [line.split("\t") for line in PRIME_VARIANTS.read_text(encoding="utf-8").splitlines()
                if line.startswith(f"prime-{variant}\t")] if PRIME_VARIANTS.is_file() else []
        if len(rows) != 1 or rows[0][1:5] != ["backend", variant, provider, self.model.id]:
            return f"variant {variant} does not resolve to backend {provider}/{self.model.id} in {PRIME_VARIANTS}"
        if not KERNEL_PYTHON.is_file():
            return f"{KERNEL_PYTHON} is missing; a fresh kernel venv would need an online bootstrap"
        try:
            provider_binding(self.models_json, provider, self.model.id)
        except (RuntimeError, OSError) as exc:
            return str(exc)
        return None

    def probe(self) -> str | None:
        blocked = endpoint_skip(self.model, allow_swap=self.allow_swap)
        if blocked:
            return blocked
        binding = provider_binding(self.models_json, self.model.prime[1], self.model.id)
        key = resolve_key_reference(binding.get("apiKey", ""))
        if self.model.litellm:
            return probe_openai_chat(LITELLM, self.model.id, key, PROBE_TIMEOUT, self.model.token_field)
        return probe_anthropic_messages(SHIM, self.model.id, key or "dummy", PROBE_TIMEOUT)

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp = run / "home", Path(tempfile.mkdtemp(prefix="mol-"))
        agent = home / ".prime" / "agent"
        agent.mkdir(parents=True)
        variant, provider = self.model.prime
        binding = provider_binding(self.models_json, provider, self.model.id)
        (agent / "models.json").write_text(json.dumps({"providers": {provider: binding}}, indent=2),
                                           encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "PRIME_AGENT_CODING_AGENT_DIR": str(agent),
                       "PRIME_AGENT_TELEMETRY": "0", "DO_NOT_TRACK": "1", "PI_OFFLINE": "1",
                       "PRIME_AGENT_KERNEL_PYTHON": str(KERNEL_PYTHON), "AGENTOPS_BIN": "/nonexistent/agentops"}
        argv = ["prime", variant, "--mode", "json", "--no-session", "--offline", "--thinking", "off",
                "--daemon-socket", str(tmp / "d.sock"), "-p", prompt]
        return Prepared(argv, environment, f"prime-{variant} -> {provider}/{self.model.id}",
                        [s for s in [resolve_key_reference(binding.get("apiKey", ""))] if s],
                        onboard("prime", repo, environment))


def claude_max_output(model: Model) -> str:
    """bebop.sh's _bebop_max_output without thinking: a local model gets 8192 (the reserve
    is subtracted from a small window, and it bounds a runaway turn), claude-* 32000."""
    return "8192" if model.local else "32000"


class Claude(Harness):
    """Claude Code through the local compass shim, exactly as `bebop <backend>` launches it.

    bebop.sh pins every model slot to the one backend (one model in VRAM, no subagent
    swap), advertises the window llama-swap serves, and points ANTHROPIC_BASE_URL at
    the shim, which sends claude-* names to Compass and everything else to LiteLLM.
    """
    name = "claude"

    def version(self) -> str | None:
        return command_version(["claude", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "claude is not installed or --version failed"
        if self.model.frontier:
            return (f"{self.model.id} is not bound to Claude Code: GPT output through the shim's Anthropic "
                    "translation renders garbled; the frontier check runs on pi, prime, Codex and dsh")
        return None

    def probe(self) -> str | None:
        status, body = http_get(f"{SHIM}/health")
        if status != 200:
            return f"the local compass shim {SHIM} is not answering (status {status})"
        blocked = endpoint_skip(self.model, allow_swap=self.allow_swap)
        if blocked:
            return blocked
        if self.model.local:
            try:
                served = json.loads(body)
                routed = set(served.get("qwen_models", [])) | set(served.get("openai_models", []))
            except ValueError:
                routed = set()
            if routed and self.model.id not in routed:
                return f"the shim routes {sorted(routed)} to LiteLLM, not {self.model.id}: add it to its .env"
        return probe_anthropic_messages(SHIM, self.model.claude, "mol-e2e-local-shim", PROBE_TIMEOUT)

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, config = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "claude-config"
        home.mkdir()
        config.mkdir()
        model = self.model.claude
        environment = {**base_environment(home, tmp, token), "ANTHROPIC_BASE_URL": SHIM,
                       # the shim has no key configured (loopback only); this is not a credential
                       "ANTHROPIC_AUTH_TOKEN": "mol-e2e-local-shim",
                       **{variable: model for variable in CLAUDE_MODEL_VARIABLES},
                       "CLAUDE_CONFIG_DIR": str(config), "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                       "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                       "CLAUDE_CODE_MAX_OUTPUT_TOKENS": claude_max_output(self.model)}
        if self.model.local:
            environment["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(CONTEXT_WINDOWS[self.model.id])
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                "--model", model, "--permission-mode", "acceptEdits",
                # one argument: the variadic `--allowedTools <tools...>` form swallows the prompt
                "--allowed-tools=Bash,Read,Write,Edit,Glob,Grep,Skill", prompt]
        route = "LiteLLM" if self.model.local else "Compass"
        return Prepared(argv, environment, f"claude -> shim -> {route} -> {model}", [],
                        onboard("claude", repo, environment))


class Codex(Harness):
    """Codex with a LiteLLM provider defined in a temporary CODEX_HOME."""
    name = "codex"
    models_json = Pi.models_json

    def version(self) -> str | None:
        return command_version(["codex", "--version"])

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "codex is not installed or --version failed"
        if not self.model.litellm:
            return ("Codex speaks the OpenAI Responses API only and LiteLLM has no claude-opus-4.8 route; "
                    "the compass model cannot be bound to Codex on this host")
        try:
            provider_binding(self.models_json, "litellm", self.model.id)
        except (RuntimeError, OSError) as exc:
            return str(exc)
        return None

    def probe(self) -> str | None:
        blocked = endpoint_skip(self.model, allow_swap=self.allow_swap)
        if blocked:
            return blocked
        binding = provider_binding(self.models_json, "litellm", self.model.id)
        key = resolve_key_reference(binding.get("apiKey", ""))
        return probe_openai_responses(LITELLM, self.model.id, key, PROBE_TIMEOUT,
                                      PROBE_TOKENS["max_completion_tokens"] if self.model.frontier else 16)

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        home, tmp, codex_home = run / "home", Path(tempfile.mkdtemp(prefix="mol-")), run / "codex-home"
        home.mkdir()
        codex_home.mkdir()
        binding = provider_binding(self.models_json, "litellm", self.model.id)
        key = resolve_key_reference(binding.get("apiKey", "")) or ""
        (codex_home / "config.toml").write_text(
            f'model = "{self.model.id}"\nmodel_provider = "mol-litellm"\n\n[model_providers.mol-litellm]\n'
            f'name = "LiteLLM"\nbase_url = "{LITELLM}/v1"\nenv_key = "MOL_LITELLM_KEY"\n'
            'wire_api = "responses"\n', encoding="utf-8")
        environment = {**base_environment(home, tmp, token), "CODEX_HOME": str(codex_home), "MOL_LITELLM_KEY": key}
        argv = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write",
                "-m", self.model.id, prompt]
        return Prepared(argv, environment, f"codex -> LiteLLM responses -> {self.model.id}", [key] if key else [],
                        onboard("codex", repo, environment))


DSH_HOME_REAL = Path("/root/.dsh")


def dsh_settings(real_settings: dict, provider: str, model_id: str) -> dict:
    """The settings.yaml of a temporary DSH_HOME: the real file's one provider, filtered to the model.

    dsh has no per-run model flag and `--patch` on `agent-default-model` loses to the
    settings file at run time (verified 2026-09-23: the patched run answered from the
    settings' zai/glm-5.3-flash). What does bind the model is the settings file itself,
    the way /root/muse-harness-ab/run_ab.py bound it for every dsh cell of the August
    campaigns: a copy with `agent-default-model` rewritten. Only the chosen provider is
    kept, so a fallback cannot reach any other backend.
    """
    providers = ((real_settings.get("llm-pi-ai") or {}).get("providers") or {})
    if provider not in providers:
        raise RuntimeError(f"settings.yaml has no llm-pi-ai provider {provider}")
    entry = dict(providers[provider])
    models = [m for m in entry.get("models", []) if isinstance(m, dict) and m.get("id") == model_id]
    if len(models) != 1:
        raise RuntimeError(f"settings.yaml provider {provider} does not declare model {model_id}")
    entry["models"] = models
    return {"agent-default-model": {"provider": provider, "model": model_id, "reasoningEffort": "off"},
            "llm-pi-ai": {"providers": {provider: entry}},
            "permission": {"defaultPreset": "danger-full-access"},
            "agent-presets": {"default": "standard"}}


def dsh_credential(real_credentials: str, key_env: str) -> tuple[str | None, str | None]:
    """(the line for key_env, its value) from ~/.dsh/.credentials.yaml -- copied, never printed."""
    for line in real_credentials.splitlines():
        if line.startswith(f"{key_env}:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            return line, (value or None)
    return None, None


class Dsh(Harness):
    """DeepSeek Harness headless, from a temporary DSH_HOME bound to the one model.

    The profile tree (~/.dsh/profiles, 345K of manifests whose node_modules are links
    into the global package) is copied with its links, so nothing is installed; the
    settings name one provider and one model; the credentials file carries only that
    provider's key. Sessions land under the temporary home and are collected as the
    transcript: dsh prints only the final message on stdout, but its session JSONL
    records every tool call, result and the provider/model that answered.
    """
    name = "dsh"
    stdout_name = "stdout.log"
    key_env = {"local-high": "LOCAL_LITELLM_API_KEY", "compass-gpt5-high": "LITELLM_MASTER_KEY",
               "compass-opus-high": "COMPASS_STAGE_API_KEY"}

    def version(self) -> str | None:
        return command_version(["dsh", "--version"])

    def _real_settings(self) -> dict:
        import yaml  # noqa: PLC0415 - only the dsh adapter needs it
        return yaml.safe_load((DSH_HOME_REAL / "settings.yaml").read_text(encoding="utf-8")) or {}

    def skip_reason(self) -> str | None:
        if self.version() is None:
            return "dsh is not installed or --version failed"
        if shutil.which("zstd") is None:
            return "zstd is not installed (dsh writes its session JSONL zstd-compressed)"
        for required in ("settings.yaml", ".credentials.yaml", "profiles/headless/package.json"):
            if not (DSH_HOME_REAL / required).is_file():
                return f"{DSH_HOME_REAL / required} is missing"
        try:
            import yaml  # noqa: F401,PLC0415
        except ImportError:
            return "python3 has no yaml module (needed to write the temporary settings.yaml)"
        try:
            dsh_settings(self._real_settings(), self.model.dsh, self.model.id)
        except RuntimeError as exc:
            return str(exc)
        line, _ = dsh_credential((DSH_HOME_REAL / ".credentials.yaml").read_text(encoding="utf-8"),
                                 self.key_env[self.model.dsh])
        if line is None:
            return f"{DSH_HOME_REAL / '.credentials.yaml'} has no {self.key_env[self.model.dsh]}"
        return None

    def probe(self) -> str | None:
        blocked = endpoint_skip(self.model, litellm=False, allow_swap=self.allow_swap)
        if blocked:
            return blocked
        _, key = dsh_credential((DSH_HOME_REAL / ".credentials.yaml").read_text(encoding="utf-8"),
                                self.key_env[self.model.dsh])
        settings = dsh_settings(self._real_settings(), self.model.dsh, self.model.id)
        base = str(settings["llm-pi-ai"]["providers"][self.model.dsh].get("baseURL", "")).rstrip("/")
        if self.model.litellm:   # an OpenAI route: llama-swap directly for a local model, LiteLLM for gpt-5
            return probe_openai_chat(base[:-3] if base.endswith("/v1") else base, self.model.id, key, PROBE_TIMEOUT,
                                     self.model.token_field)
        return probe_anthropic_messages(base, self.model.id, key or "dummy", PROBE_TIMEOUT)

    def prepare(self, run: Path, repo: Path, prompt: str, token: str) -> Prepared:
        import yaml  # noqa: PLC0415
        home, tmp = run / "home", Path(tempfile.mkdtemp(prefix="mol-"))
        home.mkdir()
        dsh_home = tmp / "dsh-home"
        dsh_home.mkdir()
        shutil.copytree(DSH_HOME_REAL / "profiles", dsh_home / "profiles", symlinks=True)
        settings = dsh_settings(self._real_settings(), self.model.dsh, self.model.id)
        (dsh_home / "settings.yaml").write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
        line, key = dsh_credential((DSH_HOME_REAL / ".credentials.yaml").read_text(encoding="utf-8"),
                                   self.key_env[self.model.dsh])
        credentials = dsh_home / ".credentials.yaml"
        credentials.write_text((line or "") + "\n", encoding="utf-8")
        credentials.chmod(0o600)
        environment = {**base_environment(home, tmp, token), "DSH_HOME": str(dsh_home),
                       "DSH_PERMISSION_MODE": "danger-full-access", "DSH_TELEMETRY_MODE": "DISABLED"}
        argv = ["dsh", "--profile", "headless", prompt]
        return Prepared(argv, environment, f"dsh -> {self.model.dsh}/{self.model.id}", [key] if key else [],
                        onboard("dsh", repo, environment))

    def collect(self, run: Path, environment: dict[str, str]) -> None:
        sessions = sorted(Path(environment["DSH_HOME"]).glob("sessions/*/*/session.jsonl*"))
        lines: list[str] = []
        for session in sessions:
            if session.suffix == ".zstd":
                result = subprocess.run(["zstd", "-dc", str(session)], capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, check=False)
                lines.extend(result.stdout.splitlines())
            else:
                lines.extend(session.read_text(encoding="utf-8", errors="replace").splitlines())
        (run / "transcript.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        (run / "sessions.txt").write_text("\n".join(str(s) for s in sessions) + "\n", encoding="utf-8")


def make_harnesses(model: Model, allow_swap: bool = False) -> dict[str, Harness]:
    return {name: cls(model, allow_swap) for name, cls in
            (("pi", Pi), ("prime", Prime), ("codex", Codex), ("claude", Claude), ("dsh", Dsh))}


def check_allowlist(harness: str, argv: list[str], environment: dict[str, str], model: Model) -> None:
    """Refuse to launch anything that is not bound to the campaign's one model."""
    if harness == "claude":
        if model.frontier:
            raise RuntimeError(f"refusing to launch claude on {model.id}: GPT output through the shim's "
                               "Anthropic translation renders garbled")
        wrong = {v: environment.get(v) for v in CLAUDE_MODEL_VARIABLES if environment.get(v) != model.claude}
        if wrong or environment.get("ANTHROPIC_BASE_URL") != SHIM:
            raise RuntimeError(f"refusing to launch claude: every model slot must be {model.claude} via {SHIM}: {wrong}")
        if argv.count("--model") != 1 or argv[argv.index("--model") + 1] != model.claude:
            raise RuntimeError(f"refusing to launch claude: --model must be exactly {model.claude}")
    elif harness == "prime":
        variant, _ = model.prime
        if argv[:2] != ["prime", variant]:
            raise RuntimeError(f"refusing to launch {argv[:2]}: only `prime {variant}` is allowed")
        if "--model" in argv or "--provider" in argv:
            raise RuntimeError("refusing to override the prime variant's model")
    elif harness == "pi":
        _, value = model.pi
        if argv.count("--model") != 1 or argv[argv.index("--model") + 1] != value or "--provider" in argv:
            raise RuntimeError(f"refusing to launch: pi model must be exactly {value}")
    elif harness == "codex":
        if not model.litellm or argv.count("-m") != 1 or argv[argv.index("-m") + 1] != model.id:
            raise RuntimeError(f"refusing to launch: codex model must be exactly {model.id} (a LiteLLM model)")
    elif harness == "dsh":
        dsh_home = environment.get("DSH_HOME", "")
        if not dsh_home or dsh_home.startswith(str(DSH_HOME_REAL)) or "--patch" in argv or "--profile" not in argv \
                or argv[argv.index("--profile") + 1] != "headless":
            raise RuntimeError("refusing to launch dsh: it must run the headless profile from a temporary DSH_HOME")
        try:
            import yaml  # noqa: PLC0415
            settings = yaml.safe_load(Path(dsh_home, "settings.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, ImportError) as exc:
            raise RuntimeError(f"refusing to launch dsh: cannot read the temporary settings: {exc}") from exc
        default = settings.get("agent-default-model") or {}
        providers = ((settings.get("llm-pi-ai") or {}).get("providers") or {})
        if (default.get("provider"), default.get("model")) != (model.dsh, model.id) or set(providers) != {model.dsh}:
            raise RuntimeError(f"refusing to launch dsh: settings must bind exactly {model.dsh}/{model.id}: "
                               f"{default} providers={sorted(providers)}")
    else:
        raise RuntimeError(f"unknown harness {harness}")


# ── one run ───────────────────────────────────────────────────────────────────

def run_one(harness: Harness, mode: str, fixture: str, evidence: Path, timeout: int,
            snapshot_before: dict[str, str], run_roots: list[Path], keep: bool = False) -> dict:
    run_id = f"{harness.name}-{mode}-{fixture}"
    dest = evidence / run_id
    if dest.exists():
        raise RuntimeError(f"evidence for {run_id} already exists at {dest}")
    root = run_root()
    run_roots.append(root)
    try:
        repo = mol_e2e.prepare_fixture(fixture, root / "repo")
        token = uuid.uuid4().hex
        summary: dict = {"run": run_id, "harness": harness.name, "mode": mode, "fixture": fixture,
                         "version": harness.version(), "transcript": str(root / "transcript.jsonl"),
                         "stderr": str(root / "stderr.log"), "repo": str(repo),
                         # This run's original temporary root: everything below is moved into the
                         # evidence directory byte-for-byte, so any absolute path the model itself
                         # recorded under this root (a contract's repository.root, for one) is still
                         # this value, not where the evidence ends up. --reevaluate materialises a
                         # copy back at exactly this path before grading; see reevaluate().
                         "root": str(root)}
        checkout_before = checkout_state()
        pristine_checkout(root, execution=mode == "auto")
        prompt = PROMPTS[mode].format(invocation=EXPLICIT[harness.name])
        prepared = harness.prepare(root, repo, prompt, token)
        check_allowlist(harness.name, prepared.argv, prepared.environment, harness.model)
        summary.update(model=prepared.model, model_id=harness.model.id, argv=prepared.argv[:-1] + ["<prompt>"],
                       prompt=prompt, onboarding=prepared.onboarding)
        stub_log = root / "harness-stub-specstride.jsonl"
        exec_stub_log = root / "harness-exec-stub-specstride.jsonl"
        environment = {**prepared.environment, "MOL_STUB_LOG": str(stub_log)}
        if mode == "auto":
            environment.update(EXECUTION_STUB_ENV, MOL_EXEC_STUB_LOG=str(exec_stub_log))
        timed_out, exit_code = False, None
        started = time.monotonic()
        reaped: list = []
        left: list = []
        try:
            with (root / harness.stdout_name).open("w", encoding="utf-8") as out, \
                    (root / "stderr.log").open("w", encoding="utf-8") as err:
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
            try:
                harness.collect(root, environment)
            except (OSError, subprocess.SubprocessError) as exc:
                (root / "collect-error.log").write_text(str(exc), encoding="utf-8")
            shutil.rmtree(tmp, ignore_errors=True)
        summary["duration_seconds"] = round(time.monotonic() - started, 1)
        summary.update(exit_code=exit_code, timed_out=timed_out, timeout_seconds=timeout, reaped=reaped, left_over=left)

        transcript_path = root / "transcript.jsonl"
        if not transcript_path.exists():
            transcript_path.write_text("", encoding="utf-8")
        transcript = mol_e2e.PARSERS[harness.name](transcript_path.read_text(encoding="utf-8").splitlines())
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
        # A sibling's temporary root is normally already gone by the time this run
        # starts -- runs are sequential -- but it is still named here so a model that
        # goes looking for one (by guessing a path, not by finding a live directory)
        # is caught the same way a read of the evidence directory itself is.
        other_roots = [str(other) for other in run_roots if other != root]
        context = mol_e2e.RunContext(harness=harness.name, mode=mode, fixture=fixture, repo=repo,
                                     transcript=transcript, timed_out=timed_out, exit_code=exit_code,
                                     home_changes=home_changes, checkout_changed=checkout_changed,
                                     harness_stub_calls=stub_calls,
                                     execution_stub_calls=exec_stub_calls,
                                     expect_execution=mode == "auto", work=root, root=root,
                                     grader_paths=[str(ROOT / "tests"), str(ROOT / "bin"), str(ROOT / "skills"),
                                                   "mol_e2e", "expectations/greeting", "reference_contract",
                                                   "summary.json", "report.json", "home-snapshot", str(evidence),
                                                   *[str(evidence / other) for other in os.listdir(evidence)
                                                     if other != run_id],
                                                   *other_roots])
        verdicts = mol_e2e.evaluate(context)
        verdicts.append(mol_e2e.verdict("no-processes-left", not left, f"reaped {len(reaped)}; left {left}"))
        if transcript.models:
            # The harness recorded which provider/model answered: it must be the campaign's one model.
            verdicts.append(mol_e2e.verdict("model-pinned", set(transcript.models) == {harness.model.id},
                                            f"answered by {sorted(transcript.models)}; configured {harness.model.id}"))
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
        redaction = redact(root, prepared.secrets)
        summary["redaction"] = redaction
        if redaction["remaining"]:
            summary["verdicts"].append(mol_e2e.verdict("evidence-redacted", False, str(redaction)))
            summary["status"] = "fail"
        if summary["status"] == "fail":
            infra = infra_reason(transcript, (root / "stderr.log").read_text(encoding="utf-8", errors="replace"),
                                 (root / harness.stdout_name).read_text(encoding="utf-8", errors="replace")
                                 if harness.stdout_name != "transcript.jsonl" else "")
            if infra:
                summary["status"], summary["infra_reason"] = "infra", infra
        (root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    finally:
        # The process group is gone well before this point (the inner finally above
        # already reaped it); only now does anything from this run reach the
        # evidence directory, moved byte-for-byte -- see archive()'s docstring for why
        # nothing here is rewritten.
        if root.is_dir():
            archive(root, dest, keep=keep)
    return summary


def infra_reason(transcript: mol_e2e.Transcript, stderr: str, stdout: str = "") -> str | None:
    """Why a failed run is the serving stack's, not the skill's or the harness's; None if it is not.

    A run whose model never produced a contract because its requests died upstream tells
    nothing about the skill. Evidence: the harness's own terminal error, a tool result or
    stderr line matching INFRA_PATTERNS, or a run that ended without a single tool call
    while stderr shows a transport error. A run that made tool calls and then failed a
    verdict is a real failure even if some request was retried along the way.
    """
    error = transcript.error_message or ""
    if INFRA_PATTERNS.search(error):
        return f"harness terminal error: {error[:200]}"
    haystack = "\n".join([stderr[-20000:], stdout[-5000:]])
    match = INFRA_PATTERNS.search(haystack)
    if match and not transcript.calls:
        line = next((ln for ln in haystack.splitlines() if match.group(0) in ln), match.group(0))
        return f"no tool call was made and the route failed: {line.strip()[:200]}"
    upstream = [c for c in transcript.calls if c.is_error and INFRA_PATTERNS.search(c.result or "")]
    if upstream and not any(c.is_error is False for c in transcript.calls if c.kind == "shell"):
        return f"every shell call failed upstream: {upstream[0].result[:200]!r}"
    return None


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

COLORS = {"pass": "\033[32m", "fail": "\033[31m", "skip": "\033[33m", "infra": "\033[35m"}


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
        elif run["status"] == "infra":
            detail = "infra: " + str(run.get("infra_reason") or run.get("reason", ""))
        else:
            failed = [v["name"] for v in run.get("verdicts", []) if v["status"] == "fail"]
            detail = ("failed: " + ", ".join(failed)) if failed else f"{sum(v['status'] == 'pass' for v in run['verdicts'])} assertions passed"
        attempt = f" (attempt {run['attempt']})" if run.get("attempt", 1) > 1 else ""
        if run.get("timed_out"):
            attempt += f" (hit the {run.get('timeout_seconds')} s budget)"
        print(f"  {run['harness']:<7} {run['mode']:<9} {run['fixture']:<17} {paint(run['status'], color):<6} "
              f"{run.get('duration_seconds', ''):>6}  {detail}{attempt}")
    superseded = report.get("superseded_runs", [])
    if superseded:
        print(f"  ({len(superseded)} infra attempt(s) retried; kept in report.json under superseded_runs)")
    for row in report.get("cross_harness", []):
        print(f"  cross-harness {row['fixture']:<17} {paint(row['status'], color):<6} runs={len(row['runs'])} "
              f"differing={row['differing'] or 'none'}")


def reevaluate(evidence: Path) -> int:
    """Recompute verdicts from a saved transcript and repository. Facts that only the live
    run could observe (timeout, exit status, reaped processes, home and checkout snapshots,
    stub calls) are carried over from the original summary, never recomputed.

    Grading always happens at the path the model worked in. validate_contract.py hashes
    sources under the contract's own recorded repository.root, so an in-memory path
    translation cannot reach it: when summary.json names a "root" other than the run's
    evidence directory, a copy of the evidence is materialised there first (never the
    other way around -- the evidence itself is never touched) and removed again once
    this run's verdicts are in. Old evidence, with no "root", already was its own root."""
    carried = ("real-homes-unchanged", "checkout-unchanged", "no-processes-left", "evidence-redacted",
               "model-pinned")
    runs = []
    for summary_path in sorted(evidence.glob("*/summary.json")):
        old = json.loads(summary_path.read_text(encoding="utf-8"))
        run = summary_path.parent
        root = Path(old["root"]) if old.get("root") else run
        # Where the model worked, for stayed-in-scope: the root itself, or for old evidence
        # the run directory as it was then (its transcript names that path, even in a copy).
        worked = root if old.get("root") else Path(old["repo"]).parent if old.get("repo") else run
        materialized = False
        if root != run:
            if root.exists():
                marker = root / "summary.json"
                same_run = marker.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("run") == old.get("run")
                if not same_run:
                    reason = f"{root} already exists and is not this run's own materialised copy"
                    runs.append({**old, "status": "skip", "reason": reason, "reevaluated": True})
                    print(f"{old.get('run', run.name):<34} skip: {reason}")
                    continue
                # left there deliberately (e.g. --keep-run-roots on the original live run):
                # grade it in place, but it is not ours to remove afterwards.
            else:
                shutil.copytree(run, root, symlinks=True)
                materialized = True
        try:
            transcript = mol_e2e.PARSERS[old["harness"]]((root / "transcript.jsonl").read_text(encoding="utf-8").splitlines())
            stub_log = root / "harness-stub-specstride.jsonl"
            stub_calls = [json.loads(line) for line in stub_log.read_text(encoding="utf-8").splitlines()] \
                if stub_log.exists() else []
            exec_log = root / "harness-exec-stub-specstride.jsonl"
            exec_calls = [json.loads(line) for line in exec_log.read_text(encoding="utf-8").splitlines()] \
                if exec_log.exists() else []
            context = mol_e2e.RunContext(harness=old["harness"], mode=old["mode"], fixture=old["fixture"],
                                         repo=root / "repo", transcript=transcript, timed_out=old["timed_out"],
                                         exit_code=old["exit_code"], harness_stub_calls=stub_calls,
                                         execution_stub_calls=exec_calls, expect_execution=old["mode"] == "auto",
                                         work=root, root=worked,
                                         grader_paths=[str(ROOT / "tests"), str(ROOT / "bin"), str(ROOT / "skills"),
                                                       "mol_e2e", "expectations/greeting", "reference_contract",
                                                       "summary.json", "report.json", "home-snapshot",
                                                       # before #26 a run worked inside the evidence
                                                       # directory, so every call named it
                                                       *([] if evidence.resolve() in worked.resolve().parents
                                                         else [str(evidence)]),
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
        finally:
            if materialized:
                shutil.rmtree(root, ignore_errors=True)
    return 1 if any(r["status"] == "fail" for r in runs) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", default="all", help="pi|prime|codex|claude|dsh|all, or a comma list")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=MODEL_CHOICES,
                        help=f"the one model every harness is bound to (default {DEFAULT_MODEL}); "
                             "`gpt-5` is Compass PROD through LiteLLM (not Claude Code); `compass` is "
                             "claude-opus-4.8 through the local shim, as plain `bebop`")
    parser.add_argument("--mode", default="both",
                        choices=("explicit", "implicit", "auto", "both", "all"),
                        help="both: explicit and implicit generation; all: those plus auto")
    parser.add_argument("--fixture", default="all", choices=("blocked", "ready", "all"))
    parser.add_argument("--timeout", type=int, default=None,
                        help="wall-clock seconds per harness run, overriding the per-harness and "
                             f"per-model budget (flat defaults {DEFAULT_TIMEOUT}, or {AUTO_TIMEOUT} "
                             "in auto mode; Claude Code on a local model gets more)")
    parser.add_argument("--retry-infra", type=int, default=1, metavar="N",
                        help="re-run a cell that ended as `infra` up to N more times once its route probes "
                             "clean again (default 1; 0 disables)")
    parser.add_argument("--allow-swap", action="store_true",
                        help="run even if llama-swap has a different model loaded (forces a swap)")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip the one-completion route probe before each harness (not recommended)")
    parser.add_argument("--evidence-dir", help="default: a new temporary directory (never inside the repository)")
    parser.add_argument("--skip-tiers", action="store_true", help="do not run Tiers 1 and 2 first")
    parser.add_argument("--keep-run-roots", action="store_true",
                        help="copy (instead of move) each run's temporary root into the evidence "
                             "directory and leave the root in place afterwards, for debugging")
    parser.add_argument("--reevaluate", metavar="EVIDENCE_DIR",
                        help="re-score saved runs with the current assertions (no model call); "
                             "writes summary-reevaluated.json next to each summary.json")
    args = parser.parse_args(argv)
    if args.reevaluate:
        return reevaluate(Path(args.reevaluate))

    model = Model(args.model)
    harnesses = make_harnesses(model, args.allow_swap)
    names = HARNESS_NAMES if args.harness == "all" else tuple(args.harness.split(","))
    unknown = [n for n in names if n not in harnesses]
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
                    "model": {"choice": model.choice, "id": model.id, "local": model.local, "frontier": model.frontier},
                    "tiers": [], "runs": [], "probes": [], "superseded_runs": []}
    if not args.skip_tiers:
        report["tiers"] = [run_tier("tier1", evidence), run_tier("tier2", evidence)]
    live = os.environ.get("MOL_LIVE_E2E") == "1"
    snapshot = home_snapshot.take() if live else None
    if snapshot is not None:
        home_snapshot.write(snapshot, evidence / "home-snapshot-before.json")
    run_roots: list[Path] = []

    def probe(harness: Harness) -> str | None:
        """The route's own answer, recorded; None when it answered."""
        if args.no_probe:
            return None
        started = time.monotonic()
        finding = harness.probe()
        report["probes"].append({"harness": harness.name, "model": model.id, "ok": finding is None,
                                 "detail": finding, "seconds": round(time.monotonic() - started, 1)})
        print(f"probe {harness.name} -> {model.id}: {'answered' if finding is None else finding}",
              file=sys.stderr, flush=True)
        return finding

    try:
        for name in names:
            harness = harnesses[name]
            reason = None if live else "MOL_LIVE_E2E=1 is not set (live runs are opt-in)"
            reason = reason or harness.skip_reason()
            infra = probe(harness) if live and not reason else None
            budgets = {mode: run_budget(name, model, mode, args.timeout) for mode in modes}
            for mode, (seconds, source) in budgets.items():
                print(f"budget {name} {mode} -> {model.id}: {seconds}s ({source})", file=sys.stderr, flush=True)
            for fixture in fixtures:
                for mode in modes:
                    base = {"run": f"{name}-{mode}-{fixture}", "harness": name, "mode": mode, "fixture": fixture,
                            "version": harness.version(), "model_id": model.id}
                    if reason:
                        report["runs"].append({**base, "status": "skip", "reason": reason})
                        continue
                    timeout, budget_source = budgets[mode]
                    attempt = 0
                    while True:
                        attempt += 1
                        if infra:
                            result = {**base, "status": "infra", "infra_reason": f"route probe: {infra}",
                                      "attempt": attempt}
                        else:
                            print(f"running {base['run']} (budget {timeout}s: {budget_source}; "
                                  f"attempt {attempt}) ...",
                                  file=sys.stderr, flush=True)
                            try:
                                result = {**run_one(harness, mode, fixture, evidence, timeout, snapshot,
                                                     run_roots, keep=args.keep_run_roots),
                                          "attempt": attempt, "budget": {"seconds": timeout, "source": budget_source}}
                            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                                result = {**base, "status": "fail", "reason": f"runner error: {exc}",
                                          "verdicts": [mol_e2e.verdict("runner", False, str(exc))], "attempt": attempt}
                        if result["status"] != "infra" or attempt > args.retry_infra:
                            break
                        # The stack failed, not the skill: give it a moment, re-probe, and try the cell again.
                        report["superseded_runs"].append(result)
                        if result.get("run") and (evidence / result["run"]).exists():
                            (evidence / result["run"]).rename(evidence / f"{result['run']}-attempt{attempt}")
                        time.sleep(30)
                        infra = probe(harness)
                    report["runs"].append(result)
    except KeyboardInterrupt:
        report["interrupted"] = True
    report["cross_harness"] = cross_harness(report["runs"])
    if snapshot is not None:
        report["home_changes_final"] = home_snapshot.diff(snapshot, home_snapshot.take())
    (evidence / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print_report(report, sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    failed = (any(t["status"] == "fail" for t in report["tiers"])
              or any(r["status"] in ("fail", "infra") for r in report["runs"])
              or any(r["status"] == "fail" for r in report["cross_harness"]) or report.get("interrupted")
              or bool(report.get("home_changes_final")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
