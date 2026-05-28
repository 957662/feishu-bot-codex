"""Tests for the default menu JSON template."""

from feishu_bot_codex.menu_template import DEFAULT_MENU, build_menu_json


def test_default_menu_has_top_level_groups():
    assert len(DEFAULT_MENU) == 6  # 会话/配置/工具/信息/控制/桥接


def test_total_buttons_within_limit():
    """Floating-style menu supports 5 main × 10 sub = 50 buttons."""
    total = sum(len(group["children"]) for group in DEFAULT_MENU)
    assert total <= 50


def test_each_button_has_event_key_and_label():
    for group in DEFAULT_MENU:
        for btn in group["children"]:
            assert btn["event_key"]
            assert btn["label"]


def test_event_keys_unique():
    keys = []
    for group in DEFAULT_MENU:
        for btn in group["children"]:
            keys.append(btn["event_key"])
    assert len(keys) == len(set(keys)), "duplicate event_key in menu"


def test_build_menu_json_structure():
    out = build_menu_json()
    assert "menu_items" in out
    assert len(out["menu_items"]) == 6
    first_group = out["menu_items"][0]
    assert "label" in first_group
    assert "children" in first_group
    for child in first_group["children"]:
        assert child["action_type"] == "send_event"
        assert "event_key" in child
