"""BindingConfig dataclass: per-binding configuration."""

from __future__ import annotations

import fcntl
import os
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

import tomli_w


@contextmanager
def _exclusive_lock(path: Path):
    """Acquire an exclusive advisory lock on a sidecar lockfile."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


_VALID_RENDER_STYLES = {"minimal", "full", "rich"}
_VALID_REPLAY = {"0", "100", "all"}


@dataclass(frozen=True)
class BindingConfig:
    """Configuration for one project ↔ bot binding.

    Frozen because BindingStore returns immutable snapshots; mutations create
    a new BindingConfig and write a fresh TOML file.
    """

    name: str
    project_dir: str
    tmux_session: str
    feishu_app_id: str
    secret_ref: str
    render_style: str = "rich"
    replay_on_start: str = "all"
    mute_thinking: bool = False
    card_throttle_ms: int = 300
    domain: str = "https://open.feishu.cn"
    api_timeout_ms: int = 5000
    upload_timeout_ms: int = 60000
    event_silent_threshold_ms: int = 60000
    event_dead_threshold_ms: int = 120000
    reconnect_grace_failures: int = 3
    allow_users: list[str] = field(default_factory=list)
    require_confirm_patterns: list[str] = field(default_factory=list)
    max_message_length: int = 8000
    session_idle_timeout_seconds: int = 0  # 0 = disabled
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must be non-empty")
        if not Path(self.project_dir).is_absolute():
            raise ValueError(f"project_dir must be absolute: {self.project_dir!r}")
        if self.render_style not in _VALID_RENDER_STYLES:
            raise ValueError(
                f"render_style must be one of {sorted(_VALID_RENDER_STYLES)}, "
                f"got {self.render_style!r}"
            )
        if self.replay_on_start not in _VALID_REPLAY:
            raise ValueError(
                f"replay_on_start must be one of {sorted(_VALID_REPLAY)}, "
                f"got {self.replay_on_start!r}"
            )
        for fname in ("api_timeout_ms", "upload_timeout_ms", "card_throttle_ms",
                      "event_silent_threshold_ms", "event_dead_threshold_ms",
                      "reconnect_grace_failures"):
            value = getattr(self, fname)
            if value < 0:
                raise ValueError(f"{fname} must be non-negative, got {value}")


_DEFAULT_FILE_MODE = 0o600


class BindingStore:
    """TOML-backed store of BindingConfig records.

    File format:
        [[binding]]
        name = "foo-bot"
        project_dir = "/abs/foo"
        ...

    Writes are atomic (write to tempfile, then rename) and enforce 0600 perms.

    Not thread-safe; create one instance per thread/process. Multi-PROCESS
    safety provided by fcntl flock on bindings.toml.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache: list[BindingConfig] = self._load()
        # Track names we originally loaded so _merge can distinguish "deleted
        # by us" from "never seen (added by another process)".
        self._known_names: set[str] = {b.name for b in self._cache}

    def all(self) -> list[BindingConfig]:
        return list(self._cache)

    def find_by_name(self, name: str) -> BindingConfig | None:
        return next((b for b in self._cache if b.name == name), None)

    def find_by_cwd(self, cwd: str) -> BindingConfig | None:
        cwd_resolved = str(Path(cwd).resolve())
        return next(
            (b for b in self._cache if str(Path(b.project_dir).resolve()) == cwd_resolved),
            None,
        )

    def add(self, binding: BindingConfig) -> None:
        if self.find_by_name(binding.name) is not None:
            raise ValueError(f"binding {binding.name!r} already exists")
        if self.find_by_cwd(binding.project_dir) is not None:
            raise ValueError(
                f"project_dir {binding.project_dir!r} already bound to a binding"
            )
        snapshot = list(self._cache)
        self._cache.append(binding)
        try:
            self._save()
        except Exception:
            self._cache = snapshot
            raise

    def remove(self, name: str) -> None:
        for i, b in enumerate(self._cache):
            if b.name == name:
                snapshot = list(self._cache)
                del self._cache[i]
                try:
                    self._save()
                except Exception:
                    self._cache = snapshot
                    raise
                return
        raise KeyError(name)

    def _load(self) -> list[BindingConfig]:
        with _exclusive_lock(self._path):
            return self._load_unlocked()

    def _load_unlocked(self) -> list[BindingConfig]:
        if not self._path.exists():
            return []
        with self._path.open("rb") as f:
            data = tomllib.load(f)
        return [_dict_to_binding(b) for b in data.get("binding", [])]

    def _merge(
        self,
        disk: list[BindingConfig],
        cache: list[BindingConfig],
    ) -> list[BindingConfig]:
        """Merge disk state with in-memory cache.

        Strategy:
        - cache wins for entries present in cache.
        - disk entries UNKNOWN to this store instance (added by another process
          while we held no lock) are preserved.
        - disk entries that were originally loaded by this instance but are now
          absent from cache were explicitly deleted — do NOT restore them.
        - Conflicts on `name` (same name, different content) raise.
        """
        cache_names: set[str] = {b.name for b in cache}
        by_name: dict[str, BindingConfig] = {}
        # Preserve disk entries that this instance never knew about (new entries
        # written by another concurrent process).
        for b in disk:
            if b.name not in self._known_names:
                by_name[b.name] = b
        # Overlay our cache (includes adds; excludes deletes).
        for b in cache:
            if b.name in by_name and by_name[b.name] != b:
                # This can only fire when two separate BindingStore instances
                # both add (or modify) the same name concurrently: one
                # instance's add lands on disk between the other's _load and
                # _merge, producing divergent content for the same key.
                raise ValueError(
                    f"concurrent modification: {b.name!r} differs on disk"
                )
            by_name[b.name] = b
        return list(by_name.values())

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._path):
            disk = self._load_unlocked()
            merged = self._merge(disk, self._cache)
            payload = {"binding": [_binding_to_dict(b) for b in merged]}
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with tmp.open("wb") as f:
                tomli_w.dump(payload, f)
            os.chmod(tmp, _DEFAULT_FILE_MODE)
            os.replace(tmp, self._path)
            # Update in-memory state AFTER the disk write succeeds, so a
            # failed write never leaves the instance ahead of the actual file.
            # _known_names is updated here (not in __init__ or add/remove) so
            # it always reflects what was last successfully committed to disk —
            # ensuring _merge can correctly distinguish "added by another
            # process" from "deleted by us".
            self._cache = merged
            self._known_names = {b.name for b in self._cache}


def _binding_to_dict(b: BindingConfig) -> dict:
    return {
        "name": b.name,
        "project_dir": b.project_dir,
        "tmux_session": b.tmux_session,
        "feishu_app_id": b.feishu_app_id,
        "secret_ref": b.secret_ref,
        "render_style": b.render_style,
        "replay_on_start": b.replay_on_start,
        "mute_thinking": b.mute_thinking,
        "card_throttle_ms": b.card_throttle_ms,
        "domain": b.domain,
        "api_timeout_ms": b.api_timeout_ms,
        "upload_timeout_ms": b.upload_timeout_ms,
        "event_silent_threshold_ms": b.event_silent_threshold_ms,
        "event_dead_threshold_ms": b.event_dead_threshold_ms,
        "reconnect_grace_failures": b.reconnect_grace_failures,
        "allow_users": list(b.allow_users),
        "require_confirm_patterns": list(b.require_confirm_patterns),
        "max_message_length": b.max_message_length,
        "session_idle_timeout_seconds": b.session_idle_timeout_seconds,
        "created_at": b.created_at,
    }


def _dict_to_binding(d: dict) -> BindingConfig:
    # Filter to known dataclass fields so a stale/hand-edited key in
    # bindings.toml (e.g. a field removed in a later version) doesn't raise
    # TypeError and take down the whole daemon at load time.
    known = {f.name for f in fields(BindingConfig)}
    return BindingConfig(**{k: v for k, v in d.items() if k in known})
