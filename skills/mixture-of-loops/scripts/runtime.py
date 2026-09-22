#!/usr/bin/env python3
"""Execute a validated mixture-of-loops launch contract without interactive input."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from contract_lib import (
    CURRENT_KIND,
    LEARNING_DEFAULT,
    LEARNING_ENV,
    LEGACY_LEARNING_ENV,
    LEGACY_STATE_DIRNAME,
    STATE_DIRNAME,
    ContractError,
    StaleSourceError,
    artifact_root,
    load_contract,
    resolve_path,
    state_dirname,
    validate_contract,
)


E_INVALID = 20
E_PREFLIGHT = 21
E_STAGE = 22
E_STALE = 23
E_LOCK = 24
ANSI = {
    "reset": "\033[0m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
}
ANSI_BYTES = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
CURRENT_CHILD: subprocess.Popen[Any] | None = None
INTERRUPTED: int | None = None


class Reporter:
    def __init__(self, log_path: Path, color: bool):
        self.color = color
        self.handle = log_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self.handle.close()

    def emit(self, label: str, message: str, color: str = "cyan") -> None:
        plain = f"[{label}] {message}"
        self.handle.write(plain + "\n")
        self.handle.flush()
        if self.color:
            print(f"{ANSI[color]}[{label}]{ANSI['reset']} {message}", flush=True)
        else:
            print(plain, flush=True)

    def child_bytes(self, content: bytes) -> None:
        plain = ANSI_BYTES.sub(b"", content).decode("utf-8", errors="replace")
        self.handle.write(plain)
        self.handle.flush()


def signal_handler(signum: int, _frame: object) -> None:
    global INTERRUPTED
    INTERRUPTED = signum
    child = CURRENT_CHILD
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def resolve_env(specification: object, base: dict[str, str] | None = None,
                kind: str | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    # A child never inherits Specstride's learning mode: only the contract may set it,
    # so the contract says what runs. Stripped before the early return, so a stage or
    # a command_success check with no `env` is cleaned too. check_condition's
    # `env_set` still reads os.environ directly, so a contract can observe an
    # inherited value but never pass it to a child.
    environment.pop(LEARNING_ENV, None)
    environment.pop(LEGACY_LEARNING_ENV, None)
    if kind == CURRENT_KIND:
        # Unset means off, passed explicitly: Specstride re-asserts an exported value
        # over a .env in its checkout, but not an absent one.
        environment[LEARNING_ENV] = LEARNING_DEFAULT
    if specification is None:
        return environment
    if not isinstance(specification, dict):
        raise ContractError("action env must be an object")
    for name, value in specification.items():
        if isinstance(value, str):
            environment[name] = value
            continue
        if isinstance(value, dict) and isinstance(value.get("from_env"), str):
            source = value["from_env"]
            if source in os.environ:
                environment[name] = os.environ[source]
            elif value.get("required", True):
                raise ContractError(f"required environment variable is unset: {source}")
            else:
                environment.pop(name, None)
            continue
        raise ContractError(f"invalid environment value for {name}")
    return environment


def redacted_action(action: dict) -> str:
    argv = " ".join(json.dumps(item) for item in action.get("argv", []))
    env = action.get("env", {})
    if isinstance(env, dict) and env:
        names = ",".join(sorted(env))
        return f"env[{names}] {argv}"
    return argv


def path_for(value: str, cwd: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    # A path into Specstride's state dir (either spelling) follows the same rule as
    # Specstride itself: .specstride/, unless the workdir only has the legacy dir.
    if path.parts and path.parts[0] in (STATE_DIRNAME, LEGACY_STATE_DIRNAME):
        path = Path(state_dirname(cwd), *path.parts[1:])
    return (cwd / path).resolve()


def dotted_value(value: object, field: str) -> object:
    current = value
    for segment in field.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise KeyError(field)
    return current


def check_condition(check: dict, cwd: Path, *, execute_commands: bool = True) -> tuple[bool, str]:
    kind = check["type"]
    if kind == "file_exists":
        path = path_for(check["path"], cwd)
        return path.is_file(), f"file exists: {path}"
    if kind == "dir_exists":
        path = path_for(check["path"], cwd)
        return path.is_dir(), f"directory exists: {path}"
    if kind == "env_set":
        name = check["name"]
        return bool(os.environ.get(name)), f"environment set: {name}"
    if kind == "command_available":
        name = check["name"]
        return shutil.which(name) is not None, f"command available: {name}"
    if kind == "json_field_equals":
        path = path_for(check["path"], cwd)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            actual = dotted_value(value, check["field"])
        except (OSError, ValueError, KeyError) as exc:
            return False, f"JSON check failed: {path}: {exc}"
        return actual == check["value"], f"JSON field {check['field']} equals expected value in {path}"
    if kind == "command_success":
        if not execute_commands:
            return True, "command check deferred"
        command_cwd = path_for(check.get("cwd", "."), cwd)
        try:
            result = subprocess.run(
                check["argv"],
                cwd=command_cwd,
                env=resolve_env(check.get("env")),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=check.get("timeout_seconds", 60),
                check=False,
            )
        except (ContractError, OSError, subprocess.TimeoutExpired) as exc:
            return False, f"command check failed: {exc}"
        return result.returncode == 0, f"command returned {result.returncode}: {redacted_action(check)}"
    return False, f"unsupported check: {kind}"


def conditions_pass(checks: list[dict], cwd: Path, *, timing: str | None = None) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for check in checks:
        if timing is not None and check.get("timing", "stage") != timing:
            continue
        passed, message = check_condition(check, cwd)
        if not passed:
            failures.append(message)
    return not failures, failures


def latest_reason(reason_spec: dict, cwd: Path, offset: int) -> str | None:
    path = path_for(reason_spec["jsonl"], cwd)
    try:
        size = path.stat().st_size
        start = offset if size >= offset else 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            rows = handle.readlines()
    except OSError:
        return None
    result = None
    for row in rows:
        try:
            event = json.loads(row)
        except ValueError:
            continue
        if reason_spec.get("event") and event.get("event") != reason_spec["event"]:
            continue
        try:
            value = dotted_value(event, reason_spec.get("field", "reason"))
        except KeyError:
            continue
        if isinstance(value, str):
            result = value
    return result


def reason_offset(reason_spec: object, cwd: Path) -> int:
    if not isinstance(reason_spec, dict) or not isinstance(reason_spec.get("jsonl"), str):
        return 0
    try:
        return path_for(reason_spec["jsonl"], cwd).stat().st_size
    except OSError:
        return 0


def terminate_child(child: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def wait_streamed(
    child: subprocess.Popen[Any],
    descriptor: int,
    timeout: int,
    reporter: Reporter,
    log_child: bool,
) -> tuple[int, str]:
    selector = selectors.DefaultSelector()
    os.set_blocking(descriptor, False)
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    open_stream = True
    try:
        while child.poll() is None or open_stream:
            if time.monotonic() >= deadline:
                if child.poll() is None:
                    terminate_child(child)
                    return 124, "timeout"
                open_stream = False
                break
            for key, _ in selector.select(0.1):
                try:
                    content = os.read(key.fd, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        content = b""
                    else:
                        raise
                if not content:
                    selector.unregister(key.fd)
                    open_stream = False
                    continue
                sys.stdout.buffer.write(content)
                sys.stdout.buffer.flush()
                if log_child:
                    reporter.child_bytes(content)
        return child.wait(), "exit"
    finally:
        selector.close()


def display_action(action: dict, kind: str, color: bool) -> dict:
    effective = dict(action)
    effective["argv"] = list(action["argv"])
    effective["env"] = dict(action.get("env", {}))
    if not color:
        effective["env"].setdefault("NO_COLOR", "1")
        if kind == CURRENT_KIND:
            effective["argv"] = [item for item in effective["argv"] if item != "--live"]
            if "--no-live" not in effective["argv"]:
                effective["argv"].append("--no-live")
            effective["env"]["SPECSTRIDE_LIVE"] = "false"
            effective["env"]["WIGGUM_LIVE"] = "false"   # older checkouts (formerly Wiggum)
    return effective


def run_action(action: dict, cwd: Path, kind: str, reporter: Reporter) -> tuple[int, str]:
    global CURRENT_CHILD
    master: int | None = None
    slave: int | None = None
    try:
        environment = resolve_env(action.get("env"), kind=kind)
        use_pty = kind == CURRENT_KIND and reporter.color and not sys.stdout.isatty()
        capture_plain = kind == CURRENT_KIND and not reporter.color
        stdout: object = None
        stderr: object = None
        if use_pty:
            master, slave = pty.openpty()
            stdout = slave
            stderr = slave
        elif capture_plain:
            stdout = subprocess.PIPE
            stderr = subprocess.STDOUT
        CURRENT_CHILD = subprocess.Popen(
            action["argv"],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        if use_pty:
            os.close(slave)
            slave = None
            return wait_streamed(
                CURRENT_CHILD,
                master,
                action.get("timeout_seconds", 3600),
                reporter,
                False,
            )
        if capture_plain and CURRENT_CHILD.stdout is not None:
            return wait_streamed(
                CURRENT_CHILD,
                CURRENT_CHILD.stdout.fileno(),
                action.get("timeout_seconds", 3600),
                reporter,
                True,
            )
        try:
            code = CURRENT_CHILD.wait(timeout=action.get("timeout_seconds", 3600))
            return code, "exit"
        except subprocess.TimeoutExpired:
            terminate_child(CURRENT_CHILD)
            return 124, "timeout"
    except OSError as exc:
        return 127, f"exec-error:{exc}"
    finally:
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        if slave is not None:
            try:
                os.close(slave)
            except OSError:
                pass
        CURRENT_CHILD = None


def action_env_ready(action: object) -> list[str]:
    failures: list[str] = []
    if not isinstance(action, dict):
        return failures
    env = action.get("env", {})
    if not isinstance(env, dict):
        return failures
    for value in env.values():
        if isinstance(value, dict) and value.get("required", True):
            source = value.get("from_env")
            if isinstance(source, str) and not os.environ.get(source):
                failures.append(f"required environment variable is unset: {source}")
    return failures


def stage_should_retry(stage: dict, code: int, cwd: Path, offset: int) -> tuple[bool, str | None]:
    recovery = stage.get("recovery", {})
    if code not in recovery.get("retry_exit_codes", []):
        return False, None
    reason_spec = recovery.get("reason")
    if not isinstance(reason_spec, dict):
        return True, None
    reason = latest_reason(reason_spec, cwd, offset)
    return reason in reason_spec.get("allowed", []), reason


def run_stage(stage: dict, cwd: Path, reporter: Reporter, implement: bool) -> tuple[bool, int | None, str]:
    if stage["kind"] == "setup" and not implement:
        passed, failures = conditions_pass(stage["postconditions"], cwd)
        if passed:
            reporter.emit("READY", f"{stage['id']} already satisfies setup postconditions", "green")
            return True, None, "existing-prerequisite"
        for failure in failures:
            reporter.emit("BLOCKED", f"{stage['id']}: {failure}; rerun with --implement", "red")
        return False, None, "setup-required"

    passed, failures = conditions_pass(stage["preconditions"], cwd, timing="stage")
    if not passed:
        for failure in failures:
            reporter.emit("BLOCKED", f"{stage['id']}: {failure}", "red")
        return False, None, "stage-precondition"

    recovery = stage.get("recovery", {})
    max_attempts = recovery.get("max_attempts", 1)
    backoff = recovery.get("backoff_seconds", [])
    deadline = recovery.get("total_timeout_seconds")
    started = time.monotonic()
    last_code = None
    for attempt in range(1, max_attempts + 1):
        if INTERRUPTED is not None:
            return False, 128 + INTERRUPTED, "interrupted"
        selected = stage.get("resume") if attempt > 1 and stage.get("resume") else stage["action"]
        action = display_action(selected, stage["kind"], reporter.color)
        offset = reason_offset(recovery.get("reason"), cwd)
        reporter.emit("RUN", f"{stage['id']} attempt {attempt}/{max_attempts}: {redacted_action(action)}", "cyan")
        last_code, outcome = run_action(action, cwd, stage["kind"], reporter)
        if INTERRUPTED is not None:
            return False, 128 + INTERRUPTED, "interrupted"
        if last_code == 0:
            passed, failures = conditions_pass(stage["postconditions"], cwd)
            if passed:
                reporter.emit("PASS", f"{stage['id']} completed", "green")
                return True, 0, "completed"
            for failure in failures:
                reporter.emit("ERROR", f"{stage['id']} postcondition: {failure}", "red")
            return False, 0, "postcondition"
        retry, reason = stage_should_retry(stage, last_code, cwd, offset)
        elapsed = time.monotonic() - started
        if deadline is not None and elapsed >= deadline:
            retry = False
            outcome = "recovery-deadline"
        if retry and attempt < max_attempts:
            delay = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0
            suffix = f", reason={reason}" if reason else ""
            reporter.emit("RECOVER", f"{stage['id']} exit {last_code}{suffix}; retry in {delay}s", "magenta")
            if delay:
                time.sleep(delay)
            continue
        reporter.emit("FAIL", f"{stage['id']} exit {last_code} ({outcome})", "red")
        return False, last_code, outcome
    return False, last_code, "recovery-exhausted"


def dry_run(contract: dict, implement: bool, smoke: bool, color: bool) -> int:
    def show(label: str, text: str, hue: str = "cyan") -> None:
        if color:
            print(f"{ANSI[hue]}[{label}]{ANSI['reset']} {text}")
        else:
            print(f"[{label}] {text}")

    show("DRY-RUN", f"contract {contract['id']} is source-current; no actions or writes will occur", "green")
    for stage in contract["stages"]:
        if stage["kind"] == "smoke" and not smoke:
            show("SKIP", f"{stage['id']} (enable with --smoke)", "yellow")
        elif stage["kind"] == "setup" and not implement:
            show("CHECK", f"{stage['id']} uses existing prerequisite; --implement would run setup")
        else:
            show("PLAN", f"{stage['id']}: {redacted_action(stage.get('action', {}))}")
    show("DIGEST", "state=dry-run exit=0 last-stage=none evidence=none", "dim")
    return 0


def effective_color(mode: str | None, no_color: bool) -> bool:
    if no_color:
        return False
    if mode == "always":
        return True
    if mode == "never":
        return False
    if "NO_COLOR" in os.environ:
        return False
    mode = mode or "auto"
    return mode == "always" or (mode == "auto" and sys.stdout.isatty())


def semantic_contract_digest(contract: dict) -> str:
    """Bind resume state to execution meaning while ignoring task checkbox progress."""
    value = copy.deepcopy(contract)
    value.pop("inventory", None)
    value.pop("generated_by", None)
    repository = value.get("repository")
    if isinstance(repository, dict):
        repository.pop("revision", None)
        repository.pop("dirty", None)
    for source in value.get("sources", []):
        if isinstance(source, dict) and source.get("kind") == "tasks" and source.get("semantic_sha256"):
            source["sha256"] = source["semantic_sha256"]
        if isinstance(source, dict):
            source.pop("semantic_sha256", None)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--launch-dir", required=True, help=argparse.SUPPRESS)
    parser.add_argument("--implement", action="store_true", help="run declared idempotent setup stages")
    parser.add_argument("--dry-run", action="store_true", help="inspect only; takes precedence over all execution flags")
    parser.add_argument("--smoke", action="store_true", help="run declared smoke stages")
    parser.add_argument("--color", choices=("auto", "always", "never"), default=None)
    parser.add_argument("--no-color", action="store_true", help="force --color=never; also honored through NO_COLOR")
    args = parser.parse_args()
    color = effective_color(args.color, args.no_color)
    try:
        contract = load_contract(args.contract)
        for warning in validate_contract(contract, allow_draft=False, check_sources=True):
            print(f"[WARN] {warning}", file=sys.stderr)
    except StaleSourceError as exc:
        print(f"[STALE] {exc}", file=sys.stderr)
        print("[DIGEST] state=failed exit=23 last-stage=none evidence=none", file=sys.stderr)
        return E_STALE
    except ContractError as exc:
        print(f"[INVALID] {exc}", file=sys.stderr)
        print("[DIGEST] state=failed exit=20 last-stage=none evidence=none", file=sys.stderr)
        return E_INVALID

    if args.dry_run:
        return dry_run(contract, args.implement, args.smoke, color)

    launch_dir = Path(args.launch_dir).resolve()
    run_dir = artifact_root(launch_dir) / "runs" / contract["id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    reporter = Reporter(run_dir / "launcher.log", color)
    lock_handle = (run_dir / "lock").open("a+")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            reporter.emit("BLOCKED", f"another launcher owns {run_dir}", "red")
            reporter.emit("DIGEST", f"state=failed exit={E_LOCK} last-stage=none evidence={run_dir}", "dim")
            return E_LOCK

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal_handler)

        root = Path(contract["repository"]["root"]).resolve()
        state_path = run_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        except (OSError, ValueError):
            state = {}
        contract_digest = semantic_contract_digest(contract)
        if state.get("contract_digest") != contract_digest:
            previous_digest = state.get("contract_digest")
            state = {
                "superseded_contract_digest": previous_digest,
                "stages": {},
            }
        state.update(
            {
                "pipeline": contract["id"],
                "contract_digest": contract_digest,
                "state": "preflight",
                "updated_at": int(time.time()),
            }
        )
        state.setdefault("stages", {})
        atomic_json(state_path, state)

        preflight_failures = []
        for stage in contract["stages"]:
            if stage["kind"] == "smoke" and not args.smoke:
                continue
            if not (stage["kind"] == "setup" and not args.implement):
                preflight_failures.extend(action_env_ready(stage.get("action")))
                if stage.get("resume"):
                    preflight_failures.extend(action_env_ready(stage.get("resume")))
            cwd = resolve_path(stage["cwd"], root)
            passed, failures = conditions_pass(stage["preconditions"], cwd, timing="preflight")
            if not passed:
                preflight_failures.extend(f"{stage['id']}: {failure}" for failure in failures)
            if stage["kind"] == "setup" and not args.implement:
                passed, failures = conditions_pass(stage["postconditions"], cwd)
                if not passed:
                    preflight_failures.extend(
                        f"{stage['id']}: {failure}; rerun with --implement" for failure in failures
                    )
        if preflight_failures:
            for failure in preflight_failures:
                reporter.emit("BLOCKED", failure, "red")
            state.update({"state": "failed", "exit_reason": "preflight", "updated_at": int(time.time())})
            atomic_json(state_path, state)
            reporter.emit("DIGEST", f"state=failed exit={E_PREFLIGHT} last-stage=none evidence={run_dir}", "dim")
            return E_PREFLIGHT

        state.update({"state": "ready", "updated_at": int(time.time())})
        atomic_json(state_path, state)
        last_stage = "none"
        for stage in contract["stages"]:
            last_stage = stage["id"]
            cwd = resolve_path(stage["cwd"], root)
            if stage["kind"] == "smoke" and not args.smoke:
                reporter.emit("SKIP", f"{stage['id']} smoke stage disabled", "yellow")
                continue
            prior = state["stages"].get(stage["id"], {})
            if prior.get("status") == "completed":
                passed, _ = conditions_pass(stage["postconditions"], cwd)
                if passed:
                    reporter.emit("RESUME", f"{stage['id']} postconditions still hold; skipping", "green")
                    continue
                reporter.emit("RESUME", f"{stage['id']} postconditions changed; rerunning", "yellow")

            state.update({"state": "running", "last_stage": stage["id"], "updated_at": int(time.time())})
            state["stages"][stage["id"]] = {"status": "running", "started_at": int(time.time())}
            atomic_json(state_path, state)
            ok, child_code, reason = run_stage(stage, cwd, reporter, args.implement)
            if not ok:
                interrupted = INTERRUPTED is not None
                launcher_code = 128 + INTERRUPTED if interrupted else E_STAGE
                terminal_state = "stopped" if interrupted or child_code == 6 else "failed"
                state["stages"][stage["id"]].update(
                    {"status": terminal_state, "child_exit_code": child_code, "reason": reason,
                     "finished_at": int(time.time())}
                )
                state.update({"state": terminal_state, "exit_reason": reason, "updated_at": int(time.time())})
                atomic_json(state_path, state)
                reporter.emit(
                    "DIGEST",
                    f"state={terminal_state} exit={launcher_code} last-stage={stage['id']} "
                    f"child-exit={child_code} evidence={run_dir}",
                    "dim",
                )
                return launcher_code
            state["stages"][stage["id"]].update(
                {"status": "completed", "child_exit_code": child_code, "reason": reason,
                 "finished_at": int(time.time()), "evidence": stage.get("evidence", [])}
            )
            atomic_json(state_path, state)

        state.update({"state": "completed", "exit_reason": "all-stages-complete", "updated_at": int(time.time())})
        atomic_json(state_path, state)
        reporter.emit("DIGEST", f"state=completed exit=0 last-stage={last_stage} evidence={run_dir}", "dim")
        return 0
    finally:
        lock_handle.close()
        reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())
