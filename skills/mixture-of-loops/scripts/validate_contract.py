#!/usr/bin/env python3
"""Validate a mixture-of-loops launch contract and its source bindings.

`--promote` is how a contract becomes `validated`: the file's status is never edited by
hand. It validates the contract as if it were `validated`; when that passes it writes
`validated` into the file, and when it does not it writes `draft` back (undoing a
hand-set `validated`), prints every blocker, and exits 20. A contract with an open
blocker therefore always stays a draft. `validated` is written with a `promotion` stamp
over the rest of the contract, and render_launcher.py renders nothing without a matching
one: a hand-typed status, or an edit after promotion, needs --promote again.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

from contract_lib import (ContractError, StaleSourceError, load_contract, promotion_digest, stamp_promotion,
                          validate_contract, warn_if_not_shell_invoked)


def write_status(path: Path, contract: dict, status: str) -> None:
    """Write `status`; `validated` carries the stamp render_launcher.py checks, `draft` drops it."""
    contract["status"] = status
    contract.pop("promotion", None)
    if status == "validated":
        stamp_promotion(contract)
    text = json.dumps(contract, indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False)
    with handle:
        handle.write(text)
    os.replace(handle.name, path)


def derivation_hints(contract: dict) -> list[str]:
    """What the draft itself says is still undone, for a strict check that failed.

    A refusal on the bootstrap's own placeholder is not the blocked outcome: derivation
    has not happened yet. And an absent prerequisite the bootstrap inventoried, with no
    open blocker at its own line, is a decision still to be written down. Existence is
    the bootstrap's fact; whether the item is non-delegable authority stays the model's.
    """
    findings = [f for f in contract.get("findings", []) if isinstance(f, dict)]
    open_blockers = [f for f in findings if f.get("severity") == "blocker" and f.get("status") == "open"]
    hints = []
    if any(f.get("id") == "semantic-derivation-required" for f in open_blockers):
        hints.append("the bootstrap placeholder `semantic-derivation-required` is still open: derivation "
                     "(SKILL.md steps 4-7) is not done, so this refusal is not yet a blocked outcome")
    anchored = {(src.get("path"), src.get("line")) for f in open_blockers
                if isinstance(src := f.get("source"), dict)}
    for entry in (contract.get("inventory") or {}).get("prerequisites", []):
        if not isinstance(entry, dict) or entry.get("present") is not False:
            continue
        source = entry.get("source") or {}
        where = (source.get("path"), source.get("line"))
        if where not in anchored:
            hints.append(f"{entry.get('id') or 'a prerequisite'} names `{entry.get('path')}`, which is absent "
                         f"({where[0]}:{where[1]}), and no open blocker has that source: if it is a "
                         "non-delegable authority, record it there (SKILL.md step 6)")
    return hints


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("contract")
    parser.add_argument("--allow-draft", action="store_true", help="validate structure without execution readiness")
    parser.add_argument("--no-source-check", action="store_true", help="skip source hash comparison")
    parser.add_argument("--promote", action="store_true",
                        help="set status to validated if and only if strict validation passes; "
                             "otherwise set it back to draft and report the blockers")
    args = parser.parse_args()
    warn_if_not_shell_invoked("validate_contract.py")
    if args.promote and args.allow_draft:
        parser.error("--promote and --allow-draft are exclusive: promotion is the strict check")
    path = Path(args.contract)
    original: dict = {}  # an unreadable contract owes nothing we can name
    try:
        original = load_contract(args.contract)
        contract = copy.deepcopy(original)
        if args.promote:
            contract["status"] = "validated"
        warnings = validate_contract(
            contract,
            allow_draft=args.allow_draft,
            check_sources=not args.no_source_check,
        )
    except StaleSourceError as exc:
        print(f"stale contract sources:\n{exc}", file=sys.stderr)
        return 23
    except ContractError as exc:
        if args.promote and original.get("status") != "draft":
            write_status(path, original, "draft")
            print("status set back to draft: the contract does not pass strict validation", file=sys.stderr)
        print(f"invalid launch contract:\n{exc}", file=sys.stderr)
        for hint in derivation_hints(original):
            print(f"next: {hint}", file=sys.stderr)
        return 20
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    stamp = original.get("promotion")
    stamped = isinstance(stamp, dict) and stamp.get("sha256") == promotion_digest(original)
    if args.promote and not (original.get("status") == "validated" and stamped):
        write_status(path, original, "validated")
        print(f"promoted to validated: {contract.get('id')}")
    state = contract.get("status", "unknown")
    print(f"valid {state} launch contract: {contract.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
