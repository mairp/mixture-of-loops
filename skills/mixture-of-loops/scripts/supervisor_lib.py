#!/usr/bin/env python3
"""Harness-side execution policy for mixture-of-loops.

The generated launcher stays the control layer around Specstride. This module is what
the *harness* is allowed to do around that launcher: choose a mode, refuse or clear a
launch gate, start the launcher detached, read the run's own telemetry, say what it
found, and relaunch only a classified transient failure within a declared budget.

Nothing here decides that a gate passed, produces evidence, or edits the run's state.
Every rule is plain data plus filesystem reads, so it is testable without a harness, a
model, or a real Specstride.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Callable, Iterable

from contract_lib import (
    ContractError,
    StaleSourceError,
    artifact_root,
    auto_budget,
    load_contract,
    resolve_path,
    validate_contract,
)
from render_launcher import existing_launcher_is_intact
from runtime import atomic_json, latest_reason, semantic_contract_digest


# ── launcher exit codes, mirrored from runtime.py ─────────────────────────────

E_OK = 0
E_INVALID = 20
E_PREFLIGHT = 21
E_STAGE = 22
E_STALE = 23
E_LOCK = 24
LAUNCHER_EXIT_REASONS = {
    E_INVALID: "invalid-contract",
    E_PREFLIGHT: "preflight-blocked",
    E_STAGE: "stage-failed",
    E_STALE: "stale-sources",
    E_LOCK: "lock-conflict",
}

# Files the supervisor owns, beside the run state the runtime owns.
HARNESS_RUN = "harness-run.json"
HARNESS_LAUNCH_LOG = "harness-launch.log"
HARNESS_REPORT_LOG = "harness-report.log"
LAUNCHER_LOG = "launcher.log"
STATE_JSON = "state.json"
LOCK = "lock"

# Reporting cadence: matched to a pipeline, not to impatience.
BASE_POLL_SECONDS = 60
MAX_POLL_SECONDS = 600
POLL_BACKOFF = 1.5

# What run_stage records in state.json when a stage ends. Confirmed against
# runtime.py:412-466: an exhausted in-stage recovery records run_action's outcome
# ("exit", "timeout", "recovery-deadline", "exec-error:<e>"), never the unreachable
# "recovery-exhausted" literal. Both spellings are accepted here so the classifier
# keeps working if that dead branch is ever revived.
TRANSIENT_STAGE_REASONS = frozenset({"exit", "timeout", "recovery-deadline", "recovery-exhausted"})
DELIBERATE_STAGE_REASONS = {
    "postcondition": "postcondition-failure",
    "stage-precondition": "stage-precondition",
    "setup-required": "setup-required",
    "interrupted": "interrupted",
    "existing-prerequisite": "stage-did-not-run",
    "completed": "stage-did-not-fail",
}

# Terminal supervision outcomes, and the process exit they map to.
EXIT_STATUS = {"completed": 0, "stopped": 2, "unknown-terminal": 3, "refused": 4,
               "supervision-expired": 5, "failed": E_STAGE, "lock-conflict": E_LOCK}

GENERATED_MARKER = "# mixture-of-loops-generated:"
BUNDLE_DIR_RE = re.compile(r'^BUNDLE_DIR="\$SCRIPT_DIR/(?P<relative>.*)"$', re.MULTILINE)
LABEL_RE = re.compile(r"^\[([A-Z][A-Z-]*)\] (.*)$")
DIGEST_RE = re.compile(
    r"^\[DIGEST\] state=(?P<state>\S+) exit=(?P<exit>-?\d+) last-stage=(?P<stage>\S+)"
    r"(?: child-exit=(?P<child>\S+))? evidence=(?P<evidence>.*)$"
)


class SupervisorError(Exception):
    """A supervision step cannot proceed. Carries the named refusal reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


# ── 1. mode selection ─────────────────────────────────────────────────────────

MODES = ("generate", "run", "auto")
TOKEN_MODES = {"--auto": "auto", "--run": "run", "--generate": "generate"}
FLAG_TOKENS = {"--implement": "implement", "--smoke": "smoke"}

# Silence is not consent to execute, so only these say "execute". A refusal beats an
# invitation, and an explicit token beats all prose.
_REFUSALS = (
    r"don'?t\s+(?:run|execute|launch|start)",
    r"do\s+not\s+(?:run|execute|launch|start)",
    r"without\s+(?:running|executing|launching|starting)",
    r"no\s+execution\b",
    r"\bdon'?t\s+auto\b",
    r"(?:just|only)\s+(?:derive|generate|render|create|build|write)",
    r"(?:derive|generate|render)\s+(?:it\s+)?only\b",
    r"generation\s+only\b",
    r"\bdry[- ]run\s+only\b",
    r"\bno\s+auto\b",
)
_AUTO = (
    r"\bauto\b",
    r"\bauto[- ]mode\b",
    r"(?:derive|generate|render|create|build|write)[^.]{0,40}?\band\s+(?:then\s+)?run\b",
    r"(?:derive|generate|render|create|build|write)[^.]{0,40}?\band\s+(?:then\s+)?execute\b",
    r"(?:derive|generate|render|create|build)[^.]{0,40}?\bthen\s+(?:run|launch|execute)\b",
)
_RUN = (
    r"\brun\s+(?:it|this|that|them)\b",
    r"\brun\s+the\s+(?:pipeline|launcher|run\s+script|script|loop|stages?)\b",
    r"\b(?:execute|launch|start|kick\s+off)\s+(?:it|this|that|the\s+\w+)",
    r"\bresume\s+the\s+(?:run|pipeline)\b",
    r"\bfor\s+real\b",
    r"\bthen\s+run\b",
    r"\band\s+run\s+it\b",
)
_GENERATE = (
    r"\b(?:derive|generate|regenerate|render|create|build|write)\b",
    r"\blaunch\s+contract\b",
    r"\bpipeline\s+for\b",
)
_DRY_RUN = re.compile(r"\bdry[- ]run\b")


@dataclass
class ModeDecision:
    mode: str
    source: str          # token | refusal | prose | default
    evidence: str        # the token or phrase that decided it
    implement: bool = False
    smoke: bool = False

    def message(self) -> str:
        flags = " ".join(f"--{name}" for name, on in
                         (("implement", self.implement), ("smoke", self.smoke)) if on)
        return (f"[MOL-MODE] mode={self.mode} source={self.source} "
                f"evidence={json.dumps(self.evidence)} flags={json.dumps(flags)}")


def _matched(text: str, patterns: Iterable[str]) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(0).strip()
    return None


def select_mode(request: str, tokens: Iterable[str] | None = None) -> ModeDecision:
    """Read the execution mode from the request. Generation is the default everywhere."""
    words = list(tokens) if tokens is not None else []
    words += re.findall(r"(?<!\S)--[a-z-]+", request or "")
    text = (request or "").lower()
    # `--dry-run` is the generation gate, never an execution mode: it can never select
    # run or auto, and it never travels with one.
    implement = "--implement" in words or bool(re.search(r"\bwith\s+--?implement\b|\bimplement\s+setup\b", text))
    smoke = "--smoke" in words or bool(re.search(r"\bwith\s+--?smoke\b|\bsmoke\s+stages?\b", text))

    for token in words:
        if token in TOKEN_MODES:
            return ModeDecision(TOKEN_MODES[token], "token", token, implement, smoke)

    refusal = _matched(text, _REFUSALS)
    if refusal:
        return ModeDecision("generate", "refusal", refusal, implement, smoke)
    if _DRY_RUN.search(text) and not _matched(text, _AUTO):
        return ModeDecision("generate", "refusal", "dry-run", implement, smoke)

    auto = _matched(text, _AUTO)
    if auto:
        return ModeDecision("auto", "prose", auto, implement, smoke)
    execute = _matched(text, _RUN)
    if execute:
        generate = _matched(text, _GENERATE)
        if generate:
            return ModeDecision("auto", "prose", f"{generate} … {execute}", implement, smoke)
        return ModeDecision("run", "prose", execute, implement, smoke)
    return ModeDecision("generate", "default", "", implement, smoke)


# ── 2. launcher resolution ────────────────────────────────────────────────────

@dataclass
class LauncherResolution:
    launcher: Path | None
    source: str                       # request | feature-default | generated-marker
    candidates: list[Path] = field(default_factory=list)
    reason: str | None = None         # named refusal when launcher is None
    detail: str = ""

    def message(self) -> str:
        if self.launcher is not None:
            return f"[MOL-LAUNCHER] launcher={self.launcher} source={self.source}"
        return (f"[MOL-REFUSE] action=resolve reason={self.reason} "
                f"detail={json.dumps(self.detail)} "
                f'next="ask for `auto` to generate the launcher first"')


def is_generated_launcher(path: Path) -> bool:
    try:
        return GENERATED_MARKER in path.read_bytes()[:400].decode("utf-8", "replace")
    except OSError:
        return False


def generated_launchers(repo: Path) -> list[Path]:
    """Every generated launcher in the repository, wherever the operator put it.

    A rendered launcher records its own output path by existing there, so this is how an
    operator-chosen path survives from generation time without a second index to drift.
    Renderer bundles and run state under .mixture-of-loops/ are copies, not launchers.
    """
    found = []
    for path in Path(repo).rglob("*"):
        parts = path.relative_to(repo).parts
        if ".git" in parts or not path.is_file() or path.is_symlink():
            continue
        if len(parts) > 1 and parts[0] == ".mixture-of-loops" and parts[1] in ("generated", "runs"):
            continue
        if is_generated_launcher(path):
            found.append(path)
    return sorted(found)


def resolve_launcher(repo: Path, *, requested: str | None = None,
                     feature: str | None = None) -> LauncherResolution:
    """Find the launcher a `run` was asked for. Never generate one silently."""
    repo = Path(repo).resolve()
    if requested:
        path = Path(requested)
        path = path if path.is_absolute() else repo / path
        if not path.is_file():
            return LauncherResolution(None, "request", reason="launcher-not-found",
                                      detail=f"the requested launcher does not exist: {path}")
        return LauncherResolution(path.resolve(), "request")
    if feature:
        slug = Path(feature).name
        default = repo / ".mixture-of-loops" / slug / f"run-{slug}.sh"
        if default.is_file():
            return LauncherResolution(default.resolve(), "feature-default")
    candidates = generated_launchers(repo)
    if len(candidates) == 1:
        return LauncherResolution(candidates[0].resolve(), "generated-marker")
    if not candidates:
        return LauncherResolution(None, "generated-marker", reason="launcher-missing",
                                  detail=f"no generated launcher exists under {repo}")
    return LauncherResolution(None, "generated-marker", candidates=candidates,
                              reason="launcher-ambiguous",
                              detail="several generated launchers exist: "
                                     + ", ".join(str(path) for path in candidates))


# ── 3. what a launcher points at ──────────────────────────────────────────────

@dataclass
class Bundle:
    launcher: Path
    bundle: Path
    contract_path: Path
    contract: dict
    pipeline: str
    run_dir: Path
    digest: str
    warnings: list[str] = field(default_factory=list)

    def stage_ids(self, *, smoke: bool = False) -> list[str]:
        return [stage["id"] for stage in self.contract.get("stages", [])
                if isinstance(stage, dict) and isinstance(stage.get("id"), str)
                and (smoke or stage.get("kind") != "smoke")]

    def stage(self, stage_id: str | None) -> dict | None:
        for value in self.contract.get("stages", []):
            if isinstance(value, dict) and value.get("id") == stage_id:
                return value
        return None


def bundle_of(launcher: Path) -> Path:
    """The content-addressed bundle a rendered launcher execs, read from the launcher."""
    launcher = Path(launcher)
    try:
        text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SupervisorError("launcher-unreadable", f"{launcher}: {exc}") from exc
    found = BUNDLE_DIR_RE.search(text)
    if not found:
        raise SupervisorError("launcher-not-generated",
                              f"{launcher} carries no generated bundle reference")
    return (launcher.parent / found.group("relative")).resolve()


def read_bundle(launcher: Path, *, check_sources: bool = True) -> Bundle:
    """Load the contract the launcher would actually run, exactly as the runtime does:
    load, validate (which normalizes in place), then digest."""
    launcher = Path(launcher).resolve()
    bundle = bundle_of(launcher)
    contract_path = bundle / "launch-contract.json"
    contract = load_contract(contract_path)
    warnings = validate_contract(contract, allow_draft=False, check_sources=check_sources)
    identifier = contract["id"]
    return Bundle(launcher=launcher, bundle=bundle, contract_path=contract_path,
                  contract=contract, pipeline=identifier,
                  run_dir=artifact_root(launcher.parent) / "runs" / identifier,
                  digest=semantic_contract_digest(contract), warnings=warnings)


# ── 4. the launch gate ────────────────────────────────────────────────────────

@dataclass
class GateCheck:
    name: str
    status: str            # pass | fail | not-evaluated
    reason: str = ""
    detail: str = ""

    def message(self) -> str:
        suffix = f" reason={self.reason} detail={json.dumps(self.detail)}" if self.reason else ""
        return f"[MOL-GATE] check={self.name} status={self.status}{suffix}"


@dataclass
class GateReport:
    checks: list[GateCheck]
    decision: str                       # launch | refuse | attach
    reason: str = ""
    detail: str = ""
    next_action: str = ""
    bundle: Bundle | None = None

    def messages(self) -> list[str]:
        lines = [check.message() for check in self.checks]
        lines.append(f"[MOL-GATE] decision={self.decision}"
                     + (f" reason={self.reason}" if self.reason else "")
                     + (f" detail={json.dumps(self.detail)}" if self.detail else "")
                     + (f" next={json.dumps(self.next_action)}" if self.next_action else ""))
        return lines


def _inside(path: Path, roots: Iterable[str]) -> bool:
    for root in roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except (ValueError, TypeError):
            continue
    return False


def run_directory_free(run_dir: Path) -> bool:
    """Whether a launcher could take the run lock right now. Read-only: the lock is
    taken non-blocking and dropped again, and no run directory is ever removed."""
    lock = Path(run_dir) / LOCK
    if not lock.is_file():
        return True
    try:
        handle = lock.open("a+")  # noqa: SIM115 - closed in the finally below
    except OSError:
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        handle.close()


def gate(launcher: Path, *, implement: bool = False, smoke: bool = False,
         dry_run_timeout: int = 300) -> GateReport:
    """Decide whether this launcher may be started, naming every check.

    The checks run in an order that is safe to execute: the launcher's own digest is
    verified before anything runs it. The first failure is the one blocking reason; the
    checks after it are reported as not evaluated rather than guessed at.
    """
    launcher = Path(launcher).resolve()
    names = ["launcher-intact", "contract-validated", "authorized-root",
             "launcher-syntax", "launcher-dry-run", "run-directory-free"]
    checks: list[GateCheck] = []
    bundle: Bundle | None = None

    def refuse(name: str, reason: str, detail: str, action: str) -> GateReport:
        checks.append(GateCheck(name, "fail", reason, detail))
        for later in names[names.index(name) + 1:]:
            checks.append(GateCheck(later, "not-evaluated"))
        return GateReport(checks, "refuse", reason, detail, action, bundle)

    if not launcher.is_file():
        return refuse("launcher-intact", "launcher-not-found",
                      f"{launcher} does not exist",
                      "generate the launcher first, or ask for `auto`")
    if not existing_launcher_is_intact(launcher):
        return refuse("launcher-intact", "launcher-edited",
                      f"{launcher} is edited or was not generated by this skill; its "
                      "generated-content-sha256 no longer matches",
                      "re-render it from the contract; --replace-edited is the operator's "
                      "decision to make, never this skill's")
    checks.append(GateCheck("launcher-intact", "pass"))

    try:
        bundle = read_bundle(launcher)
    except StaleSourceError as exc:
        return refuse("contract-validated", "stale-sources", str(exc),
                      "the sources moved under the launcher; regenerate with `auto`")
    except ContractError as exc:
        text = str(exc)
        reason = ("open-blocker" if "open blocker" in text else
                  "draft-contract" if "status must be validated" in text else "invalid-contract")
        action = ("resolve the blocking finding in the contract and re-render"
                  if reason == "open-blocker" else
                  "complete the derivation until the contract validates, then re-render")
        return refuse("contract-validated", reason, text, action)
    except SupervisorError as exc:
        return refuse("contract-validated", exc.reason, exc.detail, "re-render the launcher")
    checks.append(GateCheck("contract-validated", "pass",
                            detail=f"validated contract {bundle.pipeline}"))

    root = Path(bundle.contract["repository"]["root"]).resolve()
    if not _inside(root, bundle.contract.get("authorized_roots", [])):
        return refuse("authorized-root", "unauthorized-root",
                      f"repository root {root} is outside authorized_roots "
                      f"{bundle.contract.get('authorized_roots')}",
                      "widen authorized_roots in the contract, or run from the authorized root")
    checks.append(GateCheck("authorized-root", "pass", detail=str(root)))

    syntax = subprocess.run(["bash", "-n", str(launcher)], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, check=False)
    if syntax.returncode != 0:
        return refuse("launcher-syntax", "launcher-syntax-error",
                      syntax.stderr.strip()[:400], "re-render the launcher")
    checks.append(GateCheck("launcher-syntax", "pass"))

    environment = {**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    argv = [str(launcher), "--dry-run", "--no-color"]
    argv += ["--implement"] if implement else []
    argv += ["--smoke"] if smoke else []
    try:
        planned = subprocess.run(argv, cwd=launcher.parent, env=environment,
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=dry_run_timeout, start_new_session=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return refuse("launcher-dry-run", "dry-run-failed", str(exc), "re-render the launcher")
    if planned.returncode != 0:
        return refuse("launcher-dry-run", "dry-run-failed",
                      f"exit {planned.returncode}: "
                      f"{(planned.stdout + planned.stderr).strip()[-400:]}",
                      "fix what the dry run reports, then re-render")
    checks.append(GateCheck("launcher-dry-run", "pass", detail="exit 0"))

    if not run_directory_free(bundle.run_dir):
        checks.append(GateCheck("run-directory-free", "fail", "already-running",
                                f"a launcher already owns {bundle.run_dir}"))
        return GateReport(checks, "attach", "already-running",
                          f"a launcher already owns {bundle.run_dir}",
                          "attach and report that run instead of starting a second one",
                          bundle)
    checks.append(GateCheck("run-directory-free", "pass", detail=str(bundle.run_dir)))
    return GateReport(checks, "launch", bundle=bundle)


# ── 5. launching, and the supervisor's own launch record ──────────────────────

def _record_path(run_dir: Path) -> Path:
    return Path(run_dir) / HARNESS_RUN


def load_record(run_dir: Path) -> dict:
    path = _record_path(run_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SupervisorError("launch-record-unreadable", f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SupervisorError("launch-record-unreadable", f"{path}: not a JSON object")
    return value


def launch_argv(launcher: Path, *, implement: bool = False, smoke: bool = False) -> list[str]:
    """The exact argv, built as an array. No shell string, no eval, no interpolation.
    Color is forced off so both logs stay free of escape sequences."""
    argv = [str(launcher), "--no-color"]
    if implement:
        argv.append("--implement")
    if smoke:
        argv.append("--smoke")
    return argv


def launch(bundle: Bundle, *, mode: str, implement: bool = False, smoke: bool = False,
           budget: dict | None = None, now: float | None = None,
           environment: dict[str, str] | None = None) -> dict:
    """Start the launcher detached and write the supervisor's own launch record.

    Detached means: stdin closed, a new session of its own, and both streams redirected
    to a harness-owned log beside the run state. The pipeline therefore outlives this
    process and the harness turn that started it.
    """
    run_dir = bundle.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    launch_log = run_dir / HARNESS_LAUNCH_LOG
    launcher_log = run_dir / LAUNCHER_LOG
    offset = launcher_log.stat().st_size if launcher_log.is_file() else 0
    argv = launch_argv(bundle.launcher, implement=implement, smoke=smoke)
    started = time.time() if now is None else now
    child_env = {**(os.environ if environment is None else environment),
                 "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    with launch_log.open("ab") as handle:
        handle.write(f"\n=== launch {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                     f"{' '.join(argv)} ===\n".encode())
        handle.flush()
        child = subprocess.Popen(argv, cwd=bundle.launcher.parent, env=child_env,
                                 stdin=subprocess.DEVNULL, stdout=handle,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    _CHILDREN[child.pid] = child
    try:
        group = os.getpgid(child.pid)
    except (OSError, ProcessLookupError):
        group = child.pid
    limits = budget or auto_budget(bundle.contract)
    record = {
        "schema": "mixture-of-loops/harness-run/1",
        "pipeline": bundle.pipeline,
        "mode": mode,
        "launcher": str(bundle.launcher),
        "argv": argv,
        "cwd": str(bundle.launcher.parent),
        "contract": str(bundle.contract_path),
        "bundle": str(bundle.bundle),
        "cmdline_markers": [str(bundle.launcher), bundle.bundle.name],
        "contract_digest": bundle.digest,
        "flags": {"implement": implement, "smoke": smoke},
        "pid": child.pid,
        "pgid": group,
        "launched_at": started,
        "first_launched_at": started,
        "launcher_log_offset": offset,
        "run_dir": str(run_dir),
        "state_path": str(run_dir / STATE_JSON),
        "launcher_log": str(launcher_log),
        "launch_log": str(launch_log),
        "report_log": str(run_dir / HARNESS_REPORT_LOG),
        "stage_count": len(bundle.stage_ids(smoke=smoke)),
        "relaunch_budget": {
            "max_relaunches": limits["max_relaunches"],
            "remaining": limits["max_relaunches"],
            "wall_clock_seconds": limits["wall_clock_seconds"],
            "deadline": started + limits["wall_clock_seconds"],
            "declared_pipeline_seconds": limits["declared_pipeline_seconds"],
            "source": limits["source"],
        },
        "relaunches": [],
        "supervision": {},
    }
    atomic_json(_record_path(run_dir), record)
    return record


def relaunch(record: dict, *, justification: dict, now: float | None = None) -> dict:
    """Re-invoke the same launcher within budget and record why. Resume is re-invocation:
    the runtime reconciles against its own state.json and re-runs only what is not done."""
    run_dir = Path(record["run_dir"])
    launcher_log = run_dir / LAUNCHER_LOG
    offset = launcher_log.stat().st_size if launcher_log.is_file() else 0
    started = time.time() if now is None else now
    with Path(record["launch_log"]).open("ab") as handle:
        handle.write(f"\n=== relaunch {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                     f"{' '.join(record['argv'])} ===\n".encode())
        handle.flush()
        child = subprocess.Popen(record["argv"], cwd=record["cwd"],
                                 env={**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                                 stdin=subprocess.DEVNULL, stdout=handle,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    _forget_child(record.get("pid"))
    _CHILDREN[child.pid] = child
    try:
        group = os.getpgid(child.pid)
    except (OSError, ProcessLookupError):
        group = child.pid
    budget = record["relaunch_budget"]
    budget["remaining"] = max(0, budget["remaining"] - 1)
    record["relaunches"].append({**justification, "at": started, "pid": child.pid,
                                 "remaining": budget["remaining"]})
    record.update(pid=child.pid, pgid=group, launched_at=started, launcher_log_offset=offset)
    record["supervision"] = {}
    atomic_json(_record_path(run_dir), record)
    return record


def request_stop(record: dict, *, signal_number: int = signal.SIGTERM,
                 now: float | None = None) -> tuple[bool, str]:
    """Send the launcher's process group the stop the operator asked for.

    The runtime turns it into state=stopped and exit 128+N. A stop asked for here is
    recorded, so the run is afterwards reported as intentional and is never relaunched.
    """
    record["stop_requested"] = {"signal": int(signal_number),
                                "at": time.time() if now is None else now}
    atomic_json(_record_path(Path(record["run_dir"])), record)
    try:
        os.killpg(record["pgid"], signal_number)
    except ProcessLookupError:
        return False, "no process group to signal; the run had already ended"
    except OSError as exc:
        return False, f"cannot signal process group {record['pgid']}: {exc}"
    return True, f"sent SIG{signal.Signals(signal_number).name[3:]} to process group {record['pgid']}"


# ── 6. reading the run's own telemetry ────────────────────────────────────────

# Launchers started by this process, so a supervision loop in the same process reads
# their real status instead of a zombie that the signal probe still answers for.
_CHILDREN: dict[int, subprocess.Popen] = {}


def _forget_child(pid: object) -> None:
    """Drop a finished launcher from the registry, reaping it so no zombie is left."""
    child = _CHILDREN.pop(pid, None) if isinstance(pid, int) else None
    if child is None:
        return
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - only a wedged launcher
        child.kill()
        child.wait()


def process_alive(pid: object, markers: Iterable[str] | str | None = None) -> bool:
    """Whether the recorded launcher process is still running.

    A launcher this process started is authoritative through its own child handle, which
    also reaps it. Otherwise the signal probe answers, narrowed where /proc exists by
    requiring one of the recorded command-line markers to still be there, so a reused PID
    is not mistaken for the run. On a system without /proc a reused PID would read as
    alive; that is why liveness is only ever used to refuse a completion claim, never to
    make one.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    child = _CHILDREN.get(pid)
    if child is not None:
        return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if isinstance(markers, str):
        markers = [markers]
    markers = [value for value in (markers or []) if value]
    cmdline = Path(f"/proc/{pid}/cmdline")
    if markers and cmdline.is_file():
        try:
            text = cmdline.read_bytes().decode("utf-8", "replace")
        except OSError:
            return True
        # An exited-but-unreaped process has an empty command line: that is not running.
        return any(marker in text for marker in markers)
    return True


def read_labels(path: Path, offset: int = 0) -> list[tuple[str, str]]:
    """The labelled lines this run appended to the launcher log, from its own offset.
    The log is append-only and shared with earlier runs of the same pipeline, so only
    what was written past the offset recorded at launch belongs to this run."""
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset if size >= offset else 0)
            rows = handle.readlines()
    except OSError:
        return []
    found = []
    for row in rows:
        match = LABEL_RE.match(row.rstrip("\n"))
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def parse_digest(line: str) -> dict | None:
    match = DIGEST_RE.match(line.strip())
    if not match:
        return None
    child = match.group("child")
    return {"state": match.group("state"), "exit": int(match.group("exit")),
            "last_stage": match.group("stage"),
            "child_exit": int(child) if child not in (None, "None") else None,
            "evidence": match.group("evidence")}


def tail(path: Path, limit: int = 2000) -> str:
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().strip()
    except OSError:
        return ""


@dataclass
class Observation:
    status: str                    # starting | running | completed | failed | stopped
    #                                | unknown-terminal | lock-conflict
    pipeline: str
    alive: bool
    run_state: str | None = None
    stage_id: str | None = None
    stage_index: int = 0
    stage_count: int = 0
    stage_elapsed: float | None = None
    total_elapsed: float = 0.0
    launcher_exit: int | None = None
    child_exit: int | None = None
    stage_reason: str | None = None
    stop_reason: str | None = None
    contract_digest: str | None = None
    superseded_digest: str | None = None
    detail: str = ""
    evidence: str | None = None
    state_path: str = ""
    launch_log_tail: str = ""
    anomalies: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status not in ("starting", "running")

    def signature(self) -> tuple:
        """What "unchanged since last time" means: position and condition, not clocks."""
        return (self.status, self.run_state, self.stage_id, self.stage_index,
                self.stage_reason, self.detail, self.launcher_exit)


def observe(record: dict, bundle: Bundle | None = None, *, now: float | None = None) -> Observation:
    """One reading of the run, in the order of authority the skill declares:
    state.json, then process liveness, then the launcher log, then Specstride's own
    run_stop reason, then the harness launch log for a launch that never got started."""
    moment = time.time() if now is None else now
    run_dir = Path(record["run_dir"])
    state_path = Path(record.get("state_path") or run_dir / STATE_JSON)
    anomalies: list[str] = []

    state: dict | None = None
    if state_path.is_file():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
            state = value if isinstance(value, dict) else None
            if state is None:
                anomalies.append(f"{state_path} is not a JSON object")
        except (OSError, ValueError) as exc:
            anomalies.append(f"{state_path} could not be read: {exc}")

    alive = process_alive(record.get("pid"),
                          record.get("cmdline_markers") or record.get("cmdline_marker"))
    labels = read_labels(Path(record["launcher_log"]), record.get("launcher_log_offset", 0))
    digest = None
    for label, message in reversed(labels):
        if label == "DIGEST":
            digest = parse_digest(f"[DIGEST] {message}")
            if digest:
                break
    launch_tail = tail(Path(record["launch_log"]))
    if digest is None:
        for line in reversed(launch_tail.splitlines()):
            digest = parse_digest(line)
            if digest:
                break

    run_state = state.get("state") if isinstance(state, dict) else None
    last_stage = state.get("last_stage") if isinstance(state, dict) else None
    if last_stage is None and digest:
        last_stage = None if digest["last_stage"] == "none" else digest["last_stage"]
    launcher_exit = digest["exit"] if digest else None

    if alive:
        status = "running" if run_state in ("ready", "running") else "starting"
    elif launcher_exit == E_LOCK:
        status = "lock-conflict"
    elif run_state in ("completed", "failed", "stopped"):
        status = run_state
    elif launcher_exit is not None:
        status = ("completed" if launcher_exit == E_OK else
                  "stopped" if launcher_exit >= 128 else "failed")
    else:
        status = "unknown-terminal"
        anomalies.append("the launcher process is gone and wrote no terminal state")

    stages = (state.get("stages") or {}) if isinstance(state, dict) else {}
    stage_record = stages.get(last_stage) if isinstance(stages, dict) else None
    stage_record = stage_record if isinstance(stage_record, dict) else {}
    child_exit = stage_record.get("child_exit_code")
    if child_exit is None and digest:
        child_exit = digest["child_exit"]
    started_at = stage_record.get("started_at")
    finished_at = stage_record.get("finished_at")
    total_elapsed = max(0.0, moment - record.get("first_launched_at", moment))
    stage_elapsed = None
    if isinstance(started_at, (int, float)):
        stage_elapsed = (finished_at if isinstance(finished_at, (int, float)) else moment) - started_at
        # state.json stores whole seconds while the total is a float, so a stage can read
        # a second longer than the run that contains it. It cannot be.
        stage_elapsed = max(0.0, min(stage_elapsed, total_elapsed))

    order = bundle.stage_ids(smoke=record.get("flags", {}).get("smoke", False)) if bundle else []
    stage_index = order.index(last_stage) + 1 if last_stage in order else 0
    stage_count = len(order) or record.get("stage_count", 0)

    stop_reason = None
    if bundle is not None:
        stop_reason = latest_stop_reason(bundle, last_stage)

    detail = ""
    for label, message in reversed(labels):
        if label in ("RECOVER", "BLOCKED", "RESUME", "ERROR", "FAIL"):
            detail = f"{label}: {message}"
            break

    return Observation(
        status=status, pipeline=record.get("pipeline", ""), alive=alive, run_state=run_state,
        stage_id=last_stage, stage_index=stage_index, stage_count=stage_count,
        stage_elapsed=stage_elapsed, total_elapsed=total_elapsed,
        launcher_exit=launcher_exit, child_exit=child_exit,
        stage_reason=stage_record.get("reason"), stop_reason=stop_reason,
        contract_digest=state.get("contract_digest") if isinstance(state, dict) else None,
        superseded_digest=state.get("superseded_contract_digest") if isinstance(state, dict) else None,
        detail=detail, evidence=digest["evidence"] if digest else None,
        state_path=str(state_path),
        launch_log_tail=(launch_tail if state is None
                         or status in ("unknown-terminal", "lock-conflict") else ""),
        anomalies=anomalies)


def latest_stop_reason(bundle: Bundle, stage_id: str | None) -> str | None:
    """The newest structured run_stop reason for a stage, resolved the way the launcher's
    own retry classification resolves it, from the same JSONL under the stage's workdir."""
    stage = bundle.stage(stage_id)
    if stage is None:
        return None
    specification = (stage.get("recovery") or {}).get("reason")
    if not isinstance(specification, dict) or not isinstance(specification.get("jsonl"), str):
        return None
    root = Path(bundle.contract["repository"]["root"]).resolve()
    cwd = resolve_path(stage.get("cwd", "."), root)
    try:
        return latest_reason(specification, cwd, 0)
    except OSError:
        return None


# ── 7. relaunch policy ────────────────────────────────────────────────────────

@dataclass
class RelaunchDecision:
    relaunch: bool
    reason: str
    detail: str = ""

    def justification(self, observation: Observation) -> dict:
        return {"reason": self.reason, "detail": self.detail,
                "launcher_exit": observation.launcher_exit,
                "child_exit": observation.child_exit,
                "stage": observation.stage_id,
                "stage_reason": observation.stage_reason,
                "stop_reason": observation.stop_reason}


def classify_relaunch(observation: Observation, record: dict, bundle: Bundle | None,
                      *, now: float | None = None) -> RelaunchDecision:
    """Whether this run's own records classify its stop as a transient the harness may
    retry. Every refusal carries the reason it refused for."""
    moment = time.time() if now is None else now
    no = RelaunchDecision

    if not observation.terminal:
        return no(False, "still-running", "the launcher has not reached a terminal state")
    if record.get("stop_requested"):
        return no(False, "operator-stop", "the operator asked for this stop")
    if observation.status == "completed":
        return no(False, "completed", "the pipeline completed")
    if observation.status == "stopped":
        return no(False, "deliberate-stop",
                  "a signal or an intentional child stop ended the run; it stays stopped")
    if observation.status == "unknown-terminal":
        return no(False, "unknown-terminal",
                  "the launcher process is gone without a terminal state; this is not a "
                  "classified transient and is never relaunched")
    if observation.status == "lock-conflict":
        return no(False, "lock-conflict", "another launcher owns the run directory")

    exit_code = observation.launcher_exit
    if exit_code is None:
        return no(False, "unknown-launcher-exit", "no DIGEST line recorded a launcher exit")
    if exit_code >= 128:
        return no(False, "signal-stop", f"the launcher exited on a signal ({exit_code})")
    if exit_code != E_STAGE:
        return no(False, LAUNCHER_EXIT_REASONS.get(exit_code, f"launcher-exit-{exit_code}"),
                  f"launcher exit {exit_code} is not a stage failure")
    if observation.run_state != "failed":
        return no(False, "state-not-failed",
                  f"state.json reads {observation.run_state!r}, not 'failed'")

    stage = bundle.stage(observation.stage_id) if bundle else None
    if stage is None:
        return no(False, "unknown-stage",
                  f"the contract has no stage {observation.stage_id!r}")

    reason = observation.stage_reason or ""
    if reason in DELIBERATE_STAGE_REASONS:
        return no(False, DELIBERATE_STAGE_REASONS[reason],
                  f"the stage recorded {reason!r}, which is not a transient outcome")
    if reason.startswith("exec-error"):
        return no(False, "exec-error", f"the stage could not start its action: {reason}")
    if reason not in TRANSIENT_STAGE_REASONS:
        return no(False, "stage-reason-not-transient",
                  f"the stage recorded {reason!r}, which is not a classified transient")

    codes = (stage.get("recovery") or {}).get("retry_exit_codes") or []
    if observation.child_exit not in codes:
        return no(False, "child-exit-not-retryable",
                  f"child exit {observation.child_exit} is not in the stage's "
                  f"retry_exit_codes {list(codes)}")

    specification = (stage.get("recovery") or {}).get("reason")
    if isinstance(specification, dict):
        allowed = specification.get("allowed") or []
        if observation.stop_reason not in allowed:
            return no(False, "stop-reason-not-allowed",
                      f"the newest run_stop.reason is {observation.stop_reason!r}, "
                      f"not one of {list(allowed)}")

    recorded = record.get("contract_digest")
    if observation.contract_digest and observation.contract_digest != recorded:
        return no(False, "contract-digest-changed",
                  "the run state is bound to a different contract than the one launched; "
                  "a relaunch would silently reset the stage map")
    if bundle is not None and bundle.digest != recorded:
        return no(False, "contract-digest-changed",
                  "the launcher now points at a different contract than the one launched; "
                  "a relaunch would silently reset the stage map")
    if bundle is not None and not existing_launcher_is_intact(Path(record["launcher"])):
        return no(False, "launcher-edited",
                  "the launcher changed since it was started and is no longer the "
                  "generated script that was gated")

    budget = record.get("relaunch_budget") or {}
    if budget.get("remaining", 0) <= 0:
        return no(False, "budget-exhausted",
                  f"the declared relaunch budget of {budget.get('max_relaunches')} is spent")
    deadline = budget.get("deadline")
    if isinstance(deadline, (int, float)) and moment >= deadline:
        return no(False, "budget-deadline-exceeded",
                  f"the {budget.get('wall_clock_seconds'):g}s supervision ceiling has passed")
    return no(True, "classified-transient",
              f"stage {observation.stage_id} exited {observation.child_exit} with "
              f"run_stop.reason={observation.stop_reason!r}, both declared retryable")


# ── 8. the reporting contract ─────────────────────────────────────────────────

def report_log(run_dir: Path) -> Callable[[str], None]:
    """Emit a message to stdout and append it to the run's own report log.

    A harness may move a long-running supervision command to the background, or lose the
    stream some other way. The same lines are therefore always recoverable from
    runs/<id>/harness-report.log, which this skill owns, instead of from whatever file
    that particular harness happened to redirect to. Like every other durable log here it
    stays free of escape sequences.
    """
    path = Path(run_dir) / HARNESS_REPORT_LOG

    def emit(line: str) -> None:
        print(line, flush=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass    # reporting must never be what stops a run from being supervised

    return emit


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{int(round(value))}s"


def launch_message(record: dict) -> str:
    budget = record["relaunch_budget"]
    flags = " ".join(record["argv"][1:])
    stop = "kill -TERM -{}".format(record["pgid"])
    return (f"[MOL-LAUNCH] pipeline={record['pipeline']} mode={record['mode']} "
            f"launcher={record['launcher']} run-dir={record['run_dir']} "
            f"stages={record['stage_count']} flags={json.dumps(flags)} "
            f"relaunch-budget={budget['remaining']}/{budget['max_relaunches']} "
            f"({budget['source']}) wall-clock-ceiling={budget['wall_clock_seconds']:g}s "
            f"pid={record['pid']} stop={json.dumps(stop)}")


def state_message(observation: Observation) -> str:
    line = (f"[MOL-STATE] pipeline={observation.pipeline} "
            f"stage={observation.stage_index}/{observation.stage_count} "
            f"id={observation.stage_id or 'none'} state={observation.status} "
            f"stage-elapsed={_seconds(observation.stage_elapsed)} "
            f"total-elapsed={_seconds(observation.total_elapsed)}")
    if observation.detail:
        line += f" detail={json.dumps(observation.detail)}"
    return line


def hold_message(observation: Observation, unchanged: int) -> str:
    return (f"[MOL-HOLD] pipeline={observation.pipeline} unchanged={unchanged} "
            f"stage={observation.stage_index}/{observation.stage_count} "
            f"id={observation.stage_id or 'none'} state={observation.status} "
            f"total-elapsed={_seconds(observation.total_elapsed)}")


NEXT_ACTION = {
    "completed": "none; the pipeline completed and its evidence is in the run directory",
    "failed": "operator: read the stage's evidence, then regenerate or relaunch by hand",
    "stopped": "none; the stop was intentional and is never relaunched automatically",
    "unknown-terminal": "operator: the launcher died without writing a terminal state; "
                        "read the harness launch log before treating anything as done",
    "lock-conflict": "attach to the launcher that already owns the run directory",
    "supervision-expired": "operator: the run is still going past the declared supervision "
                           "ceiling; reattach or stop it",
}


def digest_message(observation: Observation, record: dict,
                   decision: RelaunchDecision | None = None) -> str:
    """One digest mirroring the launcher's own DIGEST line, plus the next action.
    completed, failed, stopped and unknown-terminal each read differently."""
    if decision is not None and decision.relaunch:
        following = f"relaunch: {decision.detail}"
    elif decision is not None and observation.status == "failed":
        following = f"no relaunch ({decision.reason}): {decision.detail}"
    else:
        following = NEXT_ACTION.get(observation.status, "operator: inspect the run directory")
    line = (f"[MOL-DIGEST] pipeline={observation.pipeline} state={observation.status} "
            f"exit={observation.launcher_exit if observation.launcher_exit is not None else 'unknown'} "
            f"child-exit={observation.child_exit if observation.child_exit is not None else 'none'} "
            f"last-stage={observation.stage_id or 'none'} "
            f"evidence={observation.evidence or record['run_dir']} "
            f"state-path={observation.state_path} next={json.dumps(following)}")
    return line


def relaunch_message(record: dict, observation: Observation, decision: RelaunchDecision) -> str:
    budget = record["relaunch_budget"]
    attempt = budget["max_relaunches"] - budget["remaining"] + 1
    return (f"[MOL-RELAUNCH] pipeline={observation.pipeline} "
            f"attempt={attempt}/{budget['max_relaunches']} reason={decision.reason} "
            f"exit={observation.launcher_exit} child-exit={observation.child_exit} "
            f"stage={observation.stage_id} stop-reason={observation.stop_reason!r} "
            f"budget-remaining={budget['remaining'] - 1} detail={json.dumps(decision.detail)}")


def refuse_message(action: str, reason: str, detail: str, next_action: str = "") -> str:
    line = f"[MOL-REFUSE] action={action} reason={reason} detail={json.dumps(detail)}"
    if next_action:
        line += f" next={json.dumps(next_action)}"
    return line


def anomaly_messages(observation: Observation) -> list[str]:
    lines = [f"[MOL-ANOMALY] pipeline={observation.pipeline} detail={json.dumps(text)}"
             for text in observation.anomalies]
    if observation.launch_log_tail and observation.status == "unknown-terminal":
        lines.append(f"[MOL-ANOMALY] pipeline={observation.pipeline} "
                     f"launch-log={json.dumps(observation.launch_log_tail[-600:])}")
    return lines


def next_interval(previous: float, observation: Observation, changed: bool, *,
                  base: float = BASE_POLL_SECONDS, cap: float = MAX_POLL_SECONDS) -> float:
    """Poll no tighter than the base interval, back off while a stage runs long, and go
    back to the base interval at a stage boundary or after a recovery."""
    if changed or observation.detail.startswith("RECOVER"):
        return base
    return min(previous * POLL_BACKOFF, cap)


# ── 9. the supervision loop ───────────────────────────────────────────────────

def supervise(record: dict, bundle: Bundle | None, *, emit: Callable[[str], None] = print,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.time,
              base: float = BASE_POLL_SECONDS, cap: float = MAX_POLL_SECONDS,
              allow_relaunch: bool = True, max_wait: float | None = None) -> tuple[str, dict]:
    """Poll until the run is terminal, reporting as the skill's contract says to.

    A harness with a scheduling primitive can call observe/report once per wake-up
    instead; the messages are identical, because they come from here either way.
    """
    supervision = record.get("supervision") or {}
    interval = supervision.get("interval", base)
    unchanged = supervision.get("unchanged", 0)
    previous = supervision.get("signature")
    deadline = None
    if max_wait is None:
        budget = record.get("relaunch_budget") or {}
        ceiling = budget.get("wall_clock_seconds")
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            deadline = record.get("first_launched_at", clock()) + ceiling
    elif max_wait > 0:
        deadline = clock() + max_wait

    while True:
        observation = observe(record, bundle, now=clock())
        for line in anomaly_messages(observation):
            emit(line)
        signature = list(observation.signature())
        changed = previous is None or signature != previous
        if observation.terminal:
            decision = (classify_relaunch(observation, record, bundle, now=clock())
                        if allow_relaunch else
                        RelaunchDecision(False, "relaunch-disabled", "relaunching was turned off"))
            if decision.relaunch:
                emit(relaunch_message(record, observation, decision))
                record = relaunch(record, justification=decision.justification(observation),
                                  now=clock())
                emit(launch_message(record))
                interval, unchanged, previous = base, 0, None
                continue
            emit(digest_message(observation, record, decision))
            record["supervision"] = {}
            atomic_json(_record_path(Path(record["run_dir"])), record)
            return observation.status, record

        if changed:
            emit(state_message(observation))
            unchanged = 0
        else:
            unchanged += 1
            emit(hold_message(observation, unchanged))
        interval = next_interval(interval, observation, changed, base=base, cap=cap)
        previous = signature
        record["supervision"] = {"interval": interval, "unchanged": unchanged,
                                 "signature": signature}
        atomic_json(_record_path(Path(record["run_dir"])), record)
        if deadline is not None and clock() + interval > deadline:
            emit(refuse_message("supervise", "supervision-expired",
                                "the run is still going past the declared supervision ceiling; "
                                "it was left running, not stopped",
                                NEXT_ACTION["supervision-expired"]))
            emit(digest_message(observation, record, None))
            return "supervision-expired", record
        sleep(interval)


def report_once(record: dict, bundle: Bundle | None, *, emit: Callable[[str], None] = print,
                clock: Callable[[], float] = time.time,
                base: float = BASE_POLL_SECONDS,
                cap: float = MAX_POLL_SECONDS) -> tuple[Observation, float]:
    """One scheduled wake-up: the same messages as the loop, plus the next poll delay.
    The quiet-hold count lives in the launch record, so separate wake-ups agree with a
    loop that never let go."""
    observation = observe(record, bundle, now=clock())
    for line in anomaly_messages(observation):
        emit(line)
    supervision = record.get("supervision") or {}
    previous = supervision.get("signature")
    interval = supervision.get("interval", base)
    unchanged = supervision.get("unchanged", 0)
    signature = list(observation.signature())
    changed = previous is None or signature != previous
    if observation.terminal:
        record["supervision"] = {}
        atomic_json(_record_path(Path(record["run_dir"])), record)
        return observation, 0.0
    if changed:
        emit(state_message(observation))
        unchanged = 0
    else:
        unchanged += 1
        emit(hold_message(observation, unchanged))
    interval = next_interval(interval, observation, changed, base=base, cap=cap)
    record["supervision"] = {"interval": interval, "unchanged": unchanged, "signature": signature}
    atomic_json(_record_path(Path(record["run_dir"])), record)
    return observation, interval


def exit_status(status: str) -> int:
    return EXIT_STATUS.get(status, 1)


if __name__ == "__main__":  # pragma: no cover - the CLI lives in supervise.py
    print("supervisor_lib is a library; run supervise.py", file=sys.stderr)
    raise SystemExit(2)
