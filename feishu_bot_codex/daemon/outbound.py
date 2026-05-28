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
    collect_file_paths,
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
        # Image upload caching:
        # - successful path → image_key (avoid re-uploading the same file
        #   when Claude/Codex references it across multiple turns).
        # - failed paths are remembered so we don't keep retrying a file the
        #   daemon can't read (e.g. ~/Desktop blocked by macOS TCC sandbox,
        #   or a path the model fabricated that doesn't actually exist).
        self._image_key_cache: dict[str, str] = {}
        self._failed_image_paths: set[str] = set()
        # Files we've already pushed to the user as standalone messages.
        # Without this, every flush during a long turn would re-send the
        # same file 20× because the path stays in the jsonl tool_result.
        self._sent_file_paths: set[str] = set()

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
            # User event closes the previous turn → flush as FINAL (no spinner).
            await self._flush_current_turn(in_progress=False)
            self._current_turn = Turn(user_event=event)
            self._state.reset_current_turn()
            return

        if self._current_turn is None:
            self._current_turn = Turn(user_event=None)

        self._current_turn.assistant_events.append(event)

    async def _flush_current_turn(self, in_progress: bool = True) -> None:
        """Render and send the current turn as a single card.

        `in_progress=True` (the default, used for batch-end flushes during
        active streaming) adds a "⏳ 思考中…" pacer to the card. The next
        batch-end flush rewrites the card; once a new user event arrives,
        the turn is flushed one final time WITHOUT the pacer.

        We also treat a turn as "settled" (i.e. NOT in_progress) when the
        jsonl hasn't been written for >5 seconds — covers the case where
        the very last batch happens to be the model's final output.
        """
        if self._current_turn is None:
            return
        chat_id = self._effective_chat_id()
        if not chat_id:
            return

        # Liveness heuristic: jsonl mtime within last 5s → still streaming.
        if in_progress and self._jsonl_path.exists():
            import time as _time
            try:
                age = _time.time() - self._jsonl_path.stat().st_mtime
                if age > 5.0:
                    in_progress = False
            except OSError:
                pass

        # Upload any images referenced in this turn so the card can embed them.
        image_keys = await self._upload_turn_images(self._current_turn)

        card = render_turn_to_card(
            self._current_turn,
            project_name=self._project_name,
            render_style=self._render_style,
            image_keys=image_keys,
            in_progress=in_progress,
        )
        if not card.get("body", {}).get("elements"):
            return
        await self._send_or_update_with_card(card)
        # Push any non-image files referenced in this turn as standalone
        # Feishu file messages (cards can embed images but not files).
        # Only fire when the turn has settled — avoids sending the file once
        # per polling cycle while the model is still mid-tool.
        if not in_progress:
            await self._send_attached_files(self._current_turn)

    async def _send_attached_files(self, turn: Turn) -> None:
        """Find file paths referenced in this turn; upload + push each as a
        standalone Feishu file message. Idempotent — paths already sent in
        a prior flush are skipped.
        """
        import os
        chat_id = self._effective_chat_id()
        if not chat_id:
            return
        paths = collect_file_paths(turn)
        for path in paths:
            if path in self._sent_file_paths:
                continue
            if path in self._failed_image_paths:
                # Treat the failed-image set as a shared blocklist for sandbox-
                # restricted paths so we don't waste API calls.
                continue
            if not os.path.exists(path):
                self._sent_file_paths.add(path)
                continue
            # Don't push huge files (Feishu limit 30MB; we cap at 25MB)
            try:
                if os.path.getsize(path) > 25 * 1024 * 1024:
                    logger.info("skip large file >25MB: %s", path)
                    self._sent_file_paths.add(path)
                    continue
            except OSError:
                continue
            await self._bucket.acquire()
            try:
                file_key = await self._lark.upload_file(path)
            except Exception as e:
                logger.warning("upload_file failed for %s: %s (skipping)", path, e)
                self._sent_file_paths.add(path)
                continue
            await self._bucket.acquire()
            try:
                await self._lark.send_file(
                    chat_id=chat_id,
                    file_key=file_key,
                    file_name=os.path.basename(path),
                )
                self._sent_file_paths.add(path)
            except Exception as e:
                logger.warning("send_file failed for %s: %s", path, e)
                self._sent_file_paths.add(path)

    async def _upload_turn_images(self, turn: Turn) -> dict[str, str]:
        """Upload all local image files referenced in `turn`. Returns path → image_key.

        Uses two-tier caching so we never re-upload the same file twice,
        and never re-try a path the daemon proved it can't read (file is
        missing, or macOS TCC blocks daemon's read of ~/Desktop, etc.).
        Without this, a turn that fabricates the same fake path 20 times
        would have spent 20 rate-limit tokens per flush — starving text
        cards behind a queue of doomed uploads.
        """
        import os
        paths = collect_image_paths(turn)
        if not paths:
            return {}
        result: dict[str, str] = {}
        for path in paths:
            if path in self._image_key_cache:
                result[path] = self._image_key_cache[path]
                continue
            if path in self._failed_image_paths:
                continue
            if not os.path.exists(path):
                self._failed_image_paths.add(path)
                continue
            await self._bucket.acquire()
            try:
                key = await self._lark.upload_image(path)
                self._image_key_cache[path] = key
                result[path] = key
            except Exception as e:
                logger.warning("upload_image failed for %s: %s (caching as failed)", path, e)
                self._failed_image_paths.add(path)
        return result

    async def _send_or_update_with_card(self, card: dict) -> None:
        await self._bucket.acquire()
        try:
            if self._state.current_turn_card_id is None:
                # Idempotency key (≤50 chars, [A-Za-z0-9_-] only — Feishu uuid limit).
                # MUST be unique per turn or Feishu's de-duplication will fold
                # every turn into the same card_id. Claude jsonl events have
                # a real `uuid`; Codex events DON'T (uuid="") so we fall back
                # to a hash of the user event's timestamp + first 80 chars of
                # its text.
                import hashlib
                ue = self._current_turn.user_event if self._current_turn else None
                if ue and ue.uuid:
                    seed = ue.uuid
                elif ue:
                    ts = ue.raw.get("timestamp", "")
                    seed = f"{ts}|{ue.text()[:80]}"
                else:
                    seed = f"orphan|{self._state.binding_name}|{self._state.jsonl_offset}"
                short_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
                key = f"fbc{short_id}"  # ~19 chars, safe under Feishu's 50-char uuid limit
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
