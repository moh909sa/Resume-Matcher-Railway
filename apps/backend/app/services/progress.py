"""Live stage of long AI operations, readable by a client-chosen id.

The tailoring flow already records its stage in a plain dict; registering that
dict under the request's ``progress_id`` lets GET /resumes/improve/progress/{id}
stream the real stages while the POST is still running. Single-process state,
like the rest of the app's caches (one uvicorn worker).
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

class StageTracker(dict[str, str]):
    """Keep each real stage assignment, including stages too fast for polling.

    Tracks actual engine transitions; it never advances stages by elapsed time.
    """

    def __init__(self, initial_stage: str) -> None:
        super().__init__(stage=initial_stage)
        self.history: list[str] = [initial_stage]

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        if key == "stage" and (not self.history or self.history[-1] != value):
            self.history.append(value)


_REGISTRY: dict[str, StageTracker] = {}
_MAX_ENTRIES = 256
# A finished entry stays readable this long so a late subscriber sees "done".
_DONE_RETENTION_SECONDS = 30.0
# How long a subscriber waits for the POST to register its id.
_REGISTRATION_WAIT_SECONDS = 15.0
_POLL_SECONDS = 0.25


def mark_writing_incomplete(progress: dict[str, str] | None, stage: str) -> None:
    """Record a failed writing step instead of inferring success from transition."""
    if progress is None:
        return
    progress["writing_incomplete"] = "1"
    if stage in ("critique_writing", "rewrite_writing"):
        progress["stage"] = f"{stage}_incomplete"


@contextmanager
def tracked_progress(progress_id: str | None, stage: str) -> Iterator[dict[str, str]]:
    """Yield the stage dict for one operation, registered when an id is given."""
    progress = StageTracker(stage)
    if progress_id:
        while len(_REGISTRY) >= _MAX_ENTRIES:
            _REGISTRY.pop(next(iter(_REGISTRY)))
        _REGISTRY[progress_id] = progress
    try:
        yield progress
    finally:
        progress["done"] = "1"
        if progress_id:
            try:
                asyncio.get_running_loop().call_later(
                    _DONE_RETENTION_SECONDS, _forget, progress_id, progress
                )
            except RuntimeError:
                _forget(progress_id, progress)


def get_progress_history(progress_id: str) -> list[str] | None:
    """Read the actual step transitions registered for an in-flight operation."""
    entry = _REGISTRY.get(progress_id)
    return list(entry.history) if entry is not None else None


def _forget(progress_id: str, progress: dict[str, str]) -> None:
    if _REGISTRY.get(progress_id) is progress:
        del _REGISTRY[progress_id]


def get_progress(progress_id: str) -> dict[str, str] | None:
    return _REGISTRY.get(progress_id)


async def progress_events(progress_id: str, max_seconds: float) -> AsyncIterator[str]:
    """Server-sent events: one ``data`` line per new stage, then ``done``."""
    deadline = time.monotonic() + max_seconds
    registration_deadline = time.monotonic() + _REGISTRATION_WAIT_SECONDS
    last_stage: str | None = None
    seen = False
    while time.monotonic() < deadline:
        entry = _REGISTRY.get(progress_id)
        if entry is None:
            if seen or time.monotonic() > registration_deadline:
                break
        else:
            seen = True
            stage = entry.get("stage", "")
            if stage != last_stage:
                last_stage = stage
                yield f"data: {json.dumps({'stage': stage})}\n\n"
            if entry.get("done"):
                break
        await asyncio.sleep(_POLL_SECONDS)
    yield "event: done\ndata: {}\n\n"
