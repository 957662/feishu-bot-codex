"""WebSocket-based Feishu event consumer using lark-oapi.

Replaces the lark-cli subprocess approach (`lark-cli event consume`) which:
  - has menu_v6 events as dead code (the consume subcommand doesn't register them)
  - cannot subscribe to `card.action.trigger` (card button clicks)
  - spawns a Node.js subprocess per binding (RAM overhead)

This module uses `lark-oapi` (the official Python SDK) which talks the same
long-connection WebSocket protocol Feishu's event bus exposes — no public IP
required, just outbound HTTPS to open.feishu.cn.

Architecture:
- lark-oapi's `ws.Client.start()` is BLOCKING (calls loop.run_until_complete
  internally), so we run it in a dedicated daemon thread.
- Event callbacks fire on that thread; we marshal each event into a plain
  dict (mirroring lark-cli's flat shape so existing InboundPipeline code
  needs zero changes) and push it onto an asyncio.Queue on the daemon's
  main loop via run_coroutine_threadsafe.
- A subscriber `iter_events()` async generator drains the queue.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class WSEventConsumer:
    """Per-binding WebSocket event source.

    One instance per (app_id, app_secret) pair. Subscribes to message receive,
    bot menu click, and card action trigger; emits dict-shaped events compatible
    with the existing inbound pipeline.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str = "https://open.feishu.cn",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    async def start(self) -> asyncio.Queue:
        """Spin up the WS client. Returns the asyncio.Queue events land on."""
        if self._started:
            return self._queue  # type: ignore[return-value]
        # Import inside start() so module load is cheap if the SDK isn't installed
        import lark_oapi as lark

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        builder = lark.EventDispatcherHandler.builder("", "")
        builder.register_p2_im_message_receive_v1(self._on_message)
        builder.register_p2_application_bot_menu_v6(self._on_menu)
        builder.register_p2_card_action_trigger(self._on_card_action)
        handler = builder.build()

        client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            domain=self._domain,
            log_level=lark.LogLevel.WARN,
            auto_reconnect=True,
        )

        # ws.Client.start() blocks on its own event loop. Spawn in a daemon
        # thread so the daemon's main asyncio loop stays free.
        def _run():
            try:
                client.start()
            except Exception:
                logger.exception("lark-oapi ws client crashed (app_id=%s)", self._app_id[:8])

        self._thread = threading.Thread(target=_run, daemon=True, name=f"lark-ws-{self._app_id[:8]}")
        self._thread.start()
        self._started = True
        logger.info("lark-oapi WS event consumer started (app_id=%s)", self._app_id[:8])
        return self._queue

    async def iter_events(self) -> AsyncIterator[dict]:
        """Yield events forever. Caller decides termination."""
        if self._queue is None:
            await self.start()
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            yield event

    # ---- handler callbacks (run on the WS thread) ----

    def _emit(self, event_dict: dict) -> None:
        if self._loop is None or self._queue is None:
            return
        # run_coroutine_threadsafe is the only safe cross-thread asyncio call.
        asyncio.run_coroutine_threadsafe(self._queue.put(event_dict), self._loop)

    def _on_message(self, data) -> None:
        """im.message.receive_v1 — main user message channel."""
        try:
            event = data.event
            msg = event.message
            sender_open_id = ""
            try:
                sender_open_id = event.sender.sender_id.open_id or ""
            except Exception:
                pass
            d = {
                "type": "im.message.receive_v1",
                "event_id": getattr(data.header, "event_id", None),
                "chat_id": msg.chat_id,
                "message_id": msg.message_id,
                "message_type": msg.message_type,
                "content": msg.content,
                "chat_type": getattr(msg, "chat_type", "p2p"),
                "sender_id": sender_open_id,
                # mentions[] is needed for group @bot detection
                "mentions": [self._dump_mention(m) for m in (getattr(msg, "mentions", None) or [])],
            }
            self._emit(d)
        except Exception:
            logger.exception("failed to flatten im.message.receive_v1 event")

    @staticmethod
    def _dump_mention(m) -> dict:
        """Flatten a Mention SDK object to a JSON-ish dict."""
        try:
            return {
                "key": getattr(m, "key", ""),
                "name": getattr(m, "name", ""),
                "tenant_key": getattr(m, "tenant_key", ""),
                "id": {
                    "open_id": getattr(getattr(m, "id", None), "open_id", "") if getattr(m, "id", None) else "",
                    "user_id": getattr(getattr(m, "id", None), "user_id", "") if getattr(m, "id", None) else "",
                    "union_id": getattr(getattr(m, "id", None), "union_id", "") if getattr(m, "id", None) else "",
                },
            }
        except Exception:
            return {}

    def _on_menu(self, data) -> None:
        """application.bot.menu_v6 — bot sidebar menu button click.

        This is the event that lark-cli `event consume` doesn't surface;
        going through lark-oapi WS unblocks it.
        """
        try:
            ev = data.event
            operator_open_id = ""
            try:
                operator_open_id = ev.operator.operator_id.open_id or ""
            except Exception:
                pass
            d = {
                "type": "application.bot.menu_v6",
                "event_id": getattr(data.header, "event_id", None),
                "event": {
                    "event_key": getattr(ev, "event_key", ""),
                    "operator": {"operator_id": {"open_id": operator_open_id}},
                },
            }
            self._emit(d)
        except Exception:
            logger.exception("failed to flatten application.bot.menu_v6 event")

    def _on_card_action(self, data) -> None:
        """card.action.trigger — interactive card button click."""
        try:
            ev = data.event
            # ev.action is the Action SDK object — flatten minimally
            action = getattr(ev, "action", None)
            action_dict = {}
            if action is not None:
                action_dict = {
                    "tag": getattr(action, "tag", ""),
                    "value": getattr(action, "value", None),
                }
            operator_open_id = ""
            try:
                operator_open_id = ev.operator.open_id or ""
            except Exception:
                pass
            d = {
                "type": "card.action.trigger",
                "event_id": getattr(data.header, "event_id", None),
                "action": action_dict,
                "operator": {"open_id": operator_open_id},
                "token": getattr(ev, "token", ""),
            }
            self._emit(d)
        except Exception:
            logger.exception("failed to flatten card.action.trigger event")
