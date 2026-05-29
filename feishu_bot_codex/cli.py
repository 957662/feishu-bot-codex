"""CLI entry: socket client + Click commands + terminal rendering."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from feishu_bot_codex.proto import (
    DoneEvent,
    LogEvent,
    ProgressEvent,
    QRCodeEvent,
    ResponseEvent,
    ResultEvent,
    parse_response_line,
    Request,
)


async def run_op(
    socket_path: Path,
    op: str,
    args: dict,
    request_id: str = "",
) -> AsyncIterator[ResponseEvent]:
    """Open the daemon socket, send one Request, yield ResponseEvents until done.

    Yields events as they stream in (real-time UI feedback).
    Raises ConnectionRefusedError if the daemon is not running.
    """
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        req = Request(op=op, args=args, request_id=request_id)
        writer.write((req.to_json_line() + "\n").encode())
        await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            event = parse_response_line(line.decode().rstrip("\n"))
            yield event
            if isinstance(event, DoneEvent):
                break
    finally:
        writer.close()
        await writer.wait_closed()


def render_event(event: ResponseEvent) -> str:
    """Format one ResponseEvent into a single terminal-ready string.

    Returns "" for DoneEvent (caller does nothing with it).
    """
    if isinstance(event, LogEvent):
        if event.level == "error":
            return f"ERROR: {event.msg}"
        if event.level == "warn":
            return f"WARN: {event.msg}"
        return event.msg
    if isinstance(event, QRCodeEvent):
        return f"{event.ascii}\n\nURL: {event.url}"
    if isinstance(event, ProgressEvent):
        pct = int(event.value * 100)
        return f"[{pct}%] {event.msg}"
    if isinstance(event, ResultEvent):
        if event.ok:
            payload = json.dumps(event.data, ensure_ascii=False, indent=2) if event.data else ""
            return f"OK\n{payload}" if payload else "OK"
        return f"FAILED: {event.error}"
    if isinstance(event, DoneEvent):
        return ""
    return repr(event)


import os
import subprocess
import sys

import click

DEFAULT_SOCKET = Path(os.environ.get(
    "FEISHU_BOT_CODEX_SOCKET",
    Path.home() / ".feishu-bot-codex" / "control.sock",
))


def _open_url_in_browser(url: str) -> bool:
    """Fire-and-forget open of a URL in the user's default browser.

    Returns True if the opener command was successfully launched.
    Silent on failure — caller can still display the URL for manual open.
    """
    if not url or not url.startswith("http"):
        return False
    if os.environ.get("FEISHU_BOT_CODEX_NO_AUTO_OPEN"):
        return False
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, OSError):
        return False


def _print_events_sync(socket_path: Path, op: str, args: dict) -> int:
    """Run an op, print rendered events, return exit code (0 on ok, 1 on failure)."""
    final_ok = True

    async def _drive():
        nonlocal final_ok
        try:
            async for event in run_op(socket_path=socket_path, op=op, args=args):
                rendered = render_event(event)
                if rendered:
                    click.echo(rendered)
                # Auto-open browser when the daemon emits a QR + URL.
                if isinstance(event, QRCodeEvent):
                    if _open_url_in_browser(event.url):
                        click.echo("  (浏览器已自动打开;若没弹出请手动点击上方 URL)")
                if isinstance(event, ResultEvent) and not event.ok:
                    final_ok = False
        except ConnectionRefusedError:
            click.echo(f"ERROR: daemon not running at {socket_path}", err=True)
            return 2
        return 0 if final_ok else 1

    return asyncio.run(_drive())


@click.group(help="feishu-bot-codex — Feishu bridge for Codex/Claude CLI")
@click.option("--socket", "socket_path", type=click.Path(path_type=Path), default=DEFAULT_SOCKET, show_default=True)
@click.pass_context
def main(ctx, socket_path):
    ctx.ensure_object(dict)
    ctx.obj["socket"] = socket_path


@main.command(help="Liveness check against the daemon")
@click.pass_context
def ping(ctx):
    sys.exit(_print_events_sync(ctx.obj["socket"], "ping", {}))


@main.command(name="list", help="List all bindings")
@click.pass_context
def list_cmd(ctx):
    sys.exit(_print_events_sync(ctx.obj["socket"], "list", {}))


@main.command(help="Show daemon status")
@click.pass_context
def status(ctx):
    sys.exit(_print_events_sync(ctx.obj["socket"], "status", {}))


@main.command(help="Bind current project to a new Feishu bot (Phase 7)")
@click.argument("name")
@click.option("--cwd", required=True, type=click.Path(path_type=Path))
@click.pass_context
def bind(ctx, name, cwd):
    sys.exit(_print_events_sync(ctx.obj["socket"], "bind", {"name": name, "cwd": str(cwd)}))


@main.command(help="Remove a binding")
@click.argument("name")
@click.pass_context
def unbind(ctx, name):
    sys.exit(_print_events_sync(ctx.obj["socket"], "unbind", {"name": name}))


@main.command(help="Start mirror for current project (Phase 5)")
@click.option("--cwd", required=True, type=click.Path(path_type=Path))
@click.pass_context
def start(ctx, cwd):
    sys.exit(_print_events_sync(ctx.obj["socket"], "start", {"cwd": str(cwd)}))


@main.command(help="Stop mirror for current project")
@click.option("--cwd", required=True, type=click.Path(path_type=Path))
@click.pass_context
def stop(ctx, cwd):
    sys.exit(_print_events_sync(ctx.obj["socket"], "stop", {"cwd": str(cwd)}))


@main.command(help="Adjust binding parameters")
@click.option("--cwd", required=True, type=click.Path(path_type=Path))
@click.argument("kv", nargs=-1)
@click.pass_context
def config(ctx, cwd, kv):
    sys.exit(_print_events_sync(ctx.obj["socket"], "config", {"cwd": str(cwd), "kv": list(kv)}))


def _resolve_tmux_session(socket_path: Path, cwd: Path, agent: str = "codex") -> str:
    """Ask daemon for the tmux_session name bound to `cwd`.

    Falls back to `<agent>-<basename(cwd)>` if no binding exists or daemon
    is unreachable — keeps `shell` usable in pre-bind exploration and makes
    `--agent claude` give you `claude-foo` rather than `codex-foo`.
    """
    fallback = f"{agent}-{cwd.name}"
    cwd_resolved = str(cwd.resolve())

    async def _ask():
        try:
            async for event in run_op(socket_path=socket_path, op="list", args={}):
                if isinstance(event, ResultEvent) and event.ok and event.data:
                    for b in event.data.get("bindings", []):
                        if str(Path(b.get("project_dir", "")).resolve()) == cwd_resolved:
                            return b.get("tmux_session") or fallback
                if isinstance(event, DoneEvent):
                    break
        except (ConnectionRefusedError, OSError):
            pass
        return fallback

    return asyncio.run(_ask())


@main.command(
    help="Start tmux + claude/codex shell for current project. Extra args are forwarded to the agent.",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--cwd", default=None, type=click.Path(path_type=Path))
@click.option("--agent", type=click.Choice(["codex", "claude"]), default="codex", show_default=True,
              help="Which agent CLI to spawn inside tmux.")
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def shell(ctx, cwd, agent, extra_args):
    """shell doesn't go through the daemon — it's a thin tmux wrapper.

    The tmux session name is resolved from the daemon's binding for this cwd
    when one exists (so /bot-start and the shell agree). Falls back to
    `codex-<basename(cwd)>` for unbound projects.

    Args after `--` are forwarded to the agent binary.
    Examples:
      feishu-bot-codex shell --agent codex
      feishu-bot-codex shell --agent claude -- --dangerously-skip-permissions
    """
    target = cwd or Path(os.getcwd())
    target = target.resolve()
    session_name = _resolve_tmux_session(ctx.obj["socket"], target, agent)

    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir.parent / "scripts" / "feishu-bot-codex-shell",
        pkg_dir.parent.parent / "scripts" / "feishu-bot-codex-shell",
    ]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        click.echo(f"ERROR: shell helper not found in any of: {candidates}", err=True)
        sys.exit(2)
    # argv: [script, cwd, session_name, agent, ...extra_args]
    os.execv(str(script), [str(script), str(target), session_name, agent, *extra_args])
