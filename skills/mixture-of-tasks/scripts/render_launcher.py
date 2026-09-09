#!/usr/bin/env python3
"""Render and atomically publish an executable launcher and content-addressed bundle."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from contract_lib import ContractError, StaleSourceError, canonical_bytes, load_contract, validate_contract


DIGEST_RE = re.compile(r"^(# generated-content-sha256: )[0-9a-f]{64}$", re.MULTILINE)
PLACEHOLDER = "__GENERATED_CONTENT_DIGEST__"


def normalized_launcher_digest(text: str) -> str | None:
    match = DIGEST_RE.search(text)
    if not match:
        return None
    normalized = DIGEST_RE.sub(r"\1" + PLACEHOLDER, text, count=1)
    return hashlib.sha256(normalized.encode()).hexdigest()


def existing_launcher_is_intact(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    match = DIGEST_RE.search(text)
    return bool(match and normalized_launcher_digest(text) == match.group(0).split(": ", 1)[1])


def write_atomic_file(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def publish_bundle(contract: dict, output: Path, scripts_dir: Path) -> tuple[Path, str]:
    contract_content = canonical_bytes(contract)
    contract_hash = hashlib.sha256(contract_content).hexdigest()
    bundle_parent = output.parent / ".mixture-of-tasks" / "generated" / contract["id"]
    bundle = bundle_parent / contract_hash[:20]
    bundle_parent.mkdir(parents=True, exist_ok=True)
    if bundle.exists():
        existing = bundle / "launch-contract.json"
        if not existing.is_file() or hashlib.sha256(existing.read_bytes()).hexdigest() != contract_hash:
            raise ContractError(f"content-addressed bundle is inconsistent: {bundle}")
        return bundle, contract_hash

    temporary = Path(tempfile.mkdtemp(prefix=".publish-", dir=bundle_parent))
    try:
        (temporary / "launch-contract.json").write_bytes(contract_content)
        for name in ("runtime.py", "contract_lib.py"):
            source = scripts_dir / name
            if not source.is_file():
                raise ContractError(f"renderer resource is missing: {source}")
            shutil.copyfile(source, temporary / name)
        for path in temporary.iterdir():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        os.replace(temporary, bundle)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return bundle, contract_hash


def launcher_text(output: Path, bundle: Path, contract_hash: str) -> str:
    relative = os.path.relpath(bundle, output.parent)
    template = f"""#!/usr/bin/env bash
# mixture-of-tasks-generated: renderer=1.0 contract-sha256={contract_hash}
# generated-content-sha256: {PLACEHOLDER}
set -euo pipefail
SCRIPT_DIR=\"$(cd \"$(dirname \"${{BASH_SOURCE[0]}}\")\" && pwd)\"
BUNDLE_DIR=\"$SCRIPT_DIR/{relative}\"
exec python3 \"$BUNDLE_DIR/runtime.py\" \\
  --contract \"$BUNDLE_DIR/launch-contract.json\" \\
  --launch-dir \"$SCRIPT_DIR\" \"$@\"
"""
    digest = hashlib.sha256(template.encode()).hexdigest()
    return template.replace(PLACEHOLDER, digest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--replace-edited",
        action="store_true",
        help="explicitly replace an existing generated launcher whose digest changed",
    )
    args = parser.parse_args()
    try:
        contract = load_contract(args.contract)
        validate_contract(contract, allow_draft=False, check_sources=True)
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and not existing_launcher_is_intact(output) and not args.replace_edited:
            raise ContractError(
                f"refusing to replace edited or non-generated launcher: {output}; "
                "choose another path or pass --replace-edited explicitly"
            )
        scripts_dir = Path(__file__).resolve().parent
        bundle, contract_hash = publish_bundle(contract, output, scripts_dir)
        rendered = launcher_text(output, bundle, contract_hash)
        write_atomic_file(output, rendered.encode(), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                          | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    except StaleSourceError as exc:
        print(f"stale contract sources:\n{exc}", file=sys.stderr)
        return 23
    except (ContractError, OSError) as exc:
        print(f"cannot render launcher: {exc}", file=sys.stderr)
        return 20
    print(f"rendered {output}")
    print(f"bundle {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
