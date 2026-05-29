"""Status card rendering — built on demand when the user types `!status`."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from feishu_bot_codex.rendering.card import build_card, build_header, build_markdown, build_note


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _format_age(seconds: float) -> str:
    if seconds < 1:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _extract_recent_meta(jsonl_path: Path) -> dict:
    """Pull the most useful tidbits from the tail of the jsonl.

    Returns a dict with whichever of these we could find:
        model, total_input_tokens, total_output_tokens, agent_kind
    """
    info: dict = {}
    if not jsonl_path.exists():
        return info
    # Read last ~64KB which is plenty for usage / model info
    size = jsonl_path.stat().st_size
    read_from = max(0, size - 64 * 1024)
    with jsonl_path.open("rb") as f:
        f.seek(read_from)
        tail = f.read().decode("utf-8", errors="replace")
    lines = tail.splitlines()
    # Walk in reverse to find newest model/usage
    in_tot, out_tot = 0, 0
    found_model = False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Codex envelope
        if d.get("type") == "turn_context":
            if not found_model:
                m = d.get("payload", {}).get("model")
                if m:
                    info["model"] = m
                    info["agent_kind"] = "codex"
                    found_model = True
        elif d.get("type") == "session_meta":
            if not found_model:
                p = d.get("payload", {})
                info["agent_kind"] = "codex"
                # session_meta doesn't always include model directly
                found_model = found_model or bool(info.get("model"))
        elif d.get("type") == "event_msg":
            p = d.get("payload", {})
            if p.get("type") == "token_count":
                tu = p.get("info", {}).get("total_token_usage", {}) or {}
                if not info.get("total_input_tokens"):
                    info["total_input_tokens"] = tu.get("input_tokens", 0)
                    info["total_output_tokens"] = tu.get("output_tokens", 0)
                    info["cached_input_tokens"] = tu.get("cached_input_tokens", 0)
        # Claude-shape: message.usage
        elif isinstance(d.get("message"), dict):
            msg = d["message"]
            info.setdefault("agent_kind", "claude")
            if not found_model and msg.get("model"):
                info["model"] = msg["model"]
                found_model = True
            usage = msg.get("usage") or {}
            in_tot += usage.get("input_tokens", 0) or 0
            out_tot += usage.get("output_tokens", 0) or 0
    if "total_input_tokens" not in info and (in_tot or out_tot):
        info["total_input_tokens"] = in_tot
        info["total_output_tokens"] = out_tot
    return info


def build_status_card(
    binding_name: str,
    project_dir: str,
    tmux_session: str,
    feishu_app_id: str,
    render_style: str,
    jsonl_path: Path | str | None,
    chat_id: str,
    daemon_uptime_seconds: int | None = None,
    extra: dict | None = None,
) -> dict:
    """Compose a Feishu interactive card summarizing current binding state."""
    jsonl_path = Path(jsonl_path) if jsonl_path else None
    meta = _extract_recent_meta(jsonl_path) if jsonl_path and jsonl_path.exists() else {}
    # Parenthesize: without it, `A or B if extra else None` parses as
    # `(A or B) if extra else None`, so when extra is None we'd discard the
    # agent_kind already detected from the jsonl meta.
    agent_kind = meta.get("agent_kind") or (extra.get("agent_kind") if extra else None)
    agent_kind = agent_kind or "agent"
    agent_emoji = "🤖" if agent_kind != "codex" else "🟦"

    lines = [
        f"**项目目录**: `{project_dir}`",
        f"**tmux session**: `{tmux_session}`",
        f"**飞书 App**: `{feishu_app_id}`",
        f"**chat_id**: `{chat_id or '(未 bootstrap)'}`",
    ]
    if meta.get("model"):
        lines.append(f"**model**: `{meta['model']}`")

    # jsonl state
    if jsonl_path and jsonl_path.exists():
        st = jsonl_path.stat()
        size = _format_bytes(st.st_size)
        age = _format_age(time.time() - st.st_mtime)
        lines.append(f"**会话文件**: `{jsonl_path.name}`  ({size}, 最近活动 {age})")
        # liveness — if jsonl modified in the last 30s, agent is probably mid-turn
        if time.time() - st.st_mtime < 30:
            lines.append("**状态**: 🟢 运行中(jsonl 30秒内有写入)")
        elif time.time() - st.st_mtime < 600:
            lines.append("**状态**: 🟡 空闲(10 分钟内有活动)")
        else:
            lines.append("**状态**: ⚪ 长时间空闲")
    else:
        lines.append("**状态**: ❓ 暂无 jsonl 数据")

    if meta.get("total_input_tokens") or meta.get("total_output_tokens"):
        in_ = meta.get("total_input_tokens", 0)
        out_ = meta.get("total_output_tokens", 0)
        cached = meta.get("cached_input_tokens", 0)
        if cached:
            lines.append(f"**累计 tokens**: input {in_:,}(cached {cached:,}) · output {out_:,}")
        else:
            lines.append(f"**累计 tokens**: input {in_:,} · output {out_:,}")

    lines.append(f"**渲染风格**: `{render_style}`")
    if daemon_uptime_seconds is not None:
        h = daemon_uptime_seconds // 3600
        m = (daemon_uptime_seconds % 3600) // 60
        lines.append(f"**daemon 运行**: {h}h {m}m")

    elements = [build_markdown("\n".join(lines))]

    # Cheatsheet footer
    elements.append(build_note(
        "快捷:`!中断` 中断当前任务 · `!y`/`!n` 一键确认 · `!status` 刷新本卡 · `!help` 所有命令"
    ))

    header = build_header(title=f"{agent_emoji} 状态 · {binding_name}", template="indigo")
    return build_card(header=header, elements=elements)


def build_slash_card(binding_name: str, command_map: dict[str, str] | None = None) -> dict:
    """Cheatsheet of every Claude/Codex slash command the bot can trigger.

    `command_map` is `DEFAULT_MENU_COMMAND_MAP` (event_key → /slash). We only
    show the slash bodies since that's what the user types or selects.
    """
    DEFAULT_DESC = {
        "/clear": "清空当前对话(开新会话)",
        "/compact": "压缩历史,腾出上下文",
        "/resume": "恢复上次会话",
        "/cost": "查看本次会话花费",
        "/status": "agent 当前状态",
        "/quit": "退出 TUI",
        "/model": "切换模型",
        "/config": "查看 / 修改配置",
        "/init": "在当前项目初始化 agent",
        "/permissions": "权限策略管理",
        "/login": "登录账号",
        "/logout": "登出",
        "/agents": "管理子 agent",
        "/mcp": "管理 MCP server",
        "/memory": "查看 / 编辑长期记忆",
        "/hooks": "管理 hooks",
        "/skills": "查看 / 启用 skills",
        "/add-dir": "把目录加入会话上下文",
        "/help": "agent 内置 help",
        "/usage": "用量查询",
        "/doctor": "环境诊断",
        "/bug": "报告 bug",
    }
    rows = ["| slash | 作用 |", "|---|---|"]
    seen: set[str] = set()
    if command_map:
        for slash in command_map.values():
            if slash in seen:
                continue
            seen.add(slash)
            desc = DEFAULT_DESC.get(slash, "")
            rows.append(f"| `{slash}` | {desc} |")
    # Fall back to the full list if no map passed
    if len(rows) == 2:
        for slash, desc in DEFAULT_DESC.items():
            rows.append(f"| `{slash}` | {desc} |")

    md = "### 🪄 Slash 命令\n\n" + "\n".join(rows) + (
        "\n\n直接在飞书发整行 `/slash` 或在机器人侧栏菜单一键触发。"
    )
    elements = [
        build_markdown(md),
        build_note("`!命令` 查看本卡 · `!状态` 看当前 binding · `!帮助` 看所有 ! 命令"),
    ]
    header = build_header(title=f"🪄 Slash 命令 · {binding_name}", template="blue")
    return build_card(header=header, elements=elements)


def build_bindings_card(bindings: list[dict]) -> dict:
    """List every binding on this machine + each one's chat / liveness.

    `bindings` is a list of dicts shaped like:
        {name, project_dir, tmux_session, feishu_app_id, chat_id, mtime}
    """
    if not bindings:
        elements = [build_markdown("当前没有任何 binding。在 TUI 里跑 `/bot-new <name>` 创建。")]
    else:
        lines = ["### 🔗 本机所有 binding\n"]
        for b in bindings:
            name = b.get("name", "?")
            cwd = b.get("project_dir", "?")
            session = b.get("tmux_session", "?")
            chat = b.get("chat_id") or "(未 bootstrap)"
            mtime = b.get("mtime")
            status = ""
            if mtime is not None:
                age = time.time() - mtime
                if age < 30:
                    status = " · 🟢 运行中"
                elif age < 600:
                    status = " · 🟡 空闲"
                else:
                    status = " · ⚪ 长期空闲"
            lines.append(f"**`{name}`**{status}")
            lines.append(f"   📂 `{cwd}`")
            lines.append(f"   🖥️ session: `{session}` · chat: `{chat}`\n")
        elements = [build_markdown("\n".join(lines))]
    elements.append(build_note("当前飞书 chat 跟 binding 是 1:1 绑定的;切换 binding 请到对应机器人聊天"))
    header = build_header(title="🔗 Binding 列表", template="violet")
    return build_card(header=header, elements=elements)


def build_find_card(binding_name: str, keyword: str, matches: list[dict]) -> dict:
    """Render search results across the agent's jsonl history.

    Each `matches` entry: {timestamp, role, snippet}
    """
    if not matches:
        body = f"在本项目历史里**没找到** `{keyword}`(只搜了最近的会话片段)。"
    else:
        lines = [f"在历史中找到 **{len(matches)}** 条包含 `{keyword}`:\n"]
        for m in matches[:10]:
            ts = m.get("timestamp", "")
            role = m.get("role", "?")
            snippet = m.get("snippet", "").replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            # Highlight the keyword
            try:
                idx = snippet.lower().find(keyword.lower())
                if idx >= 0:
                    snippet = (
                        snippet[:idx] + "**" + snippet[idx:idx + len(keyword)] + "**" + snippet[idx + len(keyword):]
                    )
            except Exception:
                pass
            lines.append(f"- `{ts[:19]}` *{role}* — {snippet}")
        if len(matches) > 10:
            lines.append(f"\n_…还有 {len(matches) - 10} 条未显示_")
        body = "\n".join(lines)
    elements = [
        build_markdown(body),
        build_note(f"搜索范围:本项目最近 5MB jsonl ·  关键词:`{keyword}`"),
    ]
    header = build_header(title=f"🔍 搜索 · {binding_name}", template="turquoise")
    return build_card(header=header, elements=elements)


def search_jsonl(jsonl_path: Path | str, keyword: str, max_bytes: int = 5 * 1024 * 1024, max_matches: int = 50) -> list[dict]:
    """Scan the tail of a jsonl for events whose text contains `keyword`.

    Returns a list of {timestamp, role, snippet}, oldest first. Case-insensitive.
    """
    jp = Path(jsonl_path)
    if not jp.exists() or not keyword.strip():
        return []
    needle = keyword.lower()
    size = jp.stat().st_size
    start = max(0, size - max_bytes)
    with jp.open("rb") as f:
        f.seek(start)
        chunk = f.read().decode("utf-8", errors="replace")
    # If we sliced mid-line, drop the partial first line
    if start > 0:
        i = chunk.find("\n")
        if i >= 0:
            chunk = chunk[i + 1:]
    matches: list[dict] = []
    for line in chunk.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Codex envelope
        if d.get("type") == "response_item":
            payload = d.get("payload", {}) or {}
            if payload.get("type") == "message":
                content = payload.get("content", [])
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text", "")
                        if text and needle in text.lower():
                            matches.append({
                                "timestamp": d.get("timestamp", ""),
                                "role": payload.get("role", "?"),
                                "snippet": text,
                            })
                            break
            elif payload.get("type") == "function_call_output":
                text = payload.get("output", "")
                if isinstance(text, str) and needle in text.lower():
                    matches.append({
                        "timestamp": d.get("timestamp", ""),
                        "role": "tool",
                        "snippet": text,
                    })
        # Claude shape
        elif isinstance(d.get("message"), dict):
            content = d["message"].get("content", [])
            text_acc: list[str] = []
            if isinstance(content, str):
                text_acc.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_acc.append(part.get("text", ""))
            joined = "\n".join(text_acc)
            if joined and needle in joined.lower():
                matches.append({
                    "timestamp": d.get("timestamp", ""),
                    "role": d.get("message", {}).get("role", d.get("role", "?")),
                    "snippet": joined,
                })
        if len(matches) >= max_matches:
            break
    return matches


def build_help_card(binding_name: str) -> dict:
    """Cheatsheet of every `!`-prefixed command the bot understands."""
    md = """### 🎛 键盘控制(在飞书发文字)

| 命令 | 作用 |
|---|---|
| `!中断` `!esc` `!cancel` | 中断当前任务(Esc) |
| `!退出` `!exit` `!^c` | Ctrl-C 退出 |
| `!上` `!下` `!left` `!right` | 方向键 / 历史浏览 |
| `!tab` `!补全` | Tab 自动补全 |
| `!bs` `!删` | Backspace |
| `!^l` `!^u` `!^d` | 清屏 / 杀行 / EOF |
| `!enter` `!回车` | 单独 Enter |
| `!y` `!yes` `!是` `!确认` `!ok` | 一键 yes(权限提示) |
| `!n` `!no` `!否` `!不` | 一键 no |

### 📊 状态 / 帮助

| 命令 | 作用 |
|---|---|
| `!status` `!状态` | 显示当前 binding 状态卡片 |
| `!help` `!帮助` | 显示这张帮助卡 |

### 📝 其它

- 普通文字 → 注入 TUI(多行 \\n 用 Alt+Enter 软换行,最后 Enter 提交)
- 图片消息 → 自动下载到 inbox,路径注入 TUI(Claude/Codex 自动识别图片)
- TUI 输出里包含图片路径(jsonl 里) → 自动上传到飞书显示
- 飞书机器人菜单 → 「会话/配置/工具/信息/控制/桥接」分组,常用 slash 一键触发"""

    elements = [
        build_markdown(md),
        build_note("提示:控制类命令带 `!` 前缀,避免跟普通文字冲突"),
    ]
    header = build_header(title=f"📚 命令帮助 · {binding_name}", template="green")
    return build_card(header=header, elements=elements)
