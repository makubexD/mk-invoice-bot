"""A ``SIGHUP`` reload restarts into a good config and refuses a bad one (R-9.6, D155).

A candidate that loads is installed by restarting the process on it; a rejected
one never replaces the running config, and the admins hear why (BR-O05).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from invoicebot.runtime_config import ConfigRejected, RuntimeConfig

logger = logging.getLogger(__name__)


async def reload_config(
    load: Callable[[], RuntimeConfig],
    on_rejected: Callable[[str], Awaitable[None]],
    restart: Callable[[], None],
) -> bool:
    """Load the candidate config, restarting into it or reporting its rejection (R-9.6, D155).

    Returns:
        Whether the candidate was accepted and the restart started.
    """
    try:
        await asyncio.to_thread(load)
    except ConfigRejected as err:
        logger.warning("config reload rejected", extra={"reason": err.reason})
        await on_rejected(err.reason)
        return False
    logger.info("config reload accepted")
    restart()
    return True
