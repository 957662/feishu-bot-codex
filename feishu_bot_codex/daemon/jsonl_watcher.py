"""Async wrapper that yields a signal whenever the watched jsonl file grows.

Uses watchfiles (fsevents/inotify) as the primary low-latency notifier, with
a 2-second size-polling fallback. The polling fallback is critical for Claude,
which appends to jsonl in a way that doesn't reliably trigger fsevents on
macOS — without it, the outbound mirror would stall indefinitely until
something else touched the file.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

import watchfiles

logger = logging.getLogger(__name__)

# How often to poll file size as a safety net. 1 second gives a typewriter-like
# refresh during active turn (one update_card per second well under Feishu's
# 50/s rate limit).
POLL_INTERVAL_SECONDS = 1.0

# After this much idle time (no file growth), emit ONE more change so outbound
# gets a final flush with in_progress=False — otherwise the "生成中…" spinner
# stays on the card forever when the turn really has finished.
SETTLE_AFTER_SECONDS = 6.0


class JsonlWatcher:
    """Yield a signal whenever the watched file grows or is created.

    Combines two notification paths:
    1. watchfiles.awatch on the parent dir (real-time, when it works).
    2. Periodic size poll every POLL_INTERVAL_SECONDS (always works).

    Either path triggers a yield — downstream code is expected to be
    idempotent (re-reading already-processed bytes is fine because the
    consumer tracks its own offset).
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    async def changes(self, stop_after: int = 0) -> AsyncIterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path = self._path.resolve()

        queue: asyncio.Queue[None] = asyncio.Queue()
        stop_event = asyncio.Event()

        async def _watchfiles_task():
            try:
                async for changes in watchfiles.awatch(
                    self._path.parent,
                    recursive=False,
                    stop_event=stop_event,
                ):
                    relevant = any(Path(p).resolve() == resolved_path for _, p in changes)
                    if relevant:
                        await queue.put(None)
            except Exception:
                logger.exception("watchfiles task crashed; polling fallback continues")

        async def _poll_task():
            import time
            last_size = -1
            last_change_at = 0.0
            settled_after_change = False  # have we already fired the "settle" tick?
            try:
                while not stop_event.is_set():
                    try:
                        size = self._path.stat().st_size if self._path.exists() else 0
                    except OSError:
                        size = last_size
                    now = time.time()
                    if size != last_size:
                        last_size = size
                        last_change_at = now
                        settled_after_change = False
                        if size > 0:
                            await queue.put(None)
                    elif (
                        not settled_after_change
                        and last_size > 0
                        and now - last_change_at > SETTLE_AFTER_SECONDS
                    ):
                        # File has been quiet long enough → fire one more tick
                        # so outbound can repaint the card with in_progress=False
                        # (removing the typewriter cursor / "生成中…" footer).
                        settled_after_change = True
                        await queue.put(None)
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                logger.exception("poll task crashed")

        wf = asyncio.create_task(_watchfiles_task(), name="jsonl-watchfiles")
        poll = asyncio.create_task(_poll_task(), name="jsonl-poll")

        emitted = 0
        try:
            while True:
                await queue.get()
                yield None
                emitted += 1
                if stop_after and emitted >= stop_after:
                    return
        finally:
            stop_event.set()
            for t in (wf, poll):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
