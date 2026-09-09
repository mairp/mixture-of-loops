#!/usr/bin/env python3
"""Validate a mixture-of-loops launch contract and its source bindings."""

from __future__ import annotations

import argparse
import sys

from contract_lib import ContractError, StaleSourceError, load_contract, validate_contract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract")
    parser.add_argument("--allow-draft", action="store_true", help="validate structure without execution readiness")
    parser.add_argument("--no-source-check", action="store_true", help="skip source hash comparison")
    args = parser.parse_args()
    try:
        contract = load_contract(args.contract)
        validate_contract(
            contract,
            allow_draft=args.allow_draft,
            check_sources=not args.no_source_check,
        )
    except StaleSourceError as exc:
        print(f"stale contract sources:\n{exc}", file=sys.stderr)
        return 23
    except ContractError as exc:
        print(f"invalid launch contract:\n{exc}", file=sys.stderr)
        return 20
    state = contract.get("status", "unknown")
    print(f"valid {state} launch contract: {contract.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
