"""Tests for mermaid code block detection, rendering, and card integration.

Covers:
  - extract_mermaid_blocks / collect_mermaid_blocks
  - render_mermaid_to_png happy path (mocked mmdc)
  - render_mermaid_to_png fallback to mermaid.ink when mmdc absent
  - render_mermaid_to_png returns None when both backends fail
  - render_turn_to_card emits an img element when mermaid_keys is populated
  - render_turn_to_card preserves raw fence when no key is provided
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from feishu_bot_codex.rendering.mermaid import render_mermaid_to_png
from feishu_bot_codex.rendering.turn import (
    JsonlEvent,
    MERMAID_PLACEHOLDER,
    Turn,
    collect_mermaid_blocks,
    extract_mermaid_blocks,
    render_turn_to_card,
)


def _user_evt(text: str) -> JsonlEvent:
    return JsonlEvent.from_dict(
        {"role": "user", "uuid": "u1", "content": [{"type": "text", "text": text}]}
    )


def _assistant_evt(text: str) -> JsonlEvent:
    return JsonlEvent.from_dict(
        {"role": "assistant", "uuid": "a1", "content": [{"type": "text", "text": text}]}
    )


# ---------------------------------------------------------------------------
# Block extraction
# ---------------------------------------------------------------------------


def test_extract_mermaid_blocks_none_when_no_fence():
    assert extract_mermaid_blocks("just plain text") == []
    assert extract_mermaid_blocks("```python\nprint(1)\n```") == []


def test_extract_mermaid_blocks_one():
    text = "before\n```mermaid\ngraph TD\nA --> B\n```\nafter"
    blocks = extract_mermaid_blocks(text)
    assert blocks == ["graph TD\nA --> B"]


def test_extract_mermaid_blocks_multiple_unique():
    text = (
        "first:\n```mermaid\ngraph TD\nA --> B\n```\n"
        "second:\n```mermaid\nflowchart LR\nX --> Y\n```\n"
    )
    blocks = extract_mermaid_blocks(text)
    assert blocks == ["graph TD\nA --> B", "flowchart LR\nX --> Y"]


def test_extract_mermaid_blocks_dedupes_repeats():
    """Same diagram twice in one turn should only render once."""
    same = "```mermaid\ngraph TD\nA --> B\n```\n"
    text = same + same
    blocks = extract_mermaid_blocks(text)
    assert blocks == ["graph TD\nA --> B"]


def test_extract_mermaid_blocks_case_insensitive():
    text = "```Mermaid\ngraph TD\nA --> B\n```"
    assert extract_mermaid_blocks(text) == ["graph TD\nA --> B"]


def test_extract_mermaid_blocks_tilde_fence():
    text = "~~~mermaid\ngraph TD\nA --> B\n~~~"
    assert extract_mermaid_blocks(text) == ["graph TD\nA --> B"]


def test_collect_mermaid_blocks_walks_turn():
    turn = Turn(
        user_event=_user_evt("draw a graph"),
        assistant_events=[
            _assistant_evt("ok:\n```mermaid\ngraph TD\nA --> B\n```\n"),
            _assistant_evt("second:\n```mermaid\nflowchart LR\nX --> Y\n```"),
        ],
    )
    blocks = collect_mermaid_blocks(turn)
    assert blocks == ["graph TD\nA --> B", "flowchart LR\nX --> Y"]


def test_collect_mermaid_blocks_handles_tool_result():
    tool_result_event = JsonlEvent.from_dict({
        "role": "user",
        "uuid": "u2",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": "```mermaid\ngraph LR\nP --> Q\n```",
        }],
    })
    turn = Turn(user_event=_user_evt("read it"), assistant_events=[tool_result_event])
    assert collect_mermaid_blocks(turn) == ["graph LR\nP --> Q"]


# ---------------------------------------------------------------------------
# render_mermaid_to_png
# ---------------------------------------------------------------------------


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["mmdc"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_render_with_mmdc_success(tmp_path: Path):
    code = "graph TD\nA --> B"

    def fake_run(cmd, capture_output, text, timeout):
        # `mmdc -i src -o out -b white`
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-png-data")
        return _completed(0)

    with patch("feishu_bot_codex.rendering.mermaid.shutil.which", return_value="/usr/local/bin/mmdc"), \
         patch("feishu_bot_codex.rendering.mermaid.subprocess.run", side_effect=fake_run):
        out = render_mermaid_to_png(code, tmp_path)
    assert out is not None
    assert out.exists()
    assert out.read_bytes().startswith(b"\x89PNG")


def test_render_uses_disk_cache(tmp_path: Path):
    """Second call with same code must NOT invoke mmdc."""
    code = "graph TD\nA --> B"
    call_count = {"n": 0}

    def fake_run(cmd, capture_output, text, timeout):
        call_count["n"] += 1
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"\x89PNGfake")
        return _completed(0)

    with patch("feishu_bot_codex.rendering.mermaid.shutil.which", return_value="/usr/local/bin/mmdc"), \
         patch("feishu_bot_codex.rendering.mermaid.subprocess.run", side_effect=fake_run):
        out1 = render_mermaid_to_png(code, tmp_path)
        out2 = render_mermaid_to_png(code, tmp_path)
    assert out1 == out2
    assert call_count["n"] == 1


def test_render_fallback_to_ink_when_mmdc_missing(tmp_path: Path):
    code = "graph TD\nA --> B"

    class FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("feishu_bot_codex.rendering.mermaid.shutil.which", return_value=None), \
         patch(
             "feishu_bot_codex.rendering.mermaid.urlopen",
             return_value=FakeResp(b"\x89PNG-ink-data"),
         ):
        out = render_mermaid_to_png(code, tmp_path)
    assert out is not None
    assert out.read_bytes() == b"\x89PNG-ink-data"


def test_render_returns_none_when_both_backends_fail(tmp_path: Path):
    code = "graph TD\nA --> B"

    def fake_run(*a, **kw):
        # mmdc fails with non-zero exit
        return _completed(1, stderr="error")

    def fake_urlopen(*a, **kw):
        raise OSError("network down")

    with patch("feishu_bot_codex.rendering.mermaid.shutil.which", return_value="/usr/local/bin/mmdc"), \
         patch("feishu_bot_codex.rendering.mermaid.subprocess.run", side_effect=fake_run), \
         patch("feishu_bot_codex.rendering.mermaid.urlopen", side_effect=fake_urlopen):
        out = render_mermaid_to_png(code, tmp_path)
    assert out is None


def test_render_empty_code_returns_none(tmp_path: Path):
    assert render_mermaid_to_png("", tmp_path) is None
    assert render_mermaid_to_png("   \n  ", tmp_path) is None


def test_render_mmdc_timeout_falls_back(tmp_path: Path):
    code = "graph TD\nA --> B"

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="mmdc", timeout=15)

    class FakeResp:
        def read(self):
            return b"\x89PNG-ink"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("feishu_bot_codex.rendering.mermaid.shutil.which", return_value="/usr/local/bin/mmdc"), \
         patch("feishu_bot_codex.rendering.mermaid.subprocess.run", side_effect=fake_run), \
         patch("feishu_bot_codex.rendering.mermaid.urlopen", return_value=FakeResp()):
        out = render_mermaid_to_png(code, tmp_path)
    assert out is not None
    assert out.read_bytes() == b"\x89PNG-ink"


# ---------------------------------------------------------------------------
# render_turn_to_card integration
# ---------------------------------------------------------------------------


def _img_elements(card: dict) -> list[dict]:
    return [e for e in card["body"]["elements"] if e.get("tag") == "img"]


def _markdown_elements(card: dict) -> list[dict]:
    return [e for e in card["body"]["elements"] if e.get("tag") == "markdown"]


def test_card_has_no_img_when_no_mermaid():
    turn = Turn(user_event=_user_evt("hi"), assistant_events=[_assistant_evt("hello")])
    card = render_turn_to_card(turn)
    assert _img_elements(card) == []


def test_card_inserts_img_element_when_mermaid_key_known():
    code = "graph TD\nA --> B"
    text = f"intro\n```mermaid\n{code}\n```\nafter"
    turn = Turn(user_event=_user_evt("draw"), assistant_events=[_assistant_evt(text)])
    card = render_turn_to_card(turn, mermaid_keys={code: "img_key_123"})
    imgs = _img_elements(card)
    assert len(imgs) == 1
    assert imgs[0]["img_key"] == "img_key_123"
    md = _markdown_elements(card)
    # At least one markdown element should contain the placeholder.
    assert any(MERMAID_PLACEHOLDER in m["content"] for m in md)
    # The raw mermaid source must NOT appear in any rendered markdown.
    for m in md:
        assert "graph TD" not in m["content"] or MERMAID_PLACEHOLDER in m["content"]


def test_card_preserves_raw_fence_when_no_key():
    code = "graph TD\nA --> B"
    text = f"```mermaid\n{code}\n```"
    turn = Turn(user_event=_user_evt("draw"), assistant_events=[_assistant_evt(text)])
    card = render_turn_to_card(turn, mermaid_keys={})
    assert _img_elements(card) == []
    md_texts = "\n".join(m["content"] for m in _markdown_elements(card))
    assert "graph TD" in md_texts
    assert "```mermaid" in md_texts


def test_card_handles_multiple_mermaid_blocks():
    code1 = "graph TD\nA --> B"
    code2 = "flowchart LR\nX --> Y"
    text = (
        f"first:\n```mermaid\n{code1}\n```\n"
        f"second:\n```mermaid\n{code2}\n```\n"
    )
    turn = Turn(user_event=_user_evt("draw"), assistant_events=[_assistant_evt(text)])
    card = render_turn_to_card(
        turn,
        mermaid_keys={code1: "k1", code2: "k2"},
    )
    imgs = _img_elements(card)
    assert [i["img_key"] for i in imgs] == ["k1", "k2"]


def test_card_mixed_some_keys_some_missing():
    code1 = "graph TD\nA --> B"
    code2 = "flowchart LR\nX --> Y"
    text = (
        f"a:\n```mermaid\n{code1}\n```\n"
        f"b:\n```mermaid\n{code2}\n```\n"
    )
    turn = Turn(user_event=_user_evt("draw"), assistant_events=[_assistant_evt(text)])
    card = render_turn_to_card(turn, mermaid_keys={code1: "k1"})  # k2 missing
    imgs = _img_elements(card)
    assert [i["img_key"] for i in imgs] == ["k1"]
    md_texts = "\n".join(m["content"] for m in _markdown_elements(card))
    # The second block's source remains visible as a fallback.
    assert "flowchart LR" in md_texts
