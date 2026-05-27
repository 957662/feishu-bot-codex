"""Inbound pipeline: Feishu events → tmux send-keys (text/slash/menu/image)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

from feishu_bot_codex.daemon.feishu import LarkCli
from feishu_bot_codex.daemon.tmux import Tmux

logger = logging.getLogger(__name__)

# Standard confirmation event_key → tmux keystrokes (for /clear-style Y/N prompts).
# Orchestrator merges this into its menu_command_map by default.
DEFAULT_CONFIRM_MAP: dict[str, str] = {
    "confirm_yes": "y",
    "confirm_no": "n",
}


class InboundPipeline:
    """Drive `lark-cli event consume`, route each event to tmux or a handler."""

    def __init__(
        self,
        tmux_session: str,
        tmux: Tmux,
        lark: LarkCli,
        menu_command_map: dict[str, str] | None = None,
        allow_users: set[str] | None = None,
        max_message_length: int = 8000,
        event_key: str = "im.message.receive_v1",
        on_chat_id_discovered=None,
        bootstrap_complete: bool = False,
    ) -> None:
        self._tmux_session = tmux_session
        self._tmux = tmux
        self._lark = lark
        self._menu_command_map = menu_command_map or {}
        self._allow_users = allow_users
        self._max_message_length = max_message_length
        self._event_key = event_key
        self._on_chat_id_discovered = on_chat_id_discovered
        # If the bot has already bootstrapped (e.g. persisted chat_id in state
        # from a prior daemon run), the next message is a REAL message and must
        # be forwarded to Claude — not consumed as a bootstrap.
        self._chat_id_seen = bootstrap_complete
        # event_id LRU dedup — Feishu's event bus is at-least-once, lark-cli
        # has an internal dedup filter but it doesn't always catch retries
        # (especially across reconnects). Track recent event_ids ourselves so
        # we don't double-forward the same user message to Claude.
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        self._seen_event_ids_max = 1024

    async def process_until_idle(self, max_events: int = 0) -> None:
        """Consume events until the fake queue drains or max_events hit."""
        count = 0
        async for event in self._lark.consume_events(self._event_key, max_events=max_events):
            await self._handle(event)
            count += 1
            if max_events and count >= max_events:
                break

    async def _handle(self, event: dict) -> None:
        # Drop duplicates: Feishu's event bus is at-least-once. Same event_id
        # → same physical event from the user; forwarding it twice would
        # cause Claude to receive duplicate messages.
        event_id = event.get("event_id") or event.get("header", {}).get("event_id")
        if event_id:
            if event_id in self._seen_event_ids:
                logger.info("dropping duplicate event_id=%s", event_id)
                return
            self._seen_event_ids[event_id] = None
            if len(self._seen_event_ids) > self._seen_event_ids_max:
                self._seen_event_ids.popitem(last=False)

        evt_type = event.get("type", "")
        if evt_type == "im.message.receive_v1":
            await self._handle_message(event)
        elif evt_type == "application.bot.menu_v6":
            await self._handle_menu(event)
        else:
            logger.debug("ignoring event type: %s", evt_type)

    async def _handle_message(self, event: dict) -> None:
        # lark-cli emits a flat event structure (NOT the Feishu webhook's
        # nested {event:{message:{...},sender:{...}}}). Fields like chat_id,
        # message_type, content, sender_id, message_id sit at the top level.
        # content is the plain text string for text messages, not a JSON blob.
        # Some fields (chat_id, sender_id) may also live under event.event
        # depending on lark-cli version — read with fallback.
        chat_id = event.get("chat_id") or event.get("event", {}).get("message", {}).get("chat_id", "")
        message_type = event.get("message_type") or event.get("event", {}).get("message", {}).get("message_type", "")
        content_raw = event.get("content")
        if content_raw is None:
            content_raw = event.get("event", {}).get("message", {}).get("content", "")
        sender = event.get("sender_id") or event.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "")
        message_id = event.get("message_id") or event.get("event", {}).get("message", {}).get("message_id", "")

        # Auto-discover chat_id on first message. This is the BOOTSTRAP message —
        # the user sends "hi" (or anything) to start the mirror. We consume it:
        # capture chat_id, trigger backlog replay, but do NOT forward it to Claude.
        is_bootstrap = False
        if not self._chat_id_seen:
            if chat_id:
                self._chat_id_seen = True
                is_bootstrap = True
                if self._on_chat_id_discovered is not None:
                    result = self._on_chat_id_discovered(chat_id)
                    import asyncio as _asyncio
                    if _asyncio.iscoroutine(result):
                        await result

        if is_bootstrap:
            logger.info(
                "bootstrap message received; chat_id=%s captured, history replay triggered",
                chat_id,
            )
            return

        if self._allow_users is not None and sender not in self._allow_users:
            logger.info("dropping message from non-whitelisted sender %s", sender)
            return
        # ---- Image messages ----
        # Download to a tempdir and inject the absolute file path as text.
        # Both Claude Code TUI and Codex CLI accept image file paths (drag/drop
        # equivalent) — pasting the path adds the image as input attachment.
        if message_type == "image":
            await self._handle_image(message_id, content_raw)
            if message_id:
                asyncio.create_task(self._react_quietly(message_id, "LOVE"))
            return

        if message_type != "text":
            logger.info("skipping non-text message type: %s", message_type)
            return
        # content is either a plain text string (lark-cli's flattened format)
        # or a JSON-encoded {"text": "..."} (Feishu webhook raw format).
        text = ""
        if isinstance(content_raw, str):
            stripped = content_raw.strip()
            if stripped.startswith("{"):
                try:
                    text = json.loads(stripped).get("text", "")
                except json.JSONDecodeError:
                    text = content_raw
            else:
                text = content_raw
        if not text:
            return

        # Inline images inside a text message: lark-cli's text converter
        # rewrites embedded image elements to "[Image: img_xxx]". Scan for
        # that pattern, download each, and replace with the absolute path.
        text = await self._inline_images(message_id, text)

        if len(text) > self._max_message_length:
            text = text[: self._max_message_length] + "\n...[truncated]"
        # Ack the user's message with a reaction so they see Claude received
        # it immediately (before Claude finishes generating a reply). Done
        # as fire-and-forget — reaction failure must not block forwarding.
        # See full emoji_type list at
        # open.feishu.cn/.../message-reaction/emojis-introduce
        if message_id:
            asyncio.create_task(self._react_quietly(message_id, "LOVE"))
        # Codex TUI swallows the immediate Enter after a paste because of its
        # completion popup. Send body and Enter separately with a brief gap
        # so codex's input box has time to settle. (Same workaround as
        # _handle_image — applies to all text injections.)
        self._tmux.send_keys(session=self._tmux_session, keys=text)
        await asyncio.sleep(0.4)
        self._tmux.send_keys(session=self._tmux_session, keys="\n")

    async def _handle_image(self, message_id: str, content_raw) -> None:
        """Standalone image message: download + inject absolute file path.

        Codex CLI quirks we work around:
        1. Input starting with `/` triggers slash-command parsing — a bare
           absolute path gets rejected as an unknown slash command.
        2. The input box doesn't auto-submit on raw paste of a long path
           (Enter gets swallowed by an internal completion popup).

        Solution: prefix with "Image: " so codex parses as plain text, then
        sleep briefly to let codex auto-detect the embedded absolute path,
        then send Enter separately.
        """
        image_key = self._extract_image_key(content_raw)
        if not image_key:
            logger.warning("image message %s missing image_key; content=%r", message_id, content_raw)
            return
        path = await self._download_image(message_id, image_key)
        if path is None:
            return
        # Type the body (no trailing newline yet)
        self._tmux.send_keys(session=self._tmux_session, keys=f"Image: {path}")
        # Give codex's path detector time to attach the image
        await asyncio.sleep(0.4)
        # Commit
        self._tmux.send_keys(session=self._tmux_session, keys="\n")

    async def _inline_images(self, message_id: str, text: str) -> str:
        """Replace any `[Image: img_xxx]` occurrences in text with downloaded paths."""
        if "[Image:" not in text and "image_key" not in text:
            return text
        # lark-cli formats embedded images as "[Image: img_<key>]"
        pattern = re.compile(r"\[Image:\s*(img_[A-Za-z0-9_-]+)\s*\]")
        async def _replace_one(key: str) -> str:
            p = await self._download_image(message_id, key)
            return p if p else f"[Image: {key} (download failed)]"
        # Find all unique keys, download each once
        keys = list(dict.fromkeys(pattern.findall(text)))
        if not keys:
            return text
        for key in keys:
            replacement = await _replace_one(key)
            text = text.replace(f"[Image: {key}]", replacement)
        return text

    @staticmethod
    def _extract_image_key(content_raw) -> str | None:
        """Pull image_key out of an image message's content.

        Lark-cli flattens image content to "[Image: img_xxx]". Raw Feishu
        webhooks send JSON `{"image_key":"img_xxx"}`. Handle both.
        """
        if content_raw is None:
            return None
        if isinstance(content_raw, str):
            stripped = content_raw.strip()
            if stripped.startswith("{"):
                try:
                    return json.loads(stripped).get("image_key")
                except json.JSONDecodeError:
                    pass
            m = re.search(r"img_[A-Za-z0-9_-]+", stripped)
            return m.group(0) if m else None
        if isinstance(content_raw, dict):
            return content_raw.get("image_key")
        return None

    async def _download_image(self, message_id: str, image_key: str) -> str | None:
        """Download one image to ~/.feishu-bot-codex/inbox/. Returns abs path or None."""
        if not message_id or not image_key:
            return None
        # Stash images per-binding so two bindings don't trample each other.
        inbox = Path.home() / ".feishu-bot-codex" / "inbox" / self._tmux_session
        inbox.mkdir(parents=True, exist_ok=True)
        out_path = inbox / f"{int(time.time())}-{image_key}.png"
        try:
            return await self._lark.download_message_resource(
                message_id=message_id,
                file_key=image_key,
                out_path=str(out_path),
                resource_type="image",
            )
        except Exception as e:
            logger.warning("image download failed (msg=%s key=%s): %s", message_id, image_key, e)
            return None

    async def _react_quietly(self, message_id: str, emoji_type: str) -> None:
        try:
            await self._lark.add_reaction(message_id, emoji_type)
        except Exception as e:
            logger.warning("add_reaction failed for message %s: %s", message_id, e)

    async def _handle_menu(self, event: dict) -> None:
        ev = event.get("event", {})
        sender = ev.get("operator", {}).get("operator_id", {}).get("open_id", "")
        if self._allow_users is not None and sender not in self._allow_users:
            return
        event_key = ev.get("event_key", "")
        command = self._menu_command_map.get(event_key)
        if command is None:
            logger.info("unknown menu event_key: %s", event_key)
            return
        # Same split-send pattern as _handle_message — codex's completion
        # popup sometimes swallows an immediately-following Enter.
        self._tmux.send_keys(session=self._tmux_session, keys=command)
        await asyncio.sleep(0.4)
        self._tmux.send_keys(session=self._tmux_session, keys="\n")
