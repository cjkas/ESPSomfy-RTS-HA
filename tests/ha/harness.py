"""What the tests in tests/ha share, besides fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fake_device import SERVER_ID

UNIQUE_ID = f"espsomfy_{SERVER_ID}"


async def until(condition: Callable[[], object], timeout: float = 5) -> None:
    """Wait for condition() to hold, as the integration takes events in its own time."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)
