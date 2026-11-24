# v1 对接合同

服务端方法的机器可读清单来自 `core/contracts.py`，通过 `system.info.result.methods` 返回。
CLI 继续使用原有 0.x 协议；本文件只描述新服务。适配器支持状态与当前账号授权是两件事。

## 私有传输与授权

Dola 沿用 0.5 的用户鉴权和实例派发。以下接口由主机侧 Server API 通过 `stateDirectory/manager.sock` 调用。浏览器继续使用平台入口。

1. Server API 验证用户和实例访问权后，从可信派发信息构造 grant，调用 `POST /v1/auth/tickets`。Manager 校验 audience、主体和显式范围后返回 `{"ok":true,"ticket":"..."}`。[授权示例](v1-operations.md#安装和接入)。
2. **HTTP** 调用 `POST /v1/auth/redeem`，以 `{"ticket":"..."}` 换取 `leaseToken` 和 `authContext`。随后用 `Authorization: Bearer <leaseToken>` 调用 `POST /v1/rpc` 和媒体接口。
3. **WSS** 由 Server API 连接 `/v1/ws`，首帧发送 `{"type":"auth","ticket":"..."}`。`hello` 返回可用于内部 HTTP 的租约。Server API 代理实时请求和事件，租约和票据保存在服务端。

票据一次性，有效期最长 60 秒。租约最长 300 秒。Server API 对同一授权范围可在有效期内复用租约。长连接续期前重新校验用户范围，再签领新票据并发送 `auth.refresh`。刷新要求 issuer、subject、tenant 和 audience 一致，连接重新订阅并同步历史。

```json
{"type":"hello","apiVersion":"1.0","epoch":"服务进程周期","eventReplay":false,
 "leaseToken":"仅主机侧 Server API 持有",
 "authContext":{"leaseId":"...","authzVersion":1,"expiresAt":1794480300000}}
```

`authContext` 随已鉴权响应及事件返回。客户端丢弃旧 leaseId/authzVersion 的迟到结果。
收到 auth.refreshed 后主动结束旧授权代际的 pending 请求、丢弃其结果并重新订阅；服务端不逐条补发旧请求失败响应。丢失响应的写操作通过原 clientRequestId 对账，不自动重发。
服务端会在请求前、返回前、媒体分块和每条订阅事件检查授权；无效租约不继续返回敏感结果。
当前支持按 `runtimeIds`、`agentIds`、精确 action 名或 `*` 授权。
安装/刷新可能修改共享 Skill、模型和执行任意模板脚本，要求整个 Runtime 的管理授权。
仅单 Agent 的聊天授权不能调用这些管理方法。

## 请求、响应与写操作

HTTP 请求：

```json
{"requestId":"req-1","method":"messages.list","params":{
  "runtimeId":"openclaw-primary","threadId":"thread_...","segmentId":"segment_...",
  "edge":"end","limit":50,"omitThinking":true
}}
```

WSS 请求额外添加 `"type":"request"`；响应额外添加 `"type":"response"`。

```json
{"requestId":"req-1","ok":true,"result":{},
 "authContext":{"leaseId":"...","authzVersion":1,"expiresAt":1794480300000},
 "timings":{"managerElapsedMs":12.4}}
```

失败返回 `ok:false,error:{code,message,retryable,details?}`。无有效授权时不附旧 authContext。
常见错误：`UNAUTHENTICATED`、`FORBIDDEN`、`INVALID_ARGUMENT`、`UNSUPPORTED`、`UNSUPPORTED_VERSION`、
`NOT_READY`、`NOT_FOUND`、`BUSY`、`IDEMPOTENCY_CONFLICT`、`CURSOR_EXPIRED`、`RECOVERY_REQUIRED`、`DISPATCH_UNKNOWN`、`NATIVE_ERROR`、`SEGMENT_CHANGED`。
已有历史读取错误，如 `SEGMENT_NOT_FOUND`，保持明确，绝不自动退回当前段。
Native 管理错误保留脱敏 legacyCode、备份/回滚及重启信息，不直接把命令 stdout、stderr、凭据透传到浏览器。

所有写方法必须带 `clientRequestId`，首次返回一个 accepted Operation。通过 `operations.get` 查询执行结果。
同一 issuer/subject/tenant/Runtime/method/clientRequestId 只执行一次；更换 requestId 不会重做。
参数改变但 clientRequestId 未变，返回 IDEMPOTENCY_CONFLICT。记录跨进程重启保留。
不要为了绕过 unknown 改一个 clientRequestId 自动重发；先查询原 Operation/Run 和现场状态。

```json
{"operationId":"op_...","runtimeId":"openclaw-primary","method":"runs.create",
 "clientRequestId":"send-1","status":"accepted","observation":"known",
 "createdAt":1794480000000,"updatedAt":1794480000000,"result":null,"error":null}
```

状态为 `accepted/running/completed/failed/unknown`。当前不实现操作取消和后台任务队列。
管理命令完成结果包含 `restartRequired`、`activationRequired`，其余详细字段保留旧业务结果。
Operation 持久记录 agentId/threadId；只能由相同 issuer/subject/tenant 的发起者查询，并重新检查当前授权的 Agent 范围。
Agent 范围的 Chat 用户可以查询自己的发送、重置等操作；Runtime 管理操作仍要求完整 Runtime 范围。
list/lookup 先按发起者及 Agent 范围过滤，再取最近 200 条；不能通过查询借用管理员权限。

## 当前接口

除 system/targets/environments/runtimes 列表外，请求均带 `runtimeId`。表中的参数未重复写 clientRequestId。

| 功能 | 方法 | 核心参数 / 结果 |
| --- | --- | --- |
| 服务与能力 | `system.info`, `capabilities.get` | API、版本、方法定义、支持/就绪/授权状态 |
| 注册环境 | `targets.list`, `environments.list`, `runtimes.list/get` | 管理员预注册的本机环境；不创建 Docker |
| Agent | `agents.list/create/refresh/install` | create 用 options；refresh/install 用 agentId |
| 模型 | `models.list/get/select` | select：modelId；保留底座原有模型选择语义 |
| 用量 | `usage.status` | OpenClaw 只读配额查询，参数仅 `runtimeId`；实例级 Chat 票可用，单 Agent 票不可用 |

| 渠道 | `channels.list/configure/remove` | channel=telegram/feishu/weixin；写参数放 options |
| Skill | `skills.install` | options；对应原本的 Agent 或共享 Skill 安装 |
| Codex 连接 | `connections.get/login/logout` | 当前是 OpenClaw Codex 全局登录及模型恢复 |
| 本地服务 | `services.start` | 启动已安装的 Agent 服务，不接管 Docker 生命周期 |
| Dola 初始化 | `dola.runtime.configure` | options；原 configure-instance 的 Dola 网关配置能力 |
| Dola 更新 | `dola.models.refresh`, `dola.agents.refresh` | Dola 模型目录刷新；后者保留原模型+模板刷新语义 |
| 线程 | `threads.list/get/create/update/reset/delete` | list/create 用 agentId，其他用 threadId；update 用 title |
| 历史分段 | `segments.list` | threadId；最旧到当前；当前段 isCurrent=true |
| 消息 | `messages.list/get` | threadId+segmentId；get 再带 messageId/ref |
| 媒体 | `resources.get`、HTTP 资源 URL | 返回受保护 URL；支持单段 bytes Range |
| 执行 | `runs.create/get/lookup/cancel` | create：threadId/message/expectedCurrentSegmentId；get/cancel：runId |
| 操作 | `operations.list/get/lookup` | operationId 或 clientRequestId；发起者及当前 Agent 范围 |
| 事件 | `events.subscribe/unsubscribe` | 仅 WSS，runtimeId+threadId |
| 恢复 | `runtimes.recover` | 对已知的中断刷新执行所有权检查和恢复 |

### OpenClaw Chat2 compatibility

All calls include `runtimeId`. The following methods are enabled by the instance Chat grant, not the single-Agent grant.

| Method | Additional parameters and result |
| --- | --- |
| `commands.list` | `threadId`; returns the native text command catalog for the thread's Agent. |
| `tasks.list` | `threadId`, optional `limit`, `cursor`, `status` (string or array, e.g. `["queued","running"]`). OpenClaw applies status before paging. Returns `tasks` and optional `nextCursor`. |
| `tasks.get/cancel` | `threadId`, `taskId`; cancel also requires `clientRequestId` and returns an Operation. The native task must be associated with that thread. |
| `usage.cost` | Optional `range` (`7d`, `30d` default, `90d`, `1y`); returns instance totals and daily usage. |
| `sessions.usage`, `sessions.usage.timeseries`, `sessions.usage.logs` | `threadId`; summary accepts optional `range`. Logs accept `segmentId` (default current) and `limit`; return transcript `messageId`, recorded counters and an opaque `runId` only when it matches that thread and segment. No transcript content or estimated values. |
| `chat.sidebar` | Each group includes `isSystem` (the native OpenClaw `main` Agent) and `totalCount` (null if the index could not be read). List items and `threads.list/get` expose `isMain` and `canDelete`. A verified attached group may include `platformAgentIdentify` and `canRefresh: true` for the existing authenticated TaskOrchestrator refresh API. If absent, do not use the template or opaque Chat ID as a refresh target. |
| `threads.get` | Also returns `totalTokens`, `contextTokens`, and `totalTokensFresh` when present in the native session index. Missing values are unknown, not zero. |
| Thread activity | Sidebar and thread list/detail include `runActivity` with up to eight Manager-observed `activeRuns`, `activeRunId` when exactly one exists, and `lastRun`. An empty list does not rule out runs started elsewhere. An unverified terminal boundary is not proof of transcript persistence. |
| `threads.thinking.get/update` | `threadId`; get returns the override, available levels and default when advertised. Update requires `thinkingLevel` (null clears the override) and `clientRequestId`; wait for the Operation before rereading. An unknown or busy session is not patched. |
| `threads.delete/reset` | `threadId` and `clientRequestId`; returns an Operation. Delete explicitly requests native transcript removal and rejects a main thread with `MAIN_THREAD_PROTECTED`. |
| `sessions.subscribe/unsubscribe` | WebSocket only. `sessions.changed` is an invalidation signal without a native key or message. Refresh the sidebar on receipt; subscribe again after reconnect or native resync. |
| `runs.create` attachments | Up to five inline `{type,mimeType,fileName,content}` objects. Host instance Chat allows 23 MiB of combined base64 (about 17.25 MiB of raw files) within a 24 MiB total request. OpenClaw limits each image to 6 MiB. No chunk upload; ordinary limits are unchanged. |
| `resources.get` | Obtain `resourceId` from message history. History and resource metadata include purpose, filename, MIME and size when known. `offset` and `length` (up to 128 KiB) read a base64 chunk including total size and SHA-256. Check the hash remains constant across chunks. No public Host media URL is created. |

`options` 使用 camelCase，映射到保留的请求模型。例如：

```json
{"requestId":"refresh-1","method":"agents.refresh","params":{
  "runtimeId":"openclaw-primary","agentId":"agent_...","clientRequestId":"upgrade-20261112",
  "options":{"templateName":"example","templateVersion":"0.1.3",
             "templateZipUrl":"https://templates.example.com/example-0.1.3.zip"}
}}
```

通用 `agents.refresh` 只处理模板与 install.sh；Dola 模型目录属于 `dola.agents.refresh`。
模板包仍由注册的本地模板库或明确的 HTTPS 发行地址提供；本分支没有实现市场自动查最新版。
通用 `agents.create` 的默认 workspace 在该 Runtime 的 stateDir/data 内，模板目录默认 stateDir/templates。

## MessagePage 与内容

`messages.list` 默认 50 条，范围 1–200；先取够条数，向旧翻页缺少用户消息时按已有逻辑继续，最多 1 MiB 页面预算。
`edge=start/end` 与 cursor 互斥；cursor 是原生段快照的不可篡改引用，只用于历史分页。
omitThinking 可以显式切换；不传时继承游标，projectionId 随投影变化。
view 首次默认 compact；后续省略时继承 cursor/ref，显式改变 view 需重新打开该段。

```json
{"threadId":"thread_...","segmentId":"segment_...","items":[{
  "id":"原始稳定消息ID","threadId":"thread_...","segmentId":"segment_...","role":"assistant",
  "timestamp":1794480000000,"revision":"内容版本指纹",
  "generationState":"unknown","completeness":"complete",
  "projection":{"view":"compact","omitThinking":true,"projectionId":"..."},
  "content":[{"blockId":"...","type":"text","text":"完整正文"}]
}],"olderCursor":"...","newerCursor":null,"snapshotId":"...","projectionId":"...",
 "boundary":{"reachedUser":true,"turnComplete":null},"bytesReturned":12345,
 "limitReachedReason":"boundary_or_budget","timings":{}}
```

内容块：text、reasoning、tool_call、tool_result、resource、notice。
工具默认只保留名称、短命令和明确状态；不返回 thinkingSignature。
历史记录的生成是否结束不能仅由存盘推断，所以默认为 unknown；工具调用、结果及图片引用仍保留。
reachedUser 不表示整个对话完成；快照也不是事件重放 watermark。

大消息返回 completeness=reference 和 `fullMessageRef`（统一的 messages.get 请求）。补读最多 8 MiB 原始消息，超出明确报 MESSAGE_TOO_LARGE，不静默截断。
正文与已省略工具参数不能混为一谈：工具精简内容不会通过消息补读恢复。
资源默认不是 Artifact：role 区分 user_input、assistant_output、tool_input、tool_output、unknown；工具参数中的图标记 tool_input，不作为生成交付物。正式交付物登记后续独立实现。
Manager 的媒体 URL 是内部地址。Server API 在平台入口完成鉴权后，用内部 Bearer lease 代理资源流并传递 Range，浏览器只请求平台地址。
已过期媒体快照返回 CURSOR_EXPIRED，重新打开相应历史段刷新引用。
这是新资源合同，不能把 resourceId 填进旧 Chat 的签名 mediaRef 下载器。历史灰度也需同时接入新下载器，验证缩略图/原图、租约过期重取和工具输入图过滤；旧 CLI 原有媒体返回仍保留。

## Run 与事件

`runs.create` 的 Operation.result 返回 Run，带 runId/threadId/segmentId/clientRequestId/configSnapshotId。
状态 accepted/running/cancel_requested/completed/failed/cancelled 与 observation=known/unknown 分开。
`expectedCurrentSegmentId` 必填，值来自 threads.get；仅全新且没有当前物理段的线程传 null。
Manager 串行执行自己发出的原生写命令，在发送前比对当前段，重置冲突返回 SEGMENT_CHANGED，前端保留草稿。
能力声明 segmentPrecondition=manager_serialized、nativeSegmentCAS=false：OpenClaw 7 没有原子条件发送，其他入口绕过 Manager 重置仍可能竞争。迁移发送前应统一写入口，不能把本地检查当成跨底座 CAS。

`runs.lookup(threadId,clientRequestId)` 返回 items（已关联 Run）、operations（接受记录）及 acceptance：
accepted 表示已持久接受，即使 Run 尚未建立；unknown 表示操作执行结果未知；not_accepted 表示该调用者当前没有相应接受记录。
失败 Operation 仍曾被接受；根据 status/error 判断是否发送成功。对账不需要 Runtime 管理授权。
resultSync 在执行中为 pending；终态没有可靠落盘 ID 对齐时为 unknown，前端不能无限等待 ready。usageStatus 默认 unknown；没有账单不能用 0 代替未知费用。

WSS 事件通用字段：`runtimeId,threadId,epoch,sequence,authContext`。

| type | 内容 | 客户端规则 |
| --- | --- | --- |
| `message.snapshot` | message、runId、correlation | 整条替换投影，不把 snapshot 当追加文本 |
| `run.activity` | block、runId、mode、revision、baseRevision | 工具用 snapshot；思考 delta 只接在匹配版本后 |
| `run.updated` | run | 更新运行状态，不等同于历史已落盘 |
| `resync_required` | reason、epoch | 清理不确定流状态，重订阅/读取历史，不盲目重发写入 |

Gateway 的累计 message 和只有 deltaText 的流都适配；tool/thinking 保留短工具信息和可显示思考。
当前不支持 afterSequence 重放；订阅结果明确 replay=false/resyncRequired=true。
首次订阅、换授权、原生断线、缓冲溢出都需要快照同步。
原生 message ID 缺失时只给临时 live ID，并标注 correlation=unresolved，不声称已与落盘 ID 对齐。
直播媒体尚未取得受保护的归档引用时返回 MEDIA_PENDING_HISTORY notice，保留其他正文；不能把原生路径当作下载授权。
首阶段不混合新旧实时流和当前段历史；没有经验证的 ID 对账或整轮替换边界前，保留待同步状态，不按正文猜去重。

## 能力与后续模块

方法分别返回 `support`、`readiness`、`authorized`。Gateway 未连接是 not_connected，不是功能不存在。
当前按实际 package.json 检测 July 家族，2026.7.1-2 完成真实协议测试；其他补丁只表示可尝试的家族适配，validation=unverified。
已注册底座的版本无匹配实现时，`capabilities.get` 返回实际探测版本、`adapter: null` 和未支持的底座能力。底座读写操作返回 `UNSUPPORTED_VERSION`。接口实现与版本目录规则见 [新底座与版本适配](v1-architecture.md#新底座与版本适配)。
未实现的任务队列、审批、记忆管理、分享、Site、DSH 和远程管理明确 unsupported。
`dola.shares.*` 将独立管理脱敏内容快照与撤销；`dola.sites.*` 将独立登记/发布网站，登记成功不等于部署成功或公网可达。
