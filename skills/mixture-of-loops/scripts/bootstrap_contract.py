#!/usr/bin/env python3
"""Create a provenance-bound draft launch contract from Spec Kit artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


ARTIFACTS = (
    "spec.md",
    "plan.md",
    "tasks.md",
    "research.md",
    "data-model.md",
    "quickstart.md",
    "verification-commands.json",
)
PHASE_RE = re.compile(r"^##\s+Phase\s+(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE)
TASK_RE = re.compile(
    r"^-\s*\[([ xX])\]\s+(T\d+[A-Za-z]?)\s+(?:\[P\]\s+)?(?:\[([^\]]+)\]\s+)?(.*)$"
)
FENCE_RE = re.compile(r"^```(?:bash|sh|shell|console)?\s*$", re.IGNORECASE)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def semantic_sha256(path: Path, kind: str) -> str:
    """Hash requirements while ignoring task checkbox progress only."""
    content = path.read_text(encoding="utf-8")
    if kind == "tasks":
        content = re.sub(r"(?m)^(\s*-\s*\[)[ xX](\]\s+T\d+[A-Za-z]?\b)", r"\1 \2", content)
    return hashlib.sha256(content.encode()).hexdigest()


def relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def git_state(root: Path) -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if revision.returncode:
            return None, None
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return revision.stdout.strip(), None if dirty.returncode else bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None


def resolve_features(root: Path, requested: list[str]) -> list[Path]:
    if requested:
        result = []
        for value in requested:
            path = Path(value)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            if path.is_file():
                path = path.parent
            if not path.is_dir():
                raise ValueError(f"feature path is not a directory: {value}")
            result.append(path)
        return result

    if (root / "tasks.md").is_file() or (root / "spec.md").is_file():
        return [root]
    candidates = sorted(
        path.resolve()
        for path in (root / "specs").glob("*")
        if path.is_dir() and ((path / "tasks.md").is_file() or (path / "spec.md").is_file())
    )
    if len(candidates) != 1:
        rendered = ", ".join(relative_or_absolute(path, root) for path in candidates) or "none"
        raise ValueError(
            "feature is ambiguous; pass --feature for each in-scope feature "
            f"(candidates: {rendered})"
        )
    return candidates


def source_files(root: Path, features: list[Path]) -> list[tuple[Path, str]]:
    found: dict[Path, str] = {}
    constitution = root / ".specify" / "memory" / "constitution.md"
    if constitution.is_file():
        found[constitution.resolve()] = "constitution"
    for feature in features:
        for name in ARTIFACTS:
            path = feature / name
            if path.is_file():
                found[path.resolve()] = name.rsplit(".", 1)[0]
        for dirname, kind in (("contracts", "contract"), ("checklists", "checklist")):
            directory = feature / dirname
            if directory.is_dir():
                for path in directory.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        found[path.resolve()] = kind
    return sorted(found.items(), key=lambda item: str(item[0]))


def read_tasks(path: Path, root: Path) -> tuple[list[dict], list[dict]]:
    phases: list[dict] = []
    tasks: list[dict] = []
    phase: str | None = None
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = PHASE_RE.match(raw)
        if match:
            phase = match.group(1)
            phases.append({"number": int(phase), "title": match.group(2), "line": line_number})
            continue
        match = TASK_RE.match(raw)
        if not match:
            continue
        parallel = "[P]" in raw[: raw.find(match.group(4)) if match.group(4) else len(raw)]
        tasks.append(
            {
                "id": match.group(2),
                "phase": int(phase) if phase else None,
                "line": line_number,
                "checked": match.group(1).lower() == "x",
                "parallel_marker": parallel,
                "story": match.group(3),
                "text": match.group(4).strip(),
                "source": relative_or_absolute(path, root),
            }
        )
    return phases, tasks


def shell_candidates(path: Path, root: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    candidates: list[dict] = []
    start: int | None = None
    body: list[str] = []
    for line_number, raw in enumerate(lines, 1):
        if start is None:
            if FENCE_RE.match(raw):
                start = line_number + 1
                body = []
        elif raw.strip() == "```":
            text = "\n".join(body).strip()
            if text:
                candidates.append(
                    {
                        "path": relative_or_absolute(path, root),
                        "start_line": start,
                        "end_line": line_number - 1,
                        "text": text,
                        "classification": "candidate-only",
                    }
                )
            start = None
            body = []
        else:
            body.append(raw)
    return candidates


def verification_document(path: Path, root: Path) -> dict:
    record: dict = {"path": relative_or_absolute(path, root), "valid_json": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        record["error"] = str(exc)
        return record
    record["valid_json"] = isinstance(value, dict)
    if isinstance(value, dict):
        record["feature"] = value.get("feature")
        record["phase_map"] = value.get("phase_map")
        commands = value.get("commands")
        if isinstance(commands, list):
            record["commands"] = commands
    return record


def stable_obligation_id(task: dict) -> str:
    seed = f"{task['source']}:{task['line']}:{task['id']}:{task['text']}"
    return "OBL-" + hashlib.sha256(seed.encode()).hexdigest()[:12].upper()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_contract(root: Path, features: list[Path]) -> dict:
    sources = []
    all_tasks = []
    feature_records = []
    candidates = []
    verification = []
    for path, kind in source_files(root, features):
        try:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
        except UnicodeDecodeError:
            line_count = 0
        source = {
            "path": relative_or_absolute(path, root),
            "kind": kind,
            "sha256": sha256(path),
            "lines": line_count,
        }
        if kind == "tasks":
            source["semantic_sha256"] = semantic_sha256(path, kind)
        sources.append(source)
        if path.name in {"plan.md", "quickstart.md"}:
            candidates.extend(shell_candidates(path, root))
        if path.name == "verification-commands.json":
            verification.append(verification_document(path, root))

    for feature in features:
        phases: list[dict] = []
        tasks: list[dict] = []
        tasks_path = feature / "tasks.md"
        if tasks_path.is_file():
            phases, tasks = read_tasks(tasks_path.resolve(), root)
            all_tasks.extend(tasks)
        feature_records.append(
            {
                "path": relative_or_absolute(feature, root),
                "slug": feature.name,
                "phases": phases,
                "task_count": len(tasks),
                "artifacts_present": [name for name in ARTIFACTS if (feature / name).is_file()],
                "artifacts_absent": [name for name in ARTIFACTS if not (feature / name).is_file()],
            }
        )

    revision, dirty = git_state(root)
    identifier = "-and-".join(feature.name.lower() for feature in features)
    identifier = re.sub(r"[^a-z0-9]+", "-", identifier).strip("-") or "pipeline"
    coverage = []
    for task in all_tasks:
        coverage.append(
            {
                "id": stable_obligation_id(task),
                "source": {"path": task["source"], "line": task["line"], "anchor": task["id"]},
                "kind": "implementation",
                "timing": f"wiggum-phase:{task['phase']}" if task["phase"] else "unclassified",
                "producer": "unclassified",
                "disposition": "unresolved",
                "stage_ids": [],
                "verification_ids": [],
                "rationale": task["text"],
                "evidence": [],
            }
        )
    source = coverage[0]["source"] if coverage else {
        "path": sources[0]["path"] if sources else ".",
        "line": 1,
        "anchor": "artifact-set",
    }
    return {
        "schema_version": "1.0",
        "id": identifier,
        "status": "draft",
        "generated_by": {"tool": "mixture-of-loops/bootstrap_contract.py", "version": "1.0"},
        "repository": {"root": str(root), "revision": revision, "dirty": dirty},
        "authorized_roots": [str(root)],
        "features": feature_records,
        "sources": sources,
        "inventory": {
            "tasks": all_tasks,
            "shell_candidates": candidates,
            "verification_documents": verification,
        },
        "configuration": {
            "color": "auto",
            "wiggum_live": True,
            "agent_stream": True,
            "live_detail": "full",
            "telemetry": {"loki": {"enabled": False}, "otel": {"enabled": False}},
        },
        "coverage": coverage,
        "findings": [
            {
                "id": "semantic-derivation-required",
                "severity": "blocker",
                "status": "open",
                "message": "Classify every execution-relevant obligation and construct the stage graph.",
                "source": source,
                "resolution": "Complete semantic derivation, reconcile verification, and validate the contract.",
            }
        ],
        "stages": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="repository root")
    parser.add_argument("--feature", action="append", default=[], help="feature directory; repeatable")
    parser.add_argument("--output", default="-", help="output JSON path, or - for stdout")
    args = parser.parse_args()
    root = Path(args.repo).resolve()
    if not root.is_dir():
        parser.error(f"repository is not a directory: {root}")
    try:
        features = resolve_features(root, args.feature)
        contract = build_contract(root, features)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output == "-":
        print(rendered, end="")
    else:
        write_atomic(Path(args.output).resolve(), rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
