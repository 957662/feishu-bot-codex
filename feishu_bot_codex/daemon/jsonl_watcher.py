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

# Poll cadence during an active turn — 30 Hz so the spinner advances smoothly.
# Outbound throttles the actual update_card call to ≤10 QPS per binding
# (_last_anim_flushed_at > 0.1), which keeps us well under Feishu's 50/s tenant
# cap even with multiple bindings live. Outside the active window the poller
# falls silent (see in_active_window check below).
POLL_INTERVAL_SECONDS = 0.033

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
            try:
                while not stop_event.is_set():
                    try:
                        size = self._path.stat().st_size if self._path.exists() else 0
                    except OSError:
                        size = last_size
                    now = time.time()
                    changed = size != last_size
                    if changed:
                        last_size = size
                        last_change_at = now
                    # Fire a tick while the file is "active" — either it just
                    # grew, or it grew within the last SETTLE+1s. Outbound
                    # decides what to do with each tick (advance spinner vs.
                    # finalize vs. ignore). After the window closes we go
                    # quiet to avoid useless 10 Hz polling indefinitely.
                    in_active_window = last_size > 0 and (
                        changed or now - last_change_at < SETTLE_AFTER_SECONDS + 1.0
                    )
                    if in_active_window:
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
