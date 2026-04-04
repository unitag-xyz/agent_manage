# Agent Manage V2

当前仓库后续只维护 `v2`。

以下内容已废弃，仅保留作历史备份参考：

- `openclaw_remote/`
- `scripts/agentctl.py`
- `README.md`
- `COMMANDS.md`

`v2` 入口：

```bash
python3 scripts/agentctl_v2.py
```

通用说明：

- `stdout` 只输出标准 JSON，供 `.NET`、HTTP API 或其他上层程序解析
- `stderr` 只输出执行日志
- 成功退出码为 `0`
- 失败退出码为非 `0`，但 `stdout` 仍会返回结构化错误 JSON
- 默认配置文件路径为 `~/.openclaw/openclaw.json`
- `create-instance` 默认模板目录为 `~/template`

## create-instance

### 行为说明

- `template_name` 直接作为 `agent_name`
- workspace 默认创建在 `~/data/{templateName}`，也可通过参数覆盖
- 从 `~/template/{templateName}.zip` 解压到 `~/template/{templateName}/`
- 再把 `~/template/{templateName}/` 整体复制到 workspace
- 如执行失败，默认按当前实现做回滚

前置要求：

```bash
~/template/unipay-claw-base.zip
```

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py create-instance \
  --template-name unipay-claw-base
```

可选参数：

- `--model`
- `--workspace-root`
- `--no-rollback`
- `--template-root`
- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `template_name`
- `agent_name`
- `workspace`
- `archive_path`
- `template_dir`
- `steps`

示例：

```json
{
  "result": {
    "ok": true,
    "template_name": "unipay-claw-base",
    "agent_name": "unipay-claw-base",
    "workspace": "/root/data/unipay-claw-base",
    "archive_path": "/root/template/unipay-claw-base.zip",
    "template_dir": "/root/template/unipay-claw-base",
    "steps": [
      {"step": "template.prepare", "result": {}},
      {"step": "agents.add", "result": {}},
      {"step": "workspace.populate", "result": {}}
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## add-tg-bot

### 行为说明

- 要求目标 agent 已存在
- 新增或覆盖一个 Telegram bot 账号配置
- 当前 bot 按公开模式写入：
  `dmPolicy = open`，`allowFrom = ["*"]`
- 会删除该 bot 名下旧的 Telegram binding，再写入一条新的 binding 指向指定 agent
- 不传 `--bot-name` 时自动生成 `tgbot-xxxxxxxx`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py add-tg-bot \
  --agent unipay-claw-base \
  --tg-token 123456:abc
```

指定 bot 名：

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py add-tg-bot \
  --agent unipay-claw-base \
  --tg-token 123456:abc \
  --bot-name publicbot
```

可选参数：

- `--bot-name`
- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `agent_name`
- `bot_name`
- `config_write`

示例：

```json
{
  "result": {
    "ok": true,
    "agent_name": "unipay-claw-base",
    "bot_name": "publicbot",
    "config_write": {
      "config_path": "/root/.openclaw/openclaw.json",
      "changed_paths": [
        "channels.telegram.accounts.publicbot",
        "bindings"
      ]
    }
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## check-server-status

### 行为说明

- 用一个尽量轻量的 `openclaw agents list --bindings --json` 做探活
- 默认 10 秒超时，避免等待太久同时减少误判
- 只要该命令正常返回，就认为服务器和 `openclaw` 处于可工作状态
- 这个命令不做深度巡检，不检查每个 agent 的业务状态

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py check-server-status
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `server_status`
- `openclaw_status`
- `check`
- `timeout_seconds`
- `agent_count`
- `config_path`
- `config_exists`
- `skipped`

示例：

```json
{
  "result": {
    "ok": true,
    "server_status": "ok",
    "openclaw_status": "running",
    "check": "openclaw agents list --bindings --json",
    "timeout_seconds": 10,
    "agent_count": 2,
    "config_path": "/root/.openclaw/openclaw.json",
    "config_exists": true,
    "skipped": false
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## tg-bot-status

### 行为说明

- 读取当前 `~/.openclaw/openclaw.json`
- 返回当前 bot 总数 `tg_bot_count`
- 返回当前已绑定 bot 数 `bound_tg_bot_count`
- 返回所有 Telegram bindings 总数 `total_binding_count`
- 同时返回每个 bot 的绑定情况，便于上层直接展示

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py tg-bot-status
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `telegram_enabled`
- `tg_bot_count`
- `bound_tg_bot_count`
- `total_binding_count`
- `bots`

示例：

```json
{
  "result": {
    "ok": true,
    "telegram_enabled": true,
    "tg_bot_count": 3,
    "bound_tg_bot_count": 2,
    "total_binding_count": 3,
    "bots": [
      {
        "bot_name": "idlebot",
        "enabled": true,
        "binding_count": 0,
        "is_bound": false,
        "dm_policy": "open"
      },
      {
        "bot_name": "publicbot",
        "enabled": true,
        "binding_count": 2,
        "is_bound": true,
        "dm_policy": "open"
      }
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## delete-tg-bot

### 行为说明

- 按 bot 名删除 `channels.telegram.accounts.{bot_name}`
- 同时删除所有引用该 bot 的 Telegram bindings
- 返回删除了多少条 bindings，以及剩余 bot 数量
- 如果删完后没有剩余 bot，会把 `channels.telegram.enabled` 设为 `false`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py delete-tg-bot \
  --bot-name publicbot
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `deleted_bot_name`
- `removed_bindings`
- `remaining_tg_bot_count`
- `config_write`

示例：

```json
{
  "result": {
    "ok": true,
    "deleted_bot_name": "publicbot",
    "removed_bindings": 2,
    "remaining_tg_bot_count": 1,
    "config_write": {
      "config_path": "/root/.openclaw/openclaw.json",
      "changed_paths": [
        "channels.telegram.accounts.publicbot",
        "bindings",
        "channels.telegram.enabled"
      ]
    }
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## 标准返回结构

所有 `v2` 命令统一返回以下结构：

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
    "code": "TELEGRAM_ACCOUNT_NOT_FOUND",
    "details": {},
    "steps": [],
    "rollback": []
  },
  "typeCode": 10,
  "message": "Telegram account 'publicbot' not found",
  "serverTimeStamp": "2026-04-04 09:37:57"
}
```

字段说明：

- `result`
  成功结果体，保留具体命令的业务数据
- `error`
  失败详情；成功时固定为 `null`
- `typeCode`
  响应类别码，供上游程序稳定判断
- `message`
  给人读的摘要文案，不建议上游用它做规则判断
- `serverTimeStamp`
  服务端生成响应的时间戳

当前 `typeCode` 规则：

- `1`：成功
- `2`：已受理，异步处理中
- `10`：资源不存在，例如模板、Agent、Telegram account、配置文件不存在
- `11`：参数错误或校验失败
- `12`：状态冲突，例如 Agent 已存在、workspace 非空
- `20`：底层命令执行失败
- `21`：执行失败且已经发生回滚或返回了回滚信息
- `50`：未分类内部错误

当前常用 `error.code` 包括：

- `TEMPLATE_ARCHIVE_NOT_FOUND`
- `CONFIG_FILE_NOT_FOUND`
- `AGENT_NOT_FOUND`
- `TELEGRAM_ACCOUNT_NOT_FOUND`
- `AGENT_ALREADY_EXISTS`
- `WORKSPACE_NOT_EMPTY`
- `INVALID_ARGUMENT`
- `VALIDATION_ERROR`
- `COMMAND_EXECUTION_FAILED`
- `OPERATION_FAILED_WITH_ROLLBACK`
- `INTERNAL_ERROR`
