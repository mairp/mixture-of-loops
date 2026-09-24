#!/usr/bin/env python3
"""Shared launch-contract loading and validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys


SCHEMA_VERSION = "1.0"
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KINDS = {"setup", "decision", "command", "specstride", "smoke"}
# Specstride was formerly Wiggum. Contracts written before the rename still load:
# each legacy spelling is normalized in memory to the current one and reported as
# a warning, never an error (see normalize_contract).
LEGACY_KIND = "wiggum"
CURRENT_KIND = "specstride"
LEGACY_COMMAND = "wiggum"
LEGACY_ENV_PREFIX = "WIGGUM_"
LEGACY_TIMING_PREFIX = "wiggum-phase:"
LEGACY_LIVE_KEY = "wiggum_live"
# Specstride's per-workdir state directory, and the legacy one a workdir that
# predates the rename keeps using in place.
STATE_DIRNAME = ".specstride"
LEGACY_STATE_DIRNAME = ".wiggum"
# Generated bundles and run state live here, beside the launcher that owns them.
ARTIFACT_DIRNAME = ".mixture-of-loops"
CHECKS = {
    "file_exists",
    "dir_exists",
    "env_set",
    "command_available",
    "command_success",
    "json_field_equals",
}
DISPOSITIONS = {"mapped", "optional", "out-of-scope", "unresolved", "unsupported"}
# The bootstrap's producer placeholder: like an unresolved disposition, only a draft keeps it.
# Other producers stay the model's words (references/derivation.md's Producer axis), except
# that a `stage:<id>` must name a stage.
UNCLASSIFIED_PRODUCER = "unclassified"
# The optional harness-side `configuration.auto` budget: how often a supervising harness
# may relaunch the launcher after a classified transient stage failure, and the wall clock
# that supervision may span. Both are bounded by what the stages themselves declare, so a
# budget is derived from the contract rather than chosen freely.
DEFAULT_MAX_RELAUNCHES = 2
MAX_RELAUNCHES = 5
DEFAULT_TIMEOUT_SECONDS = 3600
AUTO_KEYS = {"max_relaunches", "wall_clock_seconds"}
# Specstride's learning mode. A `specstride` stage declares it in its own `env`; the
# runtime never lets an inherited value through (see runtime.resolve_env), and an
# undeclared mode runs as `off`. `from_env` is refused for it: a reference would
# re-open exactly the hole that stripping the inherited value closes.
LEARNING_ENV = "SPECSTRIDE_LEARNING"
LEGACY_LEARNING_ENV = LEGACY_ENV_PREFIX + "LEARNING"
LEARNING_MODES = ("off", "suggest", "apply")
LEARNING_DEFAULT = "off"
# configuration.learning binds a prefix of Specstride's decision log into the contract
# (see _validate_learning): which decisions a run may act on, and a hash of them.
LEARNING_THROUGH_ENV = "SPECSTRIDE_LEARNING_THROUGH"
LEARNING_KEYS = {"mode", "decisions_through", "decisions_sha256", "effective", "source_path"}
SENSITIVE_ENV = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)(?:$|_)", re.I)
# SKILL.md's command-line examples (steps 3 and 8) prefix the script invocation with this,
# e.g. `MOL_VIA=shell python3 .../bootstrap_contract.py ...`: an inline prefix travels with
# the command line in %%bash, a `!` line, and every plain shell alike, unlike a separate
# `export` line, which a `!`-per-line notebook or a copy of only the python line would run
# in a different shell than the command and so never see. Its absence does not prove a
# wrapper -- a plain shell that just didn't type the prefix sees it too -- so this is a
# reminder on stderr, never a failure: see warn_if_not_shell_invoked.
SHELL_MARKER_ENV = "MOL_VIA"
SHELL_MARKER_VALUE = "shell"


class ContractError(Exception):
    pass


class StaleSourceError(ContractError):
    pass


class LearningDecisionsChanged(StaleSourceError):
    """The decision-log prefix configuration.learning bound no longer hashes the same."""


def warn_if_not_shell_invoked(script: str) -> None:
    """Print SKILL.md's shell-cell reminder to stderr when MOL_VIA=shell is absent.

    Never changes the exit code or stdout: callers run this unconditionally and ignore
    its result. It cannot tell a wrapper from a plain shell that just skipped the prefix,
    so it reads as a reminder, not an accusation.
    """
    if os.environ.get(SHELL_MARKER_ENV) != SHELL_MARKER_VALUE:
        print(f"note: {script} ran without {SHELL_MARKER_ENV}={SHELL_MARKER_VALUE} set; "
              f"run it as a shell command line prefixed {SHELL_MARKER_ENV}={SHELL_MARKER_VALUE} "
              f"(SKILL.md steps 3 and 8), in %%bash or a ! line in a notebook harness, not "
              f"through another language's subprocess API.",
              file=sys.stderr)


def load_contract(path: str | os.PathLike[str]) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read contract: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("contract must be a JSON object")
    return value


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def resolve_path(value: str, root: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_learning_state(path: Path) -> bool:
    """True for a path under a `learning/` directory of a Specstride state tree. Both
    state-dir spellings match literally: source paths do not go through
    runtime.path_for's state-dir swap."""
    parts = path.parts
    for index, part in enumerate(parts):
        if part in (STATE_DIRNAME, LEGACY_STATE_DIRNAME) and "learning" in parts[index + 1:-1]:
            return True
    return False


def _validate_action(action: object, label: str, errors: list[str]) -> None:
    _require(isinstance(action, dict), f"{label} must be an object", errors)
    if not isinstance(action, dict):
        return
    argv = action.get("argv")
    _require(
        isinstance(argv, list) and bool(argv) and all(isinstance(item, str) and item for item in argv),
        f"{label}.argv must be a nonempty string array",
        errors,
    )
    if isinstance(argv, list):
        _require(all("\n" not in item and "\x00" not in item for item in argv if isinstance(item, str)),
                 f"{label}.argv cannot contain newlines or NUL", errors)
        if len(argv) >= 2 and Path(str(argv[0])).name in {"sh", "bash", "zsh", "dash"}:
            _require(argv[1] not in {"-c", "-lc"}, f"{label} cannot execute a shell command string", errors)
    timeout = action.get("timeout_seconds", 3600)
    _require(isinstance(timeout, int) and timeout > 0, f"{label}.timeout_seconds must be positive", errors)
    env = action.get("env", {})
    _require(isinstance(env, dict), f"{label}.env must be an object", errors)
    if isinstance(env, dict):
        for key, value in env.items():
            _require(isinstance(key, str) and bool(key), f"{label}.env has invalid key", errors)
            valid = isinstance(value, str) or (
                isinstance(value, dict)
                and isinstance(value.get("from_env"), str)
                and isinstance(value.get("required", True), bool)
            )
            _require(valid, f"{label}.env.{key} must be a string or from_env reference", errors)
            if isinstance(key, str) and isinstance(value, str) and SENSITIVE_ENV.search(key):
                _require(False, f"{label}.env.{key} must use from_env; literal secrets are forbidden", errors)
            if key == LEGACY_LEARNING_ENV:
                _require(False, f"{label}.env.{key} cannot sit beside {LEARNING_ENV}; declare {LEARNING_ENV} only",
                         errors)
            if key == LEARNING_THROUGH_ENV:
                _require(isinstance(value, str) and bool(value),
                         f"{label}.env.{key} must be a literal run id, not from_env", errors)
            if key == LEARNING_ENV:
                _require(isinstance(value, str),
                         f"{label}.env.{key} must be a literal, not from_env: an inherited learning mode "
                         "is exactly what the runtime strips", errors)
                if isinstance(value, str):
                    _require(value in LEARNING_MODES,
                             f"{label}.env.{key} must be one of {', '.join(LEARNING_MODES)}", errors)


def _validate_check(
    check: object,
    label: str,
    errors: list[str],
    *,
    cwd: Path | None = None,
    roots: list[Path] | None = None,
    postcondition: bool = False,
) -> None:
    _require(isinstance(check, dict), f"{label} must be an object", errors)
    if not isinstance(check, dict):
        return
    kind = check.get("type")
    _require(kind in CHECKS, f"{label}.type is unsupported: {kind!r}", errors)
    if kind in {"file_exists", "dir_exists", "json_field_equals"}:
        _require(isinstance(check.get("path"), str), f"{label}.path is required", errors)
        if isinstance(check.get("path"), str) and cwd is not None and roots:
            _require(_inside(resolve_path(check["path"], cwd), roots),
                     f"{label}.path escapes authorized_roots", errors)
    if kind in {"env_set", "command_available"}:
        _require(isinstance(check.get("name"), str), f"{label}.name is required", errors)
    if kind == "command_success":
        _validate_action(check, label, errors)
        check_cwd = check.get("cwd", ".")
        _require(isinstance(check_cwd, str), f"{label}.cwd must be a string", errors)
        if isinstance(check_cwd, str) and cwd is not None and roots:
            _require(_inside(resolve_path(check_cwd, cwd), roots),
                     f"{label}.cwd escapes authorized_roots", errors)
    if kind == "json_field_equals":
        _require(isinstance(check.get("field"), str), f"{label}.field is required", errors)
        _require("value" in check, f"{label}.value is required", errors)
    _require(check.get("timing", "stage") in {"preflight", "stage"},
             f"{label}.timing must be preflight or stage", errors)
    # Preflight reads preconditions only: a postcondition marked preflight would never be
    # checked before the run, so a gate written that way silently gates nothing.
    _require(not (postcondition and check.get("timing") == "preflight"),
             f"{label}.timing preflight applies to preconditions only; move this check to "
             "preconditions to gate the run on it", errors)


def check_source_hashes(contract: dict) -> list[str]:
    root_value = contract.get("repository", {}).get("root")
    if not isinstance(root_value, str):
        return ["repository.root is invalid"]
    root = Path(root_value).resolve()
    stale: list[str] = []
    for index, source in enumerate(contract.get("sources", [])):
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            continue
        path = resolve_path(source["path"], root)
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            stale.append(f"sources[{index}] cannot be read: {path}: {exc}")
            continue
        if actual != source.get("sha256"):
            expected_semantic = source.get("semantic_sha256")
            if source.get("kind") == "tasks" and isinstance(expected_semantic, str):
                try:
                    content = path.read_text(encoding="utf-8")
                    normalized = re.sub(
                        r"(?m)^(\s*-\s*\[)[ xX](\]\s+T\d+[A-Za-z]?\b)",
                        r"\1 \2",
                        content,
                    )
                    actual_semantic = hashlib.sha256(normalized.encode()).hexdigest()
                except (OSError, UnicodeDecodeError):
                    actual_semantic = ""
                if actual_semantic == expected_semantic:
                    continue
            stale.append(f"sources[{index}] hash changed: {path}")
    return stale


def declared_stage_seconds(stage: dict) -> float:
    """The wall clock one stage can occupy at most, read from its own declarations:
    every attempt running to its timeout, plus every declared backoff."""
    action = stage.get("action")
    per_attempt = action.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS) if isinstance(action, dict) else None
    if not isinstance(per_attempt, (int, float)) or isinstance(per_attempt, bool) or per_attempt <= 0:
        per_attempt = DEFAULT_TIMEOUT_SECONDS
    recovery = stage.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    attempts = recovery.get("max_attempts", 1)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        attempts = 1
    backoff = recovery.get("backoff_seconds", [])
    delay = sum(value for value in backoff
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0) \
        if isinstance(backoff, list) else 0
    return attempts * float(per_attempt) + float(delay)


def declared_pipeline_seconds(contract: dict) -> float:
    """The wall clock the whole stage list declares for one launcher invocation."""
    stages = contract.get("stages")
    if not isinstance(stages, list):
        return 0.0
    return sum(declared_stage_seconds(stage) for stage in stages if isinstance(stage, dict))


def auto_budget(contract: dict) -> dict:
    """The relaunch budget in force for this contract.

    A declared `configuration.auto` block supplies it; otherwise the conservative default
    applies, derived from the same declared bounds: at most DEFAULT_MAX_RELAUNCHES
    relaunches, and a ceiling of the wall clock those attempts of the pipeline declare.
    """
    configuration = contract.get("configuration")
    block = configuration.get("auto") if isinstance(configuration, dict) else None
    block = block if isinstance(block, dict) else None
    pipeline = declared_pipeline_seconds(contract)
    relaunches = block.get("max_relaunches", DEFAULT_MAX_RELAUNCHES) if block else DEFAULT_MAX_RELAUNCHES
    if not isinstance(relaunches, int) or isinstance(relaunches, bool) or relaunches < 0:
        relaunches = DEFAULT_MAX_RELAUNCHES
    ceiling = block.get("wall_clock_seconds") if block else None
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool) or ceiling <= 0:
        ceiling = (relaunches + 1) * pipeline
    return {
        "max_relaunches": relaunches,
        "wall_clock_seconds": float(ceiling),
        "declared_pipeline_seconds": pipeline,
        "source": "declared" if block is not None else "default",
    }


def learning_prefix(path: Path, through: str) -> bytes | None:
    """The bytes of a Specstride applied.json up to and including the line whose
    run_id is `through`, or None when no line names it."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    end = 0
    for line in data.splitlines(keepends=True):
        end += len(line)
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("run_id") == through:
            return data[:end]
    return None


def learning_effective(prefix: bytes) -> dict:
    """{knob: {phase: value}} in effect at the end of a decision-log prefix: the
    last apply/revert per (knob, phase, shape), keeping the applies still active."""
    latest: dict = {}
    for line in prefix.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("action") not in (None, "apply", "revert"):
            continue
        latest[(entry.get("knob"), str(entry.get("phase")), entry.get("shape"))] = entry
    effective: dict = {}
    for (knob, phase, _shape), entry in latest.items():
        if entry.get("action") in (None, "apply") and isinstance(knob, str):
            effective.setdefault(knob, {})[phase] = entry.get("value")
    return effective


def learning_binding_problem(contract: dict) -> str | None:
    """Why the bound decision-log prefix no longer matches, or None when it does
    (or the contract binds none)."""
    configuration = contract.get("configuration")
    block = configuration.get("learning") if isinstance(configuration, dict) else None
    if not isinstance(block, dict):
        return None
    root = Path(str((contract.get("repository") or {}).get("root", "/")))
    path = resolve_path(str(block.get("source_path", "")), root)
    prefix = learning_prefix(path, str(block.get("decisions_through", "")))
    if prefix is None:
        return (f"configuration.learning: {path} no longer holds decision "
                f"{block.get('decisions_through')!r}")
    if hashlib.sha256(prefix).hexdigest() != block.get("decisions_sha256"):
        return (f"configuration.learning: the decisions in {path} up to "
                f"{block.get('decisions_through')!r} changed since derivation")
    return None


def _validate_learning(configuration: object, contract: dict, errors: list[str]) -> None:
    """configuration.learning is optional. When present it is the provenance record of
    the learning mode every specstride stage declares: the stages' literal
    SPECSTRIDE_LEARNING and SPECSTRIDE_LEARNING_THROUGH must match it."""
    block = configuration.get("learning") if isinstance(configuration, dict) else None
    stages = [stage for stage in contract.get("stages") or []
              if isinstance(stage, dict) and stage.get("kind") == CURRENT_KIND]
    actions = [(stage.get("id"), name, stage.get(name)) for stage in stages
               for name in ("action", "resume") if isinstance(stage.get(name), dict)]
    if block is None:
        for stage_id, name, action in actions:
            env = action.get("env") or {}
            env = env if isinstance(env, dict) else {}
            _require(LEARNING_THROUGH_ENV not in env,
                     f"stage {stage_id}.{name}.env.{LEARNING_THROUGH_ENV} needs a configuration.learning "
                     "block recording what it binds", errors)
            # apply with nothing bound would act on decisions the contract never recorded
            _require(env.get(LEARNING_ENV) != "apply",
                     f"stage {stage_id}.{name}.env.{LEARNING_ENV}=apply needs a configuration.learning "
                     "block binding the decisions it may use; with no decision log, use suggest", errors)
        return
    label = "configuration.learning"
    _require(isinstance(block, dict), f"{label} must be an object", errors)
    if not isinstance(block, dict):
        return
    _require(set(block) == LEARNING_KEYS,
             f"{label} must have exactly {', '.join(sorted(LEARNING_KEYS))}", errors)
    _require(block.get("mode") in LEARNING_MODES,
             f"{label}.mode must be one of {', '.join(LEARNING_MODES)}", errors)
    _require(isinstance(block.get("decisions_through"), str) and bool(block.get("decisions_through")),
             f"{label}.decisions_through must be a run id", errors)
    digest = block.get("decisions_sha256")
    _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)),
             f"{label}.decisions_sha256 must be lowercase SHA-256", errors)
    source = block.get("source_path")
    _require(isinstance(source, str) and bool(source) and not Path(source).is_absolute(),
             f"{label}.source_path must be the applied.json path relative to repository.root", errors)
    effective = block.get("effective")
    _require(isinstance(effective, dict) and all(
        isinstance(knob, str) and isinstance(phases, dict) and all(isinstance(k, str) for k in phases)
        for knob, phases in (effective.items() if isinstance(effective, dict) else [])),
        f"{label}.effective must map knob -> {{phase: value}}", errors)
    for stage_id, name, action in actions:
        env = action.get("env") or {}
        env = env if isinstance(env, dict) else {}
        _require(env.get(LEARNING_ENV) == block.get("mode"),
                 f"stage {stage_id}.{name}.env.{LEARNING_ENV} must be {block.get('mode')!r}, "
                 f"as {label}.mode records", errors)
        _require(env.get(LEARNING_THROUGH_ENV) == block.get("decisions_through"),
                 f"stage {stage_id}.{name}.env.{LEARNING_THROUGH_ENV} must be "
                 f"{block.get('decisions_through')!r}, as {label}.decisions_through records", errors)


def _validate_auto(configuration: object, contract: dict, errors: list[str]) -> None:
    if not isinstance(configuration, dict) or "auto" not in configuration:
        return
    block = configuration["auto"]
    label = "configuration.auto"
    _require(isinstance(block, dict), f"{label} must be an object", errors)
    if not isinstance(block, dict):
        return
    unknown = sorted(set(block) - AUTO_KEYS)
    _require(not unknown, f"{label} has unsupported keys: {', '.join(unknown)}", errors)
    relaunches = block.get("max_relaunches", DEFAULT_MAX_RELAUNCHES)
    valid_count = isinstance(relaunches, int) and not isinstance(relaunches, bool) \
        and 0 <= relaunches <= MAX_RELAUNCHES
    _require(valid_count,
             f"{label}.max_relaunches must be an integer in 0..{MAX_RELAUNCHES}", errors)
    if "wall_clock_seconds" not in block:
        return
    ceiling = block["wall_clock_seconds"]
    valid_ceiling = isinstance(ceiling, (int, float)) and not isinstance(ceiling, bool) and ceiling > 0
    _require(valid_ceiling, f"{label}.wall_clock_seconds must be a positive number", errors)
    if valid_count and valid_ceiling:
        limit = (relaunches + 1) * declared_pipeline_seconds(contract)
        _require(ceiling <= limit,
                 f"{label}.wall_clock_seconds is {ceiling:g}, above the {limit:g}s the stages "
                 f"declare for {relaunches + 1} launcher attempt(s)", errors)


def artifact_root(launcher_dir: Path) -> Path:
    """Where a launcher in `launcher_dir` keeps its bundles and run state.

    A launcher rendered into the artifact directory itself keeps that directory
    rather than nesting a second one below it, so bundles and run state hold the
    same paths whether the launcher sits at the repository root or beside them.
    """
    launcher_dir = Path(launcher_dir)
    if launcher_dir.name == ARTIFACT_DIRNAME:
        return launcher_dir
    return launcher_dir / ARTIFACT_DIRNAME


def state_dirname(cwd: Path) -> str:
    """The Specstride state-dir name under `cwd`: the legacy one only when it is the
    only one present (Specstride never moves it)."""
    if not (cwd / STATE_DIRNAME).is_dir() and (cwd / LEGACY_STATE_DIRNAME).is_dir():
        return LEGACY_STATE_DIRNAME
    return STATE_DIRNAME


def _normalize_action(action: object) -> bool:
    """Point a legacy command at `specstride` and legacy env names at SPECSTRIDE_*."""
    changed = False
    if not isinstance(action, dict):
        return changed
    argv = action.get("argv")
    if isinstance(argv, list) and argv and isinstance(argv[0], str):
        head = Path(argv[0])
        if head.name == LEGACY_COMMAND:
            argv[0] = str(head.with_name(CURRENT_KIND)) if str(head.parent) != "." else CURRENT_KIND
            changed = True
    env = action.get("env")
    if isinstance(env, dict):
        for key in [k for k in env if isinstance(k, str) and k.startswith(LEGACY_ENV_PREFIX)]:
            new = "SPECSTRIDE_" + key[len(LEGACY_ENV_PREFIX):]
            if new not in env:
                env[new] = env.pop(key)
                changed = True
    return changed


def normalize_contract(contract: dict) -> list[str]:
    """Rewrite pre-rename spellings in place; return one warning per rewrite site.

    Stage kind "wiggum" becomes "specstride"; in any stage a `wiggum` command
    (argv or command_available check) becomes `specstride` and WIGGUM_* action
    env keys become SPECSTRIDE_*; configuration.wiggum_live becomes
    specstride_live; coverage timing "wiggum-phase:N" becomes "specstride-phase:N"."""
    warnings: list[str] = []
    stages = contract.get("stages")
    if isinstance(stages, list):
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            if stage.get("kind") == LEGACY_KIND:
                stage["kind"] = CURRENT_KIND
                warnings.append(f"stages[{index}].kind {LEGACY_KIND!r} is deprecated; "
                                f"read as {CURRENT_KIND!r} (Specstride was formerly Wiggum)")
            rewrote = False
            for field in ("action", "resume"):
                rewrote = _normalize_action(stage.get(field)) or rewrote
            for field in ("preconditions", "postconditions"):
                for check in stage.get(field) or []:
                    if not isinstance(check, dict):
                        continue
                    if check.get("type") == "command_available" and check.get("name") == LEGACY_COMMAND:
                        check["name"] = CURRENT_KIND
                        rewrote = True
                    elif check.get("type") == "command_success":
                        rewrote = _normalize_action(check) or rewrote
            if rewrote:
                warnings.append(f"stages[{index}] uses the deprecated {LEGACY_COMMAND!r} command "
                                f"or {LEGACY_ENV_PREFIX}* env names; read as {CURRENT_KIND!r}")
    configuration = contract.get("configuration")
    if isinstance(configuration, dict) and LEGACY_LIVE_KEY in configuration:
        value = configuration.pop(LEGACY_LIVE_KEY)
        configuration.setdefault("specstride_live", value)
        warnings.append(f"configuration.{LEGACY_LIVE_KEY} is deprecated; read as specstride_live")
    coverage = contract.get("coverage")
    renamed_timing = 0
    if isinstance(coverage, list):
        for entry in coverage:
            timing = entry.get("timing") if isinstance(entry, dict) else None
            if isinstance(timing, str) and timing.startswith(LEGACY_TIMING_PREFIX):
                entry["timing"] = "specstride-phase:" + timing[len(LEGACY_TIMING_PREFIX):]
                renamed_timing += 1
    if renamed_timing:
        warnings.append(f"{renamed_timing} coverage timing value(s) use the deprecated "
                        f"{LEGACY_TIMING_PREFIX!r} prefix; read as 'specstride-phase:'")
    return warnings


def validate_contract(
    contract: dict, *, allow_draft: bool = False, check_sources: bool = True
) -> list[str]:
    """Normalize legacy spellings in place, validate, and return the warnings.
    Raises ContractError (or StaleSourceError) on any error."""
    warnings = normalize_contract(contract)
    errors: list[str] = []
    _require(contract.get("schema_version") == SCHEMA_VERSION,
             f"schema_version must be {SCHEMA_VERSION!r}", errors)
    identifier = contract.get("id")
    _require(isinstance(identifier, str) and bool(ID_RE.fullmatch(identifier)),
             "id must be kebab-case", errors)
    status = contract.get("status")
    _require(status in {"draft", "validated"}, "status must be draft or validated", errors)
    if not allow_draft:
        _require(status == "validated", "contract status must be validated", errors)

    repository = contract.get("repository")
    _require(isinstance(repository, dict), "repository must be an object", errors)
    root_value = repository.get("root") if isinstance(repository, dict) else None
    _require(isinstance(root_value, str) and Path(root_value).is_absolute(),
             "repository.root must be absolute", errors)
    root = Path(root_value).resolve() if isinstance(root_value, str) else Path("/")

    authorized = contract.get("authorized_roots")
    _require(isinstance(authorized, list) and bool(authorized),
             "authorized_roots must be a nonempty array", errors)
    roots: list[Path] = []
    if isinstance(authorized, list):
        for index, value in enumerate(authorized):
            _require(isinstance(value, str) and Path(value).is_absolute(),
                     f"authorized_roots[{index}] must be absolute", errors)
            if isinstance(value, str):
                roots.append(Path(value).resolve())

    sources = contract.get("sources")
    _require(isinstance(sources, list) and bool(sources), "sources must be a nonempty array", errors)
    source_paths: set[str] = set()
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            label = f"sources[{index}]"
            _require(isinstance(source, dict), f"{label} must be an object", errors)
            if not isinstance(source, dict):
                continue
            path = source.get("path")
            _require(isinstance(path, str) and bool(path), f"{label}.path is required", errors)
            _require(isinstance(source.get("kind"), str), f"{label}.kind is required", errors)
            digest = source.get("sha256")
            _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)),
                     f"{label}.sha256 must be lowercase SHA-256", errors)
            semantic = source.get("semantic_sha256")
            if semantic is not None:
                _require(isinstance(semantic, str) and bool(re.fullmatch(r"[0-9a-f]{64}", semantic)),
                         f"{label}.semantic_sha256 must be lowercase SHA-256", errors)
            if isinstance(path, str):
                _require(path not in source_paths, f"duplicate source path: {path}", errors)
                source_paths.add(path)
                if roots:
                    _require(_inside(resolve_path(path, root), roots),
                             f"{label}.path escapes authorized_roots", errors)
                _require(not _is_learning_state(resolve_path(path, root)),
                         f"{label}.path is Specstride learning state: learning/phase-<N>.json is rewritten "
                         "at every approved phase, so hashing it would refuse every relaunch (exit 23)",
                         errors)

    stages = contract.get("stages")
    _require(isinstance(stages, list), "stages must be an array", errors)
    if not allow_draft:
        _require(isinstance(stages, list) and bool(stages), "validated contract needs stages", errors)
    stage_ids: set[str] = set()
    ordered_ids: list[str] = []
    stage_kinds = ({stage.get("id"): stage.get("kind") for stage in stages if isinstance(stage, dict)}
                   if isinstance(stages, list) else {})
    if isinstance(stages, list):
        for index, stage in enumerate(stages):
            label = f"stages[{index}]"
            _require(isinstance(stage, dict), f"{label} must be an object", errors)
            if not isinstance(stage, dict):
                continue
            stage_id = stage.get("id")
            _require(isinstance(stage_id, str) and bool(ID_RE.fullmatch(stage_id)),
                     f"{label}.id must be kebab-case", errors)
            if isinstance(stage_id, str):
                _require(stage_id not in stage_ids, f"duplicate stage id: {stage_id}", errors)
                stage_ids.add(stage_id)
                ordered_ids.append(stage_id)
            _require(stage.get("kind") in KINDS, f"{label}.kind is invalid", errors)
            depends = stage.get("depends_on")
            _require(isinstance(depends, list) and all(isinstance(item, str) for item in depends),
                     f"{label}.depends_on must be a string array", errors)
            cwd = stage.get("cwd")
            _require(isinstance(cwd, str), f"{label}.cwd is required", errors)
            if isinstance(cwd, str) and roots:
                _require(_inside(resolve_path(cwd, root), roots),
                         f"{label}.cwd escapes authorized_roots", errors)
            stage_cwd = resolve_path(cwd, root) if isinstance(cwd, str) else root
            _validate_action(stage.get("action"), f"{label}.action", errors)
            if "resume" in stage:
                _validate_action(stage.get("resume"), f"{label}.resume", errors)
            for field in ("preconditions", "postconditions"):
                checks = stage.get(field)
                _require(isinstance(checks, list), f"{label}.{field} must be an array", errors)
                if isinstance(checks, list):
                    for check_index, check in enumerate(checks):
                        _validate_check(
                            check,
                            f"{label}.{field}[{check_index}]",
                            errors,
                            cwd=stage_cwd,
                            roots=roots,
                            postcondition=field == "postconditions",
                        )
            _require(bool(stage.get("postconditions")),
                     f"{label}.postconditions must support resume revalidation", errors)
            evidence = stage.get("evidence")
            _require(isinstance(evidence, list), f"{label}.evidence must be an array", errors)
            if isinstance(evidence, list):
                for evidence_index, value in enumerate(evidence):
                    _require(isinstance(value, str),
                             f"{label}.evidence[{evidence_index}] must be a string", errors)
                    if isinstance(value, str) and roots:
                        _require(_inside(resolve_path(value, stage_cwd), roots),
                                 f"{label}.evidence[{evidence_index}] escapes authorized_roots", errors)
            recovery = stage.get("recovery", {"max_attempts": 1})
            _require(isinstance(recovery, dict), f"{label}.recovery must be an object", errors)
            if isinstance(recovery, dict):
                attempts = recovery.get("max_attempts", 1)
                _require(isinstance(attempts, int) and attempts >= 1,
                         f"{label}.recovery.max_attempts must be positive", errors)
                if isinstance(attempts, int) and attempts > 1:
                    codes = recovery.get("retry_exit_codes")
                    _require(isinstance(codes, list) and bool(codes)
                             and all(isinstance(code, int) for code in codes),
                             f"{label}.recovery needs retry_exit_codes", errors)
                    if stage.get("kind") == CURRENT_KIND and isinstance(codes, list) and 4 in codes:
                        _require(isinstance(recovery.get("reason"), dict),
                                 f"{label}.recovery needs a reason constraint for Specstride exit 4", errors)
                    backoff = recovery.get("backoff_seconds", [])
                    _require(isinstance(backoff, list) and all(
                        isinstance(delay, (int, float)) and delay >= 0 for delay in backoff
                    ), f"{label}.recovery.backoff_seconds must be nonnegative numbers", errors)
                    total = recovery.get("total_timeout_seconds")
                    _require(total is None or (isinstance(total, (int, float)) and total > 0),
                             f"{label}.recovery.total_timeout_seconds must be positive", errors)
                    reason = recovery.get("reason")
                    if isinstance(reason, dict):
                        _require(isinstance(reason.get("jsonl"), str),
                                 f"{label}.recovery.reason.jsonl is required", errors)
                        _require(isinstance(reason.get("field", "reason"), str),
                                 f"{label}.recovery.reason.field must be a string", errors)
                        _require(isinstance(reason.get("allowed"), list) and bool(reason.get("allowed"))
                                 and all(isinstance(item, str) for item in reason.get("allowed", [])),
                                 f"{label}.recovery.reason.allowed must be a nonempty string array", errors)
                        if isinstance(reason.get("jsonl"), str) and roots:
                            _require(_inside(resolve_path(reason["jsonl"], stage_cwd), roots),
                                     f"{label}.recovery.reason.jsonl escapes authorized_roots", errors)

    if isinstance(stages, list):
        positions = {stage_id: index for index, stage_id in enumerate(ordered_ids)}
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict) or not isinstance(stage.get("depends_on"), list):
                continue
            for dependency in stage["depends_on"]:
                _require(dependency in stage_ids, f"stages[{index}] unknown dependency: {dependency}", errors)
                if dependency in positions:
                    _require(positions[dependency] < index,
                             f"stages[{index}] dependency must appear earlier: {dependency}", errors)

    coverage = contract.get("coverage")
    _require(isinstance(coverage, list), "coverage must be an array", errors)
    coverage_ids: set[str] = set()
    if isinstance(coverage, list):
        for index, entry in enumerate(coverage):
            label = f"coverage[{index}]"
            _require(isinstance(entry, dict), f"{label} must be an object", errors)
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            _require(isinstance(entry_id, str) and bool(entry_id), f"{label}.id is required", errors)
            if isinstance(entry_id, str):
                _require(entry_id not in coverage_ids, f"duplicate coverage id: {entry_id}", errors)
                coverage_ids.add(entry_id)
            _require(entry.get("disposition") in DISPOSITIONS,
                     f"{label}.disposition is invalid", errors)
            if not allow_draft:
                _require(entry.get("disposition") not in {"unresolved", "unsupported"},
                         f"{label} remains {entry.get('disposition')}", errors)
                producer = entry.get("producer")
                _require(producer != UNCLASSIFIED_PRODUCER,
                         f"{label}.producer is still the bootstrap's `unclassified`: name what produces it "
                         "(stage:<id> for a pending task)", errors)
                if isinstance(producer, str) and producer.startswith("stage:"):
                    _require(producer[len("stage:"):] in stage_ids,
                             f"{label}.producer {producer} names no stage", errors)
                # Implementing a task is Specstride's loop: a setup or command stage that
                # writes the code itself (2026-09-23 gpt-5 grid) is the model doing the work.
                if entry.get("kind") == "implementation" and entry.get("disposition") == "mapped":
                    _require(any(stage_kinds.get(stage_id) in ("specstride", LEGACY_KIND)
                                 for stage_id in entry.get("stage_ids") or [] if isinstance(stage_id, str)),
                             f"{label} is an implementation obligation mapped to no specstride stage: "
                             "implementation is Specstride's job, never a setup or command stage's", errors)
            source = entry.get("source")
            _require(isinstance(source, dict) and isinstance(source.get("path"), str)
                     and isinstance(source.get("line"), int) and source.get("line", 0) > 0
                     and isinstance(source.get("anchor"), str),
                     f"{label}.source needs path, line, and anchor", errors)
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                _require(source["path"] in source_paths,
                         f"{label}.source.path is not in sources: {source['path']}", errors)
            mappings = entry.get("stage_ids")
            _require(isinstance(mappings, list), f"{label}.stage_ids must be an array", errors)
            if isinstance(mappings, list):
                for stage_id in mappings:
                    _require(stage_id in stage_ids, f"{label} maps unknown stage: {stage_id}", errors)
            _require(isinstance(entry.get("verification_ids"), list),
                     f"{label}.verification_ids must be an array", errors)
            _require(isinstance(entry.get("rationale"), str) and bool(entry.get("rationale")),
                     f"{label}.rationale is required", errors)
            _require(isinstance(entry.get("evidence"), list), f"{label}.evidence must be an array", errors)

    configuration = contract.get("configuration")
    _require(configuration is None or isinstance(configuration, dict),
             "configuration must be an object", errors)
    _validate_auto(configuration, contract, errors)
    _validate_learning(configuration, contract, errors)

    findings = contract.get("findings")
    _require(isinstance(findings, list), "findings must be an array", errors)
    if isinstance(findings, list):
        finding_ids: set[str] = set()
        for index, finding in enumerate(findings):
            label = f"findings[{index}]"
            _require(isinstance(finding, dict), f"{label} must be an object", errors)
            if not isinstance(finding, dict):
                continue
            finding_id = finding.get("id")
            _require(isinstance(finding_id, str) and bool(finding_id), f"{label}.id is required", errors)
            if isinstance(finding_id, str):
                _require(finding_id not in finding_ids, f"duplicate finding id: {finding_id}", errors)
                finding_ids.add(finding_id)
            _require(finding.get("severity") in {"info", "warning", "blocker"},
                     f"{label}.severity is invalid", errors)
            _require(finding.get("status") in {"open", "resolved", "accepted"},
                     f"{label}.status is invalid", errors)
            _require(isinstance(finding.get("message"), str) and bool(finding.get("message")),
                     f"{label}.message is required", errors)
            _require(isinstance(finding.get("resolution"), str), f"{label}.resolution is required", errors)
            if not allow_draft and finding.get("severity") == "blocker" and finding.get("status") == "open":
                errors.append(f"{label} is an open blocker: {finding.get('message', '')}")

    if errors:
        raise ContractError("\n".join(errors))
    if check_sources:
        stale = check_source_hashes(contract)
        if stale:
            raise StaleSourceError("\n".join(stale))
        # the bound prefix of the decision log, re-hashed only here: supervise.py reads
        # bundles with check_sources=False, where the file may be absent
        problem = learning_binding_problem(contract)
        if problem:
            raise LearningDecisionsChanged(problem)
    return warnings


def canonical_bytes(contract: dict) -> bytes:
    return (json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
