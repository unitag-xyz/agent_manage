# OpenClaw Agent Script

> Deprecated: 本文档对应 `v1` 方案，仅保留作历史参考。
> 当前仓库后续只维护 `agent_manage/` 和 `scripts/agentctl.py`。

这套脚本现在只保留两个命令：

- `create`
- `delete`

## SSH 调用示例

你可以直接从本机通过 SSH 调服务器上的脚本：

```bash
ssh deploy@your-server 'cd /srv/openclaw && python3 scripts/agentctl_Deprecated.py create \
  --tg-id 123456789 \
  --model openai-codex/gpt-5.4 \
  --tg-bot unipaytgbot \
  --tg-bot-token 123456:abc'
```

删除示例：

```bash
ssh deploy@your-server 'cd /srv/openclaw && python3 scripts/agentctl_Deprecated.py delete \
  --tg-id 123456789'
```

它默认你的场景是：

- 一个 Telegram bot 可以服务多个 agent
- 不同 Telegram 私聊用户按 `peer.id` 路由到不同 agent
- 用户的 Telegram 账号 ID 默认直接作为 `agentId`
- 如果传了 `--agent-name`，则优先用它作为 `agentId`
- workspace 固定为 `/data/openclaw/{agent_id}`

配置写入方式：

- agent 创建和删除走 `openclaw agents add/delete`
- Telegram bot 配置和 `bindings` 不再走 `gateway call`
- 脚本直接读写 `~/.openclaw/openclaw.json`
- 每次写回前会自动备份一份 `openclaw.json.bak`

脚本入口：

```bash
python3 scripts/agentctl_Deprecated.py
```

## create

`create` 的输入核心是用户 `tg_id`。

- `tg_id` 就是 Telegram DM 用户 ID
- 默认 `agentId = tg_id`
- 如果传了 `--agent-name`，则 `agentId = agent_name`
- `workspace = /data/openclaw/{agent_id}`

示例：

```bash
python3 scripts/agentctl_Deprecated.py create \
  --tg-id 123456789 \
  --model openai-codex/gpt-5.4 \
  --tg-bot unipaytgbot \
  --tg-bot-token 123456:abc
```

指定自定义 agent 名字：

```bash
python3 scripts/agentctl_Deprecated.py create \
  --tg-id 123456789 \
  --agent-name alice \
  --model openai-codex/gpt-5.4 \
  --tg-bot unipaytgbot
```

创建逻辑：

1. 如果 agent 不存在，执行 `openclaw agents add`
2. 如果 agent 已存在，继续走后续流程
3. 如果传了 `--model` 且 agent 已存在，更新这个 agent 的模型
4. 如果传了 `--tg-bot-token`，就创建或更新这个 Telegram bot
   同时自动把当前 `tg-id` 写进这个 bot 的白名单
5. 自动写入一条 DM peer binding：

```json
{
  "agentId": "alice",
  "match": {
    "channel": "telegram",
    "accountId": "unipaytgbot",
    "peer": { "kind": "dm", "id": "123456789" }
  }
}
```

如果 Telegram bot 已经存在：

- 可以不传 `--tg-bot-token`
- 如果系统里只有一个 bot，也可以不传 `--tg-bot`

如果系统里有多个 bot，则必须显式传 `--tg-bot`。

## delete

`delete` 按 `tg_id` 删除整组资源：

```bash
python3 scripts/agentctl_Deprecated.py delete \
  --tg-id 123456789
```

如果你创建时传了 `--agent-name`，删除时也要传同一个名字：

```bash
python3 scripts/agentctl_Deprecated.py delete \
  --tg-id 123456789 \
  --agent-name alice
```

删除逻辑：

1. 删除这个 `tg_id` 对应的 Telegram DM binding
2. 删除对应 agent，默认 `agentId = tg_id`，或者你传入的 `agent-name`
3. 默认保留 `/data/openclaw/{agent_id}` 目录

如果你明确要删除 workspace，再加：

```bash
--purge-workspace
```

## Telegram bot 配置

Telegram bot 现在写成共享 bot 模式：

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "accounts": {
        "unipaytgbot": {
          "botToken": "你的_bot_token",
          "dmPolicy": "allowlist",
          "allowFrom": ["123456789"]
        }
      }
    }
  }
}
```

然后不同用户通过顶层 `bindings` 分流到不同 agent。

每次执行 `create` 时，脚本都会把当前 `tg-id` 追加进对应 bot 的 `allowFrom` 白名单里，不会覆盖已有用户。

## 通用参数

- `--openclaw-bin`: OpenClaw 可执行文件名，默认 `openclaw`
- `--project-dir`: 在哪个目录执行命令，默认当前目录
- `--config-path`: OpenClaw 配置文件路径，默认 `~/.openclaw/openclaw.json`
- `--dry-run`: 只输出将执行的 OpenClaw 命令，不真的执行

注意：这些全局参数要写在子命令前面，例如：

```bash
python3 scripts/agentctl_Deprecated.py \
  --openclaw-bin /home/ubuntu/.nvm/versions/node/v22.22.0/bin/openclaw \
  --config-path /home/ubuntu/.openclaw/openclaw.json \
  create \
  --tg-id 123456789 \
  --model openai-codex/gpt-5.4 \
  --tg-bot unipaytgbot
```

## 标准返回结构

脚本现在统一约定：

- `stdout` 只输出一段标准 JSON，供上层程序读取
- `stderr` 只输出日志
- 成功退出码为 `0`
- 失败退出码为非 `0`，但 `stdout` 仍会返回结构化错误 JSON

统一返回格式：

```json
{
  "result": {},
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

失败时：

```json
{
  "result": null,
  "error": {
    "code": "AGENT_NOT_FOUND",
    "details": {},
    "steps": [],
    "rollback": []
  },
  "typeCode": 10,
  "message": "Agent 'alice' not found",
  "serverTimeStamp": "2026-04-04 09:37:57"
}
```

字段规则：

- `result`：成功结果体
- `error`：失败详情，成功时为 `null`
- `typeCode`：供上层系统判断的稳定分类码
- `message`：给人读的摘要文案
- `serverTimeStamp`：服务器响应时间

当前 `typeCode` 约定：

- `1`：成功
- `2`：已受理，异步处理中
- `10`：资源不存在
- `11`：参数错误或校验失败
- `12`：状态冲突
- `20`：底层命令执行失败
- `21`：执行失败且带回滚信息
- `50`：内部错误

建议上层系统优先依赖 `typeCode` 和 `error.code` 做程序判断，不要依赖 `message` 文案匹配。
