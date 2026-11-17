"""Integration tests for ``invoicebot auth-google`` (R-6.4, R-6.5, BR-D01)."""

import asyncio
import base64
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pyrage
import pytest
import yaml
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.providers import (
    DRIVE_FILE_SCOPE,
    GMAIL_READ_SCOPE,
    OPENID_SCOPE,
    TOKENS_DIRECTORY,
)
from invoicebot.cli import main
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.pipeline.pending_auth import GOOGLE
from invoicebot.settings import Settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
ACCOUNT = "owner"
INBOX_ACCOUNT = "invoices_a"
CLIENT_SECRET_KEY = "google_client_secret"  # noqa: S105  # the store key's name, not a value
SYNTHETIC_CLIENT_SECRET = "synthetic-client-secret"  # noqa: S105  # a synthetic test value
SYNTHETIC_REFRESH_TOKEN = "synthetic-refresh"  # noqa: S105  # a synthetic test value
OWNER_EMAIL = "owner@example.com"
INBOX_EMAIL = "invoices@example.com"
#: Google grants the ``email`` scope back under this name (R-6.5).
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
SCOPE = f"{OPENID_SCOPE} {USERINFO_EMAIL_SCOPE} {DRIVE_FILE_SCOPE} {GMAIL_READ_SCOPE}"


def _b64url(raw: bytes) -> str:
    """Return ``raw`` as unpadded base64url, as an id_token part is encoded (D138)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _id_token(email: str, *, verified: bool = True) -> str:
    """Return a synthetic unsigned id_token naming ``email`` (D138)."""
    claims = _b64url(json.dumps({"email": email, "email_verified": verified}).encode("utf-8"))
    return f"c3ludGhldGlj.{claims}.c3ludGhldGlj"


TOKEN_RESPONSE = {
    "access_token": "a",
    "expires_in": 3600,
    "refresh_token": SYNTHETIC_REFRESH_TOKEN,
    "scope": SCOPE,
    "id_token": _id_token(OWNER_EMAIL),
}
NO_REFRESH_RESPONSE = {"access_token": "a", "expires_in": 3600, "id_token": _id_token(OWNER_EMAIL)}


def _age_material(tmp_path: Path) -> tuple[pyrage.x25519.Identity, Path, Path]:
    """Write a synthetic age identity and an encrypted secrets file for the command."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.txt"
    identity_path.write_text(str(identity), encoding="utf-8")
    plaintext = yaml.safe_dump({CLIENT_SECRET_KEY: SYNTHETIC_CLIENT_SECRET}).encode("utf-8")
    secrets_path = tmp_path / "secrets.age"
    secrets_path.write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))
    return identity, identity_path, secrets_path


def _settings(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    identity_path: Path,
    secrets_path: Path,
) -> Settings:
    """Build settings against the test database and the synthetic age material."""
    return Settings(
        config_path=EXAMPLE_CONFIG,
        database_url=pool.conninfo,
        data_dir=tmp_path / "data",
        secrets_path=secrets_path,
        age_identity_path=identity_path,
    )


def _token_path(settings: Settings, account: str = ACCOUNT) -> Path:
    """Return where the command stores the account's refresh token."""
    return settings.data_dir / TOKENS_DIRECTORY / f"{account}.google.age"


def _open_redirect(redirect_uri: str, state: str) -> None:
    """Send the loopback redirect from a thread, since the sign-in loop waits for it."""
    httpx.get(f"{redirect_uri}/?code=x&state={state}", timeout=10)


def _show_redirect(state: str | None = None) -> Callable[[str], None]:
    """Return a show that opens the printed URL's loopback redirect like a browser."""

    def show(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        redirect_uri = query["redirect_uri"][0]
        echoed_state = state if state is not None else query["state"][0]
        threading.Thread(target=_open_redirect, args=(redirect_uri, echoed_state), daemon=True).start()

    return show


def _show_redirect_after_favicon() -> Callable[[str], None]:
    """Return a show whose browser reads ``/favicon.ico`` before the redirect."""

    def show(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]

        def browser() -> None:
            httpx.get(f"{redirect_uri}/favicon.ico", timeout=10)
            _open_redirect(redirect_uri, state)

        threading.Thread(target=browser, daemon=True).start()

    return show


def _token_transport(payload: dict[str, Any]) -> httpx.MockTransport:
    """Return a token endpoint that checks the exchange and answers ``payload``."""

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        assert form["grant_type"] == ["authorization_code"]
        assert form["code_verifier"][0]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def _run_cli(
    settings: Settings,
    show: Callable[[str], None],
    transport: httpx.AsyncBaseTransport,
    account: str = ACCOUNT,
) -> int:
    """Run the command off the event loop, as the console entry point does."""
    return await asyncio.to_thread(
        main,
        ["auth-google", "--account", account],
        settings=settings,
        show=show,
        google_transport=transport,
    )


async def _seed_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], state: str) -> int:
    """Insert one invoice in ``state``, parked for this file's account, and return its id."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO invoices (profile_id, origin_channel, state, error)
            VALUES (%s, 'whatsapp', %s, %s)
            RETURNING id
            """,
            (f"profile-{uuid4().hex}", state, f"{GOOGLE}:{ACCOUNT}"),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> str:
    """Return the invoice's state."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _job_steps(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's jobs."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s", (invoice_id,))
        return [str(row[0]) for row in await cur.fetchall()]


async def test_auth_google_saves_the_refresh_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-6.4: a completed sign-in writes the account's age-encrypted refresh token."""
    identity, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)

    code = await _run_cli(settings, _show_redirect(), _token_transport(TOKEN_RESPONSE))

    assert code == 0
    token_path = _token_path(settings)
    assert token_path.is_file()
    assert pyrage.decrypt(token_path.read_bytes(), [identity]).decode("utf-8") == SYNTHETIC_REFRESH_TOKEN


async def test_auth_google_resumes_the_pending_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BR-D01: a successful sign-in moves a PENDING_AUTH invoice to DELIVERING with a deliver job."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    code = await _run_cli(settings, _show_redirect(), _token_transport(TOKEN_RESPONSE))

    assert code == 0
    assert await _state(db_pool, invoice_id) == "DELIVERING"
    assert await _job_steps(db_pool, invoice_id) == [DELIVER_STEP]
    assert re.search(r"resumed \d+ invoice\(s\)", capsys.readouterr().out)


async def test_auth_google_wrong_state_writes_no_token_and_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5: a redirect whose state differs exits non-zero, writes no token and resumes nothing."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    code = await _run_cli(settings, _show_redirect("wrong-state"), _token_transport(TOKEN_RESPONSE))

    assert code == 1
    assert not _token_path(settings).exists()
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    assert "sign-in failed" in capsys.readouterr().err


async def test_auth_google_missing_refresh_token_exits_one(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.4: a token response without refresh_token exits 1 and names the permissions page."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)

    code = await _run_cli(settings, _show_redirect(), _token_transport(NO_REFRESH_RESPONSE))

    assert code == 1
    assert not _token_path(settings).exists()
    error = capsys.readouterr().err
    assert "sign-in failed" in error
    assert "myaccount.google.com/permissions" in error


async def test_auth_google_missing_scope_exits_one_and_saves_no_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5: a token response whose scope lacks drive.file exits 1 and names that scope."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    response = {**TOKEN_RESPONSE, "scope": f"{OPENID_SCOPE} {USERINFO_EMAIL_SCOPE} {GMAIL_READ_SCOPE}"}

    code = await _run_cli(settings, _show_redirect(), _token_transport(response))

    assert code == 1
    assert not _token_path(settings).exists()
    error = capsys.readouterr().err
    assert "sign-in failed" in error
    assert "Google did not grant" in error
    assert DRIVE_FILE_SCOPE in error


async def test_auth_google_stray_request_does_not_end_the_sign_in(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-6.5: a GET /favicon.ico before the redirect is ignored and the sign-in completes."""
    identity, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)

    code = await _run_cli(
        settings,
        _show_redirect_after_favicon(),
        _token_transport(TOKEN_RESPONSE),
    )

    assert code == 0
    token_path = _token_path(settings)
    assert token_path.is_file()
    assert pyrage.decrypt(token_path.read_bytes(), [identity]).decode("utf-8") == SYNTHETIC_REFRESH_TOKEN


async def test_auth_google_unknown_account_exits_two_and_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5: an account no Google sink or gmail inbox names exits 2 and resumes nothing."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    with pytest.raises(SystemExit) as exit_info:
        await _run_cli(settings, _show_redirect(), _token_transport(TOKEN_RESPONSE), "nobody")

    assert exit_info.value.code == 2
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    error = capsys.readouterr().err
    assert "nobody" in error
    assert ACCOUNT in error
    assert INBOX_ACCOUNT in error


async def test_auth_google_inbox_account_resumes_no_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5, D129: the gmail-only account signs in but delivers no files, so it resumes nothing."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    response = {**TOKEN_RESPONSE, "id_token": _id_token(INBOX_EMAIL)}
    code = await _run_cli(settings, _show_redirect(), _token_transport(response), INBOX_ACCOUNT)

    assert code == 0
    assert _token_path(settings, INBOX_ACCOUNT).is_file()
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    assert "resumed 0 invoice(s)" in capsys.readouterr().out


async def test_auth_google_matching_mailbox_saves_the_token(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """R-6.5, D138: an id_token naming the pinned mailbox completes the sign-in."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)

    code = await _run_cli(settings, _show_redirect(), _token_transport(TOKEN_RESPONSE))

    assert code == 0
    assert _token_path(settings).is_file()


async def test_auth_google_other_mailbox_saves_nothing_and_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5, D138: an id_token for another mailbox exits 1 and saves no token."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")
    response = {**TOKEN_RESPONSE, "id_token": _id_token("someone-else@example.com")}

    code = await _run_cli(settings, _show_redirect(), _token_transport(response))

    assert code == 1
    assert not _token_path(settings).exists()
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    error = capsys.readouterr().err
    assert OWNER_EMAIL in error
    assert "someone-else@example.com" in error


async def test_auth_google_unverified_mailbox_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5, D138: an id_token whose email_verified is false exits 1 and saves no token."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    response = {**TOKEN_RESPONSE, "id_token": _id_token(OWNER_EMAIL, verified=False)}

    code = await _run_cli(settings, _show_redirect(), _token_transport(response))

    assert code == 1
    assert not _token_path(settings).exists()
    error = capsys.readouterr().err
    assert OWNER_EMAIL in error


@pytest.mark.parametrize(
    "id_token",
    [
        pytest.param(None, id="missing"),
        pytest.param("one-part", id="not-three-parts"),
        pytest.param("head.%%%.signature", id="bad-base64"),
        pytest.param(f"head.{_b64url(b'not json')}.signature", id="bad-json"),
        pytest.param(
            f"head.{_b64url(json.dumps({'email_verified': True}).encode('utf-8'))}.signature",
            id="no-email",
        ),
    ],
)
async def test_auth_google_missing_or_malformed_id_token_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    id_token: str | None,
) -> None:
    """R-6.5, D138: a missing, malformed or incomplete id_token exits 1 and saves no token."""
    _, identity_path, secrets_path = _age_material(tmp_path)
    settings = _settings(db_pool, tmp_path, identity_path, secrets_path)
    response = dict(TOKEN_RESPONSE)
    if id_token is None:
        response.pop("id_token")
    else:
        response["id_token"] = id_token

    code = await _run_cli(settings, _show_redirect(), _token_transport(response))

    assert code == 1
    assert not _token_path(settings).exists()
    error = capsys.readouterr().err
    assert "sign-in failed" in error
    assert OWNER_EMAIL in error
