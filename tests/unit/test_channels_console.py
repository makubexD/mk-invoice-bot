"""Tests for the dev-only console channel adapter (D165)."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from invoicebot.channels.console import ConsoleMediaSource, console_transport, inbox_dir
from invoicebot.channels.media_limits import MediaRejected
from invoicebot.channels.profiles import sender_role
from invoicebot.config.models import MediaLimits, MediaMaxBytes
from invoicebot.queue.outbox import OutboxRow

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PHOTO_NAME = "photo.png"


def _limits(*, image: int = 5_000_000) -> MediaLimits:
    """Limits with synthetic byte caps for each attachment kind."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(image=image, pdf=10_000_000, xml=1_000_000, zip=2_000_000),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def _png(width: int, height: int) -> bytes:
    """Build a 33-byte PNG header declaring the given pixel size."""
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data
    return _PNG_SIGNATURE + ihdr_chunk + b"\x00\x00\x00\x00"


def _inbox_file(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write data to the console inbox and return the file's path."""
    directory = inbox_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


async def test_console_media_source_copies_hashes_and_clears_the_inbox(tmp_path: Path) -> None:
    """D165: an inbox file comes back with its hash, size and mime and leaves the inbox."""
    data = _png(100, 100)
    source = _inbox_file(tmp_path, _PHOTO_NAME, data)
    out_dir = tmp_path / "out"

    downloaded = await ConsoleMediaSource(tmp_path).download(_PHOTO_NAME, "photo", out_dir, _limits())

    assert downloaded.sha256 == hashlib.sha256(data).hexdigest()
    assert downloaded.size == len(data)
    assert downloaded.mime == "image/png"
    assert downloaded.path.is_file()
    assert downloaded.path.read_bytes() == data
    assert downloaded.path.parent.parent == out_dir
    assert not source.exists()


@pytest.mark.parametrize("media_ref", ["../x.jpg", "a/b.jpg", ".hidden", "a" * 65])
async def test_console_media_source_rejects_an_unsafe_name(tmp_path: Path, media_ref: str) -> None:
    """D165: a media_ref that escapes the inbox or is too long raises ValueError."""
    with pytest.raises(ValueError, match="plain file name"):
        await ConsoleMediaSource(tmp_path).download(media_ref, "photo", tmp_path / "out", _limits())


async def test_console_media_source_rejects_a_file_over_the_photo_cap(tmp_path: Path) -> None:
    """D165: a photo over its byte cap raises MediaRejected and leaves no out file."""
    source = _inbox_file(tmp_path, _PHOTO_NAME, b"x" * 11)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with pytest.raises(MediaRejected, match="too_large"):
        await ConsoleMediaSource(tmp_path).download(_PHOTO_NAME, "photo", out_dir, _limits(image=10))

    assert list(out_dir.iterdir()) == []
    assert source.exists()


async def test_console_media_source_missing_file_raises(tmp_path: Path) -> None:
    """D165: an inbox file that does not exist raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        await ConsoleMediaSource(tmp_path).download("missing.jpg", "photo", tmp_path / "out", _limits())


async def test_console_transport_keeps_the_row() -> None:
    """D165: the console transport accepts a row and returns its console message id."""
    row = OutboxRow(
        id=7,
        channel="console",
        to="yo",
        payload={"text": "hola"},
        template=None,
        attempts=0,
    )

    assert await console_transport()(row) == "console-7"


@pytest.mark.parametrize(
    ("profile_id", "owner_profile", "admin_profiles", "role"),
    [
        ("yo", "yo", frozenset(), "owner"),
        ("asistente", "yo", frozenset({"asistente"}), "admin"),
        ("otro", "yo", frozenset({"asistente"}), "sender"),
    ],
)
def test_sender_role_follows_the_profile(
    profile_id: str, owner_profile: str, admin_profiles: frozenset[str], role: str
) -> None:
    """D165: sender_role returns owner, admin and sender for the three cases."""
    assert sender_role(profile_id, owner_profile, admin_profiles) == role
