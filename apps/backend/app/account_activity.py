"""Track this process's authenticated work for cancellation during deletion."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

_active: dict[str, dict[asyncio.Task, int]] = {}


@contextmanager
def account_activity(owner: str) -> Iterator[None]:
    task = asyncio.current_task()
    tasks = _active.setdefault(owner, {})
    if task is not None:
        tasks[task] = tasks.get(task, 0) + 1
    try:
        yield
    finally:
        if task in tasks:
            tasks[task] -= 1
            if not tasks[task]:
                tasks.pop(task)
        if not tasks:
            _active.pop(owner, None)


async def stop_account_activity(owner: str) -> None:
    """Wait for cancellation cleanup before deleting files or engine records."""
    tasks = [
        task for task in _active.get(owner, {}) if task is not asyncio.current_task()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
