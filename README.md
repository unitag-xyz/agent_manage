# Agent Manage

入口：

```bash
python3 scripts/agentctl.py
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
- 直接从 `openclaw.json` 的 `agents.list` 检查同名 agent 是否已存在
- 如果同名 agent 已存在，会跳过 `openclaw agents add`，继续后续步骤
- 如果 workspace 已存在且非空，会跳过 `workspace.populate`，继续后续步骤
- `--model-key` 为必填，会写入 `~/.openclaw/openclaw.json` 的
  `models.providers.unipay-fun.apiKey`
- 创建完成后会重写 `~/.openclaw/openclaw.json` 里的默认模型配置，只保留以下模型：
  `gpt-5.4-nano`、`gpt-5.4`、`gpt-5.3-codex`、`gpt-5.4-mini`、`gpt-5-nano`
- 默认主模型固定写成 `unipay-fun/gpt-5.4-nano`
- 如果模板原先带有 `vllm` 等旧 provider，会在初始化时被覆盖掉，只保留 `unipay-fun`
- 创建完成后会额外写入 `~/.openclaw/openclaw.json` 的工具默认配置：
  `tools.profile = coding`、`tools.exec.security = full`、
  `tools.web.search.enabled = false`、`tools.web.fetch.enabled = true`
- 如执行失败，默认按当前实现做回滚

前置要求：

```bash
~/template/unipay-claw-base.zip
```

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py create-instance \
  --template-name unipay-claw-base \
  --model-key YOUR_MODEL_KEY
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
      {"step": "workspace.populate", "result": {}},
      {"step": "config.configure_models", "result": {}},
      {"step": "config.configure_tools", "result": {}}
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

- 直接从 `openclaw.json` 的 `agents.list` 检查目标 agent 是否存在
- 新增或覆盖一个 Telegram bot 账号配置
- 当前 bot 按公开模式写入：
  `dmPolicy = open`，`allowFrom = ["*"]`
- 会删除该 bot 名下旧的 Telegram binding，再写入一条新的 binding 指向指定 agent
- 不传 `--bot-name` 时自动生成 `tgbot-xxxxxxxx`
- 写入配置后总是执行一次 `openclaw gateway restart`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py add-tg-bot \
  --agent unipay-claw-base \
  --tg-token 123456:abc
```

指定 bot 名：

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py add-tg-bot \
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
- `gateway_restart`

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
    },
    "gateway_restart": {
      "step": "gateway.restart",
      "result": {
        "command": "openclaw gateway restart",
        "returncode": 0,
        "skipped": false
      }
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

- 执行 `openclaw gateway status --require-rpc --json`
- 默认 10 秒超时，避免等待太久同时减少误判
- 只有当 gateway 服务和 RPC probe 都正常时，才认为服务器和 `openclaw` 可工作
- 同时读取一次当前 TG bot 状态、当前微信 bot 状态、当前配置模型，一并放进返回体

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py check-server-status
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `check`
- `timeout_seconds`
- `config_path`
- `config_exists`
- `gateway_status`
- `tg_bot_status`
- `weixin_bot_status`
- `current_model_status`

示例：

```json
{
  "result": {
    "ok": true,
    "check": "openclaw gateway status --require-rpc --json",
    "timeout_seconds": 10,
    "config_path": "/root/.openclaw/openclaw.json",
    "config_exists": true,
    "gateway_status": {
      "ok": true,
      "service": {
        "status": "running"
      },
      "runtime": {
        "status": "running"
      },
      "rpc": {
        "ok": true
      }
    },
    "tg_bot_status": {
      "ok": true,
      "telegram_enabled": true,
      "tg_bot_count": 1,
      "bound_tg_bot_count": 1,
      "total_binding_count": 1,
      "bots": [
        {
          "bot_name": "publicbot",
          "enabled": true,
          "binding_count": 1,
          "is_bound": true,
          "dm_policy": "open"
        }
      ]
    },
    "weixin_bot_status": {
      "ok": true,
      "weixin_bot_count": 1,
      "bound_weixin_bot_count": 1,
      "total_binding_count": 1,
      "bots": [
        {
          "account_id": "bot-a-im-bot",
          "bot_name": "客服A",
          "enabled": true,
          "binding_count": 1,
          "is_bound": true,
          "route_tag": null,
          "cdn_base_url": null,
          "has_state_file": true,
          "state_baseurl": "https://ilinkai.weixin.qq.com",
          "ilink_user_id": "wx-user-1"
        }
      ]
    },
    "current_model_status": {
      "ok": true,
      "current_model": "unipay-fun/gpt-5.4-nano",
      "configured_default_model": "unipay-fun/gpt-5.4-nano",
      "agent_overrides": [],
      "config_path": "/root/.openclaw/openclaw.json",
      "config_exists": true
    }
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
cd ~/data/agent_manage && python3 scripts/agentctl.py tg-bot-status
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

## add-weixin-bot

### 行为说明

- 用前端已经拿到的微信登录成功结果补齐本机接入流程
- 要求传入前端拿到的登录成功字段：
  `ilink_bot_id`、`bot_token`，可选 `baseurl`、`ilink_user_id`
- 直接从 `openclaw.json` 的 `agents.list` 检查目标 agent 是否存在
- 确保 `plugins.entries.openclaw-weixin.enabled = true`
- 不检查 `openclaw-weixin` 插件安装状态，不执行自动安装
- 写入配置后总是执行一次 `openclaw gateway restart`
- 将微信账号状态写入 `~/.openclaw/openclaw-weixin/accounts/<accountId>.json`
- 将账号索引写入 `~/.openclaw/openclaw-weixin/accounts.json`
- 将 `channels.openclaw-weixin.accounts.<accountId>` 和绑定关系写入 `openclaw.json`
- 更新 `channels.openclaw-weixin.channelConfigUpdatedAt`，与插件扫码登录后的刷新逻辑保持一致

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py add-weixin-bot \
  --agent unipay-claw-base \
  --ilink-bot-id caf8d0cd98a9@im.bot \
  --bot-token 'caf8d0cd98a9@im.bot:0600006dbf2f19d3a8f958823xxxxx' \
  --ilink-user-id 'o9cq80-cXVWniFqxxxx_5GWg@im.wechat'
```

可选参数：

- `--baseurl`
- `--ilink-user-id`
- `--bot-name`
- `--route-tag`
- `--cdn-base-url`
- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `agent_name`
- `account_id`
- `raw_account_id`
- `plugin_prepare`
- `stale_accounts_cleared`
- `state_write`
- `config_write`

示例：

```json
{
  "result": {
    "ok": true,
    "agent_name": "unipay-claw-base",
    "account_id": "b0f5860fdecb-im-bot",
    "raw_account_id": "B0F5860FDECB@im.bot",
    "plugin_prepare": {
      "plugin_id": "openclaw-weixin",
      "install_check_skipped": true,
      "enabled": true,
      "config_updated": false,
      "restart_required": true,
      "steps": [
        {
          "step": "gateway.restart",
          "result": {
            "command": "openclaw gateway restart",
            "returncode": 0,
            "skipped": false
          }
        }
      ]
    },
    "stale_accounts_cleared": [],
    "state_write": {
      "state_dir": "/root/.openclaw/openclaw-weixin",
      "account_path": "/root/.openclaw/openclaw-weixin/accounts/b0f5860fdecb-im-bot.json",
      "index_path": "/root/.openclaw/openclaw-weixin/accounts.json"
    },
    "config_write": {
      "config_path": "/root/.openclaw/openclaw.json",
      "changed_paths": [
        "channels.openclaw-weixin.accounts.b0f5860fdecb-im-bot",
        "channels.openclaw-weixin.channelConfigUpdatedAt",
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

## weixin-bot-status

### 行为说明

- 读取当前 `~/.openclaw/openclaw.json`
- 返回当前已登记的微信 bot 总数 `weixin_bot_count`
- 返回当前已绑定的微信 bot 数 `bound_weixin_bot_count`
- 返回所有微信 bindings 总数 `total_binding_count`
- 同时读取 `~/.openclaw/openclaw-weixin/accounts/*.json`，补充本地状态文件是否存在、`baseUrl`、`ilink_user_id`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py weixin-bot-status
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `weixin_bot_count`
- `bound_weixin_bot_count`
- `total_binding_count`
- `bots`

示例：

```json
{
  "result": {
    "ok": true,
    "weixin_bot_count": 2,
    "bound_weixin_bot_count": 1,
    "total_binding_count": 2,
    "bots": [
      {
        "account_id": "bot-a-im-bot",
        "bot_name": "客服A",
        "enabled": true,
        "binding_count": 2,
        "is_bound": true,
        "route_tag": "route-a",
        "cdn_base_url": null,
        "has_state_file": true,
        "state_baseurl": "https://ilinkai.weixin.qq.com",
        "ilink_user_id": "wx-user-1"
      },
      {
        "account_id": "bot-b-im-bot",
        "bot_name": null,
        "enabled": true,
        "binding_count": 0,
        "is_bound": false,
        "route_tag": null,
        "cdn_base_url": null,
        "has_state_file": false,
        "state_baseurl": null,
        "ilink_user_id": null
      }
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## delete-weixin-bot

### 行为说明

- 按 `ilink_bot_id` 删除对应微信账号
- 会先把传入值规范化成内部 `account_id`
- 同时删除 `channels.openclaw-weixin.accounts.<accountId>`
- 同时删除所有引用该账号的微信 bindings
- 同时删除 `~/.openclaw/openclaw-weixin/accounts/<accountId>.json` 等本地状态文件
- 更新 `channels.openclaw-weixin.channelConfigUpdatedAt`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py delete-weixin-bot \
  --ilink-bot-id caf8d0cd98a9@im.bot
```

可选参数：

- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `deleted_account_id`
- `raw_account_id`
- `removed_bindings`
- `remaining_weixin_bot_count`
- `state_delete`
- `config_write`

示例：

```json
{
  "result": {
    "ok": true,
    "deleted_account_id": "caf8d0cd98a9-im-bot",
    "raw_account_id": "caf8d0cd98a9@im.bot",
    "removed_bindings": 1,
    "remaining_weixin_bot_count": 0,
    "state_delete": {
      "deleted_files": [
        "/root/.openclaw/openclaw-weixin/accounts/caf8d0cd98a9-im-bot.json"
      ],
      "index_path": "/root/.openclaw/openclaw-weixin/accounts.json",
      "remaining_index_count": 0
    },
    "config_write": {
      "config_path": "/root/.openclaw/openclaw.json",
      "changed_paths": [
        "channels.openclaw-weixin.accounts.caf8d0cd98a9-im-bot",
        "channels.openclaw-weixin.channelConfigUpdatedAt",
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

## delete-tg-bot

### 行为说明

- 按 bot 名删除 `channels.telegram.accounts.{bot_name}`
- 同时删除所有引用该 bot 的 Telegram bindings
- 返回删除了多少条 bindings，以及剩余 bot 数量
- 如果删完后没有剩余 bot，会把 `channels.telegram.enabled` 设为 `false`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py delete-tg-bot \
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

## agents-list

### 行为说明

- 执行 `openclaw agents list --bindings --json`
- 返回当前服务器上的全部 agents
- 返回里会主动排除 `main`
- 适合给上层直接展示当前实例列表

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py agents-list
```

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `check`
- `agent_count`
- `agents`

示例：

```json
{
  "result": {
    "ok": true,
    "check": "openclaw agents list --bindings --json",
    "agent_count": 1,
    "agents": [
      {
        "id": "unipay-claw-base",
        "name": "unipay-claw-base",
        "workspace": "/home/ubuntu/data/unipay-claw-base",
        "agentDir": "/home/ubuntu/.openclaw/agents/unipay-claw-base/agent"
      }
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## set-model

### 行为说明

- 只允许在以下 5 个模型中切换：
  `unipay-fun/gpt-5.4-nano`、`unipay-fun/gpt-5.4`、`unipay-fun/gpt-5.3-codex`、`unipay-fun/gpt-5.4-mini`、`unipay-fun/gpt-5-nano`
- 传参必须写完整模型引用，不再接受简写
- 直接执行 `openclaw models set <model_ref>`
- 用于切换当前默认模型
- 按当前观察，模型会在运行中逐步切换，不需要额外重启 gateway

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py set-model \
  --model unipay-fun/gpt-5.4
```

可选模型：

- `unipay-fun/gpt-5.4-nano`
- `unipay-fun/gpt-5.4`
- `unipay-fun/gpt-5.3-codex`
- `unipay-fun/gpt-5.4-mini`
- `unipay-fun/gpt-5-nano`

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `model_ref`
- `steps`

示例：

```json
{
  "result": {
    "ok": true,
    "model_ref": "unipay-fun/gpt-5.4",
    "steps": [
      {
        "step": "models.set",
        "result": {
          "command": "openclaw models set unipay-fun/gpt-5.4",
          "returncode": 0,
          "skipped": false
        }
      }
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## current-model

### 行为说明

- 直接读取 `~/.openclaw/openclaw.json`
- 返回当前配置里的默认模型，不起 `openclaw` 子进程
- 同时返回非 `main` agent 的模型覆盖，便于排查“默认模型”和实例模型不一致的问题
- 这个命令返回的是配置结果，不代表某个 Telegram session 的临时 override

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py current-model
```

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `current_model`
- `configured_default_model`
- `agent_overrides`
- `config_path`
- `config_exists`

示例：

```json
{
  "result": {
    "ok": true,
    "current_model": "unipay-fun/gpt-5.4-nano",
    "configured_default_model": "unipay-fun/gpt-5.4-nano",
    "agent_overrides": [
      {
        "agent_id": "unipay-claw-base",
        "model": "unipay-fun/gpt-5.4-mini"
      }
    ],
    "config_path": "/root/.openclaw/openclaw.json",
    "config_exists": true
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## 标准返回结构

所有命令统一返回以下结构：

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
