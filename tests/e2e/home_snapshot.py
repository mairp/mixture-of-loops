#!/usr/bin/env python3
"""Snapshot the real harness homes so a test can prove it left them untouched.

The snapshot lists every path under ~/.pi, ~/.prime, ~/.agents, ~/.dsh and ~/.claude/skills
(which covers ~/.pi/agent/trust.json and ~/.prime/supervisor-owners/) with its type,
its link target or its SHA-256. Symlinks are recorded, never followed. The real home
comes from the password database, so a temporary HOME in the environment cannot
redirect it.

Usage:
  home_snapshot.py take OUTPUT.json
  home_snapshot.py compare BEFORE.json [AFTER.json]   # without AFTER, takes a fresh one
Exit status: 0 no difference, 1 differences (listed on stdout), 2 usage error.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import sys

RELATIVE_ROOTS = (".pi", ".prime", ".agents", ".dsh", ".claude/skills")


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def roots(home: Path | None = None) -> list[Path]:
    base = home or real_home()
    return [base / relative for relative in RELATIVE_ROOTS]


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        return f"unreadable:{exc.__class__.__name__}"
    return digest.hexdigest()


def _entry(path: Path) -> str:
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_dir():
        return "dir"
    if path.is_file():
        return "file:" + _file_digest(path)
    return "other" if path.exists() else "missing"


def take(home: Path | None = None) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for root in roots(home):
        snapshot[str(root)] = _entry(root)
        if root.is_symlink() or not root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            for name in sorted(dirnames + filenames):
                path = Path(directory) / name
                snapshot[str(path)] = _entry(path)
    return dict(sorted(snapshot.items()))


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is None:
            changes.append(f"added    {path} ({new})")
        elif new is None:
            changes.append(f"removed  {path} ({old})")
        else:
            changes.append(f"changed  {path} ({old[:24]} -> {new[:24]})")
    return changes


def write(snapshot: dict[str, str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=0, sort_keys=True) + "\n", encoding="utf-8")


def load(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "take":
        snapshot = take()
        write(snapshot, Path(argv[1]))
        print(f"snapshot of {len(snapshot)} paths under {', '.join(map(str, roots()))} -> {argv[1]}")
        return 0
    if len(argv) in (2, 3) and argv[0] == "compare":
        before = load(Path(argv[1]))
        after = load(Path(argv[2])) if len(argv) == 3 else take()
        changes = diff(before, after)
        for line in changes:
            print(line)
        print(f"{len(changes)} difference(s) across {len(after)} paths")
        return 1 if changes else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
