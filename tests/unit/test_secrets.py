"""Tests for encrypted secrets, per-adapter scoping and the startup guard (R-9.9, R-9.10)."""

from __future__ import annotations

from pathlib import Path

import pyrage
import pytest
import yaml

from invoicebot.secrets import (
    DEFAULT_ALLOW,
    PlainSecretError,
    SecretMissing,
    SecretNotDeclared,
    SecretStore,
    SolSecretNotAllowed,
    assert_no_plain_secrets,
)

VALUE_REFRESH = "synthetic-refresh-value"
VALUE_SOL = "synthetic-sol-value"
VALUE_OTHER = "synthetic-other-value"


def _write_store(tmp_path: Path, values: dict[str, str]) -> tuple[Path, Path]:
    """Encrypt ``values`` to a test age file and write the identity beside it."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(identity) + "\n", encoding="utf-8")
    store_path = tmp_path / "secrets.age"
    store_path.write_bytes(pyrage.encrypt(yaml.safe_dump(values).encode("utf-8"), [identity.to_public()]))
    return store_path, identity_path


def _loaded(tmp_path: Path, values: dict[str, str]) -> SecretStore:
    store_path, identity_path = _write_store(tmp_path, values)
    return SecretStore.load(store_path, identity_path)


def test_round_trip_through_an_age_identity(tmp_path: Path) -> None:
    """R-9.9: a pyrage identity decrypts the store in memory and keys are lower-cased."""
    store = _loaded(tmp_path, {"GMAIL_REFRESH_TOKEN": VALUE_REFRESH})

    view = store.scoped("email_inbox", {"gmail_refresh_token"})

    assert view.get("gmail_refresh_token") == VALUE_REFRESH
    assert store.values() == frozenset({VALUE_REFRESH})


def test_undeclared_key_raises(tmp_path: Path) -> None:
    """R-9.10: an adapter cannot read a key it did not declare."""
    store = _loaded(tmp_path, {"gmail_refresh_token": VALUE_REFRESH, "other_key": VALUE_OTHER})
    view = store.scoped("email_inbox", {"gmail_refresh_token"})

    with pytest.raises(SecretNotDeclared):
        view.get("other_key")


def test_non_sol_adapter_cannot_declare_sol_credentials(tmp_path: Path) -> None:
    """R-9.10: only sunat_sol may declare sol_* credentials."""
    store = _loaded(tmp_path, {"sol_password": VALUE_SOL})

    with pytest.raises(SolSecretNotAllowed):
        store.scoped("paid_api", {"sol_password"})


def test_sunat_sol_may_declare_sol_credentials(tmp_path: Path) -> None:
    """R-9.10: sunat_sol receives the SOL credentials it declares."""
    store = _loaded(tmp_path, {"sol_password": VALUE_SOL})

    view = store.scoped("sunat_sol", {"SOL_PASSWORD"})

    assert view.get("sol_password") == VALUE_SOL
    assert view.enabled is True


def test_environment_secret_fails_startup_naming_key_not_value() -> None:
    """R-9.9: a secret-looking environment variable aborts startup without echoing it."""
    with pytest.raises(PlainSecretError) as excinfo:
        assert_no_plain_secrets({"WHATSAPP_TOKEN": "synthetic-env-value"})

    message = str(excinfo.value)
    assert "WHATSAPP_TOKEN" in message
    assert "synthetic-env-value" not in message


def test_dotenv_secret_fails_startup_naming_key_not_value(tmp_path: Path) -> None:
    """R-9.9: a secret-looking key in .env aborts startup without echoing its value."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# non-secret settings only\n\nexport TELEGRAM_BOT_SECRET=synthetic-dotenv-value\nLOG_LEVEL=info\n",
        encoding="utf-8",
    )

    with pytest.raises(PlainSecretError) as excinfo:
        assert_no_plain_secrets({}, dotenv)

    message = str(excinfo.value)
    assert "TELEGRAM_BOT_SECRET" in message
    assert "synthetic-dotenv-value" not in message
    assert "LOG_LEVEL" not in message


def test_allowed_and_empty_values_pass_the_guard(tmp_path: Path) -> None:
    """R-9.9: GPG_KEY (container image) and empty values are not plaintext secrets."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("EMPTY_PASSWORD=\n# GITHUB_TOKEN=\n", encoding="utf-8")

    assert "GPG_KEY" in DEFAULT_ALLOW
    assert_no_plain_secrets({"GPG_KEY": "synthetic-gpg-value", "EMPTY_SECRET": ""}, dotenv)


def test_missing_secret_disables_the_adapter(tmp_path: Path) -> None:
    """R-9.10: a missing declared secret makes the view disabled and lists the key."""
    store = _loaded(tmp_path, {"gmail_refresh_token": VALUE_REFRESH})

    view = store.scoped("email_inbox", {"gmail_refresh_token", "gmail_client_secret"})

    assert view.enabled is False
    assert view.missing == frozenset({"gmail_client_secret"})
    with pytest.raises(SecretMissing):
        view.get("gmail_client_secret")


def test_repr_never_contains_secret_values(tmp_path: Path) -> None:
    """R-9.9: repr of the store and of an adapter view exposes no secret value."""
    store = _loaded(tmp_path, {"gmail_refresh_token": VALUE_REFRESH, "sol_password": VALUE_SOL})
    view = store.scoped("sunat_sol", {"sol_password"})

    assert VALUE_REFRESH not in repr(store)
    assert VALUE_SOL not in repr(store)
    assert VALUE_SOL not in repr(view)
    assert repr(store) == "SecretStore(keys=2)"


def test_non_mapping_plaintext_raises_value_error(tmp_path: Path) -> None:
    """R-9.9: the decrypted plaintext must be a YAML mapping of str to str."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(identity), encoding="utf-8")
    store_path = tmp_path / "secrets.age"
    store_path.write_bytes(
        pyrage.encrypt(yaml.safe_dump(["not", "a", "mapping"]).encode("utf-8"), [identity.to_public()])
    )

    with pytest.raises(ValueError, match="mapping"):
        SecretStore.load(store_path, identity_path)
