# Agent Manage

## Prebuilt container image

Template images split immutable Agent registration from order-specific runtime configuration:

```bash
# Image build: no ticket, token, or model secret is accepted.
python3 scripts/agentctl.py add-agent \
  --template-name unipay-claw-base \
  --workspace-root /home/node/.openclaw/data

# Container runtime: the model key is supplied only after the writable volume is mounted.
printf '%s' "$MODEL_KEY" | python3 scripts/agentctl.py configure-instance \
  --model-key-stdin
```

`add-agent` expands `template.yaml` multi-agent declarations and continues to use the
official `openclaw agents add --non-interactive --json` command. `configure-instance`
reads configured Agents and workspace paths from `openclaw.json`. It only applies
runtime model, Gateway, tool, and workspace-policy settings; it never reads a
template archive, registers an Agent, or populates a workspace. Legacy
`--template-name` and `--agent-zip` arguments remain accepted but are ignored.
The compatibility `create-instance` command uses the same internal registration and
configuration stages and continues to register missing Agents. Prebuilt Agent images
retain their template archive under the container template root so this compatibility
path remains available after seed initialization.
All three commands accept `--base-url https://server-001.web.dolaio.cn/`. The value
must be an HTTP(S) site root without a path, query, fragment, or credentials; it is
normalized with a trailing slash and written into the managed runtime policy for
every affected Agent workspace.
For an already-running container, `add-agent` can optionally receive
`--template-zip-url` and `--template-zip-sha256`. The URL must use HTTPS; the
archive is streamed into the persistent template directory, validated, and
atomically replaces the local `{template_name}.zip` only for a successful add.

Build assets are under `container-image/`. The build command accepts the template
zip directly and always runs the DockerManager-compatible runtime validator:

```powershell
.\container-image\build-prebuilt-image.ps1 `
  -TemplateIdentify unipay-claw-base `
  -TemplateArchive C:\artifacts\unipay-claw-base.zip `
  -ImageTag unitag/openclaw-unipay-claw-base:poc
```

The build defaults to DockerManager's pinned OpenClaw `2026.7.1-1` digest and targets
`linux/amd64`. A different immutable base can be supplied with `-OpenClawImage` and
its corresponding label with `-OpenClawVersion`. At runtime, the entrypoint merges
the secret-free `/opt/unitag/openclaw-seed` into `/home/node/.openclaw`; runtime
configuration wins recursively, so DockerManager-owned settings are preserved.
See `docs/prebuilt-agent-image.md` for publishing, labels, safety invariants, and
troubleshooting.

## Container runtime (Execute v1)

When DockerManager executes AgentManager inside a managed Container, it sets
`UNITAG_AGENT_MANAGER_RUNTIME=container`. Existing commands and VPS behavior
are unchanged. In this mode AgentManager never invokes `systemctl --user`; a
successful operation that changes Gateway configuration returns the additive
top-level JSON field `restartRequired: true`. DockerManager owns the subsequent
Container restart and readiness check.

DockerManager should pass each sensitive value as the single stdin payload and
use the corresponding switch. Plaintext switches remain supported for existing
direct/VPS callers, but cannot be combined with their stdin variants.

```bash
printf '%s' "$MODEL_KEY" | python3 scripts/agentctl.py create-instance \
  --template-name unipay-claw-base --model-key-stdin

printf '%s' "$TG_TOKEN" | python3 scripts/agentctl.py add-tg-bot \
  --agent main --tg-token-stdin

printf '%s' "$WEIXIN_TOKEN" | python3 scripts/agentctl.py add-weixin-bot \
  --agent main --ilink-bot-id bot-001 --bot-token-stdin

printf '%s' "$APP_SECRET" | python3 scripts/agentctl.py add-feishu-bot \
  --agent main --app-id cli_xxx --app-secret-stdin
```

In Container runtime, `--openclaw-bin`, `--project-dir`, `--template-root`, and
`--config-path` are rejected. The image and Container environment supply those
paths, so a command cannot redirect AgentManager outside the managed instance.

入口：

```bash
python3 scripts/agentctl.py
```

当前版本号统一维护在 `agent_manage/__init__.py` 的 `__version__`，构建配置会从该字段读取：

```bash
python3 scripts/agentctl.py --version
# agent-manage 0.4.0
```

发布新版本时同步完成三件事：更新 `__version__`、在 `CHANGELOG.md` 增加对应版本和日期、创建同名 Git tag。

### 代码结构

- `orchestrator.py`：只负责编排创建实例、本机创建和批量追加 agent
- `settings.py`：模型环境、默认商店和 URL 构造的唯一来源
- `model_management.py`：模型目录读取、过滤、路由和 OpenClaw 模型配置
- `provisioning.py`：模板解压、workspace、依赖、公共 Skill 和运行规则
- `skill_management.py`：独立安装单个 agent 或当前环境的公共 Skill
- `refresh_management.py`：模型目录刷新、模板版本/基线、冲突检测和备份回滚
- `template_safety.py`：模板名称、路径边界和压缩包解压安全校验
- `channel_management.py`：Telegram、飞书和微信渠道管理
- `gateway_management.py`：Gateway 状态、鉴权、重启和 agent 发现
- `manager_core.py`：配置原子写入、备份、步骤计时和公共错误处理

公开入口仍是 `InstanceManagerV2`，CLI、已有方法名和返回结构保持兼容。

通用说明：

- `stdout` 只输出标准 JSON，供 `.NET`、HTTP API 或其他上层程序解析
- `stderr` 只输出执行日志
- 成功退出码为 `0`
- 失败退出码为非 `0`，但 `stdout` 仍会返回结构化错误 JSON
- 默认配置文件路径为 `~/.openclaw/openclaw.json`
- `create-instance` 默认模板目录为 `~/template`；`--local` 模式默认使用
  `~/.openclaw/templates`

## add-skill

独立安装一个 Skill，无需重新执行创建实例流程。必须在 `--agent <agent_id>` 和
`--common` 中选择一个范围，在 `--skill-dir` 和 `--skill-zip` 中选择一个来源。

```bash
# 给已有 agent 添加私有 Skill（按配置中该 agent 的实际 workspace 定位）
python3 scripts/agentctl.py add-skill --agent demo --skill-dir /path/to/weather

# 给当前 OpenClaw 环境添加所有 agent 共用的 Skill
python3 scripts/agentctl.py add-skill --common --skill-dir /path/to/weather

# 从 ZIP 安装；同名 Skill 已存在时，明确指定整体替换
python3 scripts/agentctl.py add-skill --agent demo --skill-zip /path/to/weather.zip --replace

# 用自定义配置文件定位另一套 OpenClaw 环境的公共目录
python3 scripts/agentctl.py --config-path /path/to/environment/openclaw.json add-skill \
  --common --skill-dir /path/to/weather

# 预览目标和检查来源，不写入安装目录
python3 scripts/agentctl.py --dry-run add-skill --common --skill-dir /path/to/weather
```

- 私有 Skill 安装到 `<workspace>/skills/<skill_name>/`，workspace 非空也可以添加。
  agent 和 workspace 必须已存在；从配置读取实际路径，不猜测 `~/data/<agent_id>`。
  `main` 可使用 `agents.defaults.workspace`；其他 agent 需在 `agents.list` 中配置 workspace。
- 公共 Skill 安装到 `<config_path 所在目录>/skills/<skill_name>/`，默认是
  `~/.openclaw/skills/<skill_name>/`。范围是当前 OpenClaw 环境，不涉及其他环境或公网发布。
- 来源目录必须包含 `SKILL.md`，其脚本、资源、引用文件一起复制；来源保持不变。
  ZIP 支持根目录直接放 `SKILL.md`，或仅包含一个带 `SKILL.md` 的 Skill 目录。
  默认名称取来源目录名；ZIP 根目录模式取 ZIP 文件名（去掉扩展名）。
  可用 `--skill-name <名称>` 指定目标目录名，该参数不会改写 `SKILL.md` 的内容。
- 同名目录默认返回冲突；`--replace` 会整体替换，删除旧版本遗留文件。
  先完整复制到临时目录，再切换目标；复制或切换失败时保留原有 Skill。
- 拒绝路径穿越、Skill 内的符号链接和链接目标目录；不执行 Skill 脚本或自动安装依赖。
- 不修改 OpenClaw 配置、不自动重启 Gateway。成功表示文件已安装，不保证当前会话已加载。
  返回 `scope`、`agent_name`、`skill_name`、`source`、`destination`、`config_path`、
  `replaced`、`skipped`、`gateway_restarted: false`、`activation_verified: false`。
- `--dry-run` 会校验实际来源和目标；ZIP 会解压到临时目录校验，结束后清理。

## refresh-agent

刷新已存在的 agent，默认同时刷新模型目录和模板文件。模型目录和公共 Skill
作用于当前整个 OpenClaw 环境；私有模板文件作用于指定 agent 的实际 workspace。
兼容目标沿用服务器的 OpenClaw `2026.7.1-2`，保留该版 `imageGenerationModel` 配置，
不为本机新版迁移图片模型字段。集成校验可通过 `AGENT_MANAGE_TEST_OPENCLAW_BIN` 指向这一版本。

```bash
# 先预览差异、冲突和目标版本，不写入实例
python3 scripts/agentctl.py --dry-run refresh-agent --agent demo \
  --template-dir /path/to/new-template

# 刷新模型和模板；重启 Gateway 并检查 RPC
python3 scripts/agentctl.py refresh-agent --agent demo \
  --agent-zip /path/to/demo.zip --restart

# 只刷新环境的模型目录，默认不重启
python3 scripts/agentctl.py refresh-agent --agent demo --models-only

# 只升级模板，明确覆盖已经检查过的冲突文件；记忆仍保留
python3 scripts/agentctl.py refresh-agent --agent demo --template-only \
  --template-dir /path/to/new-template --replace-modified
```

- 模板必须有 `template.yaml.version`。来源支持本地目录或 ZIP，远端发布物由上层下载后传入。
  未指定来源时，使用 `--template-root` 下的 `<template_name>.zip`，不存在则用同名目录。
  模板名称取 `--template-name`、已记录的模板名称、agent id，依次回退。
  本地创建模式的模板请显式传入 `--template-root ~/.openclaw/templates` 或来源路径。
- `--models-only` 和 `--template-only` 互斥；模型刷新沿用已配置的 Dola 环境、商店和密钥/SecretRef，
  保留自定义 provider、模型别名、默认模型/回退、agent 覆盖、工具权限、渠道、绑定和 Gateway Token。
  新目录删除了仍在使用的模型，或与自定义 provider 重名时，中止刷新。
- 私有 `skills/` 和公共 Skill 按模板更新：同路径文件内容或权限不同就备份后覆盖，
  不要求旧基线，也不需要 `--replace-modified`；实例缺失的模板 Skill 文件会补齐。
  新版移除的已受管 Skill 文件会删除，用户额外添加且未被模板接管的文件保留。
  Skill 内的记忆、凭据、`.env` 等仍遵守下述保护规则。
- 其他模板文件按「旧基线 / 当前实例 / 新模板」比较。更新未修改的模板文件，删除新版中移除的受管文件，
  保留用户额外添加的文件。模板未变化的用户自定义保留；双方都变动时返回冲突，默认整次不执行。
  `--replace-modified` 允许备份后替换冲突文件。
- 首次刷新没有可信的旧版本，`version_before` 为 `null`；不会把缓存模板的版本当作已安装版本。
  已有的非 Skill 文件与新模板不同会冲突。成功刷新后，记录模板名称、实际应用的版本、文件哈希/权限和独立基线。
  现有 `create-instance` 流程尚不建立此刷新基线，首次刷新按上述规则接管。
  对 `x.y.z` 数字版本拒绝降级，防止旧缓存把已升级的实例刷回旧版本。
- `MEMORY.md`、`memory/`、`USER.md`、`TOOLS.md`、会话、凭据和 `.env` 始终排除，
  即使使用覆盖参数或实例中缺失，也不从模板补回。保留 `AGENTS.md` 中现有平台运行规则块。
  模板版本知识和操作流程应放在 Skill / references 中。
- 简单备份保存在 `<config 目录>/agent-manage/backups/<job_id>/`：完整旧配置、受影响文件、旧版本状态、
  记录原路径/存在性/哈希/权限的 `manifest.json`。新模板基线放在同一 job 的 `baseline/`，
  当前版本索引为 `agent-manage/agents/<agent_id>.json`。目录权限 `0700`，备份文件 `0600`。
  成功备份暂时保留，不自动清理；这是刷新操作备份，系统备份另行规划。
- 写入前使用本机 `openclaw config validate` 校验候选配置。写入或重启检查失败时自动恢复旧配置、
  旧文件/版本记录并移除本次新增文件；结果报告 `rollback_ok`、备份位置及运行态恢复情况。
  环境锁防止多个刷新任务并发；发现配置或受影响文件在准备期间被改变会中止。
- 默认不重启、不调用 Gateway RPC 确认热加载生效，刷新写入完成后即返回。
  返回 `restart_required: false`、`gateway_restarted: false`、`activation_verified: false`；
  `restart_required: false` 表示默认不要求重启，不代表已经验证运行态生效。
  `--dry-run` 同样不检查运行态，不因预览存在变化而要求重启。
  `--restart` 重启当前环境 Gateway 后要求 RPC 检查成功；成功只确认 Gateway 可达，
  不代表新 Skill 已在已有会话中执行验证。重启影响该环境所有 agent。
  重启前会检查已安装 Gateway 服务的配置路径，无法确认与所选环境一致时中止，防止重启另一套环境。
- 此版只接管单 agent 的文件升级，不升级 OpenClaw 程序、不自动安装程序依赖或执行模板脚本。
  `requiredLibraries` 中必需依赖须能通过 `bin` 检查已存在；团队模板需拆分单 agent 包分别刷新。

## codex-login / codex-logout / codex-status

这三个命令管理当前整个 OpenClaw 环境的 Codex 登录、模型切换和登录状态。
Codex 全局认证允许服务器 OpenClaw `2026.7.*` 正式版及数字打包修订（例如 `2026.7.1-2`），
不接受 beta/rc，也不放开 8、9 月及其他年份版本。运行时检查所需原生 SDK 模块和认证函数；
版本范围允许进入流程，不代表未来维护版本均已验证。原生集成已校验 `2026.7.1`、`-1`、`-2` 和 `2026.7.35`。
不需要 `--agent`、`--model`、`--login-id`、API key 或 OAuth 回调地址。

```bash
# 发起全局设备码登录；已有主认证可直接接管为全局登录
agent-manage codex-login

# 查询设备码、授权进度和当前全局认证状态
agent-manage codex-status

# 使用原来的切模型命令选择其他聊天模型
agent-manage set-model --model openai/gpt-6-luna

# 取消等待中的登录，清除整个环境的 Codex OAuth，恢复原模型配置
agent-manage codex-logout
```

前端配合流程：

Server API 需要允许并转发上述三个命令；本仓库不包含网页或 Server API 的路由代码。

1. 调用 Server API 执行 `codex-login`。等待授权时协议 `typeCode: 2`，
   `result.status` 为 `starting` 或 `pending`；命令只等待设备码准备最多约 2 秒，
   不阻塞等待用户完成授权。首次本地 SDK 初始化另需少量时间。
2. 从结果读取 `verification_url`、`user_code`、`expires_at`（Unix 毫秒），
   显示设备码并打开 OpenAI 授权页。若状态仍是 `starting`，先查询 `codex-status` 取码。
   用户必须在 OpenAI 页面完成批准，账号/工作区需允许设备码授权。
   此流程复用 OpenClaw 原生登录，不需要 Dola 申请 OAuth client 或接收浏览器跳转回调。
3. 每隔 2–3 秒查询 `codex-status`。设备码在 `result.login` 中；
   `result.logged_in: true`、`result.status: logged_in` 表示已完成当前环境的共享认证。
   后台使用 OpenClaw 原生 SDK 写入认证存储并自动切模型，前端不用传回 code 或 token。
   `failed` / `expired` 可以再次执行登录；要取消或换账号，先 `codex-logout` 再登录。

状态查询返回 `logged_in`、`auth_status`（`valid` / `refreshable` / `expired` / `missing`）、
`account`（可解析的邮箱、账号 ID、套餐与 Token 到期时间）、`agents[].shared_auth`、
`models_switched`、`login`。查询只检查本地原生存储，不请求模型或额度接口，
不等待授权、不主动刷新 Token。`refreshable` 表示 access 已到期但保留 refresh token，
是否仍能刷新由 OpenClaw 实际请求时确定；本地状态不保证服务端未撤销授权或具体模型可用。
所有命令的公开结果都不返回 access/refresh token。

- 登录成功后，OAuth 凭据仅存入 OpenClaw 的主认证存储，全部现有和后续 agent 继承它。
  清除现有 agent 独立的 OpenAI/Codex OAuth，统一认证顺序；保留其他 provider 和静态 API key。
  使用旧版 SDK 的原生存储事务，不直接复制 Token 到每个 agent、不直接覆盖 `auth-profiles.json`。
  重复登录复用等待中的任务或已登录的账号，不覆盖原模型备份。
- 授权完成后安装 `gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`，
  默认 `gpt-6.1-sol`。模型 ID 依据 [OpenAI 官方 Codex 模型文档](https://learn.chatgpt.com/docs/models)，
  核对日期为 2026-10-10；配置列表不代表账号拥有全部模型权限。
  当前全部 agent 的聊天模型、已有 utility / heartbeat / subagent 选择一起切换。
  `set-model` 保持原有行为，只切全局默认模型，agent 独立指定的模型继续保留。
- 普通 Codex provider 使用 `openai`、`baseUrl: https://chatgpt.com/backend-api/codex`、
  `api: openai-chatgpt-responses`、`auth: oauth`、`agentRuntime: {id: openclaw}`。
  原 OpenAI provider 同时承载图片/音频 API 时，保留 API key 与原默认传输，
  provider 默认传输也切为 Codex，聊天和绘图优先用全局 OAuth。
  绘图选择 `openai/gpt-image-2`，保留图片超时等参数，并清空原绘图 fallback；
  通过旧版原生 `image_generate` 的 Codex Responses 路径执行，实际权限与额度由账号决定。
  保留原 API key，原非 Codex 模型条目单独保存原传输和地址。
  已校验的服务器 `2026.7.1` / `-1` / `-2` / `2026.7.35` 的音频认证仍要求 API key，音频与 TTS 不切 OAuth：
  已配置的 OpenAI 音频条目保留原地址；未显式指定音频模型且原 OpenAI provider 有 key 时，
  固定 `gpt-4o-transcribe` 到原 API 地址，避免音频请求误发到 Codex 聊天接口。
  其他显式音频 provider、CLI、已有禁用状态保持原配置。
  登出恢复原 provider、绘图和音频配置；已登录旧配置再次执行 `codex-login` 即应用绘图策略，
  不重新授权、不替换最初的模型备份。`result.media` 返回绘图切换与音频保留原因。
- `models` 返回 Codex 聊天模型；`update-model` 和 `refresh-agent` 的模型部分
  返回 `skipped: true, reason: codex_login_active`，模板刷新继续。
  `configure-instance` 等重新配置 provider 的操作需要先登出。
- `codex-logout` 清除当前环境的全部 OpenAI/Codex OAuth（包括 agent 独立账号），
  保留媒体 API key，恢复原模型及认证顺序。取消任务后，后台迟到的授权结果不能重新写入。
  这是当前环境的本地登出，不撤销 OpenAI 网站或其他设备上的登录会话；
  已开始的模型请求可能仍会完成，已有会话不清空。
- 恢复记录、后台任务保存在 `<config 目录>/agent-manage/codex-*.json`，
  文件权限 `0600`、目录 `0700`。凭据事务的临时备份仅供失败回滚，成功后删除。
  如进程中断留下事务记录，执行 `codex-logout` 清理后再登录。
- 默认不重启、不探测 Gateway，`restart_required: false`、`activation_verified: false`。
  `--dry-run` 预览登录/登出，不启动授权，不写配置或认证。

## create-instance

### 行为说明

- `template_name` 直接作为默认入口 `agent_name`
- workspace 默认创建在 `~/data/{templateName}`，也可通过参数覆盖
- 从 `~/template/{templateName}.zip` 解压到 `~/template/{templateName}/`
- 如果模板含 `template.yaml.requiredLibraries`，会先检查每个依赖是否已安装；未安装时执行对应 `installCommand`，安装后再验证
- 如果模板含 `common-skills/` 或 `template.yaml.commonSkillFolders`，会把其中的 skill 目录复制到 `~/.openclaw/skills/`，作为所有 agent 共用的 skill
- 再把 `~/template/{templateName}/` 整体复制到 workspace
- 如果模板声明 `copyMode: multi_agent_template` 或 `template.yaml.agents`，会把默认入口之外的 `agents/` 子目录作为团队成员继续执行
  `openclaw agents add` 并分别复制到各自 workspace；普通单 agent 模板不受影响
- 直接从 `openclaw.json` 的 `agents.list` 检查同名 agent 是否已存在
- 如果同名 agent 已存在，会跳过 `openclaw agents add`，继续后续步骤
- 如果 workspace 已存在且非空，会跳过 `workspace.populate`，继续后续步骤
- 同名 agent 重新执行 `create-instance` 时返回 `mode: reconciled`：更新模型目录、商店路由、公共 Skill 和受管运行规则；保留已有 Gateway Token、仍受支持的默认模型和非空 workspace
- reconcile 当前不会自动合并新版模板文件到用户已使用的非空 workspace；请使用 `refresh-agent` 升级模板
- `--model-key` 为必填，会写入 `~/.openclaw/openclaw.json` 里每个模型 provider 的 `apiKey`
- 模型环境根地址集中维护，`--model-env` 默认是 `global`：

  | 环境 | 网关根地址 |
  | --- | --- |
  | `global` | `https://api.dola.io` |
  | `test` | `https://unitag.dola.fi` |
  | `cn` | `https://api.dolaio.cn` |

- `--ai-shop` 默认是 `shop`，因此默认模型目录为
  `<网关根地址>/aigateway/api/frontend/aimodels/byProvider/shop`，默认模型调用地址为
  `<网关根地址>/aigateway/shop/v1`
- 商店名只允许一个安全 URL 路径段；`.`、`..`、嵌套路径和控制字符会被拒绝
- 创建时会生成新的 `gateway_token`，写入 `gateway.auth.token`，并在返回结果里带回
- 默认 `global` 环境创建时会先从 `https://api.dola.io/aigateway/api/frontend/aimodels/byProvider/shop`
  拉取当前激活模型目录，再写入 `~/.openclaw/openclaw.json`
- `--ai-shop` 默认为 `shop`；传其他 `{shoppath}` 时，模型目录会改为从
  当前环境的 `.../aigateway/api/frontend/aimodels/byProvider/{shoppath}`
  拉取
- 传 `--ai-shop {shoppath}` 时，写入 OpenClaw 的每个模型 provider `baseUrl` 都会统一写成
  当前环境的 `.../aigateway/{shoppath}/v1` 格式；具体来源 provider 保留在
  `dolaio/gpt-5.5` 这样的模型引用里，不再写进 URL 路径
- 如果模型目录已经返回 `content.models.providers`，会按 OpenClaw 配置 schema 过滤后写入这些 providers，并用 `--model-key` 覆盖每个 provider 的 `apiKey`
- 模型目录返回的 `definition.id` 会原样保留：例如 `dolaio/gpt-5.6-sol` 不会被裁成 `gpt-5.6-sol`；官方模型返回无前缀 ID 时也不会自动补前缀
- 普通模型目录只保留 `modelCategory = chat`；`image` 会进入独立的图片模型池，`video`、`chat-audio` 和其他非聊天分类不会进入普通模型列表
- 图片生成模型固定为 `openai/gpt-image-2`，按 npm stable OpenClaw `2026.7.1-2` 的 schema 写入 `agents.defaults.imageGenerationModel.primary`；不会从目录选择或回退到其他图片模型
- 如果当前商店目录的官方 provider `openai` 下包含 `gpt-image-2`，图片 provider 使用当前商店的 `baseUrl`；不要求目录额外提供 `modelCategory`
- 如果当前商店没有官方 `openai/gpt-image-2`，图片使用的 `openai` provider 特例回退到同环境的 `/aigateway/v1`，不添加 `test` 路径；其他 provider 仍使用当前商店 `baseUrl`
- provider ID 为 `openai` 时固定使用 OpenClaw 的 `openai-responses` API 适配器；其他 OpenAI-compatible provider 保留模型目录声明的 API 类型，默认回退仍为 `openai-completions`
- 初始化会向各 agent workspace 的 `AGENTS.md` 写入精简的受管运行规则，包括命令安全、IPv4 公网附件、`nginx-delivery` 交付要求和图片默认质量；`image_generate` 默认使用 `quality: "low"`，可通过 `--image-quality low|medium|high|auto` 调整
- `create-instance` 可选传入 `--base-url https://server-001.web.dolaio.cn/`；传入后，受管运行规则会优先声明当前主机的公网文件根地址，并要求 `MEDIA:` 将公共目录下的文件映射为该域名下的公网 URL；容器模式使用 `/home/node/.openclaw/workspace/public/`，VPS 模式使用 `/var/www/html/`
- 内置公共 Skill 会同步到 `~/.openclaw/skills/`；当前包含 `nginx-delivery`，用于将明确公开的交付文件、网页和静态资源部署到 nginx、更新索引并返回经过 IPv4 验证的公网 URL
- 模型 `input` 只会写入 npm stable OpenClaw `2026.7.1-2` 支持的 `text`、`image`；`video`、`audio` 和未知值会被过滤，如果过滤后为空或原值格式错误，则回退为 `["text"]`
- `agents.defaults.models` 会按当前拉取到的模型重建
- 默认主模型优先使用拉取结果里的推荐模型；如果当前目录里没有推荐模型，则退回到拉取结果里的第一个可用模型
- 如果模板原先带有 `vllm` 等旧 provider，会在初始化时被拉取到的模型 providers 覆盖
- 创建完成后会额外写入 `~/.openclaw/openclaw.json` 的工具默认配置：
  `tools.profile = coding`、`tools.exec.security = full`、
  `tools.web.search.enabled = false`、`tools.web.fetch.enabled = true`、
  `tools.agentToAgent.enabled = true`、`tools.agentToAgent.allow` 包含 `main`、默认入口 agent 和多 agent 团队成员、
  `tools.sessions.visibility = all`
- 如执行失败，默认按当前实现做回滚

前置要求：

```bash
~/template/unipay-claw-base.zip
```

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py create-instance \
  --template-name unipay-claw-base \
  --model-key YOUR_MODEL_KEY \
  --model-env cn \
  --image-quality low
```

### 本机安装

`--local` 用于把一个 zip agent 安装到当前用户的本机 OpenClaw：

- `--agent-zip` 可直接指定 zip 文件路径；未传 `--template-name` 时会用 zip 文件名作为 agent name
- workspace 默认创建在 `~/.openclaw/data/{agentName}`
- 模板解压到 `~/.openclaw/templates/{agentName}`
- 通过 `openclaw agents add` 注册 agent；OpenClaw 自己会维护 `~/.openclaw/agents`
- 会写入模型 providers，并用 `--model-key` 覆盖每个 provider 的 `apiKey`
- 如果传 `--model`，会优先把它写成 `agents.defaults.model.primary`
- 不会生成新的 gateway token，也不会覆盖 `gateway.auth.token`

示例：

```bash
python3 scripts/agentctl.py create-instance \
  --local \
  --agent-zip /path/to/legal-team.zip \
  --model-key YOUR_MODEL_KEY \
  --model-env test \
  --model unipay-fun/gpt-5.4
```

可选参数：

- `--local`
- `--agent-zip`
- `--model-env global|test|cn`（默认 `global`）
- `--ai-shop`（默认 `shop`）
- `--model`
- `--image-quality low|medium|high|auto`（默认 `low`）
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
- `gateway_token`
- `workspace`
- `archive_path`
- `template_dir`
- `steps`

其中 `libraries.ensure` 和 `common_skills.install` 只在模板声明依赖或存在 common skills 时出现。

示例：

```json
{
  "result": {
    "ok": true,
    "template_name": "unipay-claw-base",
    "agent_name": "unipay-claw-base",
    "gateway_token": "generated-gateway-token",
    "workspace": "/root/data/unipay-claw-base",
    "archive_path": "/root/template/unipay-claw-base.zip",
    "template_dir": "/root/template/unipay-claw-base",
    "steps": [
      {"step": "template.prepare", "result": {}},
      {"step": "libraries.ensure", "result": {}},
      {"step": "common_skills.install", "result": {}},
      {"step": "agents.add", "result": {}},
      {"step": "workspace.populate", "result": {}},
      {"step": "models.fetch_catalog", "result": {}},
      {"step": "config.configure_models", "result": {}},
      {"step": "config.configure_gateway_auth", "result": {}},
      {"step": "config.configure_tools", "result": {}}
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## add-agents

### 行为说明

- 用于给已经启动的服务批量追加 agent
- `--agents` 必须传 JSON 数组
- 数组项支持两种写法：
  - 字符串：直接视为 `agent_name`
  - 对象：支持 `agent_name`，可选 `template_name`、`workspace`、`model`
- 每个条目会按 `create-instance` 的模板创建流程执行：
  `template.prepare -> libraries.ensure(可选) -> common_skills.install(可选) -> agents.add -> workspace.populate`
- 默认 `template_name = agent_name`
- 会从 `~/template/{templateName}.zip` 解压到 `~/template/{templateName}/`
- 如果模板声明了 `requiredLibraries`，会先验证/安装依赖
- 如果模板提供了 `common-skills/` 或 `commonSkillFolders`，会同步到 `~/.openclaw/skills/`
- 再把 `~/template/{templateName}/` 整体复制到 workspace
- 如果某个条目对应多 agent 模板，也会自动追加默认入口之外的 `agents/` 团队成员
- 未显式传 `workspace` 时，默认使用 `--workspace-root/{agent_name}`，默认根目录仍为 `~/data`
- 单个 agent 的创建顺序与 `create-instance` 一致，`openclaw agents add` 发生在模板解压、依赖检查和 common skills 同步之后、workspace 填充之前
- 如果同名 agent 已存在，会跳过该项并继续处理剩余项
- 如果 workspace 已存在且非空，会跳过该项的 `workspace.populate`
- 批量追加完成后会写入 `tools.agentToAgent.enabled = true`，并把 `main`、本批次 agent 和多 agent 团队成员合并进
  `tools.agentToAgent.allow`；同时设置 `tools.sessions.visibility = all`
- 批量追加完成后会为本批次全部 workspace 同步与 `create-instance` 相同的受管运行规则和内置公共 Skill；默认图片质量为 `low`
- `add-agents` 同样支持可选 `--base-url https://server-001.web.dolaio.cn/`；传入后会为本批次全部 workspace 写入与 `create-instance` 相同的公网文件根地址和 `MEDIA:` 映射规则，未传时保持原有规则不变
- 每个 `--agents` JSON 条目可选传入 `template_zip_url` 和 `template_zip_sha256`；下载只接受 HTTPS，落盘前会校验大小、SHA-256（传入时）以及模板归档安全规则
- 批量追加完成后不额外执行 `openclaw gateway restart`
- 实际新增至少一个 Agent 时返回 `restart_required = true`，全部为已存在 Agent 而跳过时返回 `false`；执行方据此决定是否重启 Gateway

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py add-agents \
  --agents '[
    "unipay-claw-base",
    {"agent_name":"unipay-claw-demo","template_name":"demo-template","model":"openai/gpt-5"},
    {"agent_name":"unipay-claw-custom","workspace":"~/agents/custom"}
  ]'
```

可选参数：

- `--workspace-root`
- `--base-url`
- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `requested_count`
- `added_count`
- `skipped_count`
- `restart_required`
- `post_batch_actions`
- `agents`
- `steps`

其中 `libraries_ensure`、`common_skills_install` 和对应 `steps` 只在模板声明依赖或存在 common skills 时有内容。

示例：

```json
{
  "result": {
    "ok": true,
    "requested_count": 2,
    "added_count": 1,
    "skipped_count": 1,
    "restart_required": true,
    "post_batch_actions": [],
    "agents": [
      {
        "agent_name": "unipay-claw-base",
        "template_name": "unipay-claw-base",
        "workspace": "/root/data/unipay-claw-base",
        "archive_path": "/root/template/unipay-claw-base.zip",
        "template_dir": "/root/template/unipay-claw-base",
        "model": null,
        "status": "skipped",
        "result": {
          "template_prepare": {
            "archive_path": "/root/template/unipay-claw-base.zip"
          },
          "libraries_ensure": {},
          "common_skills_install": {},
          "agents_add": {
            "skipped": true,
            "reason": "agent_exists",
            "agent_name": "unipay-claw-base"
          },
          "workspace_populate": {
            "skipped": true,
            "reason": "workspace_not_empty",
            "workspace": "/root/data/unipay-claw-base"
          }
        }
      },
      {
        "agent_name": "unipay-claw-demo",
        "template_name": "demo-template",
        "workspace": "/root/data/unipay-claw-demo",
        "archive_path": "/root/template/demo-template.zip",
        "template_dir": "/root/template/demo-template",
        "model": "openai/gpt-5",
        "status": "added",
        "result": {
          "template_prepare": {
            "archive_path": "/root/template/demo-template.zip"
          },
          "libraries_ensure": {},
          "common_skills_install": {},
          "agents_add": {
            "command": "openclaw agents add unipay-claw-demo --workspace /root/data/unipay-claw-demo --non-interactive --json --model openai/gpt-5",
            "returncode": 0
          },
          "workspace_populate": {
            "workspace": "/root/data/unipay-claw-demo"
          }
        }
      }
    ],
    "steps": [
      {"step": "template.prepare[unipay-claw-base]", "result": {}},
      {"step": "libraries.ensure[unipay-claw-base]", "result": {}},
      {"step": "common_skills.install[unipay-claw-base]", "result": {}},
      {"step": "agents.add[unipay-claw-base]", "result": {}},
      {"step": "workspace.populate[unipay-claw-base]", "result": {}},
      {"step": "template.prepare[unipay-claw-demo]", "result": {}},
      {"step": "libraries.ensure[unipay-claw-demo]", "result": {}},
      {"step": "common_skills.install[unipay-claw-demo]", "result": {}},
      {"step": "agents.add[unipay-claw-demo]", "result": {}},
      {"step": "workspace.populate[unipay-claw-demo]", "result": {}}
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-20 11:20:00"
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
- 写入配置后会通过 `systemctl --user stop/start openclaw-gateway.service` 重启 gateway，并轮询进程退出和端口监听

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
        "method": "systemctl_user_stop_start",
        "service": "openclaw-gateway.service",
        "port": "18889"
      }
    }
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-04-04 09:36:50"
}
```

## add-feishu-bot

### 行为说明

- 直接从 `openclaw.json` 的 `agents.list` 检查目标 agent 是否存在
- 新增或覆盖一个飞书/Lark bot 账号配置
- `--domain feishu` 表示国内飞书，`--domain lark` 表示国际 Lark
- 当前 bot 按公开模式写入 `channels.feishu.dmPolicy = open`，`channels.feishu.allowFrom = ["*"]`
- 账号配置只写入 OpenClaw Feishu schema 支持的字段：`appId`、`appSecret`、`domain`、`name`、`enabled`
- `appSecret` 写入实例服务器的 `~/.openclaw/openclaw.json`，状态接口只返回 `has_app_secret`
- 会删除该账号名下旧的 Feishu binding，再写入一条新的 binding 指向指定 agent
- 写入配置后会通过 `systemctl --user stop/start openclaw-gateway.service` 重启 gateway，并轮询进程退出和端口监听
- 可选 `--bind-lark-cli`，用于执行 `lark-cli config bind --source openclaw --app-id <appId> --identity bot-only`

### 远程执行

推荐用 stdin 传 `appSecret`，避免进入 shell history 或进程参数：

```bash
cd ~/data/agent_manage && printf '%s' "$APP_SECRET" | python3 scripts/agentctl.py add-feishu-bot \
  --agent unipay-claw-base \
  --domain feishu \
  --account-id main \
  --app-id cli_xxx \
  --app-secret-stdin \
  --bot-name "客服飞书" \
  --bind-lark-cli
```

可选参数：

- `--domain`：`feishu` 或 `lark`，默认 `feishu`
- `--account-id`：OpenClaw 内部账号名，默认 `main`
- `--bot-name`
- `--dm-policy`：默认 `open`
- `--allow-from`：可重复传；不传默认 `*`
- `--bind-lark-cli`
- `--lark-cli-identity`：`bot-only` 或 `user-default`，默认 `bot-only`
- `--config-path`
- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `agent_name`
- `account_id`
- `domain`
- `app_id`
- `bot_name`
- `dm_policy`
- `allow_from`
- `config_write`
- `lark_cli_bind`
- `gateway_restart`

示例：

```json
{
  "result": {
    "ok": true,
    "agent_name": "unipay-claw-base",
    "account_id": "main",
    "domain": "feishu",
    "app_id": "cli_xxx",
    "bot_name": "客服飞书",
    "dm_policy": "open",
    "allow_from": ["*"],
    "config_write": {
      "config_path": "/root/.openclaw/openclaw.json",
      "changed_paths": [
        "channels.feishu.enabled",
        "channels.feishu.domain",
        "channels.feishu.dmPolicy",
        "channels.feishu.allowFrom",
        "channels.feishu.accounts.main",
        "bindings"
      ]
    },
    "lark_cli_bind": {
      "step": "lark-cli.config.bind"
    },
    "gateway_restart": {
      "step": "gateway.restart",
      "result": {
        "method": "systemctl_user_stop_start",
        "service": "openclaw-gateway.service",
        "port": "18889"
      }
    }
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-05-24 02:00:00"
}
```

## feishu-bot-status

### 行为说明

- 读取当前 `~/.openclaw/openclaw.json`
- 返回当前已登记的飞书/Lark bot 总数 `feishu_bot_count`
- 返回当前已绑定的飞书/Lark bot 数 `bound_feishu_bot_count`
- 返回所有飞书/Lark bindings 总数 `total_binding_count`
- 不返回 `appSecret` 明文，只返回 `has_app_secret`
- 同时尽力读取 `~/.lark-cli/config.json`，补充 `lark_cli_bound`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py feishu-bot-status
```

### Output

成功时 `result` 里主要返回：

- `feishu_enabled`
- `feishu_bot_count`
- `bound_feishu_bot_count`
- `total_binding_count`
- `bots`

示例：

```json
{
  "result": {
    "ok": true,
    "feishu_enabled": true,
    "feishu_bot_count": 1,
    "bound_feishu_bot_count": 1,
    "total_binding_count": 1,
    "bots": [
      {
        "account_id": "main",
        "domain": "feishu",
        "app_id": "cli_xxx",
        "app_id_masked": "cli_xxx****abcd",
        "bot_name": "客服飞书",
        "enabled": true,
        "binding_count": 1,
        "is_bound": true,
        "dm_policy": "open",
        "allow_from": ["*"],
        "has_app_secret": true,
        "lark_cli_bound": true
      }
    ]
  },
  "error": null,
  "typeCode": 1,
  "message": "OK",
  "serverTimeStamp": "2026-05-24 02:00:00"
}
```

## delete-feishu-bot

### 行为说明

- 按 `account_id` 删除 `channels.feishu.accounts.{accountId}`
- 同时删除所有引用该账号的 Feishu bindings
- 如果删完后没有剩余 bot，会把 `channels.feishu.enabled` 设为 `false`
- 写入配置后会重启 gateway

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py delete-feishu-bot \
  --account-id main
```

## check-server-status

### 行为说明

- 执行 `openclaw gateway status --require-rpc --json`
- 默认 10 秒超时，避免等待太久同时减少误判
- 只有当 gateway 服务和 RPC probe 都正常时，才认为服务器和 `openclaw` 可工作
- 同时读取一次当前 TG bot 状态、当前飞书/Lark bot 状态、当前微信 bot 状态、当前配置模型，一并放进返回体

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
- `feishu_bot_status`
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
      "current_model": "unipay-fun/deepseek-v4-flash",
      "configured_default_model": "unipay-fun/deepseek-v4-flash",
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
- 写入配置后会通过 `systemctl --user stop/start openclaw-gateway.service` 重启 gateway，并轮询进程退出和端口监听
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
            "method": "systemctl_user_stop_start",
            "service": "openclaw-gateway.service",
            "port": "18889"
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

- 只允许切换到当前 `~/.openclaw/openclaw.json` 已保存的受支持模型
- 传参必须写完整模型引用，不接受简写
- 直接执行 `openclaw models set <model_ref>`
- 用于切换当前默认模型
- agent 自己指定的模型不随全局默认模型切换
- 切换成功后会通过 `systemctl --user stop/start openclaw-gateway.service` 重启 gateway，并轮询进程退出和端口监听

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py set-model \
  --model unipay-fun/gpt-5.4
```

可选模型：

- 以 `models` 返回结果为准

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `model_ref`
- `steps`
- `gateway_restart`

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
    ],
    "gateway_restart": {
      "step": "gateway.restart",
      "result": {
        "method": "systemctl_user_stop_start",
        "service": "openclaw-gateway.service",
        "port": "18889"
      }
    }
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
    "current_model": "unipay-fun/deepseek-v4-flash",
    "configured_default_model": "unipay-fun/deepseek-v4-flash",
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

## models

### 行为说明

- 直接读取 `~/.openclaw/openclaw.json`
- 返回当前本机已经保存的受支持模型列表，不请求远端接口
- 适合给上层在 `set-model` 前先拉一遍可选项

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py models
```

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `provider`
- `current_model`
- `supported_model_refs`
- `models`
- `config_path`
- `config_exists`

## update-model

### 行为说明

- 重新请求当前模型 provider `baseUrl` 对应环境的模型目录
- 如果 `baseUrl` 包含 `/aigateway/{shop}/v1`，会保留该商店路径；存在普通模型 provider 时，图片模型回退使用的无商店 `openai` 地址不会覆盖商店判断
- 将最新激活模型按当前 `openclaw` 配置格式写回 `~/.openclaw/openclaw.json`
- 优先保留当前默认模型；如果当前默认模型已经不在最新目录里，则回退到推荐默认模型
- 需要当前配置中至少一个模型 provider 已经存在 `apiKey`

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py update-model
```

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `provider`
- `current_model_before`
- `current_model_after`
- `supported_model_refs`
- `steps`
- `config_path`

## current-gateway-token

### 行为说明

- 直接读取 `~/.openclaw/openclaw.json`
- 返回当前配置里的 `gateway.auth.mode` 和 `gateway.auth.token`，不起 `openclaw` 子进程

### 远程执行

```bash
cd ~/data/agent_manage && python3 scripts/agentctl.py current-gateway-token
```

可选参数：

- `--openclaw-bin`
- `--project-dir`
- `--dry-run`

### Output

成功时 `result` 里主要返回：

- `gateway_auth_mode`
- `gateway_token`
- `config_path`
- `config_exists`

示例：

```json
{
  "result": {
    "ok": true,
    "gateway_auth_mode": "token",
    "gateway_token": "generated-gateway-token",
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
