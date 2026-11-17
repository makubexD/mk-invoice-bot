"""Tests for the deploy files (R-9.13)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"

_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_FROM = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.MULTILINE | re.IGNORECASE)
_COPY_FROM = re.compile(r"--from=(\S+)")


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _image_refs(text: str) -> list[str]:
    """Return the image references used by FROM lines and COPY --from, skipping stage names."""
    refs = [match.group(1) for match in _FROM.finditer(text)]
    refs += [ref for ref in _COPY_FROM.findall(text) if "/" in ref or ":" in ref]
    return refs


def test_dockerfile_image_refs_are_pinned_by_digest() -> None:
    """R-9.13 every base and copied image is pinned to a sha256 digest."""
    text = _dockerfile()
    refs = _image_refs(text)

    assert _FROM.search(text), "no FROM line found"
    assert _COPY_FROM.search(text), "no COPY --from line found"
    assert refs
    for ref in refs:
        assert _PINNED.search(ref), f"{ref} is not pinned to a sha256 digest"


def test_builder_disables_python_downloads() -> None:
    """R-9.13 the builder stage sets UV_PYTHON_DOWNLOADS=never."""
    text = _dockerfile()
    froms = list(_FROM.finditer(text))
    builder_stages = [
        text[match.end() : froms[index + 1].start() if index + 1 < len(froms) else len(text)]
        for index, match in enumerate(froms)
        if (match.group(2) or "").lower() == "builder"
    ]

    assert builder_stages, "no builder stage found"
    assert "UV_PYTHON_DOWNLOADS=never" in builder_stages[0]
