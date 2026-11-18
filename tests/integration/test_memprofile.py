"""Integration test for the memory and latency report (R-9.1, R-9.2, D160)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "memprofile.py"


def _load_script(name: str, path: Path) -> ModuleType:
    """Return the module at ``path`` loaded as ``name``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


memprofile = _load_script("memprofile", SCRIPT)


@pytest.mark.timeout(150)
async def test_memprofile_reports_two_invoices(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-9.1: two synthetic invoices go through the pipeline and the report exits 0."""
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    code = await asyncio.to_thread(memprofile.main, ["--invoices", "2"])

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["invoices"] == 2
