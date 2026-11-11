#!/usr/bin/env python3
"""Edit the age-encrypted secrets file without leaving plaintext behind (R-9.9).

Usage: ``secrets_edit.py <secrets.age> --identity <path> [--recipient <age1...>]``

The file is decrypted into a mode-0600 temporary file (in ``/dev/shm`` when it
exists), ``$EDITOR`` (default ``vi``) opens it, and the edited YAML mapping is
re-encrypted and atomically moved over the destination. The temporary plaintext
file is overwritten with zeros and removed in a ``finally`` block, including on
errors and Ctrl-C.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information
import yaml

SHM_DIR = Path("/dev/shm")  # noqa: S108 - RAM-backed plaintext temp file is required (R-9.9)


def main(argv: list[str] | None = None) -> int:
    """Run the editor flow and return a process exit code."""
    parser = argparse.ArgumentParser(description="Edit an age-encrypted secrets file in place.")
    parser.add_argument("path", type=Path, help="encrypted secrets file (created if absent)")
    parser.add_argument("--identity", type=Path, required=True, help="age x25519 identity file")
    parser.add_argument(
        "--recipient",
        help="age public key to encrypt to (default: the identity's public key)",
    )
    args = parser.parse_args(argv)

    try:
        identity = _load_identity(args.identity)
        recipient = (
            pyrage.x25519.Recipient.from_str(args.recipient) if args.recipient else identity.to_public()
        )
        secrets = _decrypt(args.path, identity)
        _edit(args.path, secrets, recipient)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _load_identity(identity_path: Path) -> Any:
    """Parse an age x25519 identity file, ignoring comments and blank lines."""
    text = identity_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return pyrage.x25519.Identity.from_str(candidate)
    raise ValueError(f"no age identity found in {identity_path}")


def _decrypt(path: Path, identity: Any) -> dict[str, str]:
    """Decrypt an existing file into a mapping, or return an empty map."""
    if not path.exists():
        return {}
    plaintext = pyrage.decrypt(path.read_bytes(), [identity])
    return _parse_mapping(plaintext.decode("utf-8"))


def _parse_mapping(text: str) -> dict[str, str]:
    """Require the edited text to be a YAML mapping of ``str`` to ``str``."""
    document = yaml.safe_load(text)
    if not isinstance(document, Mapping):
        raise ValueError("secrets must be a YAML mapping of str to str")
    result: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("secrets must map str keys to str values")
        result[key] = value
    return result


def _edit(destination: Path, secrets: dict[str, str], recipient: Any) -> None:
    """Open the editor on a private temp file, then re-encrypt the result."""
    temp_dir = SHM_DIR if SHM_DIR.is_dir() else None
    fd, name = tempfile.mkstemp(dir=temp_dir, prefix="invoicebot-secrets-", suffix=".yaml")
    temp_path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(secrets, sort_keys=True) if secrets else "{}\n")
        editor = os.environ.get("EDITOR") or "vi"
        # $EDITOR is operator-controlled, so passing it through shlex is intended;
        # there is no shell and no untrusted input on the command line (R-9.9).
        subprocess.run([*shlex.split(editor), str(temp_path)], check=True)  # noqa: S603
        updated = _parse_mapping(temp_path.read_text(encoding="utf-8"))
        _write_encrypted(destination, updated, recipient)
    finally:
        _shred(temp_path)


def _write_encrypted(destination: Path, secrets: dict[str, str], recipient: Any) -> None:
    """Encrypt to a sibling temp file and atomically replace the destination."""
    payload = yaml.safe_dump(secrets, sort_keys=True).encode("utf-8")
    ciphertext = pyrage.encrypt(payload, [recipient])
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    temp_path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(ciphertext)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _shred(path: Path) -> None:
    """Overwrite a plaintext temp file with zeros and unlink it, best effort."""
    try:
        if path.exists():
            size = path.stat().st_size
            with path.open("r+b") as handle:
                handle.write(b"\0" * size)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
