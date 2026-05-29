"""Per-binding coroutine group lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from feishu_bot_codex.config.binding import BindingConfig, BindingStore
from feishu_bot_codex.daemon.feishu import LarkCli
from feishu_bot_codex.daemon.inbound import InboundPipeline
from feishu_bot_codex.daemon.outbound import OutboundPipeline
from feishu_bot_codex.daemon.ratelimit import TokenBucket
from feishu_bot_codex.daemon.state import BindingRuntimeState
from feishu_bot_codex.daemon.tmux import Tmux

logger = logging.getLogger(__name__)


def _same_path(a: str, b: str) -> bool:
    """True if two paths point at the same location after normalization
    (resolve symlinks/.., normalize case on case-insensitive filesystems)."""
    try:
        pa = os.path.normcase(str(Path(a).resolve()))
        pb = os.path.normcase(str(Path(b).resolve()))
        return pa == pb
    except Exception:
        return a == b


@dataclass
class RunningBinding:
    """Live state of one binding that's actively mirroring."""

    config: BindingConfig
    state: BindingRuntimeState
    outbound: OutboundPipeline
    inbound: InboundPipeline
    tasks: list[asyncio.Task] = field(default_factory=list)


class Orchestrator:
    """Owns per-binding coroutine groups; lifecycle is start/stop per cwd."""

    def __init__(
        self,
        store: BindingStore,
        tmux_factory: Callable[[str], Tmux],
        lark_factory: Callable[[BindingConfig], LarkCli],
        data_dir: Path,
    ) -> None:
        self._store = store
        self._tmux_factory = tmux_factory
        self._lark_factory = lark_factory
        self._data_dir = Path(data_dir)
        self._running: dict[str, RunningBinding] = {}
        self._chat_id_for: dict[str, str] = {}
        self.pending_binds: dict[str, asyncio.Task] = {}

    def set_chat_id(self, binding_name: str, chat_id: str) -> None:
        """Test/wiring helper: tell the orchestrator which chat_id to send to."""
        self._chat_id_for[binding_name] = chat_id

    def get_running(self, name: str) -> RunningBinding | None:
        return self._running.get(name)

    def list_running(self) -> list[str]:
        return sorted(self._running.keys())

    async def start_binding(self, cwd: str, jsonl_path: Path | None = None) -> RunningBinding:
        cfg = self._store.find_by_cwd(cwd)
        if cfg is None:
            raise KeyError(f"no binding for cwd {cwd!r}")
        if cfg.name in self._running:
            raise RuntimeError(f"binding {cfg.name!r} is already running")

        tmux = self._tmux_factory(cfg.name)
        if not tmux.has_session(cfg.tmux_session):
            raise RuntimeError(
                f"tmux session {cfg.tmux_session!r} is not running — start Claude first"
            )

        lark = self._lark_factory(cfg)
        state_path = self._data_dir / f"state-{cfg.name}.json"
        state = BindingRuntimeState.load(cfg.name, state_path)

        # Feishu app-bot messaging cap is ~50/sec, 1000/min per tenant.
        # Burst capacity 50 = 1s headroom; replay drains in ~10 min for 30k turns.
        bucket = TokenBucket(rate_per_sec=45, capacity=50)

        if jsonl_path is None:
            jsonl_path = self._guess_jsonl_path(cfg)

        chat_id = self._chat_id_for.get(cfg.name, "")
        outbound = OutboundPipeline(
            jsonl_path=jsonl_path,
            chat_id=chat_id,
            project_name=cfg.name,
            state=state,
            lark=lark,
            bucket=bucket,
            render_style=cfg.render_style,
            state_path=state_path,
        )
        # Wire inbound's chat_id discovery to outbound's bootstrap.
        # When the user sends their first message to the bot in Feishu, the
        # inbound pipeline captures the chat_id, then calls this callback
        # which (1) sets outbound's chat_id and (2) replays the full Claude
        # jsonl history into that chat.
        def _on_chat_discovered(chat_id: str):
            return outbound.bootstrap_with_chat_id(chat_id)

        from feishu_bot_codex.menu_template import (
            DEFAULT_MENU_COMMAND_MAP,
            DEFAULT_MENU_SPECIAL_MAP,
            DEFAULT_MENU_YESNO_MAP,
        )
        from feishu_bot_codex.rendering.status import (
            build_status_card,
            build_help_card,
            build_slash_card,
            build_bindings_card,
            build_find_card,
            search_jsonl,
        )

        # Closures over binding context — captured at start_binding time.
        # Note: state.chat_id reads the LIVE state object each call (because
        # the closure captures the reference, not a snapshot), so post-bootstrap
        # changes are picked up.
        def _status_card() -> dict:
            return build_status_card(
                binding_name=cfg.name,
                project_dir=cfg.project_dir,
                tmux_session=cfg.tmux_session,
                feishu_app_id=cfg.feishu_app_id,
                render_style=cfg.render_style,
                jsonl_path=jsonl_path,
                chat_id=state.chat_id,
            )
        def _help_card() -> dict:
            return build_help_card(binding_name=cfg.name)
        def _slash_card() -> dict:
            return build_slash_card(binding_name=cfg.name, command_map=DEFAULT_MENU_COMMAND_MAP)
        def _bindings_card() -> dict:
            # Read all bindings; for each, snapshot file mtime if jsonl exists
            rows: list[dict] = []
            for bc in self._store.all():
                row = {
                    "name": bc.name,
                    "project_dir": bc.project_dir,
                    "tmux_session": bc.tmux_session,
                    "feishu_app_id": bc.feishu_app_id,
                }
                try:
                    sp = self._data_dir / f"state-{bc.name}.json"
                    if sp.exists():
                        import json as _json
                        row["chat_id"] = _json.loads(sp.read_text()).get("chat_id", "")
                except Exception:
                    pass
                try:
                    jp = self._guess_jsonl_path(bc)
                    if jp.exists():
                        row["mtime"] = jp.stat().st_mtime
                except Exception:
                    pass
                rows.append(row)
            return build_bindings_card(rows)
        def _find_card(query: str) -> dict:
            matches = search_jsonl(jsonl_path, query) if jsonl_path else []
            return build_find_card(binding_name=cfg.name, keyword=query, matches=matches)
        def _current_chat_id() -> str:
            return state.chat_id

        inbound = InboundPipeline(
            tmux_session=cfg.tmux_session,
            tmux=tmux,
            lark=lark,
            menu_command_map=DEFAULT_MENU_COMMAND_MAP,
            menu_special_map=DEFAULT_MENU_SPECIAL_MAP,
            menu_yesno_map=DEFAULT_MENU_YESNO_MAP,
            allow_users=set(cfg.allow_users) if cfg.allow_users else None,
            max_message_length=cfg.max_message_length,
            on_chat_id_discovered=_on_chat_discovered,
            status_card_builder=_status_card,
            help_card_builder=_help_card,
            slash_card_builder=_slash_card,
            bindings_card_builder=_bindings_card,
            find_card_builder=_find_card,
            chat_id_provider=_current_chat_id,
            # If state already has chat_id (prior bootstrap), skip the
            # "consume first message" behavior on this restart.
            bootstrap_complete=bool(state.chat_id),
        )

        # Initial backlog process. If chat_id is already known from a prior
        # bootstrap (persisted in state), this will send/update cards. If not,
        # _flush_current_turn silently skips and replay waits for first message.
        await outbound.process_backlog()

        running = RunningBinding(config=cfg, state=state, outbound=outbound, inbound=inbound)

        # Long-running tasks
        running.tasks.append(asyncio.create_task(
            self._outbound_loop(running, jsonl_path, state_path),
            name=f"outbound-{cfg.name}",
        ))
        running.tasks.append(asyncio.create_task(
            self._inbound_loop(running),
            name=f"inbound-{cfg.name}",
        ))
        self._running[cfg.name] = running
        marker = self._data_dir / f"running-{cfg.name}"
        marker.write_text(json.dumps({"jsonl_path": str(jsonl_path)}))
        return running

    async def stop_binding(self, cwd: str) -> None:
        cfg = self._store.find_by_cwd(cwd)
        if cfg is None:
            raise KeyError(f"no binding for cwd {cwd!r}")
        running = self._running.pop(cfg.name, None)
        if running is None:
            return
        for task in running.tasks:
            task.cancel()
        for task in running.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        marker = self._data_dir / f"running-{cfg.name}"
        marker.unlink(missing_ok=True)

    async def stop_all(self) -> None:
        for name in list(self._running.keys()):
            cfg = self._store.find_by_name(name)
            if cfg:
                await self.stop_binding(cwd=cfg.project_dir)

    async def restore_from_disk(self) -> list[str]:
        """Re-attach every binding that's been used at least once.

        We do NOT rely on `running-<name>` marker files anymore. Markers get
        deleted on daemon shutdown (stop_all → stop_binding → marker.unlink),
        so a clean restart would orphan every binding until the user manually
        re-ran `feishu-bot-* start`. Instead we treat the state file
        (`state-<name>.json` with a persisted `chat_id`) as the source of
        truth — once a binding has been bootstrapped, it stays "live" until
        the user explicitly unbinds it.

        A binding is restored if:
          - it's in BindingStore (`bindings.toml`)
          - its tmux session is alive (otherwise: stale, user needs to
            re-run `feishu-bot-* shell` to relaunch the TUI)

        Bindings without a chat_id (never bootstrapped) are also restored so
        outbound starts watching jsonl immediately; outbound's
        `_effective_chat_id()` guard ensures no cards are sent until the user
        gives the bot a first message.
        """
        stale: list[str] = []
        for cfg in self._store.all():
            tmux = self._tmux_factory(cfg.name)
            if not tmux.has_session(cfg.tmux_session):
                stale.append(cfg.name)
                # Clean up any leftover marker
                (self._data_dir / f"running-{cfg.name}").unlink(missing_ok=True)
                continue
            # Prefer the jsonl path from the marker if still present (it pins
            # the daemon to the exact file in use). Otherwise re-guess.
            jsonl_path = None
            marker = self._data_dir / f"running-{cfg.name}"
            if marker.exists():
                try:
                    data = json.loads(marker.read_text())
                    jsonl_path_str = data.get("jsonl_path", "")
                    if jsonl_path_str:
                        jsonl_path = Path(jsonl_path_str)
                except Exception:
                    pass
            try:
                await self.start_binding(cwd=cfg.project_dir, jsonl_path=jsonl_path)
            except Exception:
                logger.exception("failed to restore binding %s", cfg.name)
                stale.append(cfg.name)
        return stale

    def _guess_jsonl_path(self, cfg: BindingConfig) -> Path:
        """Find newest jsonl for this binding.

        Codex variant default: prefer ~/.codex/sessions (this is the codex
        bot's natural backend). Only fall back to ~/.claude/projects when
        no codex session exists for this cwd — without this, a stale claude
        invocation in the same cwd could win the mtime tiebreak even though
        the user is driving codex.
        """
        import json
        home = Path.home()

        # Pass 1: codex backend
        codex_candidates: list[Path] = []
        sessions_root = home / ".codex" / "sessions"
        if sessions_root.exists():
            for path in sessions_root.glob("*/*/*/rollout-*.jsonl"):
                try:
                    with path.open("r", encoding="utf-8") as f:
                        first = f.readline().strip()
                    if not first:
                        continue
                    meta = json.loads(first)
                    if meta.get("type") != "session_meta":
                        continue
                    sess_cwd = meta.get("payload", {}).get("cwd")
                    # Normalize both sides: a session_meta.cwd with a trailing
                    # slash / symlink / (on Windows) drive-case difference must
                    # still match the bound project_dir, else we silently fall
                    # through to the claude backend / no-session.
                    if sess_cwd and _same_path(sess_cwd, cfg.project_dir):
                        codex_candidates.append(path)
                except Exception:
                    continue
        if codex_candidates:
            codex_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return codex_candidates[0]

        # Pass 2: claude backend (legacy / dual-agent users)
        encoded = cfg.project_dir.replace("/", "-").lstrip("-")
        claude_dir = home / ".claude" / "projects" / f"-{encoded}"
        if claude_dir.exists():
            claude_candidates = sorted(
                claude_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if claude_candidates:
                return claude_candidates[0]

        # Nothing found — return a placeholder under the preferred backend.
        if sessions_root.exists():
            return sessions_root / "no-session.jsonl"
        return claude_dir / "no-session.jsonl"

    async def _outbound_loop(self, running: RunningBinding, jsonl_path: Path, state_path: Path) -> None:
        """Watch jsonl, process new bytes on each change, persist state."""
        from feishu_bot_codex.daemon.jsonl_watcher import JsonlWatcher
        watcher = JsonlWatcher(jsonl_path)
        try:
            async for _ in watcher.changes():
                try:
                    await running.outbound.process_backlog()
                    running.state.save(state_path)
                except Exception:
                    logger.exception("outbound process failed for %s", running.config.name)
        except asyncio.CancelledError:
            running.state.save(state_path)
            raise

    async def _inbound_loop(self, running: RunningBinding) -> None:
        try:
            # Real lark-cli streams forever; Fake drains the queue and returns.
            await running.inbound.process_until_idle(max_events=0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("inbound loop failed for %s", running.config.name)
