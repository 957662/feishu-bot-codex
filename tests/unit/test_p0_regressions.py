"""Regression tests for the P0 bugs found in the 2026-05 deep review.

These guard shared logic that has been lost more than once during cross-repo
syncs (the 4 feishu-bot-* repos mirror each other). Keep this file in sync
across all four repos — if a fix here regresses, the bug is back.
"""

import json
import time

import pytest

from feishu_bot_codex.config.binding import BindingConfig, _dict_to_binding
from feishu_bot_codex.proto import Request
from feishu_bot_codex.rendering.status import build_status_card
from feishu_bot_codex.rendering.turn import _FILE_PATH_RE, _IMAGE_PATH_RE


def test_missing_op_raises_valueerror_not_keyerror():
    """A request line without "op" must surface as a clean ValueError from
    validate(), not a KeyError that escapes the parse try-block and crashes
    the handler (the "client handler crashed: KeyError 'op'" prod bug)."""
    req = Request.from_json_line('{"args": {}}')  # no "op"
    assert req.op == ""
    with pytest.raises(ValueError):
        req.validate()


def test_dict_to_binding_ignores_unknown_keys():
    """A stale/hand-edited key in bindings.toml must not crash the loader —
    it used to raise TypeError and take down the whole daemon at startup."""
    cfg = _dict_to_binding({
        "name": "foo",
        "project_dir": "/abs/foo",
        "tmux_session": "claude-foo",
        "feishu_app_id": "cli_x",
        "secret_ref": "ref",
        "some_removed_field": "garbage",  # unknown → must be ignored, not crash
    })
    assert isinstance(cfg, BindingConfig)
    assert cfg.name == "foo"
    assert not hasattr(cfg, "some_removed_field")


@pytest.mark.parametrize("rx", [_IMAGE_PATH_RE, _FILE_PATH_RE])
def test_path_regex_no_catastrophic_backtracking(rx):
    """A long path-like string lacking a valid extension must not trigger
    exponential backtracking (was ~12s on a 60KB input; must be well < 1s)."""
    evil = "/" + "a." * 30000
    t0 = time.time()
    rx.findall(evil)
    assert time.time() - t0 < 1.0


def test_path_regex_still_matches_real_paths():
    assert _IMAGE_PATH_RE.search("see /Users/q/pic.png here")
    assert _IMAGE_PATH_RE.search(r"C:\x\y.png")
    assert _FILE_PATH_RE.search("/tmp/a.b/report.pdf")


def test_agent_kind_not_dropped_when_extra_is_none(tmp_path):
    """Operator-precedence regression: with extra=None the agent_kind detected
    from the jsonl meta must survive. The buggy form
    `A or B if extra else None` parses as `(A or B) if extra else None`, so
    extra=None silently discarded the meta value and fell back to "agent"."""
    jsonl = tmp_path / "rollout.jsonl"
    jsonl.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
    card = build_status_card(
        binding_name="b",
        project_dir="/x",
        tmux_session="codex-b",
        feishu_app_id="cli_x",
        render_style="rich",
        jsonl_path=jsonl,
        chat_id="oc_1",
        extra=None,
    )
    blob = json.dumps(card, ensure_ascii=False)
    assert "🟦" in blob  # codex emoji — would be the generic 🤖 if detection were dropped
