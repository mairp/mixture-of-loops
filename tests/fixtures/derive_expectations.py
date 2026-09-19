#!/usr/bin/env python3
"""Derive each fixture's expected contract facts without importing the skill.

The facts come straight from the fixture files with the standard library, so the
Tier 1 comparison against bootstrap_contract.py checks two independent readings of
the same sources: raw SHA-256 per source, the checkbox-normalized SHA-256 for
tasks.md (launch-contract spec: a `- [x] T001` checkbox is progress, not a
requirement), task IDs and phases, the literally declared command and its argv,
the PRE-001 prerequisite's source line, and the one correct outcome.

Usage: derive_expectations.py [--check]   (--check: fail if a checked-in file differs)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

HERE = Path(__file__).resolve().parent
REPOS = HERE / "repos"
EXPECTATIONS = HERE / "expectations"
FEATURE = "specs/001-greeting"
KINDS = {"spec.md": "spec", "plan.md": "plan", "tasks.md": "tasks"}
PREREQUISITE_FILE = "approvals/release-approval.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive(repo: Path) -> dict:
    feature = repo / FEATURE
    sources = {}
    for name, kind in sorted(KINDS.items()):
        data = (feature / name).read_bytes()
        record = {"kind": kind, "sha256": sha256(data)}
        if kind == "tasks":
            text = data.decode("utf-8")
            unchecked = "\n".join(
                re.sub(r"^(\s*- \[)[xX](\] T\d+)", r"\1 \2", line) for line in text.split("\n"))
            record["semantic_sha256"] = sha256(unchecked.encode("utf-8"))
        sources[f"{FEATURE}/{name}"] = record

    phase = None
    tasks = []
    for line in (feature / "tasks.md").read_text(encoding="utf-8").splitlines():
        heading = re.match(r"## Phase (\d+):", line)
        if heading:
            phase = int(heading.group(1))
        task = re.match(r"- \[[ xX]\] (T\d+) ", line)
        if task:
            tasks.append({"id": task.group(1), "phase": phase})

    plan_lines = (feature / "plan.md").read_text(encoding="utf-8").splitlines()
    commands = []
    opened = None
    for number, line in enumerate(plan_lines, 1):
        if opened is None and line.strip() == "```bash":
            opened = number
        elif opened is not None and line.strip() == "```":
            text = "\n".join(plan_lines[opened:number - 1]).strip()
            commands.append({"path": f"{FEATURE}/plan.md", "start_line": opened + 1, "text": text,
                             "argv": shlex.split(text)})
            opened = None

    prerequisite = [(number, line) for number, line in enumerate(plan_lines, 1) if line.startswith("- PRE-001:")]
    assert len(prerequisite) == 1, "fixture must declare PRE-001 exactly once"
    present = (repo / PREREQUISITE_FILE).is_file()
    return {
        "fixture": repo.name,
        "feature": FEATURE,
        "sources": sources,
        "tasks": tasks,
        "declared_commands": commands,
        "prerequisite": {"id": "PRE-001", "path": f"{FEATURE}/plan.md", "line": prerequisite[0][0],
                         "file": PREREQUISITE_FILE, "present": present},
        "expected_outcome": "validated" if present else "blocked",
    }


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    EXPECTATIONS.mkdir(exist_ok=True)
    stale = []
    for repo in sorted(path for path in REPOS.iterdir() if path.is_dir()):
        rendered = json.dumps(derive(repo), indent=2, sort_keys=True) + "\n"
        target = EXPECTATIONS / f"{repo.name}.json"
        if check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                stale.append(str(target))
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target}")
    for path in stale:
        print(f"stale expectation: {path}", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
