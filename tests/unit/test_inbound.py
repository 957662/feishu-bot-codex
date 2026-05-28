"""Tests for InboundPipeline — Feishu events → tmux send-keys."""

import json

import pytest

from feishu_bot_codex.daemon.feishu import FakeLarkCli
from feishu_bot_codex.daemon.inbound import InboundPipeline
from feishu_bot_codex.daemon.tmux import FakeTmux


def _text_event(text: str, sender_id: str = "ou_user") -> dict:
    return {
        "type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": sender_id}},
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _menu_event(event_key: str, sender_id: str = "ou_user") -> dict:
    return {
        "type": "application.bot.menu_v6",
        "event": {
            "operator": {"operator_id": {"open_id": sender_id}},
            "event_key": event_key,
        },
    }


@pytest.mark.asyncio
async def test_inbound_routes_text_to_tmux():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_text_event("hello claude"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo",
        tmux=tmux,
        lark=lark,
    )
    await pipeline.process_until_idle(max_events=1)

    # New split-send: text body via send_keys, Enter via send_special.
    send_calls = [c for c in tmux.calls if c[0] in ("send_keys", "send_special")]
    typed = "".join(
        c[1].get("keys", "") if c[0] == "send_keys" else
        ("\n" if c[1]["key"] == "Enter" else "")
        for c in send_calls
    )
    assert typed == "hello claude\n"


@pytest.mark.asyncio
async def test_inbound_routes_slash_command_to_tmux():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_text_event("/compact"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo", tmux=tmux, lark=lark,
    )
    await pipeline.process_until_idle(max_events=1)

    send_calls = [c for c in tmux.calls if c[0] in ("send_keys", "send_special")]
    typed = "".join(
        c[1].get("keys", "") if c[0] == "send_keys" else
        ("\n" if c[1]["key"] == "Enter" else "")
        for c in send_calls
    )
    assert typed == "/compact\n"


@pytest.mark.asyncio
async def test_inbound_routes_menu_button_to_command():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_menu_event("cmd_clear"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo", tmux=tmux, lark=lark,
        menu_command_map={"cmd_clear": "/clear", "cmd_compact": "/compact"},
    )
    await pipeline.process_until_idle(max_events=1)

    send_calls = [c for c in tmux.calls if c[0] in ("send_keys", "send_special")]
    typed = "".join(
        c[1].get("keys", "") if c[0] == "send_keys" else
        ("\n" if c[1]["key"] == "Enter" else "")
        for c in send_calls
    )
    assert typed == "/clear\n"


@pytest.mark.asyncio
async def test_inbound_unknown_menu_key_logs_and_skips():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_menu_event("totally_unknown_key"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo", tmux=tmux, lark=lark,
        menu_command_map={"cmd_clear": "/clear"},
    )
    await pipeline.process_until_idle(max_events=1)

    send_keys_calls = [c for c in tmux.calls if c[0] == "send_keys"]
    assert send_keys_calls == []


@pytest.mark.asyncio
async def test_inbound_whitelist_drops_other_senders():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_text_event("hi", sender_id="ou_attacker"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo", tmux=tmux, lark=lark,
        allow_users={"ou_owner"},
    )
    await pipeline.process_until_idle(max_events=1)

    send_keys_calls = [c for c in tmux.calls if c[0] == "send_keys"]
    assert send_keys_calls == []


@pytest.mark.asyncio
async def test_inbound_truncates_long_message():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_text_event("x" * 20_000))

    pipeline = InboundPipeline(
        tmux_session="claude-foo", tmux=tmux, lark=lark,
        max_message_length=100,
    )
    await pipeline.process_until_idle(max_events=1)

    send_keys_calls = [c for c in tmux.calls if c[0] == "send_keys"]
    assert send_keys_calls
    keys = send_keys_calls[0][1]["keys"]
    assert len(keys) <= 200


@pytest.mark.asyncio
async def test_inbound_invokes_chat_id_callback():
    """When a message arrives, on_chat_id_discovered should be called with the chat_id."""
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event({
        "type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "chat_id": "oc_test_chat",
            },
        },
    })

    discovered_ids: list[str] = []

    pipeline = InboundPipeline(
        tmux_session="claude-foo",
        tmux=tmux,
        lark=lark,
        on_chat_id_discovered=lambda chat_id: discovered_ids.append(chat_id),
    )
    await pipeline.process_until_idle(max_events=1)
    assert discovered_ids == ["oc_test_chat"]


@pytest.mark.asyncio
async def test_inbound_callback_only_fired_once():
    """The chat_id callback should be invoked only on the first message."""
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    for i in range(3):
        lark.enqueue_event({
            "type": "im.message.receive_v1",
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_type": "text",
                    "content": json.dumps({"text": f"msg-{i}"}),
                    "chat_id": "oc_test_chat",
                },
            },
        })

    discovered_ids: list[str] = []

    pipeline = InboundPipeline(
        tmux_session="claude-foo",
        tmux=tmux,
        lark=lark,
        on_chat_id_discovered=lambda chat_id: discovered_ids.append(chat_id),
    )
    await pipeline.process_until_idle(max_events=3)
    assert discovered_ids == ["oc_test_chat"]  # only once
