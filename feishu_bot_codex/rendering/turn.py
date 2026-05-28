"""Parse Codex CLI session jsonl + group into turns for rendering.

Codex's jsonl format differs from Claude's, but we translate it to the same
internal shape so the rest of the rendering pipeline (tool blocks, cards,
token usage) is reused without changes.

Codex top-level shape:
  {"type": "<envelope>", "timestamp": "...", "payload": {...}}

envelope ∈ {session_meta, turn_context, event_msg, response_item}

Only `response_item` carries actual conversation content. Within it, the
payload subtypes we care about:

| payload.type           | maps to internal                                              |
|------------------------|---------------------------------------------------------------|
| message (role=user)    | role="user",      content=[{type:"text", text: ...}]          |
| message (role=assist.) | role="assistant", content=[{type:"text", text: ...}]          |
| message (role=devel.)  | role="_meta" (skip — system prompt)                           |
| function_call          | role="assistant", content=[{type:"tool_use", id, name, input}]|
| function_call_output   | role="assistant", content=[{type:"tool_result", tool_use_id, content}] |
| reasoning              | role="_meta" (skip — thinking trace)                          |

Envelopes session_meta / turn_context / event_msg become role="_meta" and
are filtered out by downstream code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class JsonlEvent:
    """One line from a Codex session jsonl, translated to a Claude-shaped event."""

    role: str
    uuid: str
    content: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> JsonlEvent:
        # Codex format: top-level "type" with sub-payload. Translate.
        envelope = d.get("type")
        if envelope in {"session_meta", "turn_context", "event_msg"}:
            return cls(role="_meta", uuid="", content=[], raw=d)

        if envelope == "response_item":
            return cls._from_codex_response_item(d)

        # Fallback: maybe this is the legacy Claude shape (in case a user feeds
        # us a Claude jsonl by mistake — be lenient).
        return cls._from_claude_legacy(d)

    @classmethod
    def _from_codex_response_item(cls, d: dict) -> JsonlEvent:
        payload = d.get("payload", {}) or {}
        ptype = payload.get("type")

        if ptype == "message":
            role = payload.get("role", "")
            if role == "developer":
                # System / developer prompt — not user-visible. Skip.
                return cls(role="_meta", uuid="", content=[], raw=d)
            # Codex content parts use "input_text" (user) and "output_text"
            # (assistant). Normalize both to {type:"text", text:...}.
            content: list[dict] = []
            for part in payload.get("content", []) or []:
                if not isinstance(part, dict):
                    if isinstance(part, str):
                        content.append({"type": "text", "text": part})
                    continue
                pt = part.get("type")
                if pt in {"input_text", "output_text", "text"}:
                    text = part.get("text", "")
                    if text:
                        content.append({"type": "text", "text": text})
            return cls(role=role, uuid="", content=content, raw=d)

        if ptype == "function_call":
            # Codex serializes arguments as a JSON-encoded string.
            args_raw = payload.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            tool_use = {
                "type": "tool_use",
                "id": payload.get("call_id", ""),
                "name": payload.get("name", ""),
                "input": args,
            }
            return cls(role="assistant", uuid="", content=[tool_use], raw=d)

        if ptype == "function_call_output":
            tool_result = {
                "type": "tool_result",
                "tool_use_id": payload.get("call_id", ""),
                "content": payload.get("output", ""),
                "is_error": bool(payload.get("error")),
            }
            return cls(role="assistant", uuid="", content=[tool_result], raw=d)

        # reasoning, web_search_call, etc. — ignore for now.
        return cls(role="_meta", uuid="", content=[], raw=d)

    @classmethod
    def _from_claude_legacy(cls, d: dict) -> JsonlEvent:
        msg_obj = d.get("message")
        if isinstance(msg_obj, dict):
            raw_content = msg_obj.get("content", d.get("content", []))
            role = msg_obj.get("role", d.get("role", ""))
        else:
            raw_content = d.get("content", [])
            role = d.get("role", "")
        if isinstance(raw_content, str):
            normalized = [{"type": "text", "text": raw_content}]
        elif isinstance(raw_content, list):
            normalized = []
            for part in raw_content:
                if isinstance(part, dict):
                    normalized.append(part)
                elif isinstance(part, str):
                    normalized.append({"type": "text", "text": part})
        else:
            normalized = []
        return cls(role=role, uuid=d.get("uuid", ""), content=normalized, raw=d)

    @classmethod
    def load_file(cls, path: Path) -> Iterator[JsonlEvent]:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield cls.from_dict(json.loads(line))

    def text(self) -> str:
        return "".join(c.get("text", "") for c in self.content if c.get("type") == "text")

    def has_only_tool_results(self) -> bool:
        if not self.content:
            return False
        return all(c.get("type") == "tool_result" for c in self.content)

    def is_meta(self) -> bool:
        return self.role == "_meta"


@dataclass
class Turn:
    """One conversation turn: a user message and the assistant response(s)."""

    user_event: JsonlEvent | None
    assistant_events: list[JsonlEvent] = field(default_factory=list)


def group_into_turns(events: Iterable[JsonlEvent]) -> list[Turn]:
    """Group an iterable of JsonlEvents into Turn list.

    A new Turn starts on a `user` event with actual text content (not a meta
    event, not a pure tool_result delivery).
    """
    turns: list[Turn] = []
    current: Turn | None = None

    for event in events:
        if event.is_meta():
            continue
        if event.role == "user" and not event.has_only_tool_results():
            current = Turn(user_event=event)
            turns.append(current)
        else:
            if current is None:
                current = Turn(user_event=None)
                turns.append(current)
            current.assistant_events.append(event)

    return turns


from feishu_bot_codex.rendering.card import build_card, build_header, build_image, build_markdown, build_note
from feishu_bot_codex.rendering.tools import render_tool_block

# Feishu card limits (see tools.py): cap individual markdown elements and
# total element count to stay under the per-message budget (~30KB / ~50 elements).
MARKDOWN_CHAR_LIMIT = 4000
MAX_ELEMENTS_PER_CARD = 40


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(截断 {len(text) - limit} 字符)…"



# Regex to spot absolute image paths embedded in plain text.
_IMAGE_PATH_RE = re.compile(
    r"(?:!?\[[^\]]*\]\()?(?P<path>(?:/[\w./\-]+|[A-Za-z]:\\[\w.\\\- ]+)\.(?:png|jpe?g|gif|webp|bmp))\)?",
    re.IGNORECASE,
)

# Non-image file extensions we'll auto-upload as standalone Feishu messages.
_FILE_PATH_RE = re.compile(
    r"(?<![\w./])"
    r"(?P<path>(?:/[\w./\-]+|[A-Za-z]:\\[\w.\\\- ]+)\."
    r"(?:pdf|txt|md|markdown|csv|tsv|json|yaml|yml|toml|xml|"
    r"py|js|ts|tsx|jsx|go|rs|java|kt|swift|c|h|cpp|hpp|cs|rb|php|"
    r"sh|bash|zsh|fish|sql|html|css|scss|log|conf|ini|cfg|"
    r"docx?|xlsx?|pptx?))"
    r"(?![\w])",
    re.IGNORECASE,
)


def collect_file_paths(turn: Turn) -> list[str]:
    seen: dict[str, None] = {}
    for event in turn.assistant_events:
        for part in event.content:
            ptype = part.get("type")
            if ptype == "text" and isinstance(part.get("text"), str):
                for m in _FILE_PATH_RE.finditer(part["text"]):
                    seen.setdefault(m.group("path"), None)
            elif ptype == "tool_result":
                content = part.get("content", "")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                if isinstance(content, str):
                    for m in _FILE_PATH_RE.finditer(content):
                        seen.setdefault(m.group("path"), None)
    return list(seen.keys())




def collect_image_paths(turn: Turn) -> list[str]:
    """Scan a Turn for local image file paths to upload + show."""
    seen: dict[str, None] = {}
    for event in turn.assistant_events:
        for part in event.content:
            ptype = part.get("type")
            if ptype == "image":
                src = part.get("source") or {}
                if src.get("type") == "path" and src.get("path"):
                    seen.setdefault(src["path"], None)
            elif ptype == "text" and isinstance(part.get("text"), str):
                for m in _IMAGE_PATH_RE.finditer(part["text"]):
                    seen.setdefault(m.group("path"), None)
            elif ptype == "tool_result":
                content = part.get("content", "")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                if isinstance(content, str):
                    for m in _IMAGE_PATH_RE.finditer(content):
                        seen.setdefault(m.group("path"), None)
    return list(seen.keys())


def _append_inline_images(elements, text, image_keys):
    if not image_keys:
        return
    seen = set()
    for m in _IMAGE_PATH_RE.finditer(text):
        path = m.group("path")
        if path in seen:
            continue
        seen.add(path)
        if path in image_keys:
            elements.append(build_image(image_keys[path], alt=os.path.basename(path)))

def render_turn_to_card(
    turn: Turn,
    project_name: str = "project",
    render_style: str = "rich",
    image_keys: dict[str, str] | None = None,
    in_progress: bool = False,
) -> dict:
    """Render a Turn to a Feishu interactive card JSON.

    `in_progress=True` appends a pacer "思考中…" line so the card visibly
    differs across updates while the model is mid-turn.
    """
    image_keys = image_keys or {}
    elements: list[dict] = []
    for event in turn.assistant_events:
        for part in event.content:
            if part.get("type") == "text" and part.get("text"):
                elements.append(build_markdown(_truncate(part["text"], MARKDOWN_CHAR_LIMIT)))
                _append_inline_images(elements, part["text"], image_keys)
            elif part.get("type") == "image":
                src = part.get("source") or {}
                if src.get("type") == "path" and src.get("path") in image_keys:
                    elements.append(build_image(image_keys[src["path"]], alt=src.get("alt", "")))
            elif part.get("type") == "tool_use":
                tool_use = part
                tool_result = None
                for later in turn.assistant_events:
                    for p in later.content:
                        if p.get("type") == "tool_result" and p.get("tool_use_id") == tool_use.get("id"):
                            tool_result = p
                            break
                block = render_tool_block(tool_use, tool_result, render_style=render_style)
                if block is not None:
                    elements.append(block)
                if tool_result is not None:
                    content = tool_result.get("content", "")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                    if isinstance(content, str):
                        _append_inline_images(elements, content, image_keys)

    # Codex emits usage via event_msg.token_count which is skipped as _meta.
    if len(elements) > MAX_ELEMENTS_PER_CARD - 1:
        dropped = len(elements) - (MAX_ELEMENTS_PER_CARD - 1)
        elements = elements[:MAX_ELEMENTS_PER_CARD - 1]
        elements.append(build_note(f"…省略 {dropped} 个工具调用/段落…"))

    if in_progress:
        import time
        n = int(time.time()) % 4
        dots = "·" * n + " " * (3 - n)
        elements.append(build_note(f"⏳ 思考中…{dots}"))

    header = build_header(title=f"🤖 Codex · {project_name}")
    return build_card(header=header, elements=elements)
