# Agent Manage V2

旧版实现仍保留在 `openclaw_remote/` 和 `scripts/agentctl.py`，作为历史备份参考。

新版本的 `create_instance` 流程放在 `agent_manage_v2/`，入口是：

```bash
python3 scripts/agentctl_v2.py create-instance --template-name unipay-claw-base
```

当前行为：

1. `template_name` 就是 `agent_name`
2. workspace 固定到 `~/data/{templateName}`，也可通过参数覆盖
3. 从 `~/template/{templateName}.zip` 解压到 `~/template/{templateName}/`
4. 再把 `~/template/{templateName}/` 整体复制到 workspace

所以远端最简用法是：

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py create-instance \
  --template-name unipay-claw-base
```

这要求你至少提前上传：

```bash
~/template/unipay-claw-base.zip
```

新增 `add-tg-bot` 命令：

```bash
cd ~/data/agent_manage && python3 scripts/agentctl_v2.py add-tg-bot \
  --agent unipay-claw-base \
  --tg-token 123456:abc
```

可选参数：

- `--bot-name`：不传时自动生成一个 `tgbot-xxxxxxxx` 标识名
- `--config-path`：默认写 `~/.openclaw/openclaw.json`

当前写法是公开 bot 绑定，不再写旧版的 DM allowlist。
