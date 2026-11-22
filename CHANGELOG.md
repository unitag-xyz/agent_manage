# Changelog

## Unreleased

## 1.0.002 - 2026-11-22

- v1 沿用 0.5 的平台鉴权与主机派发分工。Server API 通过私有 Unix socket 委托本地授权并调用 Manager，移除 Dola 回调兑换和服务凭据配置。旧 CLI 派发方式继续可用。
- HTTP、WSS 和媒体接口统一通过 `stateDirectory/manager.sock` 访问。平台 Server API 需要接入该 socket。升级旧验证环境时按[迁移说明](docs/v1-operations.md#安装和接入)移除旧鉴权与监听配置。

Git tag 和 CLI 版本为 `1.0.002`。Python 安装包版本为 `1.0.2`。

## 1.0.001 - 2026-11-22

### 1.0 统一接口接入验证

- 新增独立 HTTP RPC、WSS 和二进制媒体 Range 服务，统一运行实例、Agent、线程、分段、消息、运行与操作标识。保留现有 CLI 入口和参数，升级行为差异见下文。
- 新增当前 OpenClaw 适配器，连接协议 4，支持真实 Gateway 发送、流事件、取消和会话管理；历史读取继续使用原只读解析器。未来底座与未实现接口显式声明不支持。
- 按底座和协议兼容范围组织适配器。OpenClaw 工厂按实际安装版本选择 `2026.7.x` 实现，其他版本可以查询状态和能力，底座操作返回 `UNSUPPORTED_VERSION`。
- 新增一次性票据、短期租约、逐请求/订阅/媒体读取授权、过期与撤销检查；提供可配置的可信票据服务适配，不内置 Dola 生产地址或账号系统。
- 写操作持久化并按调用者、Runtime、方法及 clientRequestId 去重。重启后的未确认操作标记 unknown，不自动重发；运行状态与观察状态分开。
- Chat 发起者可在自身 Agent 范围内查询接受记录；发送校验当前段并与 Manager 重置串行。分页继承投影设置，媒体区分工具输入与输出，无法关联的直播媒体明确等待历史引用。
- 修复部分本机子进程继承错误 OpenClaw 配置的问题；管理写流程共用环境锁，并加入配置并发检查、刷新写前日志与中断恢复。外部安装脚本的副作用不假装可回滚。
- 媒体块读取改为有界读取和少量文件指纹缓存；历史/控制/媒体采用独立并发预算，慢 WSS 消费者明确要求重新同步。
- Dola 专用初始化和模型目录刷新放在 `dola.*` 命名空间；保留上层 DockerManager 的容器生命周期职责。

### 升级限制

本版用于 v1 分支接入验证。已有 `0.5.9` 生产实例原地升级前，需验证以下场景：

- 旧刷新日志保留 `rollback_failed` 等未完成状态时，容器启动可能被 `RECOVERY_REQUIRED` 阻止。
- 旧 CLI 依赖 `OPENCLAW_CONFIG_PATH` 或 `OPENCLAW_STATE_DIR` 选择环境时，底座子进程可能转向默认环境。
- 加装 Agent 在写入配置后失败时，回滚可能保留本次操作写入的工具设置。

Git tag 和 CLI 版本为 `1.0.001`。Python 打包按 PEP 440 将版本规范化为 `1.0.1`。

## 0.5.9 - 2026-11-04

- 刷新 Agent 可接收平台下发的目标版本和新版模板 ZIP 地址，缓存版本相同时跳过下载，同步后再运行安装脚本。

## 0.5.8 - 2026-11-04

- `refresh-agent` 同步模板后重新执行 `install.sh`，支持更新网站和应用；无文件变化也可重试安装，模型单独刷新与预览不执行。安装失败返回脱敏错误并保留已同步文件和安装现场。

## 0.5.7 - 2026-11-03

- Agent 安装完成后支持运行根目录 `install.sh`，返回脱敏错误和退出码，失败保留现场；新增 `agent-install --agent` 重试和 `agents-start` 启动入口。
- 容器启动时后台恢复已安装 Agent 服务，单个启动失败不影响其他服务；镜像构建阶段延后安装，正式实例配置完成后执行。
- 保留 0.5.6 的 Host 热加载重启判断，并同时返回安装脚本执行结果。

## 0.5.6 - 2026-10-26

- 标准 Host 安装下的创建、配置和追加 Agent 根据实际配置变化与固定 OpenClaw 热加载规则判断是否仍需调用方重启；未知情况保留兜底，VPS/Fly 合同保持隔离。
- 补充重启模式、首次预装配置、幂等与顶层标志聚合测试，以及精确基座的本地离线容器验证脚本；不将异步 reload 的静态判断视为配置即时生效确认。

- 自动生成的 Agent `AGENTS.md` 在平台运行规则末尾加入进度沟通要求，明确实时可见输出和耗时任务的进展更新。
- 共享 Skill 安装和首次 seed 初始化确保环境属主可读写，保留脚本执行权限；管理员安装按环境目录属主归属，普通用户无法修复属主时明确报错。刷新自动修复受管理共享 Skill 的已有只读父目录，并避免再次写入只读文件；预览不修权限，权限修复在内容回滚后保留。刷新前检查目录写权限，原文件未变化时不再重复回滚写入。

- `refresh-agent` 默认跳过冲突文件并继续更新其他文件及模型，返回 `partial`、`skipped_conflicts`；持续记录未解决冲突，保留显式覆盖和事务回滚。

## 0.5.5 - 2026-10-18

- `refresh-agent` 同步内置共享 Skill，新增和覆盖同名文件；所有共享 Skill 保留目标目录多余文件，更新纳入原有备份与回滚，`--models-only` 不同步 Skill。
- nginx-delivery 适配不同主机的服务、配置和公开目录，静态发布按需验证配置，并补充公开页面分享海报要求，并增加 worker 访问权限与网页图片压缩提示。
- 历史向前读取先取够 limit 条，批内没有用户消息时才继续补读至用户消息，并返回 `reachedUser`；保留 1 MiB 预算及单条预览，向后读取仍按条数分页。
- 历史分页允许显式切换 `omit-thinking`，不再因过滤设置与游标不一致报错；不传时仍继承游标设置。
- `--omit-thinking` 兼容显式 `true/false` 参数，同时保留无值开关用法，方便命令 API 转发。
- 历史读取默认条数从 30 调整为 50，最大条数从 100 放宽到 200，单页消息预算从 320 KiB 提高到 1 MiB；超长单条消息和媒体分块规则不变。

## 0.5.4 - 2026-10-17

- 历史读取新增 `--omit-thinking`，可省略思考内容；分页及消息补读继承过滤设置，默认仍保留思考。
- 历史消息过滤后为空时继续读取同一段补足页面，减少重复请求，保留工具状态与媒体记录。

## 0.5.3 - 2026-10-17

- 统一命令错误详情，修复创建流程包装异常时丢失 `timed_out` 的问题。
- 提取原子文件写入工具，修复 seed 刷盘失败时遗留临时文件的问题；各流程保留自己的锁、备份和回滚策略。
- 复用渠道绑定、模板注册、附属 Agent 编排和 workspace 配置步骤，整理无参数 CLI 命令分发并移除无效参数链。
- 增加错误、文件写入、渠道和流程兼容性测试，补齐原生 Codex 媒体测试的离线 DNS mock。

## 0.5.2 - 2026-10-16

- 将完整 JSON 响应上限从 512 KiB 提高到 8 MiB，以容纳 5 MiB 媒体分块的 Base64 数据及包装字段；超限错误处理保持不变。
- 增加覆盖完整媒体响应与游标续读的回归测试，并补充调用方输出限制说明。

## 0.5.1 - 2026-10-16

- 新增 Fly 容器的 `activate-instance` 配置发布流程，提供原子写入、锁定和激活确认所需的配置哈希。
- 容器模式的 `agents-list` 支持受限的独立超时配置；其他命令和非容器模式保持原有超时。
- 增加只读会话分段历史与媒体读取能力，并将媒体分段上限调整为 5 MiB。
- 预制镜像 seed 支持与非空运行目录深度合并，保留 DockerManager 运行时配置，并通过 marker 保持幂等和中断恢复。
- seed 默认写入无秘密的 `gateway.mode=local` 与 token auth 模式，使 Gateway 可在运行时 Token 环境变量下直接启动。
- 镜像布局协议升级到 v2；构建脚本接受任意模板 zip，并强制执行真实 Gateway readiness、Agent、配置合并、Token 不落盘和 labels 验证。
- 新增一键构建、验证、推送并输出 immutable digest 的发布脚本及操作文档。

## 0.5.0 - 2026-09-01

- 新增 `add-agent` 与 `configure-instance`，将模板 Agent 预注册和订单运行时秘密配置分离；`create-instance` 保持兼容。
- `configure-instance` 只接受已预制的主 Agent 与附属 Agent，缺失时稳定失败且不会隐式调用 `openclaw agents add`。
- 新增固定 `linux/amd64` 的预制镜像构建资产，默认锁定 DockerManager 当前 OpenClaw 镜像 digest，并携带模板、OpenClaw、AgentManager 和布局协议 labels。
- 新增 `/opt/unitag/openclaw-seed` 空卷初始化机制、中断恢复和 seed 秘密扫描；模型密钥、Gateway Token 和认证资料只允许在容器运行时注入。

## 0.4.0 - 2026-08-22

- 将 3000 多行单体管理器拆分为模型、模板与 workspace、渠道、Gateway、公共核心和顶层编排模块；`InstanceManagerV2`、CLI、方法名和返回结构保持兼容。
- 新增集中式模型网关配置与 URL 构造模块，三个环境域名各保留一个定义；相关 URL 测试独立管理。
- `--ai-shop` 默认值统一为 `shop`，未传时模型目录使用 `/byProvider/shop`，模型调用地址使用 `/aigateway/shop/v1`。
- 项目 Python 版本声明调整为 `>=3.10`，与现有类型语法保持一致。
- 增加模板名称、路径边界、压缩包链接/越界/体积校验与原子目录替换，防止模板包写出目标目录或误删已有 workspace。
- 同名 agent 重跑 `create-instance` 时保留已有 Gateway Token 和仍受支持的默认模型，并明确返回 `mode: reconciled`。
- 修复 provider 已包含在模型 ID 中时生成重复模型前缀、无 PyYAML 时字符串 Skill 列表漏解析、Gateway 端口子串误判等边界问题。
- 配置写入增加临时文件清理、落盘同步和 `0600` 权限，创建流程失败时恢复操作前配置快照；执行环境补齐 system sbin PATH，关键 OpenClaw 命令设置超时。

## 0.3.3 - 2026-08-22

- 新增 `global` 模型环境，使用 `https://api.dola.io`；未传 `--model-env` 时默认使用 `global`，原有 `test`、`cn` 地址保持不变。

## 0.3.2 - 2026-08-18

- 新增内置公共 Skill `nginx-delivery`，并将精简运行规则同步到创建及批量追加的全部 agent workspace；旧图片规则区块会自动迁移。
- 兼容 npm stable OpenClaw `2026.7.1-2`，写入模型配置时仅保留 `text`、`image` 输入类型，避免 `video`、`audio` 导致模型 schema 校验失败。

## 0.3.1 - 2026-08-16

- 修复与 npm stable OpenClaw `2026.7.1-2` 的配置兼容性：图片生成模型改写到 `agents.defaults.imageGenerationModel`。
- 创建或更新模型配置时移除此前写入、会被 stable 判为未知字段的 `agents.defaults.mediaModels`。
- 模型列表读取同时识别 stable 的独立媒体模型字段和新版 `mediaModels` 结构，继续排除媒体生成模型。
- 图片路由按商店目录判断：官方 `openai` provider 包含 `gpt-image-2` 时沿用当前商店（无需 `modelCategory`），否则图片使用的 `openai` provider 回退到同环境的无商店 `/aigateway/v1`；其他 provider 仍沿用当前商店。
- 修复 `update-model` 重新拉取目录时丢失已有商店路径的问题，并避免让图片 provider 的无商店回退地址覆盖实例商店判断。

## 0.3.0 - 2026-08-16

- 创建和更新模型目录时，普通模型列表只保留 `chat` 分类，排除图片、视频和音频模型。
- 创建实例时固定配置 `openai/gpt-image-2`，支持 `--image-quality`，并通过 workspace 受管规则把图片生成质量默认设为 `low`。
- 新增统一版本来源和 `agent-manage --version` / `scripts/agentctl.py --version` 查询入口。

## 0.2.1 - 2026-05-06

- `create-instance` 和 `add-agents` 支持模板声明的 `requiredLibraries` 依赖检查/安装，以及 `common-skills` 全局 skill 同步。

## 0.2.0 - 2026-04-20

- 新增 `add-agents` 命令，支持用 JSON 数组为已启动服务批量追加 agent。
