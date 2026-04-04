# OpenClaw Agent Manage Commands

> Deprecated: 本文档描述的是 `v1` 命令集，仅保留作历史参考。
> 当前仓库后续只维护 `v2`，入口为 `python3 scripts/agentctl_v2.py`。

这个工具当前只有 2 个命令。

## 命令列表

- `create`
  添加一个新的 agent，并补齐当前实现里已经支持的相关配置，例如 workspace、model、Telegram bot 白名单和 DM binding。

- `delete`
  删除一个已有 agent，并清理这个 agent 对应的 Telegram DM binding；可选是否一并删除 workspace。

## 补充说明

- 脚本入口是 `python3 scripts/agentctl.py`
- 当前没有独立的“修改模型”命令
- 修改模型目前是 `create` 的一部分：当 agent 已存在且传入新的 `--model` 时，会更新该 agent 的模型配置

## 标准输出协议

所有 CLI 命令统一遵循以下规则：

- `stdout` 只输出标准 JSON
- `stderr` 只输出日志
- 成功退出码为 `0`
- 失败退出码为非 `0`，但仍需在 `stdout` 输出结构化错误

标准结构：

```json
{
  "result": {},
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

失败结构：

```json
{
  "result": null,
  "error": {
    "code": "COMMAND_EXECUTION_FAILED",
    "details": {},
    "steps": [],
    "rollback": []
  },
  "typeCode": 20,
  "message": "Command failed with exit code 1",
  "serverTimeStamp": "2026-04-04 09:37:57"
}
```

`typeCode` 规则：

- `1`：成功
- `2`：已受理，异步处理中
- `10`：资源不存在
- `11`：参数错误或校验失败
- `12`：状态冲突
- `20`：底层命令执行失败
- `21`：执行失败且包含回滚信息
- `50`：内部错误


## 建议后续补充的端口

- `delete_agent`
  删除一个 Agent，可后续再细分是否保留 workspace、是否强制删除。

- `list_agents`
  查看当前 OpenClaw 下已有的 Agent 列表。

- `get_agent_detail`
  查看某个 Agent 的完整配置和当前状态。

- `set_agent_skills`
  单独维护某个 Agent 的 skills 配置。

- `set_agent_soul`
  单独维护某个 Agent 的 soul、system prompt 或角色设定。

- `set_agent_workspace`
  修改某个 Agent 的 workspace 或工作目录配置。

- `bind_channel`
  给某个 Agent 绑定外部接入渠道，例如 Telegram。

- `unbind_channel`
  解除某个 Agent 的渠道绑定。

- `set_agent_access`
  维护某个 Agent 的访问范围，例如白名单、allowlist 或账号权限。
