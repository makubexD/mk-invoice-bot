"""Tests for the kept chat documents (D206, BR-D24)."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoicebot.pipeline.chat_docs import DOCS_DIRECTORY, keep_documents, kept_documents


def test_kept_files_are_listed_with_mime_and_size(tmp_path: Path) -> None:
    """D206: two kept files come back sorted by name with their MIME types and sizes."""
    count = keep_documents(tmp_path, 7, [("b.xml", b"<a/>"), ("a.pdf", b"%PDF-1")])

    documents = kept_documents(tmp_path, 7)

    assert count == 2
    assert [(d.filename, d.mime, d.size) for d in documents] == [
        ("a.pdf", "application/pdf", 6),
        ("b.xml", "application/xml", 4),
    ]
    assert all(Path(d.path).parent == tmp_path / DOCS_DIRECTORY / "7" for d in documents)


def test_keeping_again_replaces_the_earlier_files(tmp_path: Path) -> None:
    """D206: a second keep for the same invoice leaves only its own files, never an earlier attempt's."""
    keep_documents(tmp_path, 5, [("old.pdf", b"old")])

    keep_documents(tmp_path, 5, [("new.pdf", b"new")])

    assert [d.filename for d in kept_documents(tmp_path, 5)] == ["new.pdf"]


def test_nothing_kept_lists_nothing(tmp_path: Path) -> None:
    """D206: an invoice with no kept folder has no documents."""
    assert kept_documents(tmp_path, 9) == []


@pytest.mark.parametrize("name", ["../x.pdf", "a/b.pdf", "..", "a\\b.pdf", "a\r.pdf", "a\n.pdf", ""])
def test_a_bad_name_writes_nothing(tmp_path: Path, name: str) -> None:
    """D206: a name with a separator, `..` or CR/LF raises and no file is written."""
    with pytest.raises(ValueError, match=r"."):
        keep_documents(tmp_path, 3, [("ok.pdf", b"x"), (name, b"y")])

    assert not (tmp_path / DOCS_DIRECTORY).exists()
