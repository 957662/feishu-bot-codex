"""Inbound pipeline: Feishu events → tmux send-keys (text/slash/menu/image/key)."""

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


# `!<word>` from Feishu → tmux/zellij special key. Case-insensitive, also
# accepts a small set of Chinese aliases so users can type "!中断" / "!上"
# without leaving Chinese input mode. The user-facing word is intentionally
# short — these are typed dozens of times a session.
KEY_COMMANDS: dict[str, str] = {
    # Cancel / interrupt current model turn (Esc in Claude / Codex)
    "esc": "Escape", "cancel": "Escape", "interrupt": "Escape",
    "中断": "Escape", "取消": "Escape", "停": "Escape",
    # Hard exit (Ctrl-C, twice usually quits the TUI)
    "^c": "C-c", "ctrl-c": "C-c", "exit": "C-c", "quit": "C-c", "退出": "C-c",
    # Other Ctrl combos
    "^d": "C-d", "ctrl-d": "C-d",
    "^l": "C-l", "clear-screen": "C-l",
    "^u": "C-u", "kill-line": "C-u",
    # Arrows / history navigation
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "上": "Up", "下": "Down", "左": "Left", "右": "Right",
    # Tab (completion)
    "tab": "Tab", "补全": "Tab",
    # Backspace
    "bs": "BSpace", "backspace": "BSpace", "删": "BSpace",
    # Bare Enter (for prompts already showing on screen)
    "enter": "Enter", "回车": "Enter",
}


# Common Y/N confirmation shortcuts. These send the letter + Enter atomically,
# so a flying-permission prompt can be answered with `!y` / `!n` / `!是` / `!否`.
YESNO_COMMANDS: dict[str, str] = {
    "y": "y", "yes": "y", "是": "y", "确认": "y", "ok": "y", "好": "y",
    "n": "n", "no": "n", "否": "n", "不": "n", "cancel-no": "n",
}

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
        menu_special_map: dict[str, str] | None = None,
        menu_yesno_map: dict[str, str] | None = None,
        allow_users: set[str] | None = None,
        max_message_length: int = 8000,
        event_key: str = "im.message.receive_v1",
        on_chat_id_discovered=None,
        bootstrap_complete: bool = False,
        status_card_builder=None,
        help_card_builder=None,
        slash_card_builder=None,
        bindings_card_builder=None,
        find_card_builder=None,
        chat_id_provider=None,
    ) -> None:
        self._tmux_session = tmux_session
        self._tmux = tmux
        self._lark = lark
        self._menu_command_map = menu_command_map or {}
        self._menu_special_map = menu_special_map or {}
        self._menu_yesno_map = menu_yesno_map or {}
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
        # Optional callbacks for `!status` / `!help` self-reporting cards.
        # `status_card_builder()` returns a dict (the rendered card JSON).
        # `chat_id_provider()` returns the binding's chat_id at call time
        # (so it picks up post-bootstrap state without re-binding).
        self._status_card_builder = status_card_builder
        self._help_card_builder = help_card_builder
        self._slash_card_builder = slash_card_builder
        self._bindings_card_builder = bindings_card_builder
        # find_card_builder takes the keyword and returns a card dict
        self._find_card_builder = find_card_builder
        self._chat_id_provider = chat_id_provider

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

        # ---- File messages (.pdf / .txt / .py / .md / etc.) ----
        # Same idea as image: download into inbox, inject absolute path as
        # text so the TUI's @file_reference parser picks it up.
        if message_type == "file":
            await self._handle_file(message_id, content_raw)
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

        # ---- Keyboard / control commands (! prefix) ----
        # Single-token `!<word>` → special key (Esc / Up / Down / Tab / ...).
        # `!y` / `!n` / `!是` / `!否` → write letter + Enter (one-tap y/n).
        if await self._maybe_handle_key_command(text, message_id):
            return

        # Inline images inside a text message: lark-cli's text converter
        # rewrites embedded image elements to "[Image: img_xxx]". Scan for
        # that pattern, download each, and replace with the absolute path.
        text = await self._inline_images(message_id, text)

        if len(text) > self._max_message_length:
            text = text[: self._max_message_length] + "\n...[truncated]"
        # Ack the user's message with a reaction so they see Claude received it.
        if message_id:
            asyncio.create_task(self._react_quietly(message_id, "LOVE"))
        # Inject the body. Multi-line text uses Alt+Enter as a soft newline
        # so the TUI sees each \n as "next line of input" instead of "submit
        # now"; the final Enter commits the whole thing.
        await self._inject_multiline_text(text)

    async def _maybe_handle_key_command(self, text: str, message_id: str) -> bool:
        """If `text` is a `!<word>` keyboard command, dispatch it and return True."""
        stripped = text.strip()
        if not stripped.startswith("!"):
            return False
        word = stripped[1:].strip().lower()
        if not word:
            return False

        # Status / help / slash / bindings / find — sent BACK as a card, not to the TUI.
        if word in ("status", "state", "状态", "现状"):
            await self._send_status_card(message_id)
            return True
        if word in ("help", "帮助", "?", "？"):
            await self._send_help_card(message_id)
            return True
        if word in ("slash", "命令", "commands", "cmd"):
            await self._send_slash_card(message_id)
            return True
        if word in ("list", "bindings", "projects", "项目"):
            await self._send_bindings_card(message_id)
            return True
        # `!find <keyword>` / `!搜 <keyword>` — keep the rest of the message
        # body as the search query (case-preserving).
        for kw_prefix in ("find ", "搜 ", "search "):
            if stripped[1:].lower().lstrip().startswith(kw_prefix):
                # Extract the original-case query from `text`
                # (use stripped[1:] to drop the `!`, then strip the prefix)
                rest = stripped[1:].lstrip()
                # remove the prefix word + any following whitespace
                idx = rest.lower().find(kw_prefix)
                if idx == 0:
                    query = rest[len(kw_prefix):].strip()
                else:
                    query = rest.strip()
                if query:
                    await self._send_find_card(message_id, query)
                    return True

        special = KEY_COMMANDS.get(word)
        if special is not None:
            try:
                self._tmux.send_special(session=self._tmux_session, key=special)
                if message_id:
                    asyncio.create_task(self._react_quietly(message_id, "OK"))
                logger.info("key command %r → %s", word, special)
            except Exception as e:
                logger.warning("send_special %r failed: %s", special, e)
            return True

        yn = YESNO_COMMANDS.get(word)
        if yn is not None:
            try:
                self._tmux.send_keys(session=self._tmux_session, keys=yn)
                await asyncio.sleep(0.2)
                self._tmux.send_special(session=self._tmux_session, key="Enter")
                if message_id:
                    asyncio.create_task(self._react_quietly(message_id, "OK"))
                logger.info("yes/no command %r → %r", word, yn)
            except Exception as e:
                logger.warning("y/n %r failed: %s", word, e)
            return True

        # `!` prefix but unrecognized — log + react with question; fall through
        # to normal text injection so we don't silently drop user input.
        logger.info("unknown key command %r — treating as literal text", word)
        if message_id:
            asyncio.create_task(self._react_quietly(message_id, "QUESTION"))
        return False

    async def _inject_multiline_text(self, text: str) -> None:
        """Type text into the TUI, preserving line breaks via Alt+Enter."""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                self._tmux.send_keys(session=self._tmux_session, keys=line)
                await asyncio.sleep(0.05)
            if i < len(lines) - 1:
                # Soft newline inside the TUI input box. Both Claude Code
                # and Codex map M-Enter (Alt+Enter) to "add a new line to
                # the current message", not "submit".
                self._tmux.send_special(session=self._tmux_session, key="M-Enter")
                await asyncio.sleep(0.05)
        # Settle, then commit.
        await asyncio.sleep(0.4)
        self._tmux.send_special(session=self._tmux_session, key="Enter")

    async def _handle_image(self, message_id: str, content_raw) -> None:
        """Standalone image message: download + inject absolute file path."""
        image_key = self._extract_image_key(content_raw)
        if not image_key:
            logger.warning("image message %s missing image_key; content=%r", message_id, content_raw)
            return
        path = await self._download_image(message_id, image_key)
        if path is None:
            return
        # Send the absolute path as input text. Claude / Codex parse pasted
        # paths as image attachments.
        self._tmux.send_keys(session=self._tmux_session, keys=path + "\n")

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

    async def _handle_file(self, message_id: str, content_raw) -> None:
        """File message → download to inbox + inject absolute path into TUI."""
        info = self._extract_file_info(content_raw)
        if not info:
            logger.warning("file message %s missing file_key; content=%r", message_id, content_raw)
            return
        file_key, file_name = info
        path = await self._download_file(message_id, file_key, file_name)
        if path is None:
            return
        # Same split-send pattern as text — type the body, wait, then Enter.
        # Prefix with "File:" so codex doesn't interpret a leading `/` (from
        # the absolute path) as a slash command.
        self._tmux.send_keys(session=self._tmux_session, keys=f"File: {path}")
        await asyncio.sleep(0.4)
        self._tmux.send_special(session=self._tmux_session, key="Enter")

    @staticmethod
    def _extract_file_info(content_raw) -> tuple[str, str] | None:
        """Pull (file_key, file_name) out of a file message's content."""
        if content_raw is None:
            return None
        if isinstance(content_raw, dict):
            key = content_raw.get("file_key")
            name = content_raw.get("file_name", "")
            return (key, name) if key else None
        if isinstance(content_raw, str):
            stripped = content_raw.strip()
            if stripped.startswith("{"):
                try:
                    d = json.loads(stripped)
                    key = d.get("file_key")
                    if key:
                        return (key, d.get("file_name", ""))
                except json.JSONDecodeError:
                    pass
            # lark-cli's text rendering wraps file as <file key="file_xxx" name="..."/>
            m = re.search(r'file\s+key="(file_[A-Za-z0-9_-]+)"(?:\s+name="([^"]*)")?', stripped)
            if m:
                return (m.group(1), m.group(2) or "")
            # bare file_xxx token
            m = re.search(r'(file_[A-Za-z0-9_-]+)', stripped)
            if m:
                return (m.group(1), "")
        return None

    async def _download_file(self, message_id: str, file_key: str, file_name: str) -> str | None:
        if not message_id or not file_key:
            return None
        inbox = Path.home() / ".feishu-bot-codex" / "inbox" / self._tmux_session
        inbox.mkdir(parents=True, exist_ok=True)
        # Keep the original extension when we know it. Sanitize to avoid
        # path traversal: only the basename is used.
        safe_name = os.path.basename(file_name) if file_name else file_key
        # Strip any path separators just in case
        safe_name = safe_name.replace("/", "_").replace("\\", "_")
        if not safe_name:
            safe_name = file_key
        out_path = inbox / f"{int(time.time())}-{safe_name}"
        try:
            return await self._lark.download_message_resource(
                message_id=message_id,
                file_key=file_key,
                out_path=str(out_path),
                resource_type="file",
            )
        except Exception as e:
            logger.warning("file download failed (msg=%s key=%s): %s", message_id, file_key, e)
            return None

    async def _react_quietly(self, message_id: str, emoji_type: str) -> None:
        try:
            await self._lark.add_reaction(message_id, emoji_type)
        except Exception as e:
            logger.warning("add_reaction failed for message %s: %s", message_id, e)

    async def _send_status_card(self, message_id: str) -> None:
        """Build + push a status card back to the binding's chat."""
        if self._status_card_builder is None or self._chat_id_provider is None:
            logger.info("!status requested but no builder/chat_id provider configured")
            return
        chat_id = self._chat_id_provider()
        if not chat_id:
            return
        try:
            card = self._status_card_builder()
            await self._lark.send_card(chat_id=chat_id, card=card)
            if message_id:
                asyncio.create_task(self._react_quietly(message_id, "OK"))
        except Exception as e:
            logger.warning("send status card failed: %s", e)

    async def _send_help_card(self, message_id: str) -> None:
        await self._send_meta_card(message_id, self._help_card_builder, "help")

    async def _send_slash_card(self, message_id: str) -> None:
        await self._send_meta_card(message_id, self._slash_card_builder, "slash")

    async def _send_bindings_card(self, message_id: str) -> None:
        await self._send_meta_card(message_id, self._bindings_card_builder, "bindings")

    async def _send_find_card(self, message_id: str, query: str) -> None:
        if self._find_card_builder is None or self._chat_id_provider is None:
            return
        chat_id = self._chat_id_provider()
        if not chat_id:
            return
        try:
            card = self._find_card_builder(query)
            await self._lark.send_card(chat_id=chat_id, card=card)
            if message_id:
                asyncio.create_task(self._react_quietly(message_id, "OK"))
        except Exception as e:
            logger.warning("send find card failed: %s", e)

    async def _send_meta_card(self, message_id: str, builder, name: str) -> None:
        """Generic helper for builders that take no args and return a card dict."""
        if builder is None or self._chat_id_provider is None:
            return
        chat_id = self._chat_id_provider()
        if not chat_id:
            return
        try:
            card = builder()
            await self._lark.send_card(chat_id=chat_id, card=card)
            if message_id:
                asyncio.create_task(self._react_quietly(message_id, "OK"))
        except Exception as e:
            logger.warning("send %s card failed: %s", name, e)

    async def _handle_menu(self, event: dict) -> None:
        ev = event.get("event", {})
        sender = ev.get("operator", {}).get("operator_id", {}).get("open_id", "")
        if self._allow_users is not None and sender not in self._allow_users:
            return
        event_key = ev.get("event_key", "")

        # 1. Special key (Escape / Up / C-c / ...)
        special = self._menu_special_map.get(event_key)
        if special is not None:
            try:
                self._tmux.send_special(session=self._tmux_session, key=special)
            except Exception as e:
                logger.warning("menu special %r failed: %s", special, e)
            return

        # 2. Yes/No shortcut (letter + Enter)
        yn = self._menu_yesno_map.get(event_key)
        if yn is not None:
            try:
                self._tmux.send_keys(session=self._tmux_session, keys=yn)
                await asyncio.sleep(0.2)
                self._tmux.send_special(session=self._tmux_session, key="Enter")
            except Exception as e:
                logger.warning("menu y/n %r failed: %s", yn, e)
            return

        # 3. Text command (slash etc.)
        command = self._menu_command_map.get(event_key)
        if command is None:
            logger.info("unknown menu event_key: %s", event_key)
            return
        self._tmux.send_keys(session=self._tmux_session, keys=command)
        await asyncio.sleep(0.2)
        self._tmux.send_special(session=self._tmux_session, key="Enter")
