"""Encrypted secrets with per-adapter scoping and a startup plaintext guard.

Secrets live in one ``age``-encrypted YAML file and are decrypted into memory
only (R-9.9). Each adapter declares the keys it needs and receives a
:class:`Secrets` view that refuses everything else (R-9.10); only ``sunat_sol``
may declare SOL credentials.

The startup guard, :func:`assert_no_plain_secrets`, rejects secret-looking
variables that were left in the process environment or in ``.env``, naming the
offending keys but never their values (R-9.9).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyrage  # type: ignore[import-untyped]  # pyrage ships no type information
import yaml

#: Adapter allowed to declare SOL credentials (R-9.10).
SOL_ADAPTER = "sunat_sol"
SOL_KEY_PREFIX = "sol_"

#: Environment suffixes that mark a value as secret (R-9.9), compared upper case.
SECRET_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_KEY")

#: Non-secret variables that match a suffix above. ``GPG_KEY`` is set by the
#: official Python container image and is not used by this application.
DEFAULT_ALLOW: frozenset[str] = frozenset({"GPG_KEY"})


class SecretError(Exception):
    """Base class for secrets failures."""


class SecretNotDeclared(SecretError):
    """The adapter asked for a secret it did not declare (R-9.10)."""


class SolSecretNotAllowed(SecretNotDeclared):
    """A non-SOL adapter declared a ``sol_*`` credential (R-9.10)."""


class SecretMissing(SecretError):
    """A declared secret is absent from the encrypted store."""


class PlainSecretError(SecretError):
    """Secret-looking values were found in plain text at startup (R-9.9)."""


class Secrets:
    """The subset of secrets one adapter declared (R-9.10).

    The view never exposes the store or the whole mapping; :meth:`get` is the
    only way to read a value. ``repr`` deliberately shows counts, not values.
    """

    def __init__(self, store: SecretStore, adapter_name: str, declared: frozenset[str]) -> None:
        self._store = store
        self._adapter_name = adapter_name
        self._declared = declared

    def get(self, key: str) -> str:
        """Return the value of ``key``, or raise if it is undeclared/missing."""
        normalized = key.lower()
        if normalized not in self._declared:
            raise SecretNotDeclared(
                f"adapter {self._adapter_name!r} did not declare secret {normalized!r} (R-9.10)"
            )
        value = self._store._lookup(normalized)  # noqa: SLF001 - same module owns the store
        if value is None:
            raise SecretMissing(
                f"secret {normalized!r} declared by {self._adapter_name!r} is missing (R-9.10)"
            )
        return value

    @property
    def missing(self) -> frozenset[str]:
        """Declared keys that the store does not hold."""
        return frozenset(key for key in self._declared if self._store._lookup(key) is None)

    @property
    def enabled(self) -> bool:
        """False when any declared secret is missing: adapters fail closed."""
        return not self.missing

    def __repr__(self) -> str:
        return (
            f"Secrets(adapter={self._adapter_name!r}, "
            f"declared={len(self._declared)}, missing={len(self.missing)})"
        )


class SecretStore:
    """In-memory secrets decrypted from one age file (R-9.9).

    The mapping is private: adapters receive a :class:`Secrets` view through
    :meth:`scoped`, and only the redaction filter (R-9.16) reads every value
    through :meth:`values`. ``repr`` shows the key count alone.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values: dict[str, str] = {key.lower(): value for key, value in values.items()}

    @classmethod
    def load(cls, path_age: Path, identity_path: Path) -> SecretStore:
        """Decrypt ``path_age`` with the x25519 identity in ``identity_path``.

        The plaintext must be a YAML mapping of string keys to string values;
        anything else raises :class:`ValueError`. Keys are lower-cased.
        """
        identity = _load_identity(Path(identity_path))
        ciphertext = Path(path_age).read_bytes()
        plaintext = pyrage.decrypt(ciphertext, [identity])
        document = yaml.safe_load(plaintext.decode("utf-8"))
        return cls(_validate_mapping(document))

    def scoped(self, adapter_name: str, declared: set[str]) -> Secrets:
        """Return the view for ``adapter_name``, restricted to ``declared`` (R-9.10)."""
        normalized = frozenset(key.lower() for key in declared)
        forbidden = sorted(key for key in normalized if key.startswith(SOL_KEY_PREFIX))
        if forbidden and adapter_name != SOL_ADAPTER:
            raise SolSecretNotAllowed(
                f"adapter {adapter_name!r} may not declare SOL credentials "
                f"({', '.join(forbidden)}); only {SOL_ADAPTER!r} may (R-9.10)"
            )
        return Secrets(self, adapter_name, normalized)

    def values(self) -> frozenset[str]:
        """Every secret value, for the logging redaction filter (R-9.16)."""
        return frozenset(self._values.values())

    def _lookup(self, key: str) -> str | None:
        return self._values.get(key.lower())

    def __repr__(self) -> str:
        return f"SecretStore(keys={len(self._values)})"


def _load_identity(identity_path: Path) -> Any:
    """Parse an age x25519 identity file, ignoring comments and blank lines."""
    text = identity_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return pyrage.x25519.Identity.from_str(candidate)
    raise ValueError(f"no age identity found in {identity_path}")


def _validate_mapping(document: object) -> dict[str, str]:
    """Require a YAML mapping of ``str`` to ``str`` without echoing values."""
    if not isinstance(document, dict):
        raise ValueError("secrets plaintext must be a YAML mapping of str to str")
    result: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("secrets plaintext must map str keys to str values")
        result[key] = value
    return result


def assert_no_plain_secrets(
    environ: Mapping[str, str],
    dotenv_path: Path | None = None,
    *,
    allow: frozenset[str] = DEFAULT_ALLOW,
) -> None:
    """Fail when a secret-looking value is still in plain text (R-9.9).

    Keys in ``environ`` and in ``dotenv_path`` (when given and present) that end
    in ``_TOKEN``, ``_SECRET``, ``_PASSWORD`` or ``_KEY`` and hold a non-empty
    value are rejected, except the names in ``allow``. The error lists only the
    offending key names, never their values.
    """
    allowed = {key.upper() for key in allow}
    offenders: set[str] = set()
    for name, value in environ.items():
        if _is_secret_name(name) and value.strip() and name.upper() not in allowed:
            offenders.add(name)
    if dotenv_path is not None and dotenv_path.exists():
        for name, value in _read_dotenv(dotenv_path).items():
            if _is_secret_name(name) and value and name.upper() not in allowed:
                offenders.add(name)
    if offenders:
        names = ", ".join(sorted(offenders))
        raise PlainSecretError(
            f"plaintext secrets found in the environment or .env: {names} (R-9.9); "
            "move them to the encrypted secrets file"
        )


def _is_secret_name(name: str) -> bool:
    return name.upper().endswith(SECRET_ENV_SUFFIXES)


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` file: comments, blanks and ``export `` ignored."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        values[name] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
