"""Tests for /clear-style confirmation flow: confirm_yes/no → y/n keystrokes."""

import pytest

from feishu_bot_codex.daemon.feishu import FakeLarkCli
from feishu_bot_codex.daemon.inbound import InboundPipeline, DEFAULT_CONFIRM_MAP
from feishu_bot_codex.daemon.tmux import FakeTmux


def _menu_event(event_key, sender_id="ou_user"):
    return {
        "type": "application.bot.menu_v6",
        "event": {
            "operator": {"operator_id": {"open_id": sender_id}},
            "event_key": event_key,
        },
    }


def test_default_confirm_map_has_yes_and_no():
    """DEFAULT_CONFIRM_MAP exposed for orchestrator wiring."""
    assert DEFAULT_CONFIRM_MAP["confirm_yes"] == "y"
    assert DEFAULT_CONFIRM_MAP["confirm_no"] == "n"


@pytest.mark.asyncio
async def test_confirm_yes_sends_y_to_tmux():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_menu_event("confirm_yes"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo",
        tmux=tmux,
        lark=lark,
        menu_command_map=DEFAULT_CONFIRM_MAP,
    )
    await pipeline.process_until_idle(max_events=1)
    send_keys_calls = [c for c in tmux.calls if c[0] == "send_keys"]
    keys_seq = "".join(c[1]["keys"] for c in send_keys_calls)
    assert keys_seq == "y\n"


@pytest.mark.asyncio
async def test_confirm_no_sends_n_to_tmux():
    tmux = FakeTmux()
    tmux.set_session("claude-foo", exists=True)
    lark = FakeLarkCli()
    lark.enqueue_event(_menu_event("confirm_no"))

    pipeline = InboundPipeline(
        tmux_session="claude-foo",
        tmux=tmux,
        lark=lark,
        menu_command_map=DEFAULT_CONFIRM_MAP,
    )
    await pipeline.process_until_idle(max_events=1)
    send_keys_calls = [c for c in tmux.calls if c[0] == "send_keys"]
    keys_seq = "".join(c[1]["keys"] for c in send_keys_calls)
    assert keys_seq == "n\n"
