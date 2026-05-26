"""Atomic Feishu interactive card JSON builders.

Each function returns a single element or a complete card dict. They compose:
build_card(header=..., elements=[build_markdown(...), build_divider(), ...]).
"""

from __future__ import annotations

from typing import Iterable, Literal

Template = Literal["red", "orange", "yellow", "green", "blue", "purple", "indigo", "wathet", "turquoise", "carmine", "violet", "grey"]
ButtonType = Literal["default", "primary", "danger"]


def build_header(title: str, template: Template = "purple") -> dict:
    return {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }


def _strip_markdown_tables(content: str) -> str:
    """Feishu auto-converts `|...|...|` blocks into table elements with a hard
    per-card cap (~3). Long assistant outputs blow past it and the whole card
    is rejected (code 11310: card table number over limit). We escape pipe
    characters OUTSIDE fenced code blocks so they render as literal `|` text.
    """
    lines = content.split("\n")
    in_code_fence = False
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            out.append(line)
            continue
        if in_code_fence:
            out.append(line)
            continue
        # outside code: replace pipes with their escaped form.
        out.append(line.replace("|", "\\|"))
    return "\n".join(out)


def build_markdown(content: str) -> dict:
    return {"tag": "markdown", "content": _strip_markdown_tables(content)}


def build_divider() -> dict:
    return {"tag": "hr"}


def build_image(image_key: str, alt: str = "") -> dict:
    """Card element showing a previously-uploaded Feishu image.

    `image_key` comes from a successful `lark-cli im images create` call.
    """
    return {
        "tag": "img",
        "img_key": image_key,
        "alt": {"tag": "plain_text", "content": alt or " "},
    }


def build_note(content: str) -> dict:
    # Feishu schema 2.0 dropped the "note" tag. Render as small/grey markdown
    # to preserve the visual hint (token usage, truncation marker, etc.) without
    # triggering "unsupported tag note" rejections.
    return {"tag": "markdown", "content": f"<font color='grey'>{content}</font>"}


def build_collapsible(summary: str, body_markdown: str, expanded: bool = False) -> dict:
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "plain_text", "content": summary},
        },
        "elements": [build_markdown(body_markdown)],
    }


def build_action_buttons(buttons: Iterable[tuple[str, str, ButtonType]]) -> dict:
    """buttons is iterable of (event_key, label, type)."""
    actions = []
    for event_key, label, btn_type in buttons:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btn_type,
            "value": {"event_key": event_key},
        })
    return {"tag": "action", "actions": actions}


def build_card(header: dict, elements: list[dict]) -> dict:
    # Feishu schema 2.0 expects elements nested under "body", not at the top level.
    # See: open.feishu.cn card docs.
    return {
        "schema": "2.0",
        "header": header,
        "body": {"elements": elements},
    }
