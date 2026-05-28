"""End-to-end: spawn daemon subprocess, run CLI against it, verify ping succeeds."""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_daemon_subprocess_responds_to_cli(tmp_path):
    # Use short /tmp path due to macOS AF_UNIX 104-char limit
    socket_path = Path(tempfile.gettempdir()) / f"fbc-{uuid.uuid4().hex[:8]}.sock"
    bindings_path = tmp_path / "bindings.toml"

    env = os.environ.copy()
    env["FEISHU_BOT_CODEX_SOCKET"] = str(socket_path)
    env["FEISHU_BOT_CODEX_BINDINGS"] = str(bindings_path)

    daemon = subprocess.Popen(
        [sys.executable, "-m", "feishu_bot_codex", "daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 3s for the socket to appear
        deadline = time.time() + 3
        while time.time() < deadline:
            if socket_path.exists():
                break
            await asyncio.sleep(0.05)
        assert socket_path.exists(), "daemon failed to create socket within 3s"

        # Run the CLI ping
        result = subprocess.run(
            [sys.executable, "-m", "feishu_bot_codex", "--socket", str(socket_path), "ping"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "pong" in result.stdout.lower() or "OK" in result.stdout
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=2)
        except subprocess.TimeoutExpired:
            daemon.kill()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_daemon_list_empty(tmp_path):
    """A fresh daemon with no bindings returns an empty list."""
    socket_path = Path(tempfile.gettempdir()) / f"fbc-{uuid.uuid4().hex[:8]}.sock"
    bindings_path = tmp_path / "bindings.toml"

    env = os.environ.copy()
    env["FEISHU_BOT_CODEX_SOCKET"] = str(socket_path)
    env["FEISHU_BOT_CODEX_BINDINGS"] = str(bindings_path)

    daemon = subprocess.Popen(
        [sys.executable, "-m", "feishu_bot_codex", "daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not socket_path.exists():
            await asyncio.sleep(0.05)
        assert socket_path.exists()

        result = subprocess.run(
            [sys.executable, "-m", "feishu_bot_codex", "--socket", str(socket_path), "list"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "bindings" in result.stdout
        assert "[]" in result.stdout or '"bindings": []' in result.stdout
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=2)
        except subprocess.TimeoutExpired:
            daemon.kill()
        socket_path.unlink(missing_ok=True)
