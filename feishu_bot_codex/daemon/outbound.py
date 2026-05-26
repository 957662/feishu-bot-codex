"""Outbound pipeline: jsonl tail → group into turns → send/update Feishu cards."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from feishu_bot_codex.daemon.feishu import LarkCli
from feishu_bot_codex.daemon.ratelimit import TokenBucket
from feishu_bot_codex.daemon.state import BindingRuntimeState
from feishu_bot_codex.rendering.turn import (
    JsonlEvent,
    Turn,
    collect_image_paths,
    render_turn_to_card,
)

logger = logging.getLogger(__name__)


class OutboundPipeline:
    """Read jsonl events past state.jsonl_offset, render turns, send/update cards."""

    def __init__(
        self,
        jsonl_path: Path,
        chat_id: str,
        project_name: str,
        state: BindingRuntimeState,
        lark: LarkCli,
        bucket: TokenBucket,
        render_style: str = "rich",
        state_path: Path | None = None,
    ) -> None:
        self._jsonl_path = Path(jsonl_path)
        self._chat_id = chat_id
        self._project_name = project_name
        self._state = state
        self._lark = lark
        self._bucket = bucket
        self._render_style = render_style
        self._state_path = Path(state_path) if state_path else None
        self._current_turn: Turn | None = None

    async def process_backlog(self) -> None:
        """Read new bytes from jsonl past current offset; render any new turns.

        Strategy: accumulate all events for a turn FIRST, send ONCE per turn
        when the turn closes (user event arrives, or stream ends). Avoids
        sending dozens of intermediate "in-progress" cards per turn.
        """
        if not self._jsonl_path.exists():
            return
        size = self._jsonl_path.stat().st_size
        if size <= self._state.jsonl_offset:
            return

        with self._jsonl_path.open("rb") as f:
            f.seek(self._state.jsonl_offset)
            new_bytes = f.read()
        self._state.jsonl_offset = size

        lines = new_bytes.decode("utf-8", errors="replace").splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = JsonlEvent.from_dict(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed jsonl line: %r", line[:80])
                continue
            await self._handle_event(event)
        # End of batch: flush the final turn (no following user event to trigger it)
        await self._flush_current_turn()

    async def _handle_event(self, event: JsonlEvent) -> None:
        if event.role == "user" and not event.has_only_tool_results():
            # User event closes the previous turn → flush it.
            await self._flush_current_turn()
            self._current_turn = Turn(user_event=event)
            self._state.reset_current_turn()
            return

        if self._current_turn is None:
            self._current_turn = Turn(user_event=None)

        self._current_turn.assistant_events.append(event)

    async def _flush_current_turn(self) -> None:
        """Render and send the current turn as a single card. No-op on empty."""
        if self._current_turn is None:
            return
        chat_id = self._effective_chat_id()
        if not chat_id:
            return

        # Upload any images referenced in this turn so the card can embed them.
        # Failures are tolerated — the path stays in the markdown as text.
        image_keys = await self._upload_turn_images(self._current_turn)

        card = render_turn_to_card(
            self._current_turn,
            project_name=self._project_name,
            render_style=self._render_style,
            image_keys=image_keys,
        )
        if not card.get("body", {}).get("elements"):
            return
        await self._send_or_update_with_card(card)

    async def _upload_turn_images(self, turn: Turn) -> dict[str, str]:
        """Upload all local image files referenced in `turn`. Returns path → image_key."""
        import os
        paths = collect_image_paths(turn)
        if not paths:
            return {}
        result: dict[str, str] = {}
        for path in paths:
            if not os.path.exists(path):
                continue
            # Rate-limited: each upload counts against the same bucket so we
            # don't blow Feishu's app-bot quota with a burst.
            await self._bucket.acquire()
            try:
                key = await self._lark.upload_image(path)
                result[path] = key
            except Exception as e:
                logger.warning("upload_image failed for %s: %s", path, e)
        return result

    async def _send_or_update_with_card(self, card: dict) -> None:
        await self._bucket.acquire()
        try:
            if self._state.current_turn_card_id is None:
                # Idempotency key: max 32 chars + hex only (Feishu constraint).
                # Use last 16 hex chars of user_uuid (or random fallback).
                import hashlib
                user_uuid = self._current_turn.user_event.uuid if self._current_turn and self._current_turn.user_event else ""
                short_id = hashlib.sha256(user_uuid.encode()).hexdigest()[:16] if user_uuid else "noUUID"
                key = f"fbc{short_id}"  # ~19 chars, safe under any plausible limit
                msg_id = await self._lark.send_card(
                    chat_id=self._effective_chat_id(),
                    card=card,
                    idempotency_key=key,
                )
                self._state.set_current_turn_card(msg_id)
            else:
                await self._lark.update_card(
                    message_id=self._state.current_turn_card_id,
                    card=card,
                )
        except Exception as e:
            logger.warning(
                "send/update card failed for turn (binding=%s): %s",
                self._state.binding_name, e,
            )

    def _effective_chat_id(self) -> str:
        """chat_id source of truth: state (persisted) overrides constructor arg."""
        return self._state.chat_id or self._chat_id

    async def bootstrap_with_chat_id(self, chat_id: str) -> None:
        """Called when the user sends their first message to the bot.

        Sets the discovered chat_id and replays the FULL Claude jsonl history
        into that chat. Subsequent jsonl events stream normally via
        process_backlog calls from the orchestrator's outbound loop.

        chat_id is persisted BEFORE replay so a partial-replay failure doesn't
        lose the bootstrap state.
        """
        if not chat_id or chat_id == self._state.chat_id:
            return  # idempotent
        self._state.chat_id = chat_id
        self._state.jsonl_offset = 0
        self._state.reset_current_turn()
        self._current_turn = None
        # Persist chat_id immediately so a crash during replay still leaves
        # the binding bootstrapped. The orchestrator's outbound loop will
        # save again after each successful replay batch.
        if self._state_path is not None:
            try:
                self._state.save(self._state_path)
            except Exception:
                pass  # best-effort
        await self.process_backlog()
