"""tmux process wrapper: real + fake implementations."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod


class Tmux(ABC):
    """Interface for tmux session management."""

    @abstractmethod
    def has_session(self, name: str) -> bool:
        """Return True if a tmux session with `name` exists."""

    @abstractmethod
    def new_session(self, name: str, cwd: str, command: str, attach_if_exists: bool = False) -> None:
        """Create a new detached tmux session named `name` running `command` in `cwd`.

        If `attach_if_exists` is True and a session with `name` already exists, behaves
        as a no-op (the caller will attach separately).
        If False and the session exists, raises ValueError.
        """

    @abstractmethod
    def send_keys(self, session: str, keys: str) -> None:
        """Send literal keystrokes to the session's primary pane.

        `keys` should include trailing newlines if you want Enter pressed.
        Raises RuntimeError if the session doesn't exist.
        """

    @abstractmethod
    def send_special(self, session: str, key: str) -> None:
        """Send a single special key by name. Supported names:

        Escape, Enter, Tab, BSpace, Up, Down, Left, Right,
        M-Enter (Alt+Enter, soft newline in Claude/Codex input box),
        C-c (Ctrl+C), C-d (Ctrl+D), C-l (Ctrl+L), C-u (Ctrl+U).

        Implementations translate to the underlying multiplexer's key syntax.
        """

    @abstractmethod
    def kill_session(self, name: str) -> None:
        """Kill the session. No-op if missing."""


class FakeTmux(Tmux):
    """In-memory fake — records all calls, lets tests configure session existence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._sessions: set[str] = set()

    def set_session(self, name: str, exists: bool) -> None:
        """Test helper: set whether a session is considered alive."""
        if exists:
            self._sessions.add(name)
        else:
            self._sessions.discard(name)

    def has_session(self, name: str) -> bool:
        self.calls.append(("has_session", {"name": name}))
        return name in self._sessions

    def new_session(self, name: str, cwd: str, command: str, attach_if_exists: bool = False) -> None:
        if name in self._sessions:
            if attach_if_exists:
                self.calls.append(("attach_session", {"name": name}))
                return
            raise ValueError(f"session {name!r} already exists")
        self.calls.append(("new_session", {"name": name, "cwd": cwd, "command": command}))
        self._sessions.add(name)

    def send_keys(self, session: str, keys: str) -> None:
        if session not in self._sessions:
            raise RuntimeError(f"no session: {session!r}")
        self.calls.append(("send_keys", {"session": session, "keys": keys}))

    def send_special(self, session: str, key: str) -> None:
        if session not in self._sessions:
            raise RuntimeError(f"no session: {session!r}")
        self.calls.append(("send_special", {"session": session, "key": key}))

    def kill_session(self, name: str) -> None:
        self.calls.append(("kill_session", {"name": name}))
        self._sessions.discard(name)


class RealTmux(Tmux):
    """Real tmux backend — shells out to `tmux` binary."""

    _NO_SESSION_RETURNCODE = 1  # tmux's exit code when the session is missing

    def has_session(self, name: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, text=True,
        )
        return result.returncode == 0

    def new_session(self, name: str, cwd: str, command: str, attach_if_exists: bool = False) -> None:
        if self.has_session(name):
            if attach_if_exists:
                return
            raise ValueError(f"session {name!r} already exists")
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", cwd, command],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux new-session failed (exit {result.returncode}): {result.stderr.strip()}"
            )

    def send_keys(self, session: str, keys: str) -> None:
        # Use -l (literal) so /, $, etc. aren't interpreted by tmux's key syntax.
        # Send the keys themselves, then a separate Enter so newlines are reliable.
        stripped = keys
        needs_enter = stripped.endswith("\n")
        if needs_enter:
            stripped = stripped.rstrip("\n")

        if stripped:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, "-l", stripped],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                msg = result.stderr.strip()
                # tmux 3.x → "can't find session", 3.4+ → "can't find pane",
                # and some shells emit "session not found". Treat all as "no session".
                lower = msg.lower()
                if (
                    "can't find session" in lower
                    or "can't find pane" in lower
                    or "no session" in lower
                    or "session not found" in lower
                ):
                    raise RuntimeError(f"no session: {session!r}")
                raise RuntimeError(f"tmux send-keys failed: {msg}")

        if needs_enter:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, "Enter"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                msg = result.stderr.strip()
                # tmux 3.x → "can't find session", 3.4+ → "can't find pane",
                # and some shells emit "session not found". Treat all as "no session".
                lower = msg.lower()
                if (
                    "can't find session" in lower
                    or "can't find pane" in lower
                    or "no session" in lower
                    or "session not found" in lower
                ):
                    raise RuntimeError(f"no session: {session!r}")
                raise RuntimeError(f"tmux send-keys Enter failed: {msg}")

    def send_special(self, session: str, key: str) -> None:
        """Send a tmux-syntax key (Escape, Up, C-c, M-Enter, etc.) — NOT literal."""
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session, key],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            msg = result.stderr.strip()
            lower = msg.lower()
            if (
                "can't find session" in lower
                or "can't find pane" in lower
                or "no session" in lower
                or "session not found" in lower
            ):
                raise RuntimeError(f"no session: {session!r}")
            raise RuntimeError(f"tmux send-special {key!r} failed: {msg}")

    def kill_session(self, name: str) -> None:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True, text=True,
        )
        if result.returncode != 0 and "can't find session" not in result.stderr:
            raise RuntimeError(f"tmux kill-session failed: {result.stderr.strip()}")
