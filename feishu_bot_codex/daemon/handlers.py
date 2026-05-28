"""Daemon op handlers — async generators yielding ResponseEvents."""

from __future__ import annotations

from typing import AsyncIterator

from feishu_bot_codex.proto import DoneEvent, ResponseEvent, ResultEvent


async def handle_ping(args: dict) -> AsyncIterator[ResponseEvent]:
    """Liveness check. Yields a single ok result and done."""
    yield ResultEvent(ok=True, data={"pong": True}, error=None)
    yield DoneEvent()


from feishu_bot_codex.config.binding import BindingStore


def _binding_summary(b) -> dict:
    return {
        "name": b.name,
        "project_dir": b.project_dir,
        "tmux_session": b.tmux_session,
        "feishu_app_id": b.feishu_app_id,
        "render_style": b.render_style,
    }


async def handle_list(args: dict, store: BindingStore) -> AsyncIterator[ResponseEvent]:
    """Return all bindings as a list of summary dicts."""
    bindings = [_binding_summary(b) for b in store.all()]
    yield ResultEvent(ok=True, data={"bindings": bindings}, error=None)
    yield DoneEvent()


import time

import feishu_bot_codex

_DAEMON_START_TIME = time.time()


async def _not_implemented(op: str) -> AsyncIterator[ResponseEvent]:
    yield ResultEvent(ok=False, data=None, error=f"{op}: not yet implemented (later phase)")
    yield DoneEvent()


async def handle_bind(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("bind"):
        yield ev


async def handle_unbind(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("unbind"):
        yield ev


async def handle_start(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("start"):
        yield ev


async def handle_stop(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("stop"):
        yield ev


async def handle_config(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("config"):
        yield ev


async def handle_shell(args: dict) -> AsyncIterator[ResponseEvent]:
    async for ev in _not_implemented("shell"):
        yield ev


async def handle_status(args: dict) -> AsyncIterator[ResponseEvent]:
    yield ResultEvent(
        ok=True,
        data={
            "version": feishu_bot_codex.__version__,
            "uptime_seconds": int(time.time() - _DAEMON_START_TIME),
        },
        error=None,
    )
    yield DoneEvent()


from pathlib import Path

from feishu_bot_codex.daemon.orchestrator import Orchestrator


async def handle_start_with_orchestrator(args: dict, orchestrator: Orchestrator) -> AsyncIterator[ResponseEvent]:
    cwd = args.get("cwd", "")
    jsonl_path_str = args.get("jsonl_path")
    jsonl_path = Path(jsonl_path_str) if jsonl_path_str else None
    try:
        running = await orchestrator.start_binding(cwd=cwd, jsonl_path=jsonl_path)
        bootstrap_done = bool(running.state.chat_id)
        data = {
            "name": running.config.name,
            "tmux_session": running.config.tmux_session,
            "chat_id": running.state.chat_id,
            "bootstrap_done": bootstrap_done,
        }
        if not bootstrap_done:
            data["next"] = (
                f"Open Feishu app → search for the bot bound to '{running.config.name}' "
                f"(app_id {running.config.feishu_app_id}) → send any message to bootstrap. "
                f"Your entire current Claude conversation will load into that chat automatically."
            )
        yield ResultEvent(ok=True, data=data, error=None)
    except KeyError as e:
        yield ResultEvent(ok=False, data=None, error=str(e))
    except RuntimeError as e:
        yield ResultEvent(ok=False, data=None, error=str(e))
    yield DoneEvent()


async def handle_stop_with_orchestrator(args: dict, orchestrator: Orchestrator) -> AsyncIterator[ResponseEvent]:
    cwd = args.get("cwd", "")
    try:
        await orchestrator.stop_binding(cwd=cwd)
        yield ResultEvent(ok=True, data={"stopped": True}, error=None)
    except KeyError as e:
        yield ResultEvent(ok=False, data=None, error=str(e))
    yield DoneEvent()


from datetime import datetime, timezone

from feishu_bot_codex.config.binding import BindingConfig
from feishu_bot_codex.config.keychain import KeychainStore
from feishu_bot_codex.daemon.auth import bot_new
from feishu_bot_codex.daemon.menu import push_menu_with_fallback
from feishu_bot_codex.menu_template import build_menu_json
from feishu_bot_codex.proto import LogEvent, ProgressEvent, QRCodeEvent


def _extract_app_id_from_larkcli(profile_name: str) -> str | None:
    """Look up the Feishu app_id for a lark-cli profile.

    Primary method: shell out to `lark-cli profile list --json` and find the
    entry matching profile_name. This works across lark-cli versions/storage
    layouts.

    Fallback: read ~/.lark-cli/config.json (the active profile's config) if it
    matches profile_name. Useful when shelling out is slow or unavailable.

    Returns None on failure — caller decides whether to abort the bind.
    """
    import json
    import subprocess
    from pathlib import Path

    # Primary: `lark-cli profile list` (default output is JSON)
    try:
        result = subprocess.run(
            ["lark-cli", "profile", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            profiles = json.loads(result.stdout.strip() or "[]")
            for prof in profiles:
                if prof.get("name") == profile_name:
                    app_id = prof.get("appId") or prof.get("app_id") or prof.get("AppId")
                    if app_id:
                        return app_id
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        pass

    # Fallback: read the active config file directly
    config_path = Path.home() / ".lark-cli" / "config.json"
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text())
            if data.get("profile") == profile_name:
                app_id = data.get("appId") or data.get("app_id") or data.get("AppId")
                if app_id:
                    return app_id
        except (json.JSONDecodeError, OSError):
            pass

    return None


async def handle_bind_with_orchestrator(
    args: dict,
    store: BindingStore,
    keychain: KeychainStore,
    auth_runner_factory,
    menu_pusher,
    data_dir,
    orchestrator=None,  # NEW — for pending_binds dict
) -> AsyncIterator[ResponseEvent]:
    import asyncio
    name = args.get("name", "")
    cwd = args.get("cwd", "")
    if not name or not cwd:
        yield ResultEvent(ok=False, data=None, error="bind requires name and cwd")
        yield DoneEvent()
        return

    if store.find_by_cwd(cwd) is not None:
        yield ResultEvent(ok=False, data=None, error=f"cwd already bound: {cwd}")
        yield DoneEvent()
        return
    if store.find_by_name(name) is not None:
        yield ResultEvent(ok=False, data=None, error=f"name already exists: {name}")
        yield DoneEvent()
        return
    if orchestrator is not None and name in orchestrator.pending_binds:
        yield ResultEvent(ok=False, data=None, error=f"bind for {name!r} already in progress")
        yield DoneEvent()
        return

    yield LogEvent(level="info", msg="Starting Feishu OAuth flow (扫码新建 App)...")

    loop = asyncio.get_event_loop()
    qr_future: asyncio.Future = loop.create_future()

    async def on_auth_event(event: dict) -> None:
        if event.get("type") == "qrcode" and not qr_future.done():
            qr_future.set_result((event.get("ascii", ""), event.get("url", "")))

    async def background_finish() -> None:
        from pathlib import Path
        import logging
        log = logging.getLogger("feishu_bot_codex.bind")
        try:
            try:
                await bot_new(runner=auth_runner_factory(name), on_event=on_auth_event)
            except RuntimeError as e:
                # OAuth failed — NOT saving a binding with placeholder app_id.
                # User can retry /bot-new <same-name> after fixing the root cause.
                log.error("Bind %r FAILED: %s", name, e)
                return
            # bot_new succeeded → user completed scan, lark-cli profile saved
            app_id = _extract_app_id_from_larkcli(name)
            if not app_id:
                log.error(
                    "Bind %r: lark-cli auth completed but couldn't extract app_id from "
                    "any known profile path. NOT saving binding to avoid stale state.",
                    name,
                )
                return
            secret_ref = f"feishu-bot-codex.{name}.app_secret"
            try:
                keychain.put(secret_ref, "")  # lark-cli manages real secret
            except Exception:
                pass  # keychain may not be writable in test envs

            binding = BindingConfig(
                name=name,
                project_dir=cwd,
                tmux_session=f"codex-{name}",
                feishu_app_id=app_id,
                secret_ref=secret_ref,
                created_at=datetime.now(timezone.utc),
            )
            store.add(binding)
            log.info("Bind %r SAVED: app_id=%s project_dir=%s", name, app_id, cwd)

            if menu_pusher is not None:
                try:
                    menu_json = build_menu_json()
                    await push_menu_with_fallback(
                        lark_menu=menu_pusher,
                        app_id=app_id,
                        menu_json=menu_json,
                        fallback_dir=Path(data_dir) / "menus",
                        binding_name=name,
                    )
                except Exception:
                    pass  # best-effort
        finally:
            if orchestrator is not None:
                orchestrator.pending_binds.pop(name, None)

    # Launch the background task. NOTE: the task reference is held in pending_binds
    # so it isn't garbage collected after this handler returns.
    task = asyncio.create_task(background_finish())
    if orchestrator is not None:
        orchestrator.pending_binds[name] = task

    # Wait briefly for the QR URL to appear. lark-cli outputs the QR within ~3-5 seconds.
    try:
        ascii_qr, url = await asyncio.wait_for(qr_future, timeout=30.0)
        yield QRCodeEvent(ascii=ascii_qr, url=url)
        yield ResultEvent(
            ok=True,
            data={
                "name": name,
                "url": url,
                "status": "awaiting_scan",
                "instructions": "Open the URL above in your browser, scan with Feishu mobile to authorize. Binding will be saved automatically.",
            },
            error=None,
        )
    except asyncio.TimeoutError:
        # Cancel the task — it never produced a URL
        task.cancel()
        if orchestrator is not None:
            orchestrator.pending_binds.pop(name, None)
        yield ResultEvent(
            ok=False,
            data=None,
            error="OAuth flow did not produce a URL within 30 seconds. Check daemon logs.",
        )

    yield DoneEvent()


async def handle_unbind_with_orchestrator(
    args: dict,
    store: BindingStore,
    keychain: KeychainStore,
) -> AsyncIterator[ResponseEvent]:
    name = args.get("name", "")
    binding = store.find_by_name(name)
    if binding is None:
        yield ResultEvent(ok=False, data=None, error=f"no binding named {name!r}")
        yield DoneEvent()
        return
    store.remove(name)
    keychain.delete(binding.secret_ref)
    yield ResultEvent(ok=True, data={"removed": name}, error=None)
    yield DoneEvent()
