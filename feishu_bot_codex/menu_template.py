"""Default Feishu bot menu — 5 groups × up to 10 buttons each."""

from __future__ import annotations

DEFAULT_MENU: list[dict] = [
    {
        "label": "会话",
        "children": [
            {"event_key": "cmd_clear", "label": "/clear"},
            {"event_key": "cmd_compact", "label": "/compact"},
            {"event_key": "cmd_resume", "label": "/resume"},
            {"event_key": "cmd_cost", "label": "/cost"},
            {"event_key": "cmd_status_repl", "label": "/status"},
            {"event_key": "cmd_quit", "label": "/quit"},
        ],
    },
    {
        "label": "配置",
        "children": [
            {"event_key": "cmd_model", "label": "/model"},
            {"event_key": "cmd_config", "label": "/config"},
            {"event_key": "cmd_init", "label": "/init"},
            {"event_key": "cmd_permissions", "label": "/permissions"},
            {"event_key": "cmd_login", "label": "/login"},
            {"event_key": "cmd_logout", "label": "/logout"},
        ],
    },
    {
        "label": "工具",
        "children": [
            {"event_key": "cmd_agents", "label": "/agents"},
            {"event_key": "cmd_mcp", "label": "/mcp"},
            {"event_key": "cmd_memory", "label": "/memory"},
            {"event_key": "cmd_hooks", "label": "/hooks"},
            {"event_key": "cmd_skills", "label": "/skills"},
            {"event_key": "cmd_add_dir", "label": "/add-dir"},
        ],
    },
    {
        "label": "信息",
        "children": [
            {"event_key": "cmd_help", "label": "/help"},
            {"event_key": "cmd_usage", "label": "/usage"},
            {"event_key": "cmd_doctor", "label": "/doctor"},
            {"event_key": "cmd_bug", "label": "/bug"},
        ],
    },
    {
        "label": "控制",
        "children": [
            {"event_key": "key_cancel", "label": "✋ 中断"},
            {"event_key": "key_yes", "label": "✅ 确认 y"},
            {"event_key": "key_no", "label": "❌ 拒绝 n"},
            {"event_key": "key_exit", "label": "🛑 退出"},
            {"event_key": "key_up", "label": "⬆ 上一条"},
            {"event_key": "key_down", "label": "⬇ 下一条"},
        ],
    },
    {
        "label": "桥接",
        "children": [
            {"event_key": "bridge_pause", "label": "暂停镜像"},
            {"event_key": "bridge_resume", "label": "恢复镜像"},
            {"event_key": "bridge_reload", "label": "重载配置"},
            {"event_key": "bridge_show", "label": "查看绑定"},
        ],
    },
]


# event_key → special key name (for tmux/zellij send_special), in addition
# to DEFAULT_MENU_COMMAND_MAP (which contains text-to-inject commands).
DEFAULT_MENU_SPECIAL_MAP: dict[str, str] = {
    "key_cancel": "Escape",
    "key_exit": "C-c",
    "key_up": "Up",
    "key_down": "Down",
}


# event_key → "y" or "n" letter that should be typed and then committed
# with Enter (one-shot permission prompt answer).
DEFAULT_MENU_YESNO_MAP: dict[str, str] = {
    "key_yes": "y",
    "key_no": "n",
}


# Map event_key → tmux keystrokes for slash commands (consumed by InboundPipeline)
DEFAULT_MENU_COMMAND_MAP: dict[str, str] = {
    "cmd_clear": "/clear",
    "cmd_compact": "/compact",
    "cmd_resume": "/resume",
    "cmd_cost": "/cost",
    "cmd_status_repl": "/status",
    "cmd_quit": "/quit",
    "cmd_model": "/model",
    "cmd_config": "/config",
    "cmd_init": "/init",
    "cmd_permissions": "/permissions",
    "cmd_login": "/login",
    "cmd_logout": "/logout",
    "cmd_agents": "/agents",
    "cmd_mcp": "/mcp",
    "cmd_memory": "/memory",
    "cmd_hooks": "/hooks",
    "cmd_skills": "/skills",
    "cmd_add_dir": "/add-dir",
    "cmd_help": "/help",
    "cmd_usage": "/usage",
    "cmd_doctor": "/doctor",
    "cmd_bug": "/bug",
    # bridge_* keys handled internally, not injected
}


def build_menu_json() -> dict:
    """Return the menu config dict ready for Feishu open platform consumption."""
    menu_items = []
    for group in DEFAULT_MENU:
        children = [
            {
                "label": btn["label"],
                "action_type": "send_event",
                "event_key": btn["event_key"],
            }
            for btn in group["children"]
        ]
        menu_items.append({
            "label": group["label"],
            "children": children,
        })
    return {"menu_items": menu_items}
