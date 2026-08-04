"""Async utilities — unblock the event loop during heavy I/O.

Wraps synchronous operations with asyncio.to_thread() via a bounded
thread pool. This prevents blocking the event loop when multiple MCP
tool calls are handled concurrently.

Full aiosqlite rewrite is Phase 3; for now, thread offloading is sufficient.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

_executor = ThreadPoolExecutor(max_workers=4)


async def run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous function in the thread pool without blocking the event loop.

    Forwards both positional and keyword arguments to ``func`` (keywords are
    bound via ``functools.partial`` since ``run_in_executor`` itself takes only
    positional args). Passing call-site args by keyword guards against silent
    metadata scramble if the callee's signature drifts.
    """
    loop = asyncio.get_event_loop()
    call = functools.partial(func, *args, **kwargs) if kwargs else func
    if kwargs:
        return await loop.run_in_executor(_executor, call)
    return await loop.run_in_executor(_executor, call, *args)
