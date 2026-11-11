"""Tests for media size, pixel and zip limits (R-1.11, BR-E07)."""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from invoicebot.channels.media_limits import (
    ByteCap,
    MediaRejected,
    check_image_header,
    check_zip,
    image_megapixels,
    max_bytes_for,
)
from invoicebot.config.models import MediaLimits, MediaMaxBytes

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8"
_JPEG_APP0 = b"\xff\xe0\x00\x04\x00\x00"
_JPEG_SOF_COMPONENTS = b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"


def _png_header(width: int, height: int) -> bytes:
    """Build a 33-byte PNG header declaring the given pixel size."""
    ihdr_data = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr_data
    return _PNG_SIGNATURE + ihdr_chunk + b"\x00\x00\x00\x00"


def _jpeg_header(marker: int, width: int, height: int) -> bytes:
    """Build a JPEG head with an APP0 segment followed by one SOF segment."""
    sof_payload = b"\x08" + struct.pack(">HH", height, width) + _JPEG_SOF_COMPONENTS
    sof_segment = bytes([0xFF, marker]) + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return _JPEG_SOI + _JPEG_APP0 + sof_segment + b"\xff\xd9"


def _limits() -> MediaLimits:
    """Limits with distinct synthetic byte caps for each attachment kind."""
    return MediaLimits(
        max_megapixels=40,
        max_bytes=MediaMaxBytes(image=5_000_000, pdf=10_000_000, xml=1_000_000, zip=2_000_000),
        zip_max_entries=20,
        zip_max_ratio=50,
    )


def test_png_header_over_pixel_limit_is_rejected() -> None:
    """R-1.11: a 8000x6000 PNG header is 48 MP and exceeds a 40 MP cap."""
    head = _png_header(8000, 6000)
    assert len(head) == 33
    assert image_megapixels(head) == 48.0
    with pytest.raises(MediaRejected) as exc:
        check_image_header(head, 40)
    assert exc.value.reason == "too_many_pixels"


def test_png_header_under_pixel_limit_passes() -> None:
    """R-1.11: a 2000x1500 PNG header is 3 MP and passes a 40 MP cap."""
    head = _png_header(2000, 1500)
    assert image_megapixels(head) == 3.0
    check_image_header(head, 40)


def test_jpeg_sof_headers_report_megapixels() -> None:
    """BR-E07: SOF0 and SOF2 JPEG headers both declare 4000x3000, or 12 MP."""
    assert image_megapixels(_jpeg_header(0xC0, 4000, 3000)) == 12.0
    assert image_megapixels(_jpeg_header(0xC2, 4000, 3000)) == 12.0


def test_jpeg_without_sof_and_non_image_are_rejected() -> None:
    """R-1.11: a head cut before SOF is unknown and a GIF is unsupported."""
    assert image_megapixels(_JPEG_SOI + _JPEG_APP0) is None
    with pytest.raises(MediaRejected) as exc:
        check_image_header(b"GIF89a" + b"\x00" * 20, 40)
    assert exc.value.reason == "unsupported_type"


def test_byte_cap_rejects_once_total_exceeds_limit() -> None:
    """R-1.11: a ByteCap allows exactly its limit and rejects the next byte."""
    cap = ByteCap(10)
    cap.add(6)
    cap.add(4)
    assert cap.total == 10
    with pytest.raises(MediaRejected) as exc:
        cap.add(1)
    assert exc.value.reason == "too_large"


def test_max_bytes_for_maps_kinds_to_configured_caps() -> None:
    """R-1.11: photo, pdf and xml use their configured cap; captcha has none."""
    limits = _limits()
    assert max_bytes_for("photo", limits) == 5_000_000
    assert max_bytes_for("pdf", limits) == 10_000_000
    assert max_bytes_for("xml", limits) == 1_000_000
    with pytest.raises(MediaRejected) as exc:
        max_bytes_for("captcha", limits)
    assert exc.value.reason == "unsupported_type"


def test_zip_with_too_many_entries_is_rejected(tmp_path: Path) -> None:
    """R-1.11: a zip above the entry cap is rejected before the ratio check."""
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(21):
            archive.writestr(f"entry{index}.txt", "x")
    with pytest.raises(MediaRejected) as exc:
        check_zip(path, max_entries=20, max_ratio=50)
    assert exc.value.reason == "zip_entries"


def test_zip_with_high_compression_ratio_is_rejected(tmp_path: Path) -> None:
    """BR-E07: one megabyte of zeros deflates above the ratio cap."""
    path = tmp_path / "zeros.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("zeros.bin", b"\x00" * (1024 * 1024), compress_type=zipfile.ZIP_DEFLATED)
    with pytest.raises(MediaRejected) as exc:
        check_zip(path, max_entries=20, max_ratio=50)
    assert exc.value.reason == "zip_ratio"


def test_small_normal_zip_passes(tmp_path: Path) -> None:
    """R-1.11: a small stored zip is within both the entry and ratio caps."""
    path = tmp_path / "small.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "hola", compress_type=zipfile.ZIP_STORED)
    check_zip(path, max_entries=20, max_ratio=50)


def test_non_zip_file_is_rejected(tmp_path: Path) -> None:
    """R-1.11: a file that is not a zip is an unsupported type."""
    path = tmp_path / "notes.txt"
    path.write_text("not a zip")
    with pytest.raises(MediaRejected) as exc:
        check_zip(path, max_entries=20, max_ratio=50)
    assert exc.value.reason == "unsupported_type"


def test_jpeg_segment_length_zero_returns_none() -> None:
    """R-1.11: a zero-length JPEG segment is malformed, not a crash."""
    assert image_megapixels(b"\xff\xd8\xff\xe0\x00\x00") is None


def test_empty_head_returns_none() -> None:
    """R-1.11: an empty head declares no dimensions."""
    assert image_megapixels(b"") is None
