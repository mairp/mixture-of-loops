#!/usr/bin/env python3
"""Validate a mixture-of-loops launch contract and its source bindings.

`--promote` is how a contract becomes `validated`: the file's status is never edited by
hand. It validates the contract as if it were `validated`; when that passes it writes
`validated` into the file, and when it does not it writes `draft` back (undoing a
hand-set `validated`), prints every blocker, and exits 20. A contract with an open
blocker therefore always stays a draft.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

from contract_lib import ContractError, StaleSourceError, load_contract, validate_contract, warn_if_not_shell_invoked


def write_status(path: Path, contract: dict, status: str) -> None:
    contract["status"] = status
    text = json.dumps(contract, indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False)
    with handle:
        handle.write(text)
    os.replace(handle.name, path)


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
        return 20
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.promote and original.get("status") != "validated":
        write_status(path, original, "validated")
        print(f"promoted to validated: {contract.get('id')}")
    state = contract.get("status", "unknown")
    print(f"valid {state} launch contract: {contract.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
