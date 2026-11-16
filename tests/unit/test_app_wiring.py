"""Unit tests for the daily-task wiring (R-9.11, D119)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from invoicebot.app import _daily_tasks
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


async def _noop() -> None:
    """A daily task that does nothing, so these tests need no database."""


def _with_backup_at(cfg: Config, at: time) -> Config:
    """Return ``cfg`` with the backup moved to ``at``."""
    backup = cfg.ops.backup.model_copy(update={"at": at})
    return cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})


def test_daily_tasks_have_the_three_keys_at_their_times() -> None:
    """R-9.11: backup runs at its configured time, retention 30 min later and digest at digest_at."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _noop, _noop, _noop)

    assert {task.key for task in tasks} == {"backup", "retention", "digest"}
    by_key = {task.key: task.at for task in tasks}
    assert by_key["backup"] == cfg.ops.backup.at == time(3, 0)
    assert by_key["retention"] == time(3, 30)
    assert by_key["digest"] == cfg.gaps.digest_at == time(8, 0)


def test_daily_tasks_retention_wraps_past_midnight() -> None:
    """R-9.11: a 23:45 backup wraps retention to 00:15 rather than staying past midnight."""
    cfg = _with_backup_at(load_config(EXAMPLE), time(23, 45))

    tasks = _daily_tasks(cfg, _noop, _noop, _noop)

    assert {task.key: task.at for task in tasks}["retention"] == time(0, 15)


def test_daily_tasks_with_another_backup_target_kind_raise() -> None:
    """D119: a backup target kind other than onedrive fails startup naming the kind."""
    cfg = load_config(EXAMPLE)
    target = cfg.ops.backup.target.model_copy(update={"kind": "s3"})
    backup = cfg.ops.backup.model_copy(update={"target": target})
    unsupported = cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})

    with pytest.raises(ValueError, match="s3"):
        _daily_tasks(unsupported, _noop, _noop, _noop)
