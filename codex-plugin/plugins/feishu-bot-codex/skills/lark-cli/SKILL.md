---
name: lark-cli
description: "Operate Feishu / Lark via the `lark-cli` binary already installed on this machine. Trigger AGGRESSIVELY whenever the user asks you to *do* something on Feishu beyond mirroring this chat — sending a message to a specific chat/user, creating / editing / reading a calendar event, uploading or fetching a file on Feishu Drive, reading or writing a sheet / wiki / doc, looking up someone in the company directory, posting an announcement, downloading a meeting recording, etc. Also trigger on Chinese 飞书 / Lark / 发飞书 / 查日历 / 上传到云文档 / 搜通讯录 / 飞书云文档 / 飞书审批 / 飞书会议 / 找用户 / 发群消息 phrases. Refuse + ask if the user wants to mass-message or operate cross-tenant."
---

# lark-cli — operate Feishu directly from this TUI

The `lark-cli` binary (Go, installed via `npm i -g @larksuite/cli`) exposes
almost the entire Feishu open-platform API as shell subcommands. You have
shell access via the `Bash` tool, so you can call it directly to do real
work in Feishu on the user's behalf — send messages, manage calendars,
read/write cloud docs, query contacts, etc.

**This skill is separate from the `feishu-bot` skill.** That one is for
*managing the bot binding itself* (binding, mirroring, stopping). THIS one
is for *using Feishu features* (send a message, create a doc, etc.).

## Quick command surface (full list: `lark-cli --help`)

| Service | What it does | Typical commands |
|---|---|---|
| `im` | Messages / chats / reactions | `im +messages-send`, `im +chat-create`, `im reactions create`, `im +messages-search` |
| `calendar` | Events / attendees / shares | `calendar +agenda`, `calendar events instance_view`, `calendar events create` |
| `drive` | Files / folders / uploads | `drive +files-upload`, `drive files list`, `drive files download` |
| `docs` | Docs content | `docs +docs-create`, `docs blocks list`, `docs blocks patch` |
| `sheets` | Spreadsheets | `sheets +sheet-create`, `sheets values append/get/update` |
| `wiki` | Wiki spaces / nodes | `wiki +nodes-create`, `wiki spaces list` |
| `contact` | Users / departments | `contact +search-user --query "name"`, `contact users get` |
| `base` | Bitable (multi-dim table) | `base +tables-create`, `base records list/create/update/delete` |
| `approval` | Approval instances | `approval +instances-create`, `approval +instances-list` |
| `attendance` | Attendance records | `attendance +records-query` |
| `vc` | Video meetings | `vc +meetings-create`, `vc +reserves-apply` |
| `mail` | Email / drafts | `mail +messages-list`, `mail +drafts-send` |
| `task` `okr` `minutes` `slides` `whiteboard` | Self-explanatory | same `+<verb>` pattern |
| `api` | Raw HTTP fallback | `api POST /open-apis/<path> --data '<json>'` |
| `schema` | Inspect parameters | `schema im.messages.create --format pretty` |

## How to discover the exact command for a task

When the user describes something not on the table above (or you're unsure
of the flags):

```bash
lark-cli <service> --help                  # list subcommands
lark-cli <service> <subcommand> --help     # list flags
lark-cli schema <service>.<resource>       # full input/output JSON shape
```

Always do this BEFORE constructing a command for a destructive operation
(send, create, update, delete) — Feishu errors are opaque and getting flags
wrong wastes user time.

## Profile selection

`lark-cli profile list` shows the configured bot apps. On this machine:

- Each profile has its own `app_id` (and thus its own bot identity)
- Pass `--profile <name>` explicitly for every command — without it,
  lark-cli picks the active default, which may be a DIFFERENT bot than the
  one bound to this Codex session

How to pick:
1. Look at the **current binding name** the user is talking to you through
   (often visible as "🤖 Codex · `<binding>`" on Feishu cards). Use that
   profile name first.
2. If unclear, ask the user: "你想用哪个机器人身份发?(列出 profile list 让用户挑)"
3. For operations that should be the **user's own** identity (e.g. reading
   their personal calendar), pass `--as user` instead of `--as bot`.

## Identity flag

- `--as bot` (default in most contexts) → tenant_access_token, operates as
  the bot. Required for sending to chats the bot is in.
- `--as user` → user_access_token, operates as the logged-in human. Required
  for personal data (own calendar, own drive root, own draft mail).

## Common recipes

### Send a text message to a chat
```bash
lark-cli im +messages-send --profile <bot> --as bot \
  --chat-id oc_xxxxxxxx --text "你好,这是机器人发的"
```

### Send an interactive card
```bash
lark-cli im +messages-send --profile <bot> --as bot \
  --chat-id oc_xxx --msg-type interactive \
  --content '{"schema":"2.0","header":{"title":{"tag":"plain_text","content":"标题"}},"body":{"elements":[{"tag":"markdown","content":"卡片内容"}]}}'
```

### Search for a user by name
```bash
lark-cli contact +search-user --profile <bot> --as user --query "张三"
```

### List today's calendar events
```bash
lark-cli calendar +agenda --profile <bot> --as user
```

### Create a calendar event
```bash
lark-cli calendar events create --profile <bot> --as user \
  --params '{"calendar_id":"primary"}' \
  --data '{"summary":"和团队同步","start_time":{"timestamp":"1730000000"},"end_time":{"timestamp":"1730003600"}}'
```

### Upload a file to Drive
```bash
lark-cli drive +files-upload --profile <bot> --as user \
  --file file=/local/path/report.pdf --parent-token <folder_token>
```

### Create / append rows in a Bitable
```bash
lark-cli base records create --profile <bot> --as bot \
  --params '{"app_token":"...","table_id":"..."}' \
  --data '{"fields":{"Name":"Alice","Status":"Done"}}'
```

### Reach for `api` when no shortcut exists
```bash
lark-cli api POST /open-apis/<service>/<resource>/<action> --as bot \
  --data '{...}'
```

## Output shape

Every command returns JSON like:
```json
{"ok": true, "identity": "bot", "data": { ... }}
```
or on failure:
```json
{"ok": false, "error": {"type": "...", "code": 230099, "message": "...", "detail": {...}}}
```

When you see `ok: false`, **read `error.code` + `error.message`** and report
the actual cause. Common codes:
- `230025` body too long
- `230099 / 11310 element/table/note` card schema violation
- `99992402` field validation (often uuid > 50 chars, image_key invalid, etc.)
- `11232` rate-limited
- `230028` permission denied / scope missing
- `400/401` auth scope missing — tell the user which scope to grant

## Safety rules

Before doing **anything destructive or that contacts other humans**, confirm:

1. **Cross-chat sends**: if the user said "发消息" without specifying which
   chat, ask which `chat_id` or list `im +chat-list` and present options.
2. **Mass operations**: never loop a send across many chats / users without
   the user typing out an explicit acknowledgement.
3. **Cross-tenant**: refuse if a command would reach a tenant the user
   isn't logged into.
4. **Approvals / finance / HR**: just-confirm before submitting.
5. **Drive deletes**: never `drive files delete` without confirmation.

Reads are safe by default (list, get, search). Writes need confirmation
unless the user phrased the request very explicitly ("发『辛苦了』给 oc_xxx").

## Connecting with the bot bridge (this session)

The Feishu card you're rendering output to is in chat
**${FEISHU_CHAT_ID}** (the binding's chat_id, persisted in
`~/.feishu-bot-*/state-<binding>.json`). If the user says "把刚才的输出
转到群 A", you can:

1. `lark-cli im +chat-search --query "群 A 名字"` → get the new `chat_id`
2. `lark-cli im +messages-send --chat-id <new> --content '<card json>'`

Don't replicate the bridge's card-rendering pipeline — just reuse the bot
output the user already sees.

## When NOT to invoke this skill

- User is asking a CODE question about Feishu's API (write docs / explain) →
  answer in chat, don't actually call lark-cli
- Just discussing options ("能不能…") → describe, don't act
- User is binding/unbinding the bot itself → use the `feishu-bot` skill
- Pure offline workflow (no Feishu side-effect needed) → don't call lark-cli
