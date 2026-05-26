"""lark-cli subprocess wrapper: real + fake implementations.

Real implementation spawns `lark-cli` subprocesses for each operation.
Fake records calls and replays canned NDJSON events for tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator


class FeishuThrottled(RuntimeError):
    """Raised when Feishu returns error 11232 (rate limited)."""


class LarkCli(ABC):
    """Async wrapper around the `lark-cli` binary."""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str, idempotency_key: str | None = None) -> str:
        """Send a plain text message. Returns the message_id (om_xxx)."""

    @abstractmethod
    async def send_card(self, chat_id: str, card: dict, idempotency_key: str | None = None) -> str:
        """Send an interactive card. Returns the message_id."""

    @abstractmethod
    async def update_card(self, message_id: str, card: dict) -> None:
        """Update the content of a previously-sent card by message_id."""

    @abstractmethod
    async def add_reaction(self, message_id: str, emoji_type: str) -> None:
        """Attach a reaction emoji to an existing message."""

    @abstractmethod
    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        out_path: str,
        resource_type: str = "image",
    ) -> str:
        """Download an image/file from a Feishu message to `out_path`.

        Returns the absolute path of the saved file.
        """

    @abstractmethod
    async def upload_image(self, path: str) -> str:
        """Upload a local image file to Feishu IM. Returns image_key."""

    @abstractmethod
    def consume_events(self, event_key: str, max_events: int = 0) -> AsyncIterator[dict]:
        """Subscribe to a Feishu event key, yielding event dicts as they arrive.

        max_events=0 means unlimited.
        """

    @abstractmethod
    def auth_bot_new_stream(self, name: str) -> AsyncIterator[str]:
        """Spawn `lark-cli config init --new --name <name>` and stream its stdout line-by-line."""

    @abstractmethod
    async def push_menu(self, app_id: str, menu_json: dict) -> None:
        """Push a menu config to the Feishu open platform for `app_id`."""


class FakeLarkCli(LarkCli):
    """In-memory fake — records send calls, replays queued consume events."""

    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self._consume_queue: list[dict] = []
        self._counter = 0
        self._auth_lines: list[str] = []
        self._auth_should_fail = False
        self._menu_pushes: list[dict] = []
        self._menu_should_fail = False
        self._pending_throttle = 0
        self.throttle_attempts = 0

    def _next_message_id(self) -> str:
        self._counter += 1
        return f"om_fake_{self._counter}"

    def enqueue_event(self, event: dict) -> None:
        """Test helper: add an event for the next consume() call to yield."""
        self._consume_queue.append(event)

    def set_auth_lines(self, lines: list[str]) -> None:
        """Test helper: lines to emit from auth_bot_new_stream."""
        self._auth_lines = list(lines)

    def fail_menu_push(self, fail: bool = True) -> None:
        self._menu_should_fail = fail

    def simulate_throttle(self, times: int) -> None:
        """Test helper: the next `times` calls to send_text/send_card fail with throttle."""
        self._pending_throttle = times

    async def send_text(self, chat_id: str, text: str, idempotency_key: str | None = None) -> str:
        if self._pending_throttle > 0:
            self._pending_throttle -= 1
            self.throttle_attempts += 1
            raise FeishuThrottled("11232 rate limited")
        self.send_calls.append({
            "kind": "text",
            "chat_id": chat_id,
            "text": text,
            "idempotency_key": idempotency_key,
        })
        return self._next_message_id()

    async def send_card(self, chat_id: str, card: dict, idempotency_key: str | None = None) -> str:
        if self._pending_throttle > 0:
            self._pending_throttle -= 1
            self.throttle_attempts += 1
            raise FeishuThrottled("11232 rate limited")
        self.send_calls.append({
            "kind": "card",
            "chat_id": chat_id,
            "card": card,
            "idempotency_key": idempotency_key,
        })
        return self._next_message_id()

    async def update_card(self, message_id: str, card: dict) -> None:
        self.send_calls.append({
            "kind": "update",
            "message_id": message_id,
            "card": card,
        })

    async def add_reaction(self, message_id: str, emoji_type: str) -> None:
        self.send_calls.append({
            "kind": "reaction",
            "message_id": message_id,
            "emoji_type": emoji_type,
        })

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        out_path: str,
        resource_type: str = "image",
    ) -> str:
        self.send_calls.append({
            "kind": "download",
            "message_id": message_id,
            "file_key": file_key,
            "out_path": out_path,
            "resource_type": resource_type,
        })
        return out_path

    async def upload_image(self, path: str) -> str:
        self._counter += 1
        key = f"img_fake_{self._counter}"
        self.send_calls.append({"kind": "upload", "path": path, "image_key": key})
        return key

    async def consume_events(self, event_key: str, max_events: int = 0) -> AsyncIterator[dict]:
        emitted = 0
        while True:
            if not self._consume_queue:
                break
            yield self._consume_queue.pop(0)
            emitted += 1
            if max_events > 0 and emitted >= max_events:
                break

    async def auth_bot_new_stream(self, name: str) -> AsyncIterator[str]:
        for line in self._auth_lines:
            await asyncio.sleep(0)
            yield line + "\n"

    async def push_menu(self, app_id: str, menu_json: dict) -> None:
        if self._menu_should_fail:
            raise RuntimeError("fake menu API failure")
        self._menu_pushes.append({"app_id": app_id, "menu": menu_json})


class RealLarkCli(LarkCli):
    """Real backend — spawns `lark-cli` subprocesses for each operation.

    Authentication is expected to be set up externally before instantiating
    this (via `lark-cli auth login` or via env vars). Each `send`/`consume`
    runs a fresh subprocess.
    """

    def __init__(
        self,
        binary: str = "lark-cli",
        as_bot: bool = True,
        extra_env: dict[str, str] | None = None,
        profile: str | None = None,
    ) -> None:
        self._binary = binary
        self._as_bot = as_bot
        self._extra_env = dict(extra_env or {})
        self._profile = profile

    async def _run_raw(self, args: list[str], timeout: float = 30.0) -> tuple[str, int]:
        """Run `lark-cli <args>`. Return (combined stdout+stderr, returncode).

        stderr is merged into the returned text so callers can include real
        error details when reporting failures. (Some lark-cli errors go to
        stderr while exit code is non-zero, leaving stdout empty.)
        """
        env = os.environ.copy()
        env.update(self._extra_env)
        proc = await asyncio.create_subprocess_exec(
            self._binary, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        combined = stdout.decode()
        err_text = stderr.decode()
        if err_text.strip():
            combined = combined + ("\n[stderr]\n" if combined.strip() else "") + err_text
        return combined, proc.returncode

    def _common_args(self) -> list[str]:
        """Args injected into every lark-cli invocation.

        --profile <name> is critical when multiple lark-cli profiles exist on
        the same machine (e.g. running both feishu-bot-claude and
        feishu-bot-codex side by side, each with its own Feishu app). Without
        it, lark-cli picks the global default — which may be the WRONG
        profile, silently routing events to a different daemon's inbound.
        """
        args: list[str] = []
        if self._profile:
            args += ["--profile", self._profile]
        if self._as_bot:
            args += ["--as", "bot"]
        return args

    def _extract_message_id(self, out: str) -> str:
        """lark-cli emits a (possibly pretty-printed) JSON object on stdout.

        Try parsing the whole output as JSON first; fall back to scanning each
        line. message_id is usually under `data.message_id` for v1+ output.
        """
        candidates: list[dict] = []
        # Whole-output JSON (pretty-printed)
        try:
            candidates.append(json.loads(out.strip()))
        except json.JSONDecodeError:
            pass
        # Line-by-line for single-line JSON outputs
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        for payload in candidates:
            for path in (("message_id",), ("data", "message_id"), ("data", "messageId"), ("messageId",)):
                node = payload
                ok = True
                for k in path:
                    if isinstance(node, dict) and k in node:
                        node = node[k]
                    else:
                        ok = False
                        break
                if ok and isinstance(node, str) and node:
                    return node
        raise RuntimeError(f"could not extract message_id from lark-cli output: {out!r}")

    async def send_text(self, chat_id: str, text: str, idempotency_key: str | None = None) -> str:
        # lark-cli `im +messages-send` flags: --text auto-wraps as JSON.
        args = [
            "im", "+messages-send",
            *self._common_args(),
            "--chat-id", chat_id,
            "--text", text,
        ]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        out, code = await self._run_raw(args, timeout=30.0)
        if code != 0:
            raise RuntimeError(f"lark-cli im +messages-send failed (exit {code}): {out!r}")
        return self._extract_message_id(out)

    async def send_card(self, chat_id: str, card: dict, idempotency_key: str | None = None) -> str:
        # Correct flags: --msg-type interactive + --content <card-json>
        args = [
            "im", "+messages-send",
            *self._common_args(),
            "--chat-id", chat_id,
            "--msg-type", "interactive",
            "--content", json.dumps(card, ensure_ascii=False),
        ]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        out, code = await self._run_raw(args, timeout=30.0)
        if code != 0:
            raise RuntimeError(f"lark-cli send_card failed (exit {code}): {out!r}")
        return self._extract_message_id(out)

    async def update_card(self, message_id: str, card: dict) -> None:
        # lark-cli 1.0.41 has no `im messages patch` shortcut. Call the raw
        # Feishu API: PATCH /open-apis/im/v1/messages/{message_id}
        # Body: {"content": "<card json string>"} — content must be a *string*
        # containing the card JSON, NOT a nested object (per Feishu docs).
        body = {"content": json.dumps(card, ensure_ascii=False)}
        args = [
            "api", "PATCH", f"/open-apis/im/v1/messages/{message_id}",
            *self._common_args(),
            "--data", json.dumps(body, ensure_ascii=False),
        ]
        out, code = await self._run_raw(args, timeout=30.0)
        if code != 0:
            raise RuntimeError(f"lark-cli update_card failed (exit {code}): {out!r}")

    async def add_reaction(self, message_id: str, emoji_type: str) -> None:
        args = [
            "im", "reactions", "create",
            *self._common_args(),
            "--params", json.dumps({"message_id": message_id}, ensure_ascii=False),
            "--data", json.dumps({"reaction_type": {"emoji_type": emoji_type}}, ensure_ascii=False),
        ]
        out, code = await self._run_raw(args, timeout=15.0)
        if code != 0:
            raise RuntimeError(f"lark-cli add_reaction failed (exit {code}): {out!r}")

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        out_path: str,
        resource_type: str = "image",
    ) -> str:
        """Download an image or file from a Feishu message.

        Wraps `lark-cli im +messages-resources-download`. The CLI rejects
        absolute paths and `..` traversal in `--output`, so we cd into the
        parent directory and pass just the basename.
        """
        import os
        out_path = os.path.abspath(out_path)
        parent = os.path.dirname(out_path)
        basename = os.path.basename(out_path)
        os.makedirs(parent, exist_ok=True)
        args = [
            "im", "+messages-resources-download",
            *self._common_args(),
            "--message-id", message_id,
            "--file-key", file_key,
            "--type", resource_type,
            "--output", basename,
        ]
        env = os.environ.copy()
        env.update(self._extra_env)
        proc = await asyncio.create_subprocess_exec(
            self._binary, *args,
            cwd=parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0:
            combined = stdout.decode() + "\n" + stderr.decode()
            raise RuntimeError(f"lark-cli download failed (exit {proc.returncode}): {combined!r}")
        if not os.path.exists(out_path):
            raise RuntimeError(f"download claims to succeed but {out_path} doesn't exist")
        return out_path

    async def upload_image(self, path: str) -> str:
        """Upload an image to Feishu IM. Returns image_key (img_xxx)."""
        import os
        if not os.path.exists(path):
            raise RuntimeError(f"upload_image: file not found: {path}")
        args = [
            "im", "images", "create",
            *self._common_args(),
            "--file", f"image={path}",
            "--data", json.dumps({"image_type": "message"}),
        ]
        out, code = await self._run_raw(args, timeout=60.0)
        if code != 0:
            raise RuntimeError(f"lark-cli upload_image failed (exit {code}): {out!r}")
        try:
            payload = json.loads(out.strip())
        except json.JSONDecodeError:
            payload = None
            for line in reversed(out.strip().splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line); break
                    except json.JSONDecodeError:
                        continue
        if not payload:
            raise RuntimeError(f"upload_image: cannot parse output: {out!r}")
        key = None
        for path_keys in (("image_key",), ("data", "image_key")):
            node = payload; ok = True
            for k in path_keys:
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    ok = False; break
            if ok and isinstance(node, str) and node:
                key = node; break
        if not key:
            raise RuntimeError(f"upload_image: image_key missing in response: {payload!r}")
        return key

    async def consume_events(self, event_key: str, max_events: int = 0) -> AsyncIterator[dict]:
        args = [
            "event", "consume", event_key,
            *self._common_args(),
        ]
        if max_events > 0:
            args += ["--max-events", str(max_events)]

        env = os.environ.copy()
        env.update(self._extra_env)
        # IMPORTANT: lark-cli event consume treats stdin EOF as "exit signal".
        # The daemon inherits launchd's /dev/null stdin → immediate EOF → lark-cli
        # exits before any event arrives. Pass stdin=PIPE so the child sees an
        # open stdin held by us, which we never write to or close until we cancel.
        proc = await asyncio.create_subprocess_exec(
            self._binary, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().rstrip("\n")
                if not line_str.strip():
                    continue
                try:
                    yield json.loads(line_str)
                except json.JSONDecodeError:
                    continue
        finally:
            # Close our end of the pipe, then terminate.
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def auth_bot_new_stream(self, name: str) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            self._binary, "config", "init", "--new", "--name", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        tail_lines: list[str] = []  # remember last few lines for error reporting
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace")
                tail_lines.append(decoded.rstrip("\n"))
                if len(tail_lines) > 30:
                    tail_lines.pop(0)
                yield decoded
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            # After process exits, check if it actually succeeded.
            # lark-cli returns 0 only when the user has completed the browser
            # auth flow AND Feishu accepted the app registration.
            if proc.returncode != 0:
                tail = "\n".join(tail_lines[-10:])
                raise RuntimeError(
                    f"lark-cli config init exited with code {proc.returncode}; "
                    f"OAuth flow did not complete successfully. Last output:\n{tail}"
                )

    async def push_menu(self, app_id: str, menu_json: dict) -> None:
        """Push menu config via lark-cli. Subcommand may vary; raises on failure."""
        args = [
            "apps", "menu", "update",
            "--app-id", app_id,
            "--menu", json.dumps(menu_json, ensure_ascii=False),
        ]
        out, code = await self._run_raw(args, timeout=30.0)
        if code != 0:
            raise RuntimeError(f"lark-cli menu update failed (exit {code}): {out!r}")
