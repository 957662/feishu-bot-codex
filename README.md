# feishu-bot-codex

> **让本地的 Codex CLI(OpenAI 官方),通过飞书机器人随时随地遥控。**
> 跟姊妹仓 [feishu-bot-claude](https://github.com/957662/feishu-bot-claude) 同一套架构,把 Claude 换成了 Codex,**也兼容 Claude**(选 `--agent` 即可)。

[![Platform](https://img.shields.io/badge/platform-macOS-blue)](https://github.com/957662/feishu-bot-codex)
[![Agent](https://img.shields.io/badge/agent-codex%20%7C%20claude-purple)]()
[![Python](https://img.shields.io/badge/python-3.11%2B-green)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)
[![Commercial](https://img.shields.io/badge/commercial%20use-prohibited%20without%20license-red)](#license)

---

## ⚠️ 状态:实验性,Codex 通道已跑通

- ✅ **Codex jsonl 解析**:已读真实 codex session(150 MB / 708 事件 / 11 turn / 0 错误)
- ✅ **卡片渲染**:Codex 的 `function_call` / `function_call_output` 自动渲染成飞书折叠工具块
- ✅ **同时兼容 Claude jsonl**:legacy fallback 路径保留
- ⏳ **斜杠命令(`/bot-new` 等在 Codex TUI 内触发)**:Codex 的 slash 走 plugin marketplace,本仓暂只接入 CLI 入口
- ⏳ **多 agent 并存(同机两个机器人,自动识别)**:后续设计,见末尾

## 🤔 它跟 feishu-bot-claude 的关系

| | feishu-bot-claude | **feishu-bot-codex(本仓)** |
|---|---|---|
| 默认 agent | Claude Code | **Codex CLI** |
| 支持 agent | Claude only | **Codex + Claude(选)** |
| jsonl 格式适配 | `~/.claude/projects/-<cwd>/*.jsonl`(role/content) | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`(envelope/payload)**+** Claude legacy |
| 卡片标题 | 🤖 Claude · ... | 🤖 Codex · ... |
| 飞书侧代码 | 一样 | 一样(逐字相同) |
| 多 backend dispatch | 否 | **是(`--agent codex\|claude`)** |

## 📦 适配 Codex 的关键改动

> 如果你想看代码差异,主要集中在 3 个文件:

1. **`rendering/turn.py::JsonlEvent.from_dict`** —— 加 codex 格式翻译层:
   - `response_item.payload.type=message,role=user/assistant` → 内部 `role:"user/assistant"` + `content:[{type:"text"}]`
   - `response_item.payload.type=function_call` → 内部 `tool_use`
   - `response_item.payload.type=function_call_output` → 内部 `tool_result`
   - `session_meta` / `turn_context` / `event_msg` / `reasoning` / developer 消息 → `role:"_meta"`(下游过滤)

2. **`daemon/orchestrator.py::_guess_jsonl_path`** —— 同时扫两个 backend 找最新:
   ```python
   # Codex: 扫 ~/.codex/sessions/*/*/*/rollout-*.jsonl,首行 session_meta.payload.cwd 匹配
   # Claude: 扫 ~/.claude/projects/-<encoded-cwd>/*.jsonl
   # 按 mtime 排序选最新
   ```

3. **`cli.py shell` + `scripts/feishu-bot-claude-shell`** —— 加 `--agent codex|claude` 选项,默认 `codex`。

## 🚀 快速开始

```bash
git clone https://github.com/957662/feishu-bot-codex ~/project/feishu-bot-codex
cd ~/project/feishu-bot-codex
./setup.sh
# 可选: npm i -g @mermaid-js/mermaid-cli   # 把 ```mermaid 代码块自动渲染成图片插到飞书卡片里
cd ~/your-project

# 默认起 codex
feishu-bot-codex shell

# 或者起 claude(同样工作)
feishu-bot-codex shell --agent claude
```

进入 Codex / Claude TUI 后:

```
/bot-new my-project-bot     # 创建飞书 App,弹浏览器扫码
/bot-start                  # 启动镜像
```

> ⚠️ **关于 `--dangerously-skip-permissions`(全权限模式)**
> 加上后 Claude/Codex 跳过所有权限确认 —— 删文件 / 跑 shell / 推 git 全直接执行。**只在隔离环境或一次性 VM 里用**,生产机/有重要数据的主力机别加。

## 🗺️ 整体架构图

```
┌────────────────────────────────────────────────────────────────────┐
│                            你的 Mac                                │
│                                                                    │
│  ┌──────────────────────────┐                                      │
│  │  Terminal (tmux session) │                                      │
│  │                          │                                      │
│  │  ┌─── 二选一 ────┐       │                                      │
│  │  │ Codex CLI    │  ←──── tmux send-keys 注入用户消息 ───────┐   │
│  │  │  (default)   │       │                                  │   │
│  │  └──────────────┘       │                                  │   │
│  │  ┌──────────────┐       │                                  │   │
│  │  │ Claude Code  │  ←─── 也可以 (同样 send-keys) ────────────┤   │
│  │  └──────────────┘       │                                  │   │
│  └─────────┬────────────────┘                                  │   │
│            │                                                   │   │
│            │ 各自写自己的 jsonl                                 │   │
│            ▼                                                   │   │
│   ~/.codex/sessions/.../rollout-*.jsonl     ┐                  │   │
│   ~/.claude/projects/-<cwd>/*.jsonl         ┘                  │   │
│            │                                                   │   │
│            │ tail-f + 自动识别格式                              │   │
│            ▼                                                   │   │
│  ┌──────────────────────────────────────────────────────────┐  │   │
│  │             feishu-bot-codex daemon                      │  │   │
│  │   (~/.feishu-bot-codex/control.sock)                     │  │   │
│  │                                                          │  │   │
│  │   ┌────────────────┐       ┌─────────────────────────┐  │  │   │
│  │   │ outbound 流水线 │       │     inbound 流水线      │  │  │   │
│  │   │ (双格式适配)    │       │  飞书事件 → tmux ─────────┘  │   │
│  │   │ jsonl → 卡片   │       │                         │  │      │
│  │   └────────┬───────┘       └─────────────────────────┘  │      │
│  └────────────┼─────────────────────────────────────────────┘      │
│               │                                                    │
│               ▼ lark-cli messages-send                             │
└───────────────┼────────────────────────────────────────────────────┘
                ▼
        ┌───────────────────────┐
        │  open.feishu.cn (云)  │
        └──────────┬────────────┘
                   ▼
       ┌──────────────────────────────┐
       │  飞书机器人聊天框 (你手机/PC) │
       └──────────────────────────────┘
```

## 🧪 端到端验证

```bash
cd ~/project/feishu-bot-codex && .venv/bin/python -c "
from pathlib import Path
from feishu_bot_codex.rendering.turn import JsonlEvent, group_into_turns, render_turn_to_card

# 拿你最大的一个 codex session 测一下
fixture = max(Path('~/.codex/sessions').expanduser().glob('*/*/*/rollout-*.jsonl'),
              key=lambda p: p.stat().st_size)
events = list(JsonlEvent.load_file(fixture))
turns = group_into_turns(events)
print(f'解析 {len(events)} 事件 → {len(turns)} 个 turn')
for t in turns[-1:]:
    card = render_turn_to_card(t, project_name='smoke', render_style='rich')
    print(f'最后一 turn: {len(card[\"body\"][\"elements\"])} 个卡片元素')
"
```

预期输出类似:
```
解析 708 事件 → 11 个 turn
最后一 turn: 22 个卡片元素
```

## ⚠️ 已知限制(相比 feishu-bot-claude)

| 问题 | 原因 | 影响 |
|---|---|---|
| `/bot-new` 等斜杠命令在 Codex TUI 里没有 | Codex 走 plugin marketplace,本仓没打包成 plugin | 你需要用 CLI 命令 `feishu-bot-codex bind <name> --cwd <path>` |
| Codex 的 reasoning(思考)不显示 | 渲染时丢弃 `payload.type=reasoning` | 你只看到结论,不看到推理过程(其实更干净) |
| Token usage 不显示在卡片底部 | Codex 把 usage 放在 `event_msg.token_count`,我们暂时跳过 | 卡片底部少一行"500+20 tokens" |
| 同机两个 agent 两个机器人没自动 dispatch | 当前 `--agent` 是 CLI 参数,binding 还没存 agent 字段 | 一个 cwd 暂时只能绑一个 agent |

后三个会在合并到主仓时统一解决(`BindingConfig` 加 `agent` 字段 + AgentBackend 接口)。

## 📚 详细文档

其余部分(常用命令、配置项、架构详解、FAQ、故障排查、测试)请直接参考姊妹仓 [feishu-bot-claude](https://github.com/957662/feishu-bot-claude) 的 README —— **95% 内容相同**,只是把 `claude` 换成 `codex / claude`。

## License

本项目使用 **[PolyForm Noncommercial License 1.0.0](LICENSE)** —— **仅限非商业用途**。
商业使用 = 侵权。详见 [LICENSE](LICENSE) 完整条款。

商业授权咨询:GitHub [@957662](https://github.com/957662)
