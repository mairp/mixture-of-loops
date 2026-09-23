"""Shared Tier 3 logic: fixtures, transcript parsing, and the assertions.

Everything here is deterministic and model-free, so tests/test_e2e_logic.py can
check the verdicts against synthetic transcripts. tests/e2e/run_harness_e2e.py
feeds it real harness runs. Verdicts come from the event stream and the
filesystem, never from the model's prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "mixture-of-loops"
SCRIPTS = SKILL / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
STUB_BIN = Path(__file__).resolve().parent / "stub-bin"
EXEC_STUB_BIN = Path(__file__).resolve().parent / "exec-stub-bin"
NAME = "mixture-of-loops"
SCRIPT_NAMES = ("bootstrap_contract.py", "validate_contract.py", "render_launcher.py")
GENERATED_MARKER = "# mixture-of-loops-generated:"
FIXTURE_NAMES = ("greeting-blocked", "greeting-ready")


# ── fixtures ──────────────────────────────────────────────────────────────────

def expectations(fixture: str) -> dict:
    return json.loads((FIXTURES / "expectations" / f"{fixture}.json").read_text(encoding="utf-8"))


def prepare_fixture(fixture: str, destination: Path) -> Path:
    """Copy a fixture repository and commit it, so bootstrap sees a clean git repo."""
    shutil.copytree(FIXTURES / "repos" / fixture, destination)
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@invalid",
               "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@invalid",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", "-C", str(destination), *args], check=True, env=git_env,
                       stdin=subprocess.DEVNULL, capture_output=True)
    return destination


def tree_state(root: Path) -> dict[str, str]:
    """Every path under root except .git, with its type and content hash."""
    state = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not (Path(directory) == root and name == ".git")]
        for name in dirnames + filenames:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if path.is_symlink():
                state[relative] = "link:" + os.readlink(path)
            elif path.is_dir():
                state[relative] = "dir"
            else:
                state[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def run_script(name: str, *args: object, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run one of the skill's scripts ourselves: no TTY, stdin closed.

    MOL_VIA=shell tells the script this reproduces a shell invocation exactly (correct cwd,
    env, argv), the same declaration SKILL.md's command examples make — so its own
    scripts-only-via-shell reminder (see warn_if_not_shell_invoked) stays quiet here.
    """
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "MOL_VIA": "shell"}
    return subprocess.run([sys.executable, str(SCRIPTS / name), *[str(a) for a in args]], cwd=cwd,
                          env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, start_new_session=True, check=False)


# ── transcripts ───────────────────────────────────────────────────────────────

# A python cell that executes (not merely reads) a script: subprocess, os.system/popen, %run, runpy.
PYTHON_EXEC = {name: re.compile(r"(subprocess\.\w+|os\.(system|popen|exec\w*)|%run|runpy\.run_path)\b[^\n]*"
                                + re.escape(name)) for name in SCRIPT_NAMES}
TOOL_TIMEOUT = re.compile(r"Command timed out after \d+ seconds")
SCRIPT_RUN = {name: re.compile(r"\bpython3?(?:\.\d+)?\s+(?:-\S+\s+)*[^\s;|&]*" + re.escape(name))
              for name in SCRIPT_NAMES}


@dataclass
class ToolCall:
    id: str
    tool: str
    args: dict
    kind: str = "other"          # read | shell | other
    via: str | None = None       # bash | %%bash | ! | python (not counted as shell)
    text: str = ""               # the shell text, or the path read
    is_error: bool | None = None  # None: no tool_execution_end seen
    result: str = ""

    def runs(self, script: str) -> bool:
        return self.kind == "shell" and bool(SCRIPT_RUN[script].search(self.text))


@dataclass
class Transcript:
    calls: list[ToolCall] = field(default_factory=list)
    user_texts: list[str] = field(default_factory=list)
    agent_end: bool = False
    stop_reason: str | None = None
    error_message: str | None = None
    events: int = 0
    bad_lines: int = 0
    skill_invoked: list[str] = field(default_factory=list)   # harness-level skill invocations
    models: list[str] = field(default_factory=list)          # model ids the harness recorded as answering


def _ipython_shell(code: str) -> tuple[str | None, str]:
    """(via, shell text) for an ipython cell: a leading %%bash cell, or ! lines."""
    stripped = code.lstrip()
    if stripped.startswith("%%bash") or stripped.startswith("%%script bash") or stripped.startswith("%%sh"):
        return "%%bash", stripped.split("\n", 1)[1] if "\n" in stripped else ""
    bang = [line.strip()[1:] for line in code.splitlines() if line.strip().startswith("!")]
    if bang:
        return "!", "\n".join(bang)
    return None, ""


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))
    return ""


def classify(call: ToolCall) -> ToolCall:
    args = call.args if isinstance(call.args, dict) else {}
    if call.tool == "read":
        call.kind, call.text = "read", str(args.get("path") or args.get("file_path") or "")
    elif call.tool == "bash":
        call.kind, call.via, call.text = "shell", "bash", str(args.get("command", ""))
    elif call.tool == "ipython":
        code = str(args.get("code", ""))
        via, shell = _ipython_shell(code)
        if via:
            call.kind, call.via, call.text = "shell", via, shell
        else:
            call.via, call.text = "python", code
            reads = re.findall(r"open\(\s*['\"]([^'\"]+SKILL\.md)['\"]|Path\(\s*['\"]([^'\"]+SKILL\.md)['\"]\s*\)\.read_text",
                               code)
            if reads:
                call.kind, call.text = "read", next(filter(None, reads[0]))
    return call


def parse_transcript(lines: Iterable[str]) -> Transcript:
    """Normalize pi / prime `--mode json` JSONL into tool calls and the terminal state."""
    transcript = Transcript()
    by_id: dict[str, ToolCall] = {}
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            transcript.bad_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        transcript.events += 1
        kind = event.get("type")
        if kind == "tool_execution_start":
            call = classify(ToolCall(id=str(event.get("toolCallId")), tool=str(event.get("toolName")),
                                     args=event.get("args") or {}))
            by_id[call.id] = call
            transcript.calls.append(call)
        elif kind == "tool_execution_end":
            call = by_id.get(str(event.get("toolCallId")))
            if call is not None:
                call.is_error = bool(event.get("isError"))
                result = event.get("result") or {}
                call.result = _text(result.get("content") if isinstance(result, dict) else result)
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "user":
                transcript.user_texts.append(_text(message.get("content")))
            elif message.get("role") == "assistant":
                transcript.stop_reason = message.get("stopReason")
                transcript.error_message = message.get("errorMessage")
        elif kind == "agent_end":
            transcript.agent_end = True
            for message in reversed(event.get("messages") or []):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    transcript.stop_reason = message.get("stopReason", transcript.stop_reason)
                    transcript.error_message = message.get("errorMessage", transcript.error_message)
                    break
    return transcript


def _json_lines(lines: Iterable[str], transcript: Transcript) -> Iterable[dict]:
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            transcript.bad_lines += 1
            continue
        if isinstance(event, dict):
            transcript.events += 1
            yield event


def parse_claude_stream(lines: Iterable[str]) -> Transcript:
    """Claude Code `-p --output-format stream-json --verbose` into the same shape."""
    transcript = Transcript()
    by_id: dict[str, ToolCall] = {}
    for event in _json_lines(lines, transcript):
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if event.get("type") == "assistant" and isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                arguments = item.get("input") or {}
                call = ToolCall(id=str(item.get("id")), tool=str(item.get("name")), args=arguments)
                if call.tool == "Bash":
                    call.kind, call.via, call.text = "shell", "bash", str(arguments.get("command", ""))
                elif call.tool == "Read":
                    call.kind, call.text = "read", str(arguments.get("file_path", ""))
                elif call.tool == "Skill":
                    transcript.skill_invoked.append(str(arguments.get("skill") or arguments.get("command") or ""))
                by_id[call.id] = call
                transcript.calls.append(call)
        elif event.get("type") == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    call = by_id.get(str(item.get("tool_use_id")))
                    if call is not None:
                        call.is_error = bool(item.get("is_error"))
                        call.result = _text(item.get("content"))
                elif isinstance(item, dict) and item.get("type") == "text":
                    transcript.user_texts.append(item.get("text", ""))
        elif event.get("type") == "user" and isinstance(content, str):
            transcript.user_texts.append(content)
        elif event.get("type") == "result":
            transcript.agent_end = True
            ok = event.get("subtype") == "success" and not event.get("is_error")
            transcript.stop_reason = "stop" if ok else "error"
            transcript.error_message = None if ok else str(event.get("result") or event.get("subtype"))
    return transcript


def parse_codex_stream(lines: Iterable[str]) -> Transcript:
    """`codex exec --json` into the same shape (command executions are shell calls)."""
    transcript = Transcript()
    for event in _json_lines(lines, transcript):
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "item.completed" and item.get("type") == "command_execution":
            command = item.get("command")
            text = " ".join(command) if isinstance(command, list) else str(command or "")
            call = ToolCall(id=str(item.get("id")), tool="command_execution", args={"command": text},
                            kind="shell", via="bash", text=text,
                            is_error=item.get("exit_code") not in (0, None) or item.get("status") == "failed",
                            result=str(item.get("aggregated_output", ""))[:4000])
            if re.search(rf"\b(cat|sed|head|less|nl)\b[^\n]*{NAME}/SKILL\.md", text):
                transcript.calls.append(ToolCall(id=call.id + "-read", tool="read", args={}, kind="read",
                                                 text=text.split()[-1].strip("'\"")))
            transcript.calls.append(call)
        elif kind == "turn.completed":
            transcript.agent_end, transcript.stop_reason = True, "stop"
        elif kind in ("turn.failed", "error"):
            transcript.agent_end, transcript.stop_reason = True, "error"
            error = event.get("error") or {}
            transcript.error_message = str(error.get("message") if isinstance(error, dict) else event.get("message"))
    return transcript


def parse_dsh_session(lines: Iterable[str]) -> Transcript:
    """dsh's session JSONL (decompressed) into the same shape.

    Headless dsh prints only the final message on stdout; the session under $DSH_HOME
    records `tool/call` (name, callId, JSON-string arguments), `tool/result` (content
    per toolCallId, isError), `turn/end` (reason.kind completed|aborted|error) and, in
    `request/context` and each `assistant/message` source, the provider/model that
    answered -- which is how a run proves it stayed on the configured model.
    """
    transcript = Transcript()
    by_id: dict[str, ToolCall] = {}
    for event in _json_lines(lines, transcript):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "tool/call":
            arguments = data.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {"raw": arguments}
            call = classify(ToolCall(id=str(data.get("callId")), tool=str(data.get("name")),
                                     args=arguments if isinstance(arguments, dict) else {}))
            if call.tool == "skill":
                transcript.skill_invoked.append(str(call.args.get("name", "")))
            by_id[call.id] = call
            transcript.calls.append(call)
        elif kind == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            for item in message.get("content") or []:
                if not isinstance(item, dict) or item.get("type") != "tool-result":
                    continue
                call = by_id.get(str(item.get("toolCallId")))
                if call is not None:
                    call.is_error = bool(item.get("isError"))
                    call.result = _text(item.get("content"))
        elif kind == "user/message":
            transcript.user_texts.append(_text(data.get("content")))
        elif kind == "request/context":
            if data.get("model"):
                transcript.models.append(str(data["model"]))
        elif kind == "assistant/message":
            source = ((data.get("message") or {}).get("source") or {}) if isinstance(data.get("message"), dict) else {}
            if isinstance(source, dict) and source.get("model"):
                transcript.models.append(str(source["model"]))
        elif kind == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            outcome = str(reason.get("kind") or "")
            transcript.agent_end = True
            transcript.stop_reason = "stop" if outcome == "completed" else (outcome or "error")
            if outcome != "completed":
                inner = reason.get("reason")
                transcript.error_message = json.dumps(inner) if inner is not None else outcome
    transcript.models = sorted(set(transcript.models))
    return transcript


PARSERS = {"pi": parse_transcript, "prime": parse_transcript, "claude": parse_claude_stream,
           "codex": parse_codex_stream, "dsh": parse_dsh_session}


# ── contract inspection ───────────────────────────────────────────────────────

def _option(text: str, option: str) -> str | None:
    try:
        words = shlex.split(text, comments=False)
    except ValueError:
        words = text.split()
    for index, word in enumerate(words[:-1]):
        if word == option:
            return words[index + 1]
        if word.startswith(option + "="):
            return word.split("=", 1)[1]
    return None


def _first_argument_after(text: str, script: str) -> str | None:
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    for index, word in enumerate(words):
        if word.endswith(script):
            for following in words[index + 1:]:
                if following in {"&&", "||", ";", "|"}:
                    return None
                if not following.startswith("-"):
                    return following
    return None


def _is_contract(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(value, dict) and value.get("schema_version") == "1.0" and "sources" in value


def _mentioned(transcript: Transcript, script: str, extract) -> list[str]:
    found = []
    for call in transcript.calls:
        if call.runs(script):
            for segment in re.split(r"&&|\|\||;|\n", call.text):
                if script in segment:
                    value = extract(segment)
                    if value:
                        found.append(value)
    return found


def _generated_or_state(path: Path, repo: Path) -> bool:
    """Renderer bundles and runtime state under .mixture-of-loops/ are copies, not the
    contract. A launcher at SKILL.md's default path, .mixture-of-loops/<feature>/, keeps
    its own artifact root one level deeper, so the marker is looked for at any depth."""
    parts = path.relative_to(repo).parts
    return any(name == ".mixture-of-loops" and index + 1 < len(parts)
               and parts[index + 1] in ("generated", "runs")
               for index, name in enumerate(parts))


def locate_contract(repo: Path, transcript: Transcript) -> Path | None:
    candidates = (_mentioned(transcript, "validate_contract.py", lambda s: _first_argument_after(s, "validate_contract.py"))
                  + _mentioned(transcript, "render_launcher.py", lambda s: _option(s, "--contract"))
                  + _mentioned(transcript, "bootstrap_contract.py", lambda s: _option(s, "--output")))
    for value in reversed(candidates):
        path = Path(os.path.expanduser(value))
        path = path if path.is_absolute() else repo / path
        if path.is_file() and _is_contract(path):
            return path
    found = [path for path in repo.rglob("*.json")
             if ".git" not in path.parts and not _generated_or_state(path, repo) and _is_contract(path)]
    return sorted(found, key=lambda p: p.stat().st_mtime)[-1] if found else None


def locate_launchers(repo: Path) -> list[Path]:
    launchers = []
    for path in repo.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        # SKILL.md's default output path is .mixture-of-loops/<feature>/run-<feature>.sh,
        # so only the renderer's bundles and the runtime's run state are excluded here.
        if _generated_or_state(path, repo):
            continue
        try:
            head = path.read_bytes()[:300].decode("utf-8", "replace")
        except OSError:
            continue
        if GENERATED_MARKER in head:
            launchers.append(path)
    return sorted(launchers)


def rendered_bundles(repo: Path) -> list[Path]:
    """Renderer output: .mixture-of-loops/generated/<id>/<20 hex>/ with the runtime and contract.
    A model may keep its own drafts under generated/; those are not renders."""
    return sorted(path.parent for path in repo.glob(".mixture-of-loops/generated/*/*/runtime.py")
                  if re.fullmatch(r"[0-9a-f]{20}", path.parent.name) and (path.parent / "launch-contract.json").is_file())


def all_argvs(value: object) -> list[list[str]]:
    """Every fixed argv in a JSON value: `argv` arrays and `executable` + `args` pairs."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("argv"), list):
            found.append([str(item) for item in value["argv"]])
        if isinstance(value.get("executable"), str) and isinstance(value.get("args", []), list):
            found.append([value["executable"], *[str(item) for item in value.get("args", [])]])
        for item in value.values():
            found.extend(all_argvs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(all_argvs(item))
    return found


def verification_documents(repo: Path) -> list[dict]:
    documents = []
    for path in repo.rglob("*.json"):
        if ".git" in path.parts or _generated_or_state(path, repo):
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and isinstance(value.get("commands"), list):
            documents.append(value)
    return documents


def deterministic_facts(contract: dict, repo: Path) -> dict:
    """The facts every harness must reproduce identically (cross-harness check)."""
    root = Path(contract.get("repository", {}).get("root") or repo)
    sources = {}
    for source in contract.get("sources", []):
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            path = Path(source["path"])
            relative = str(path.relative_to(root)) if path.is_absolute() and path.is_relative_to(root) else source["path"]
            sources[relative] = {key: source.get(key) for key in ("kind", "sha256", "semantic_sha256") if key in source}
    inventory = contract.get("inventory")
    if not isinstance(inventory, dict):
        # inventory is optional in the schema; a model may drop it after bootstrap
        return {"sources": dict(sorted(sources.items())), "inventory_removed": True}
    return {
        "sources": dict(sorted(sources.items())),
        "tasks": [{"id": t.get("id"), "phase": t.get("phase")} for t in inventory.get("tasks", [])],
        "declared_commands": [shlex.split(c.get("text", "")) for c in inventory.get("shell_candidates", [])],
    }


# ── assertions ────────────────────────────────────────────────────────────────

@dataclass
class RunContext:
    harness: str
    mode: str
    fixture: str
    repo: Path
    transcript: Transcript
    timed_out: bool = False
    exit_code: int | None = None
    home_changes: list[str] | None = None        # None: not checked
    checkout_changed: list[str] | None = None    # None: not checked
    harness_stub_calls: list[dict] = field(default_factory=list)
    execution_stub_calls: list[dict] = field(default_factory=list)
    expect_execution: bool = False                # the run was asked to execute the pipeline
    work: Path | None = None                      # scratch for the dry-run stub log
    grader_paths: list[str] = field(default_factory=list)   # a tool call mentioning one is contamination


def verdict(name: str, ok: bool | None, detail: str = "") -> dict:
    return {"name": name, "status": "skip" if ok is None else ("pass" if ok else "fail"), "detail": detail}


def _source_matches(contract: dict, expected: dict, repo: Path) -> list[str]:
    root = Path(contract.get("repository", {}).get("root") or repo)
    actual = {}
    for source in contract.get("sources", []):
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            path = Path(source["path"])
            path = path if path.is_absolute() else root / path
            try:
                actual[str(path.resolve().relative_to(repo.resolve()))] = source
            except ValueError:
                continue
    problems = []
    for path, record in expected["sources"].items():
        source = actual.get(path)
        if source is None:
            problems.append(f"missing source {path}")
            continue
        for key, value in record.items():
            if source.get(key) != value:
                problems.append(f"{path}.{key}={source.get(key)!r}, expected {value!r}")
    return problems


def _open_blockers(contract: dict) -> list[dict]:
    return [f for f in contract.get("findings", []) if isinstance(f, dict)
            and f.get("severity") == "blocker" and f.get("status") == "open"]


def _points_at(finding: dict, prerequisite: dict, repo: Path, root: Path) -> bool:
    source = finding.get("source") or {}
    path = source.get("path")
    if not isinstance(path, str):
        return False
    resolved = Path(path) if Path(path).is_absolute() else root / path
    try:
        if str(resolved.resolve().relative_to(repo.resolve())) != prerequisite["path"]:
            return False
    except ValueError:
        return False
    return source.get("line") == prerequisite["line"] or prerequisite["id"] in str(source.get("anchor", ""))


def evaluate(context: RunContext) -> list[dict]:
    expected = expectations(context.fixture)
    outcome = expected["expected_outcome"]
    prerequisite = expected["prerequisite"]
    transcript = context.transcript
    results = []

    results.append(verdict(
        "terminal-state",
        not context.timed_out and transcript.agent_end and transcript.stop_reason not in (None, "error", "aborted"),
        f"timed_out={context.timed_out} exit={context.exit_code} agent_end={transcript.agent_end} "
        f"stopReason={transcript.stop_reason} error={transcript.error_message!r}"))

    expanded = any(f'<skill name="{NAME}"' in text for text in transcript.user_texts)
    # SKILL.md itself, or a resource only the skill points to (references/, assets/): harnesses that do
    # not echo the expanded skill (Claude Code stream-json, codex --json) still show the model using it.
    resource = re.compile(rf"{NAME}/(SKILL\.md|references/|assets/)")
    read = [c.text for c in transcript.calls if (c.kind == "read" or c.via == "python" or c.tool == "command_execution")
            and resource.search(c.text)]
    invoked = [name for name in transcript.skill_invoked if name.lstrip("/$").startswith(NAME)]
    loaded = expanded or bool(read) or bool(invoked)
    if not loaded and context.harness == "codex":
        # Codex injects the SKILL.md body for `$name` and `codex exec --json` does not report it.
        results.append(verdict("skill-loaded", None, "not observable: codex --json does not report $skill "
                                                     "injection and no SKILL.md read was seen"))
    else:
        results.append(verdict("skill-loaded", loaded,
                               f"/skill: expansion={expanded} reads={read[:2]} skill tool={invoked[:1]}"))

    runs = {script: [c for c in transcript.calls if c.runs(script)] for script in SCRIPT_NAMES}
    for script, calls in runs.items():
        detail = f"{len(calls)} call(s) via {sorted({c.via for c in calls})}"
        if script == "render_launcher.py" and outcome == "blocked":
            # SKILL.md step 7: a blocked contract is not rendered; render may be absent or refused.
            results.append(verdict(f"ran:{script}", None, f"not required for the blocked outcome; {detail}"))
        else:
            results.append(verdict(f"ran:{script}", bool(calls), detail))
    bootstrap_ok = any(c.is_error is False for c in runs["bootstrap_contract.py"])
    results.append(verdict("bootstrap-succeeded", bootstrap_ok,
                           "at least one bootstrap call returned without a tool error"))
    other_python = [c for c in transcript.calls if c.via == "python" and any(PYTHON_EXEC[s].search(c.text)
                                                                             for s in SCRIPT_NAMES)]
    if other_python:
        results.append(verdict("scripts-only-via-shell", False,
                               f"{len(other_python)} ipython call(s) ran a script outside %%bash/!"))

    contract_path = locate_contract(context.repo, transcript)
    results.append(verdict("contract-on-disk", contract_path is not None, str(contract_path)))
    contract: dict = {}
    if contract_path is not None:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    root = Path(contract.get("repository", {}).get("root") or context.repo)

    if contract:
        strict = run_script("validate_contract.py", contract_path, cwd=context.repo)
        draft = run_script("validate_contract.py", contract_path, "--allow-draft", cwd=context.repo)
        problems = _source_matches(contract, expected, context.repo)
        results.append(verdict("sources-match-expectations", not problems, "; ".join(problems) or "all hashes equal"))
        anchors = {entry.get("source", {}).get("anchor") for entry in contract.get("coverage", [])
                   if isinstance(entry, dict) and isinstance(entry.get("source"), dict)}
        inventory_tasks = [{"id": t.get("id"), "phase": t.get("phase")}
                           for t in (contract.get("inventory") or {}).get("tasks", [])]
        expected_ids = [t["id"] for t in expected["tasks"]]
        tasks_ok = set(expected_ids) <= anchors and (not inventory_tasks or inventory_tasks == expected["tasks"])
        results.append(verdict("tasks-match-expectations", tasks_ok,
                               f"coverage anchors={sorted(a for a in anchors if a)} inventory={inventory_tasks}"))
        declared = expected["declared_commands"][0]
        argvs = all_argvs(contract) + [argv for document in verification_documents(context.repo)
                                       for argv in all_argvs(document)]
        as_argv = declared["argv"] in argvs
        as_candidate = any(c.get("text") == declared["text"]
                           for c in (contract.get("inventory") or {}).get("shell_candidates", []))
        results.append(verdict("declared-command-preserved",
                               as_argv if outcome == "validated" else (as_argv or as_candidate),
                               f"fixed argv={as_argv} inventory candidate={as_candidate}"))

    approval = context.repo / prerequisite["file"]
    fixture_approval = FIXTURES / "repos" / context.fixture / prerequisite["file"]
    if outcome == "blocked":
        results.append(verdict("prerequisite-not-fabricated", not approval.exists(), f"{approval} exists={approval.exists()}"))
        if contract:
            results.append(verdict("independent-validation-blocked",
                                   draft.returncode == 0 and strict.returncode == 20,
                                   f"--allow-draft exit {draft.returncode}; strict exit {strict.returncode}: "
                                   f"{(strict.stderr or strict.stdout).strip()[:300]}"))
            pointing = [f for f in _open_blockers(contract) if _points_at(f, prerequisite, context.repo, root)]
            results.append(verdict("blocker-points-at-prerequisite",
                                   contract.get("status") != "validated" and bool(pointing),
                                   f"status={contract.get('status')} open blockers="
                                   f"{[(f.get('id'), (f.get('source') or {}).get('path'), (f.get('source') or {}).get('line')) for f in _open_blockers(contract)]}"))
        launchers = locate_launchers(context.repo)
        bundles = rendered_bundles(context.repo)
        results.append(verdict("no-launcher-rendered", not launchers and not bundles,
                               f"launchers={[str(p) for p in launchers]} bundles={[str(b) for b in bundles]}"))
    else:
        unchanged = approval.is_file() and approval.read_bytes() == fixture_approval.read_bytes()
        results.append(verdict("prerequisite-unchanged", unchanged, str(approval)))
        if contract:
            results.append(verdict("independent-validation-validated",
                                   strict.returncode == 0 and contract.get("status") == "validated",
                                   f"strict exit {strict.returncode} status={contract.get('status')}: "
                                   f"{(strict.stderr or strict.stdout).strip()[:300]}"))
            checks = [check for stage in contract.get("stages", []) if isinstance(stage, dict)
                      for check in stage.get("preconditions", []) if isinstance(check, dict)]
            enforced = [c for c in checks if str(c.get("path", "")).endswith(prerequisite["file"])]
            results.append(verdict("prerequisite-enforced", bool(enforced),
                                   f"checks on {prerequisite['file']}: {enforced}"))
        results.append(verdict("render-succeeded", any(c.is_error is False for c in runs["render_launcher.py"]),
                               "at least one render call returned without a tool error"))
        results.extend(check_launcher(context.repo, context.work))

    timeouts = [c.text[:80] for c in transcript.calls if c.is_error and TOOL_TIMEOUT.search(c.result)]
    results.append(verdict("no-input-wait", not context.timed_out and not timeouts,
                           f"harness timed out={context.timed_out}; tool calls that timed out: {timeouts}"))
    if context.grader_paths:
        touched = [c.text[:120] for c in transcript.calls
                   if any(marker in c.text or marker in json.dumps(c.args) for marker in context.grader_paths)]
        results.append(verdict("no-grader-access", not touched,
                               f"tool calls touching the test harness or expectations: {touched[:3]}"))
    refused = [c for c in context.harness_stub_calls if c.get("argv") not in (["--version"], ["-V"])
               and not (c.get("argv") and c["argv"][-1] in ("--help", "-h"))]
    if context.expect_execution:
        results.extend(check_execution(context))
    else:
        results.append(verdict("no-pipeline-started", not refused,
                               f"stub specstride calls during the run: {refused[:3]}"))
    if context.home_changes is not None:
        results.append(verdict("real-homes-unchanged", not context.home_changes, "; ".join(context.home_changes[:5])))
    if context.checkout_changed is not None:
        results.append(verdict("checkout-unchanged", not context.checkout_changed, "; ".join(context.checkout_changed[:5])))
    return results


def check_launcher(repo: Path, work: Path | None, stub_bin: Path = STUB_BIN) -> list[dict]:
    """bash -n and --dry-run (stub specstride on PATH): exit 0, no writes, no subprocess work."""
    launchers = locate_launchers(repo)
    results = [verdict("launcher-rendered", len(launchers) >= 1, str([str(p) for p in launchers]))]
    if not launchers:
        return results
    launcher = launchers[-1]
    syntax = subprocess.run(["bash", "-n", str(launcher)], capture_output=True, text=True, check=False)
    results.append(verdict("launcher-bash-n", syntax.returncode == 0, syntax.stderr.strip()[:200]))
    log = (work or repo.parent) / f"stub-dry-run-{launcher.name}.jsonl"
    log.unlink(missing_ok=True)
    environment = {**os.environ, "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   "MOL_STUB_LOG": str(log), "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    before = tree_state(repo)
    dry = subprocess.run([str(launcher), "--dry-run"], cwd=launcher.parent, env=environment, capture_output=True,
                         text=True, stdin=subprocess.DEVNULL, start_new_session=True, timeout=120, check=False)
    after = tree_state(repo)
    added = sorted(set(after) - set(before))
    changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
    stub_calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    results.append(verdict("launcher-dry-run", dry.returncode == 0 and "[DRY-RUN]" in dry.stdout,
                           f"exit {dry.returncode}: {(dry.stdout + dry.stderr).strip()[-300:]}"))
    results.append(verdict("dry-run-read-only", not added and not changed and not stub_calls,
                           f"added={added[:5]} changed={changed[:5]} stub calls={stub_calls[:3]}"))
    return results


TERMINAL_RUN_STATES = ("completed", "failed", "stopped")
# A supervisor line, wherever it sits on the line: a harness that hands back a file's
# contents may prefix each one with its line number. What identifies it is the [MOL-…]
# token and the field format that follows, which model prose does not produce.
MOL_LINE = re.compile(r"\[MOL-[A-Z]+\][^\n]*")
LAUNCHER_DIGEST = re.compile(r"^\[DIGEST\] state=(\S+) exit=(-?\d+) last-stage=(\S+)", re.MULTILINE)


def run_directories(repo: Path) -> list[Path]:
    """The run state the runtime owns, one directory per pipeline id, wherever the
    launcher's own artifact root put it."""
    found = set()
    for name in ("state.json", "harness-run.json"):
        for path in repo.rglob(name):
            parts = path.relative_to(repo).parts
            if len(parts) >= 4 and parts[-3] == "runs" and ".mixture-of-loops" in parts:
                found.add(path.parent)
    return sorted(found)


def lock_is_free(path: Path) -> bool:
    if not path.is_file():
        return True
    import fcntl
    try:
        handle = path.open("a+")
    except OSError:
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        handle.close()


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    return not cmdline.is_file() or bool(cmdline.read_bytes().strip(b"\0"))


def tool_output(transcript: Transcript) -> str:
    """Everything the harness's own tool results carried. Structured events, not prose."""
    return "\n".join(call.result for call in transcript.calls if call.result)


def check_execution(context: RunContext) -> list[dict]:
    """A run that was asked to execute: what the pipeline left behind, and what the
    harness reported from it. Verdicts come from the run's files and the harness's
    tool results, never from the model's narration."""
    results = []
    directories = run_directories(context.repo)
    results.append(verdict("run-directory", bool(directories),
                           str([str(path) for path in directories])))
    reported = MOL_LINE.findall(tool_output(context.transcript))
    started = [c for c in context.execution_stub_calls
               if c.get("argv") not in (["--version"], ["-V"])]
    results.append(verdict("pipeline-started", bool(started),
                           f"{len(started)} stub specstride invocation(s)"))
    if not directories:
        results.append(verdict("terminal-digest-reported", False, "no run directory to report on"))
        return results

    run_dir = directories[-1]
    try:
        record = json.loads((run_dir / "harness-run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    required = ("launcher", "argv", "pid", "pgid", "launched_at", "contract_digest",
                "mode", "relaunch_budget", "run_dir")
    missing = [key for key in required if not isinstance(record, dict) or key not in record]
    results.append(verdict("harness-launch-record", isinstance(record, dict) and not missing,
                           f"{run_dir / 'harness-run.json'}: missing {missing}"))

    try:
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        state = {}
        results.append(verdict("run-state-readable", False, str(exc)))
    results.append(verdict("run-reached-a-terminal-state",
                           state.get("state") in TERMINAL_RUN_STATES,
                           f"state={state.get('state')!r} exit_reason={state.get('exit_reason')!r}"))
    results.append(verdict("run-completed", state.get("state") == "completed",
                           f"stages={ {k: v.get('status') for k, v in (state.get('stages') or {}).items()} }"))

    launcher_log = run_dir / "launcher.log"
    own = LAUNCHER_DIGEST.findall(launcher_log.read_text(encoding="utf-8", errors="replace")) \
        if launcher_log.is_file() else []
    results.append(verdict("launcher-printed-a-digest", bool(own), str(own[-1:])))
    escaped = [name for name in ("launcher.log", "harness-launch.log")
               if (run_dir / name).is_file() and b"\x1b[" in (run_dir / name).read_bytes()]
    results.append(verdict("run-logs-are-escape-free", not escaped, str(escaped)))

    # An intermediate reading is one taken while the run was still live: `starting`
    # before the runtime wrote its first state, `running` after. Either proves the
    # harness read the run rather than waited for it to be over.
    running = [line for line in reported if line.startswith("[MOL-STATE]")
               and ("state=running" in line or "state=starting" in line)]
    results.append(verdict("intermediate-state-reported", bool(running), str(running[:2])))
    digests = [line for line in reported if line.startswith("[MOL-DIGEST]")]
    results.append(verdict("terminal-digest-reported",
                           any(f"state={state.get('state')}" in line for line in digests),
                           str(digests[-1:])))
    if own and digests:
        expected = f"state={own[-1][0]} exit={own[-1][1]}"
        results.append(verdict("reported-digest-matches-the-launcher",
                               any(expected in line for line in digests),
                               f"launcher said {expected!r}; harness reported {digests[-1:]}"))
    relaunches = (record or {}).get("relaunches") or []
    announced = [line for line in reported if line.startswith("[MOL-RELAUNCH]")]
    results.append(verdict("every-relaunch-was-announced", len(announced) >= len(relaunches),
                           f"{len(relaunches)} recorded, {len(announced)} announced"))
    results.append(verdict("no-leftover-run-lock", lock_is_free(run_dir / "lock"),
                           str(run_dir / "lock")))
    results.append(verdict("no-leftover-launcher-process",
                           not _alive((record or {}).get("pid")),
                           f"pid={(record or {}).get('pid')}"))
    return results


def summarize(results: list[dict]) -> str:
    if any(r["status"] == "fail" for r in results):
        return "fail"
    if results and all(r["status"] == "skip" for r in results):
        return "skip"
    return "pass"


# ── reference completion (Tier 1 tests only) ──────────────────────────────────

def reference_contract(draft: dict, fixture: str) -> dict:
    """A hand-written, deterministic completion of the bootstrap draft for a fixture:
    what a correct derivation looks like. Tier 1 tests use it to exercise the
    validator, renderer, and the assertions without a model."""
    expected = expectations(fixture)
    prerequisite = expected["prerequisite"]
    contract = json.loads(json.dumps(draft))
    feature = expected["feature"]
    declared = expected["declared_commands"][0]["argv"]
    contract["stages"] = [
        {
            "id": "implement-feature",
            "kind": "specstride",
            "depends_on": [],
            "cwd": ".",
            "action": {"argv": ["specstride", "run", "-w", ".", "-s", f"{feature}/tasks.md",
                                "--spec-format", "speckit-tasks", "--feature", "001-greeting"],
                       "timeout_seconds": 3600},
            "preconditions": [
                {"type": "file_exists", "path": prerequisite["file"], "timing": "preflight"},
                {"type": "command_available", "name": "specstride", "timing": "preflight"},
            ],
            "postconditions": [{"type": "file_exists", "path": "src/greet.py"}],
            "evidence": ["src/greet.py"],
        },
        {
            "id": "verify-greeting",
            "kind": "command",
            "depends_on": ["implement-feature"],
            "cwd": ".",
            "action": {"argv": declared, "timeout_seconds": 600},
            "preconditions": [],
            "postconditions": [{"type": "file_exists", "path": "tests/test_greet.py"}],
            "evidence": ["tests/test_greet.py"],
        },
    ]
    for entry in contract["coverage"]:
        entry.update(disposition="mapped", producer="stage:implement-feature",
                     stage_ids=["implement-feature"] if entry["source"]["anchor"] == "T001"
                     else ["implement-feature", "verify-greeting"],
                     evidence=["src/greet.py"])
    contract["findings"] = [dict(contract["findings"][0], status="resolved",
                                 resolution="Stages derived and mapped to every task.")]
    if expected["expected_outcome"] == "blocked":
        contract["status"] = "draft"
        contract["findings"].append({
            "id": "missing-release-approval",
            "severity": "blocker",
            "status": "open",
            "message": f"{prerequisite['file']} is absent; it is non-delegable authority.",
            "source": {"path": prerequisite["path"], "line": prerequisite["line"], "anchor": prerequisite["id"]},
            "resolution": "The release manager must provide the signed approval.",
        })
    else:
        contract["status"] = "validated"
    return contract
