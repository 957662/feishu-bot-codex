"""IPC protocol types — request/response dataclasses with JSON roundtrip."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Union


@dataclass(frozen=True)
class Request:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> Request:
        data = json.loads(line)
        # .get not [] — a missing "op" must surface as a clean ValueError from
        # validate() (caught as "bad request"), not a KeyError that escapes the
        # parse try-block and crashes the handler.
        return cls(
            op=data.get("op", ""),
            args=data.get("args", {}),
            request_id=data.get("request_id", ""),
        )

    def validate(self) -> None:
        """Raise ValueError if the request is malformed."""
        known_ops = {"bind", "unbind", "start", "stop", "list", "config", "status", "shell", "ping"}
        if self.op not in known_ops:
            raise ValueError(f"unknown op: {self.op!r} (known: {sorted(known_ops)})")
        required = {
            "bind": ("name", "cwd"),
            "unbind": ("name",),
            "start": ("cwd",),
            "stop": ("cwd",),
            "config": ("cwd",),
            "status": (),
            "list": (),
            "shell": ("cwd",),
            "ping": (),
        }
        for key in required[self.op]:
            if key not in self.args:
                raise ValueError(f"{self.op} requires arg {key!r}")


@dataclass(frozen=True)
class LogEvent:
    level: Literal["debug", "info", "warn", "error"]
    msg: str
    type: Literal["log"] = "log"

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class QRCodeEvent:
    ascii: str
    url: str
    type: Literal["qrcode"] = "qrcode"

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ProgressEvent:
    value: float
    msg: str
    type: Literal["progress"] = "progress"

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ResultEvent:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    type: Literal["result"] = "result"

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class DoneEvent:
    type: Literal["done"] = "done"

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)


ResponseEvent = Union[LogEvent, QRCodeEvent, ProgressEvent, ResultEvent, DoneEvent]

_EVENT_TYPES: dict[str, type] = {
    "log": LogEvent,
    "qrcode": QRCodeEvent,
    "progress": ProgressEvent,
    "result": ResultEvent,
    "done": DoneEvent,
}


def parse_response_line(line: str) -> ResponseEvent:
    """Parse one JSON line into the appropriate event dataclass."""
    data = json.loads(line)
    type_name = data.get("type")
    cls = _EVENT_TYPES.get(type_name)
    if cls is None:
        raise ValueError(f"unknown event type: {type_name!r}")
    payload = {k: v for k, v in data.items() if k != "type"}
    return cls(**payload)
